from __future__ import annotations

from datetime import timedelta

import httpx
from PySide6.QtCore import Qt

from app.db.repo import Database
from app.external.aion2exchange import Aion2ExchangeSource
from app.external.scheduler import RefreshWorker, refresh_once
from tests.conftest import ODYLE, SERVER, break_api, load


def test_refresh_fills_the_database(db, source, clock):
    result = refresh_once(db, source, clock)
    assert result.error is None and result.freshness.state == "ok"
    assert result.server_id == SERVER and result.items_updated == 20

    mine = [s for s in db.servers() if s["is_mine"]]
    assert [s["api_server_id"] for s in mine] == [SERVER]
    rows = db.latest_snapshots(SERVER)
    odyle = next(r for r in rows if r["item_key"] == ODYLE)
    assert odyle["name"] == "Odyle" and odyle["section"] == "materials"
    assert odyle["price"] == 1185 and odyle["units_sold"] == 1960380
    assert odyle["captured_at"] == "2026-10-13T02:05:21.591Z"


def test_server_setting_overrides_api_default(db, source, clock, api):
    db.set_setting("server_id", 12304)
    assert refresh_once(db, source, clock).server_id == 12304
    assert api["items"].calls[0].request.url.params["server"] == "12304"


def test_refresh_is_idempotent_and_keeps_user_fields(db, source, clock):
    db.set_watch(ODYLE)
    refresh_once(db, source, clock)
    clock.at += timedelta(minutes=10)
    refresh_once(db, source, clock)
    count = db.conn.execute(
        "SELECT COUNT(*) FROM item_snapshots WHERE item_key = ? AND server_id = ?",
        (ODYLE, SERVER),
    ).fetchone()[0]
    assert count == 1  # same API snapshot, same row
    assert db.watched_keys() == [ODYLE]


def test_watched_items_get_sales_and_price_history(db, source, clock, api):
    db.set_watch(ODYLE)
    assert refresh_once(db, source, clock).sales_updated == 1
    days = db.conn.execute("SELECT * FROM sales_days ORDER BY registered").fetchall()
    assert len(days) == 9 and days[-1]["units_delta"] == 302730
    assert db.conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 142

    clock.at += timedelta(minutes=10)
    assert refresh_once(db, source, clock).sales_updated == 0  # not due for 6 hours
    assert api["volume"].call_count == 1
    clock.at += timedelta(hours=6)
    assert refresh_once(db, source, clock).sales_updated == 1


def test_refresh_survives_api_down(db, source, clock, api):
    refresh_once(db, source, clock)
    before = db.latest_snapshots(SERVER)

    clock.at += timedelta(minutes=10)
    break_api(api, httpx.ConnectError("offline"))
    result = refresh_once(db, source, clock)
    assert result.freshness.state == "down" and result.freshness.alerts_paused
    assert result.served_stale and result.items_updated == 0 and result.error is None
    assert [tuple(r) for r in db.latest_snapshots(SERVER)] == [tuple(r) for r in before]


def test_first_run_offline_reports_down(db, source, clock, api):
    break_api(api, httpx.ConnectError("offline"))
    result = refresh_once(db, source, clock)
    assert result.freshness.state == "down" and result.error
    assert db.latest_snapshots(SERVER) == []


def test_stale_health_is_reported(db, source, clock, api):
    old = load("health") | {"latest_snapshot_at": "2026-10-12T20:00:00Z"}
    api["health"].mock(return_value=httpx.Response(200, json=old))
    result = refresh_once(db, source, clock)
    assert result.freshness.state == "stale" and result.items_updated == 20


def test_worker_refreshes_at_startup_and_stops(tmp_path, api):
    path = tmp_path / "market.db"
    worker = RefreshWorker(
        path, lambda db: Aion2ExchangeSource(db, min_interval_s=0, sleep=lambda s: None)
    )
    results = []
    worker.refreshed.connect(results.append, Qt.ConnectionType.DirectConnection)
    worker.start()
    for _ in range(100):
        if results:
            break
        worker.wait(100)
    worker.request_refresh()  # ignored: too soon after the last refresh
    worker.stop()

    assert len(results) == 1 and results[0].items_updated == 20
    assert worker.next_refresh_at is not None
    db = Database(path)
    assert len(db.latest_snapshots(SERVER)) == 20
    db.close()
