"""The analysis run end to end on a database filled from the recorded responses."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.analysis import service
from app.external.scheduler import refresh_once
from app.report import build_report
from tests.conftest import ODYLE, SERVER


@pytest.fixture
def filled(db, source, clock):
    result = refresh_once(db, source, clock)
    return db, source, clock, result


def test_reference_servers_are_refreshed_every_six_hours(filled, api):
    db, source, clock, result = filled
    references = db.reference_server_ids(SERVER)
    mine = next(s for s in db.servers() if s["id"] == SERVER)
    assert references and SERVER not in references
    assert all(
        (s["region"], s["faction"]) == (mine["region"], mine["faction"])
        for s in db.servers()
        if s["id"] in references
    )
    assert result.reference_updated == len(references)

    clock.at += timedelta(minutes=10)
    assert refresh_once(db, source, clock).reference_updated == 0
    clock.at += timedelta(hours=6)
    assert refresh_once(db, source, clock).reference_updated == len(references)


def test_reference_scope_setting(db, source, clock):
    db.set_setting("reference_scope", "off")
    assert refresh_once(db, source, clock).reference_updated == 0
    db.set_setting("reference_scope", "region")
    wide = db.reference_server_ids(SERVER)
    db.set_setting("reference_scope", "region_faction")
    assert len(wide) > len(db.reference_server_ids(SERVER))


def test_item_views_with_sales_data(filled):
    db, _, clock, result = filled
    views = service.item_views(db, SERVER, clock.at, result.freshness.sales_batch_at)
    assert len(views) == 20
    odyle = next(v for v in views if v.item_key == ODYLE)
    # Real sales average 1,999 but the cheapest seller is at 1,185: Danny must undercut.
    assert (odyle.estimate.price, odyle.estimate.basis) == (1184, "undercut")
    assert odyle.liquidity.source == "sales"
    assert odyle.liquidity.units_per_day == pytest.approx(1960380 / 28)
    assert odyle.usable and not odyle.age.sales_stale
    assert "thin_history" in odyle.estimate.flags and odyle.estimate.confidence == "low"


def test_item_views_without_sales_data_still_work(filled):
    """Danny's server: listings but no sales statistics."""
    db, _, clock, _ = filled
    db.conn.execute(
        "UPDATE item_snapshots SET units_sold = NULL, sold_avg = NULL, sold_min = NULL, "
        "sold_max = NULL WHERE server_id = ?",
        (SERVER,),
    )
    odyle = next(v for v in service.item_views(db, SERVER, clock.at) if v.item_key == ODYLE)
    assert odyle.estimate.price == 1184 and "no_sales_data" in odyle.estimate.flags
    assert odyle.estimate.illiquid is None
    # No stored history yet, so demand is borrowed from the reference servers and labelled.
    assert odyle.liquidity.source == "reference"
    assert odyle.liquidity.units_per_day == pytest.approx(1960380 / 28)


def test_listing_turnover_is_used_once_history_exists(filled):
    db, _, clock, _ = filled
    db.conn.execute(
        "UPDATE item_snapshots SET units_sold = NULL, sold_avg = NULL WHERE server_id = ?",
        (SERVER,),
    )
    for hours_ago, listings in ((48, 260), (24, 230)):
        db.conn.execute(
            "INSERT INTO item_snapshots (item_key, server_id, captured_at, price, listings) "
            "VALUES (?, ?, ?, 1185, ?)",
            (ODYLE, SERVER, (clock.at - timedelta(hours=hours_ago)).isoformat(), listings),
        )
    odyle = next(v for v in service.item_views(db, SERVER, clock.at) if v.item_key == ODYLE)
    assert odyle.liquidity.source == "listings_proxy"
    assert odyle.liquidity.units_per_day == pytest.approx(33.6, abs=0.5)  # 66 gone in ~47 h


def test_region_gap_flags_items_cheap_here(filled):
    db, _, clock, _ = filled
    views = service.item_views(db, SERVER, clock.at)
    assert all(v.region.verdict == "in_line" for v in views)  # same recording everywhere

    db.conn.execute(
        "UPDATE item_snapshots SET price = 600 WHERE server_id = ? AND item_key = ?",
        (SERVER, ODYLE),
    )
    [cheap] = service.region_gaps(service.item_views(db, SERVER, clock.at))
    assert cheap.item_key == ODYLE and cheap.region.regional_median == 1185
    assert cheap.region.gap_pct == pytest.approx(-49.4, abs=0.1)
    assert cheap.region.servers == len(db.reference_server_ids(SERVER))


def test_stale_and_insane_items_are_not_usable(filled):
    db, _, clock, _ = filled
    late = clock.at + timedelta(hours=3)
    assert not any(v.usable for v in service.item_views(db, SERVER, late))


def test_flips_need_history(filled):
    db, _, clock, _ = filled
    assert service.flip_candidates(db, service.item_views(db, SERVER, clock.at)) == []


def test_flip_found_from_stored_history(filled):
    db, _, clock, _ = filled
    for hours_ago in range(24, 24 * 6, 12):
        db.conn.execute(
            "INSERT INTO item_snapshots (item_key, server_id, captured_at, price, listings, "
            "units_sold, sold_avg) VALUES (?, ?, ?, 50000, 190, 1960380, 48000)",
            (ODYLE, SERVER, (clock.at - timedelta(hours=hours_ago)).isoformat()),
        )
    db.conn.execute(
        "UPDATE item_snapshots SET price = 30000, sold_avg = 48000, sold_min = 100, "
        "sold_max = 90000 WHERE server_id = ? AND item_key = ? AND listings = 194",
        (SERVER, ODYLE),
    )
    [flip] = service.flip_candidates(db, service.item_views(db, SERVER, clock.at))
    assert flip.item_key == ODYLE and flip.buy_price == 30000
    assert (flip.resale_target, flip.basis) == (48000, "sales_avg")
    assert flip.profit == pytest.approx(0.9 * 48000 - 30000)


def test_craft_table_from_recorded_model(filled):
    db, source, clock, _ = filled
    views = service.item_views(db, SERVER, clock.at)
    rows = service.craft_table(
        db, source.crafts_model(SERVER).data, source.crafts(SERVER).data, views
    )
    assert len(rows) == 19
    earrings = next(r for r in rows if r.target_key == 310230013)
    assert earrings.attempts == pytest.approx([74.9966, 17.4595, 4.1306], abs=1e-3)
    assert earrings.api_cost == pytest.approx(11155083.47, abs=1)
    # The recorded item list holds only the 20 best sellers, so the target is not in it.
    assert "not_listed_here" in earrings.flags and earrings.profit is None


def test_report_builds(filled):
    db, source, _, _ = filled
    text = build_report(db, source, SERVER)
    assert "Flip candidates" in text and "Crafts" in text and "reference servers" in text
