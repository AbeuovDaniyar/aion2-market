"""Refresh worker: runs the source on a schedule, writes to SQLite, signals the UI.

refresh_once() is plain Python so it can be tested without Qt; RefreshWorker
wraps it in a background QThread with its own database connection.
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


@dataclass(frozen=True)
class RefreshResult:
    at: datetime
    server_id: int | None
    freshness: Freshness
    items_updated: int = 0
    sales_updated: int = 0
    served_stale: bool = False  # item data came from cache because the API was down
    error: str | None = None


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

    freshness = source.freshness(server_id)
    items_updated = sales_updated = 0
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
    except SourceError as exc:
        error = str(exc)

    # TODO(milestone 2): recompute the pricing model for the items that changed.
    return RefreshResult(now(), server_id, freshness, items_updated, sales_updated, served_stale, error)


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


class RefreshWorker(QThread):
    """Background thread. The UI only talks to it through request_refresh() and signals."""

    refreshed = Signal(object)  # RefreshResult
    started_refresh = Signal()

    def __init__(
        self,
        db_path: str | Path,
        source_factory: Callable[[Database], PriceSource],
        parent=None,
    ):
        super().__init__(parent)
        self._db_path = db_path
        self._source_factory = source_factory
        self._jobs: queue.Queue[str] = queue.Queue()
        self.min_manual_gap_s = 60.0
        self.next_refresh_at: datetime | None = None

    def request_refresh(self) -> None:
        """The "Refresh now" button."""
        self._jobs.put("refresh")

    def stop(self) -> None:
        self._jobs.put("stop")
        self.wait()

    def run(self) -> None:
        db = Database(self._db_path)  # SQLite connections stay on their own thread
        source = self._source_factory(db)
        try:
            while True:  # refreshes once at startup, then on schedule or on request
                self.started_refresh.emit()
                result = refresh_once(db, source)
                last = time.monotonic()
                minutes = max(10.0, db.get_float("refresh_minutes"))  # polite floor
                self.next_refresh_at = utcnow() + timedelta(minutes=minutes)
                self.refreshed.emit(result)
                while True:
                    try:
                        remaining = last + minutes * 60 - time.monotonic()
                        job = self._jobs.get(timeout=max(0.0, remaining))
                    except queue.Empty:
                        break
                    if job == "stop":
                        return
                    # Repeated clicks on "Refresh now" must not hammer the API.
                    if time.monotonic() - last >= self.min_manual_gap_s:
                        break
        finally:
            close = getattr(source, "close", None)
            if close:
                close()
            db.close()
