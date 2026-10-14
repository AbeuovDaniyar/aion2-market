"""Refresh worker: runs the source on a schedule, writes to SQLite, signals the UI.

refresh_once() and load_item() are plain Python so they can be tested without Qt;
RefreshWorker wraps them in a background QThread with its own database connection.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.db.repo import Database, parse_iso, utcnow
from app.external.source import Freshness, PriceSource, SourceError

HISTORY_DAYS = 30  # price history fetched for an item's chart


@dataclass(frozen=True)
class RefreshResult:
    at: datetime
    server_id: int | None
    freshness: Freshness
    items_updated: int = 0
    sales_updated: int = 0
    served_stale: bool = False  # item data came from cache because the API was down
    error: str | None = None
    reference_updated: int = 0  # other servers refreshed this round


def resolve_server(db: Database, source: PriceSource) -> int:
    """Danny's server from Settings, or the API's default; also syncs the servers table."""
    servers = source.servers().data
    db.upsert_servers(servers.servers)
    chosen = db.get_setting("server_id")
    server_id = int(chosen) if chosen else servers.default
    db.set_my_server(server_id)
    return server_id


def refresh_once(
    db: Database, source: PriceSource, now: Callable[[], datetime] = utcnow
) -> RefreshResult:
    """One scheduled refresh. Never raises: failures end up in the result."""
    server_id: int | None = None
    try:
        server_id = resolve_server(db, source)
    except SourceError as exc:
        chosen = db.get_setting("server_id")
        server_id = int(chosen) if chosen else None
        if server_id is None:
            return RefreshResult(now(), None, Freshness("down", now(), detail=str(exc)), error=str(exc))

    try:
        from app.db.recipes_import import ensure_recipes

        ensure_recipes(db, server_id)
    except (OSError, ValueError):
        pass  # a broken bundled file leaves the recipes as they are

    freshness = source.freshness(server_id)
    items_updated = sales_updated = reference_updated = 0
    served_stale = False
    error = None
    try:
        items = source.all_items(server_id)
        served_stale = items.stale
        # A stale body was already stored when it was first fetched.
        if not items.stale:
            items_updated = db.save_item_summaries(server_id, items.data, items.fetched_at)
        if freshness.state != "down":
            sales_updated = _refresh_sales(db, source, server_id, now())
            reference_updated = _refresh_reference_servers(db, source, server_id, now())
            # Kept in the response cache for the Crafting tab, which never waits on the network.
            source.crafts(server_id)
            source.crafts_model(server_id)
    except SourceError as exc:
        error = str(exc)

    # The pricing model is computed on read (app.analysis.service), so nothing to recompute.
    return RefreshResult(
        now(), server_id, freshness, items_updated, sales_updated, served_stale, error,
        reference_updated,
    )


def _refresh_sales(db: Database, source: PriceSource, server_id: int, now: datetime) -> int:
    """Sales history and price history for watched items, at most every few hours each."""
    max_age = timedelta(hours=db.get_float("sales_refresh_hours"))
    updated = 0
    for key in db.watched_keys():
        marker = f"sales_refreshed:{server_id}:{key}"
        last = parse_iso(db.get_setting(marker))
        if last and now - last < max_age:
            continue
        volume = source.volume(key, server_id)
        prices = source.prices(key, server_id)
        if volume.stale or prices.stale:
            continue
        db.save_volume_series(server_id, volume.data)
        db.save_price_series(server_id, prices.data)
        db.set_setting(marker, now.isoformat())
        updated += 1
    return updated


def _refresh_reference_servers(
    db: Database, source: PriceSource, server_id: int, now: datetime
) -> int:
    """Catalog snapshots of the region's other servers and of the demand-reference servers:
    one call each, every few hours."""
    max_age = timedelta(hours=db.get_float("reference_refresh_hours"))
    others = dict.fromkeys(db.reference_server_ids(server_id) + db.demand_server_ids(server_id))
    updated = 0
    for other in others:
        marker = f"reference_refreshed:{other}"
        last = parse_iso(db.get_setting(marker))
        if last and now - last < max_age:
            continue
        items = source.all_items(other)
        if items.stale:
            continue
        db.save_item_summaries(other, items.data, items.fetched_at)
        db.set_setting(marker, now.isoformat())
        updated += 1
    return updated


def backfill_some(db: Database, source: PriceSource, server_id: int, limit: int = 2) -> int:
    """Fetch 7 days of hourly price history for a few items that have none stored yet, so
    the flip finder (which needs 3+ days) works without waiting days. One request per item,
    once per item and server. Returns how many were filled."""
    if db.get_setting("history_backfill") != "1":
        return 0
    rows = db.conn.execute(
        "SELECT DISTINCT s.item_key FROM item_snapshots s "
        "LEFT JOIN history_backfill b ON b.server_id = s.server_id AND b.item_key = s.item_key "
        "LEFT JOIN items i ON i.item_key = s.item_key "
        "WHERE s.server_id = ? AND b.item_key IS NULL ORDER BY i.watch DESC, s.item_key LIMIT ?",
        (server_id, limit),
    ).fetchall()
    done = 0
    for row in rows:
        try:
            prices = source.prices(row["item_key"], server_id, bucket="1h", days=7)
        except SourceError:
            break
        if prices.stale:
            break
        db.save_price_series(server_id, prices.data)
        db.conn.execute(
            "INSERT OR REPLACE INTO history_backfill (server_id, item_key, at) VALUES (?, ?, ?)",
            (server_id, row["item_key"], utcnow().isoformat()),
        )
        db.conn.commit()
        done += 1
    return done


def load_item(db: Database, source: PriceSource, server_id: int, item_key: int) -> str | None:
    """On-demand detail for one item's charts: price history, sales days, depth ladder.
    Returns an error message, or None."""
    try:
        prices = source.prices(item_key, server_id, bucket="1h", days=HISTORY_DAYS)
        db.save_price_series(server_id, prices.data)
        db.save_volume_series(server_id, source.volume(item_key, server_id).data)
        source.depth_ladder(item_key, server_id)  # read back from the response cache
    except SourceError as exc:
        return str(exc)
    return None


class RefreshWorker(QThread):
    """Background thread. The UI only talks to it through request_*() and signals."""

    refreshed = Signal(object)  # RefreshResult
    started_refresh = Signal()
    item_loaded = Signal(int, object)  # item_key, error message or None
    icons_loaded = Signal(int)  # how many item icons were just fetched
    history_backfilled = Signal(int)  # how many items just got their 7-day price history

    def __init__(
        self,
        db_path: str | Path,
        source_factory: Callable[[Database], PriceSource],
        parent=None,
        icon_fetcher_factory: Callable[[Database], object] | None = None,
    ):
        super().__init__(parent)
        self._db_path = db_path
        self._source_factory = source_factory
        self._icon_fetcher_factory = icon_fetcher_factory
        self._jobs: queue.Queue[tuple] = queue.Queue()
        self.min_manual_gap_s = 60.0
        self.next_refresh_at: datetime | None = None

    def request_refresh(self, force: bool = False) -> None:
        """The "Refresh now" button. force skips the minimum gap (server or settings changed)."""
        self._jobs.put(("force" if force else "refresh",))

    def request_item(self, server_id: int, item_key: int) -> None:
        self._jobs.put(("item", server_id, item_key))

    def stop(self) -> None:
        self._jobs.put(("stop",))
        self.wait()

    def run(self) -> None:
        db = Database(self._db_path)  # SQLite connections stay on their own thread
        source = self._source_factory(db)
        icons = self._icon_fetcher_factory(db) if self._icon_fetcher_factory else None
        try:
            while True:  # refreshes once at startup, then on schedule or on request
                self.started_refresh.emit()
                result = refresh_once(db, source)
                last = time.monotonic()
                minutes = max(10.0, db.get_float("refresh_minutes"))  # polite floor
                self.next_refresh_at = utcnow() + timedelta(minutes=minutes)
                self.refreshed.emit(result)
                server = result.server_id
                backfill_pending = server is not None and result.freshness.state != "down"
                icons_pending = icons is not None and server is not None
                while True:
                    busy = backfill_pending or icons_pending
                    try:
                        remaining = last + minutes * 60 - time.monotonic()
                        # Background work runs a little at a time between jobs.
                        job = self._jobs.get(timeout=0.05 if busy else max(0.0, remaining))
                    except queue.Empty:
                        if busy and remaining > 0:
                            if backfill_pending:
                                filled = backfill_some(db, source, server)
                                backfill_pending = filled > 0
                                if filled:
                                    self.history_backfilled.emit(filled)
                            elif icons_pending:
                                fetched = icons.fetch_some(server, 2)
                                icons_pending = fetched > 0
                                if fetched:
                                    self.icons_loaded.emit(fetched)
                            continue
                        break
                    if job[0] == "stop":
                        return
                    if job[0] == "item":
                        self.item_loaded.emit(job[2], load_item(db, source, job[1], job[2]))
                        continue
                    # Repeated clicks on "Refresh now" must not hammer the API.
                    if job[0] == "force" or time.monotonic() - last >= self.min_manual_gap_s:
                        break
        finally:
            for closable in (source, icons):
                close = getattr(closable, "close", None)
                if close:
                    close()
            db.close()
