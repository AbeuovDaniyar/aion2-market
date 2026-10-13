from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.external import models as m
from app.external.source import SourceError
from tests.conftest import ODYLE, SERVER, break_api, load


def test_every_recorded_response_parses(source):
    assert source.servers().data.default == SERVER
    assert source.health(SERVER).data.status == "ok"
    assert source.collection(SERVER).data.runs > 0
    assert source.categories(SERVER).data.total_items > 0
    assert source.overview(SERVER).data.server_id == SERVER
    assert source.prices(ODYLE, SERVER).data.points
    assert source.depth(ODYLE, 10, SERVER).data.units == 10
    assert source.depth_ladder(ODYLE, SERVER).data.levels
    assert source.volume(ODYLE, SERVER).data.days
    assert source.observations(ODYLE, SERVER).data.item_key == ODYLE
    assert source.crafts(SERVER).data.targets
    assert source.crafts_model(SERVER).data.profiles
    assert source.craft(310230013, SERVER).data.steps


def test_only_api_paths_are_called(source, api):
    source.all_items(SERVER)
    source.volume(ODYLE, SERVER)
    assert all(call.request.url.path.startswith("/api/") for call in api.calls)


def test_responses_are_cached_and_logged(source, db, api):
    first = source.volume(ODYLE, SERVER)
    again = source.volume(ODYLE, SERVER)
    assert not first.from_cache and again.from_cache and not again.stale
    assert api["volume"].call_count == 1
    assert db.cache_get(f"/api/items/{ODYLE}/volume?server={SERVER}")["status"] == 200
    log = db.conn.execute("SELECT endpoint, status, error FROM fetch_log").fetchall()
    assert [tuple(r) for r in log] == [(f"/api/items/{ODYLE}/volume", 200, None)]


def test_cache_expires(source, clock, api):
    source.volume(ODYLE, SERVER)
    clock.at += timedelta(hours=2)
    assert not source.volume(ODYLE, SERVER).from_cache
    assert api["volume"].call_count == 2


def test_keys_are_batched_100_per_call(source, api):
    keys = list(range(100000001, 100000251))
    source.items_by_keys(keys, SERVER)
    calls = api["items"].calls
    assert len(calls) == 3
    sizes = [len(c.request.url.params["keys"].split(",")) for c in calls]
    assert sizes == [100, 100, 50]
    assert all(c.request.url.params["limit"] == "100" for c in calls)


def test_all_items_pages_until_total(source, api):
    page = load("items")
    page["total"] = 2500
    api["items"].mock(return_value=httpx.Response(200, json=page))
    source.all_items(SERVER)
    assert [c.request.url.params["offset"] for c in api["items"].calls] == ["0", "1000", "2000"]


def test_retries_5xx_with_backoff_then_succeeds(source, api, sleeps):
    api["health"].mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            httpx.Response(200, json=load("health")),
        ]
    )
    assert not source.health(SERVER).stale
    assert api["health"].call_count == 3
    assert sleeps == [1.0, 2.0]


def test_does_not_retry_4xx(source, api):
    api["health"].mock(return_value=httpx.Response(422, json={"detail": []}))
    with pytest.raises(SourceError, match="HTTP 422"):
        source.health(SERVER)
    assert api["health"].call_count == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(500), httpx.ConnectError("offline"), httpx.ReadTimeout("slow")],
    ids=["5xx", "network", "timeout"],
)
def test_api_down_serves_cache_marked_stale(source, api, clock, failure):
    live = source.all_items(SERVER)
    clock.at += timedelta(hours=3)
    break_api(api, failure)
    cached = source.all_items(SERVER)
    assert cached.stale and cached.from_cache
    assert cached.fetched_at == live.fetched_at
    assert [i.item_key for i in cached.data] == [i.item_key for i in live.data]


def test_api_down_without_cache_raises(source, api):
    break_api(api, httpx.ConnectError("offline"))
    with pytest.raises(SourceError):
        source.health(SERVER)


def test_missing_fields_fall_back_to_cache(source, api, db):
    source.health(SERVER)
    broken = load("health")
    del broken["snapshot_age_seconds"]
    api["health"].mock(return_value=httpx.Response(200, json=broken))
    assert source.health(SERVER).stale
    assert api["health"].call_count == 2  # a bad shape is not retried
    assert "unexpected response shape" in db.conn.execute(
        "SELECT error FROM fetch_log ORDER BY id DESC"
    ).fetchone()["error"]


def test_requests_are_spaced(db, api, clock):
    from app.external.aion2exchange import Aion2ExchangeSource

    waits: list[float] = []
    src = Aion2ExchangeSource(db, min_interval_s=1.1, sleep=waits.append, now=clock)
    src.health(SERVER)
    src.collection(SERVER)
    src.overview(SERVER)
    src.close()
    assert len(waits) == 2 and all(1.0 < w <= 1.1 for w in waits)


# -- freshness ----------------------------------------------------------------


def test_freshness_ok(source):
    f = source.freshness(SERVER)
    assert f.state == "ok" and not f.alerts_paused and not f.sales_stale
    assert f.snapshot_age_seconds == 2378  # 02:45:00 minus 02:05:21.591


def test_freshness_stale_after_two_hours(source, clock):
    clock.at += timedelta(hours=2)
    f = source.freshness(SERVER)
    assert f.state == "stale" and f.alerts_paused


def test_freshness_sales_stale_after_two_days(source, clock):
    clock.at += timedelta(days=3)
    assert source.freshness(SERVER).sales_stale


def test_freshness_collector_failing(source, api):
    failing = load("collection")
    failing["failed_runs_since_ok"] = 4
    failing["failing_since"] = "2026-10-13T01:00:00Z"
    api["collection"].mock(return_value=httpx.Response(200, json=failing))
    f = source.freshness(SERVER)
    assert f.state == "failing" and f.failed_runs_since_ok == 4 and f.alerts_paused


def test_freshness_down_with_and_without_cache(source, api):
    break_api(api, httpx.ConnectError("offline"))
    assert source.freshness(SERVER).state == "down"
    break_api(api, httpx.Response(200, json=load("health")))
    api["health"].mock(return_value=httpx.Response(200, json=load("health")))
    source.health(SERVER)
    api["health"].mock(side_effect=httpx.ConnectError("offline"))
    f = source.freshness(SERVER)
    assert f.state == "down" and f.snapshot_at is not None


def test_models_ignore_unknown_fields():
    body = load("health") | {"new_field": 1}
    assert m.Health.model_validate(body).status == "ok"
