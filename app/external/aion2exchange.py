"""aion2.exchange API client: all HTTP, caching and freshness checks live here.

Only the published /api/... endpoints are called. Every response is cached in
SQLite; when the API is down the last good body is served, marked stale.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TypeVar
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ValidationError

from app.db.repo import Database, parse_iso, utcnow
from app.external import models as m
from app.external.source import Fetched, Freshness, PriceSource, SourceError

BASE_URL = "https://aion2.exchange"
USER_AGENT = "aion2-market-assistant/0.1 (personal desktop app)"
TIMEOUT_S = 15.0
MAX_ATTEMPTS = 3
KEYS_PER_CALL = 100
ITEMS_PAGE = 1000

M = TypeVar("M", bound=BaseModel)

# How long a cached body is served without asking again (on-demand data).
HOUR = timedelta(hours=1)
MAX_AGE_SERVERS = 24 * HOUR
MAX_AGE_CATALOG = 24 * HOUR
MAX_AGE_HISTORY = HOUR
MAX_AGE_CRAFTS = HOUR


class Aion2ExchangeSource(PriceSource):
    name = "aion2.exchange"

    def __init__(
        self,
        db: Database,
        client: httpx.Client | None = None,
        min_interval_s: float = 1.1,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = utcnow,
    ):
        self.db = db
        self.client = client or httpx.Client(
            base_url=BASE_URL, timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}
        )
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._now = now
        self._last_request = float("-inf")

    def close(self) -> None:
        self.client.close()

    # -- transport ----------------------------------------------------------

    def _get(
        self, path: str, model: type[M], max_age: timedelta | None = None, **params
    ) -> Fetched[M]:
        params = {k: v for k, v in sorted(params.items()) if v is not None}
        url = cache_url(path, **params)
        cached = self.db.cache_get(url)

        if cached and max_age is not None:
            fetched_at = parse_iso(cached["fetched_at"])
            if self._now() - fetched_at < max_age:
                return Fetched(model.model_validate_json(cached["body"]), fetched_at, True)

        error = "no attempt"
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self._sleep(2.0 ** (attempt - 1))  # 1 s, 2 s
            self._throttle()
            started = time.monotonic()
            status: int | None = None
            retry = False
            try:
                response = self.client.get(path, params=params)
                status = response.status_code
                if status == 200:
                    data = model.model_validate_json(response.text)
                    at = self._now()
                    self.db.cache_put(url, response.text, at)
                    self._log(path, status, started)
                    return Fetched(data, at)
                error = f"HTTP {status}"
                retry = status >= 500
            except httpx.TransportError as exc:
                error = f"{type(exc).__name__}: {exc}"
                retry = True
            except ValidationError as exc:
                error = f"unexpected response shape: {exc.error_count()} errors"
            self._log(path, status, started, error)
            if not retry:
                break

        if cached:
            try:
                data = model.model_validate_json(cached["body"])
            except ValidationError:
                raise SourceError(f"{url}: {error}; cached copy is unreadable") from None
            return Fetched(data, parse_iso(cached["fetched_at"]), True, True)
        raise SourceError(f"{url}: {error}")

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval_s - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def _log(self, path: str, status: int | None, started: float, error: str | None = None) -> None:
        ms = int((time.monotonic() - started) * 1000)
        self.db.log_fetch(self._now(), path, status, ms, error)

    # -- endpoints ------------------------------------------------------------

    def servers(self) -> Fetched[m.ServerList]:
        return self._get("/api/servers", m.ServerList, MAX_AGE_SERVERS)

    def health(self, server: int | None = None) -> Fetched[m.Health]:
        return self._get("/api/health", m.Health, server=server)

    def collection(self, server: int | None = None) -> Fetched[m.Collection]:
        return self._get("/api/collection", m.Collection, server=server)

    def categories(self, server: int | None = None) -> Fetched[m.Categories]:
        return self._get("/api/categories", m.Categories, MAX_AGE_CATALOG, server=server)

    def overview(self, server: int | None = None, window: str = "24h") -> Fetched[m.Overview]:
        return self._get("/api/overview", m.Overview, server=server, window=window)

    def all_items(self, server: int | None = None) -> Fetched[list[m.ItemSummary]]:
        """The whole catalog with current prices: one call per 1000 items."""
        pages: list[Fetched[m.ItemList]] = []
        offset = 0
        while True:
            page = self._get(
                "/api/items", m.ItemList, server=server, sort="key", limit=ITEMS_PAGE, offset=offset
            )
            pages.append(page)
            offset += ITEMS_PAGE
            if offset >= page.data.total or not page.data.items:
                break
        return _merge(pages)

    def items_by_keys(
        self, keys: list[int], server: int | None = None
    ) -> Fetched[list[m.ItemSummary]]:
        keys = sorted(set(keys))
        pages = [
            self._get(
                "/api/items",
                m.ItemList,
                server=server,
                keys=",".join(map(str, keys[i : i + KEYS_PER_CALL])),
                limit=KEYS_PER_CALL,
            )
            for i in range(0, len(keys), KEYS_PER_CALL)
        ]
        return _merge(pages) if pages else Fetched([], self._now())

    def prices(
        self,
        item_key: int,
        server: int | None = None,
        bucket: str | None = None,
        days: int | None = None,
    ) -> Fetched[m.PriceSeries]:
        params = {"server": server, "bucket": bucket}
        if days:
            # Whole days, so the cache key is stable within a day.
            start = (self._now() - timedelta(days=days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            params["from"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get(
            f"/api/items/{item_key}/prices", m.PriceSeries, MAX_AGE_HISTORY, **params
        )

    def depth(
        self, item_key: int, units: int = 10, server: int | None = None
    ) -> Fetched[m.DepthSeries]:
        return self._get(
            f"/api/items/{item_key}/depth",
            m.DepthSeries,
            MAX_AGE_HISTORY,
            server=server,
            units=units,
        )

    def depth_ladder(self, item_key: int, server: int | None = None) -> Fetched[m.DepthLadder]:
        return self._get(
            f"/api/items/{item_key}/depth/ladder", m.DepthLadder, MAX_AGE_HISTORY, server=server
        )

    def volume(self, item_key: int, server: int | None = None) -> Fetched[m.VolumeSeries]:
        return self._get(
            f"/api/items/{item_key}/volume", m.VolumeSeries, MAX_AGE_HISTORY, server=server
        )

    def observations(
        self, item_key: int, server: int | None = None
    ) -> Fetched[m.ObservationSeries]:
        return self._get(
            f"/api/items/{item_key}/observations",
            m.ObservationSeries,
            MAX_AGE_HISTORY,
            server=server,
        )

    def crafts(self, server: int | None = None) -> Fetched[m.CraftList]:
        return self._get("/api/crafts", m.CraftList, MAX_AGE_CRAFTS, server=server)

    def craft(self, target_key: int, server: int | None = None) -> Fetched[m.CraftDetail]:
        return self._get(
            f"/api/crafts/{target_key}", m.CraftDetail, MAX_AGE_CRAFTS, server=server
        )

    def crafts_model(self, server: int | None = None) -> Fetched[m.CraftModelInfo]:
        return self._get("/api/crafts/model", m.CraftModelInfo, MAX_AGE_CATALOG, server=server)

    # -- freshness ------------------------------------------------------------

    def freshness(self, server: int | None = None) -> Freshness:
        """Call /health first, then /collection and /overview; never raises."""
        now = self._now()
        try:
            health = self.health(server)
        except SourceError as exc:
            return Freshness("down", now, detail=f"API unreachable: {exc}")

        snapshot_at = health.data.latest_snapshot_at
        # Age is measured against our clock, so a cached /health keeps ageing.
        age = int((now - snapshot_at).total_seconds()) if snapshot_at else None
        fields = dict(checked_at=now, snapshot_at=snapshot_at, snapshot_age_seconds=age)
        if health.stale:
            return Freshness("down", detail="API unreachable; showing cached data", **fields)

        failed = 0
        try:
            failed = self.collection(server).data.failed_runs_since_ok
        except SourceError:
            pass
        try:
            batch = self.overview(server).data.freshness.latest_batch_day
        except SourceError:
            batch = None
        fields["failed_runs_since_ok"] = failed
        fields["sales_batch_at"] = batch
        fields["sales_stale"] = batch is None or now - batch > timedelta(
            days=self.db.get_float("stale_sales_days")
        )

        if failed >= self.db.get_float("collector_failing_runs"):
            return Freshness("failing", detail=f"collector failed {failed} runs in a row", **fields)
        if health.data.last_run_kind not in (None, "ok") and failed:
            fields["detail"] = f"last collector run: {health.data.last_run_kind}"
        if age is None or age > self.db.get_float("stale_listings_hours") * 3600:
            return Freshness("stale", **{"detail": "listings snapshot is old", **fields})
        return Freshness("ok", **{"detail": "", **fields})


def cache_url(path: str, **params) -> str:
    """The api_cache key of a request."""
    params = {k: v for k, v in sorted(params.items()) if v is not None}
    return path + ("?" + urlencode(params) if params else "")


def read_cached(db: Database, model: type[M], path: str, **params) -> Fetched[M] | None:
    """The last stored response, without touching the network (for the UI thread)."""
    row = db.cache_get(cache_url(path, **params))
    if row is None:
        return None
    try:
        return Fetched(model.model_validate_json(row["body"]), parse_iso(row["fetched_at"]), True)
    except ValidationError:
        return None


def _merge(pages: list[Fetched[m.ItemList]]) -> Fetched[list[m.ItemSummary]]:
    return Fetched(
        [item for p in pages for item in p.data.items],
        min(p.fetched_at for p in pages),
        any(p.from_cache for p in pages),
        any(p.stale for p in pages),
    )
