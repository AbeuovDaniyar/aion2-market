"""Milestone 4: alerts, trade ledger, farming, timing, merge mode, and their tabs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analysis import alerts, farming, ledger, service
from app.analysis.fees import Fees
from app.analysis.pricing import Obs
from app.db.repo import iso
from app.external.scheduler import refresh_once
from tests.conftest import ODYLE, SERVER
from tests.test_ui import pump, qapp, window  # noqa: F401  (fixtures)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def trade(id, side, qty, price, day, fee=None, key=1, done=True):
    at = iso(T0 + timedelta(days=day))
    return dict(id=id, item_key=key, side=side, qty=qty, unit_price=price, fee_paid=fee,
                listed_at=at, done_at=at if done else None)


# -- alerts -------------------------------------------------------------------------------


def test_undercut_and_dip():
    assert alerts.is_undercut(1000, 990)
    assert not alerts.is_undercut(1000, 1000) and not alerts.is_undercut(1000, None)
    assert alerts.price_dip(700, 1000) == pytest.approx(30)
    assert alerts.price_dip(850, 1000) is None and alerts.price_dip(700, None) is None


def test_supply_squeeze_needs_fewer_listings_and_a_higher_price():
    def history(listings, prices):
        return [Obs(T0 + timedelta(hours=12 * i), p, n) for i, (n, p) in enumerate(zip(listings, prices))]

    now = T0 + timedelta(days=3)
    squeezed = history([100, 90, 80, 70, 60, 55, 50], [10, 10, 10, 11, 12, 13, 14])
    assert alerts.supply_squeeze(squeezed, now) == pytest.approx(50)
    falling_price = history([100, 90, 80, 70, 60, 55, 50], [14, 13, 12, 11, 10, 10, 10])
    assert alerts.supply_squeeze(falling_price, now) is None
    small_drop = history([100, 98, 96, 94, 92, 90, 88], [10, 10, 10, 11, 12, 13, 14])
    assert alerts.supply_squeeze(small_drop, now) is None
    assert alerts.supply_squeeze(squeezed[:3], now) is None  # too little history


def test_sell_vs_alternative():
    result = alerts.sell_vs_alternative(10_000, 8_000)
    assert (result.better, result.sell_net, result.difference) == ("sell", 9000, 1000)
    assert alerts.sell_vs_alternative(10_000, 9_500).better == "alternative"


# -- ledger -------------------------------------------------------------------------------


def test_realized_profit_is_first_in_first_out():
    trades = [
        trade(1, "buy", 10, 100, day=0),
        trade(2, "buy", 10, 200, day=1),
        trade(3, "sell", 15, 300, day=2),  # 10 @ 100 + 5 @ 200 = 2,000 cost
    ]
    [row] = ledger.realized_profit(trades)
    assert row.cost == 2000 and row.proceeds == pytest.approx(15 * 270)
    assert row.profit == pytest.approx(2050) and row.unmatched_qty == 0


def test_recorded_fee_and_unmatched_sales():
    trades = [trade(1, "buy", 2, 100, day=0, fee=10), trade(2, "sell", 5, 300, day=1, fee=100)]
    [row] = ledger.realized_profit(trades)
    assert row.cost == 210 and row.proceeds == 1400 and row.unmatched_qty == 3


def test_active_listings_are_not_realized_and_fee_rule_applies():
    trades = [trade(1, "buy", 1, 100, day=0), trade(2, "sell", 1, 300, day=1, done=False)]
    assert ledger.realized_profit(trades) == []
    trades[1] = trade(2, "sell", 1, 300, day=1)
    assert ledger.realized_profit(trades, Fees(20))[0].profit == pytest.approx(140)


def test_profit_by_week_and_item():
    trades = [
        trade(1, "buy", 2, 100, day=0), trade(2, "sell", 1, 200, day=1),
        trade(3, "sell", 1, 200, day=8), trade(4, "sell", 1, 1000, day=8, key=2),
    ]
    rows = ledger.realized_profit(trades)
    assert ledger.by_week(rows) == {"2026-W41": pytest.approx(980), "2026-W40": pytest.approx(80)}
    assert list(ledger.by_item(rows)) == [2, 1]


# -- farming and timing -------------------------------------------------------------------


def test_run_value_and_route_ranking():
    prices = {1: 1000, 2: 500}
    slow = farming.run_value(60, 2000, [(1, 3), (9, 1)], prices.get)
    assert slow.value == pytest.approx(2000 + 2700) and slow.per_hour == pytest.approx(4700)
    assert slow.unpriced == 1
    fast = farming.run_value(30, 0, [(2, 20)], prices.get)
    assert fast.per_hour == pytest.approx(18_000)
    routes = farming.rank_routes([("Cave", 60, slow), ("Ridge", 30, fast), ("Cave", 60, slow)])
    assert [r.route for r in routes] == ["Ridge", "Cave"]
    assert routes[1].runs == 2 and routes[1].hours == 2 and routes[1].per_hour == pytest.approx(4700)


def test_timing_profile_needs_two_weeks_and_finds_the_dear_day():
    week = [(T0 + timedelta(hours=h), 100.0) for h in range(0, 24 * 7, 6)]
    assert farming.timing_profile(week) is None
    points = []
    for h in range(0, 24 * 21, 3):
        ts = T0 + timedelta(hours=h)
        points.append((ts, 150.0 if ts.astimezone().weekday() == 5 else 100.0))
    profile = farming.timing_profile(points)
    assert profile.best_weekday == "Sat" and profile.by_weekday["Sat"] == 150
    assert profile.weekday_spread_pct > 30 and len(profile.by_hour) == 8


# -- service: alerts, merge, trade checks ---------------------------------------------------


@pytest.fixture
def filled(db, source, clock):
    refresh_once(db, source, clock)
    return db, clock


def test_build_alerts_undercut_and_dip(filled):
    db, clock = filled
    db.add_trade(ODYLE, "sell", 100, 1500, clock.at, done=False)
    db.add_trade(ODYLE, "sell", 100, 1100, clock.at, done=False)  # below the market: fine
    db.set_watch(ODYLE)
    for hours_ago in range(12, 96, 12):
        db.conn.execute(
            "INSERT INTO item_snapshots (item_key, server_id, captured_at, price, listings) "
            "VALUES (?, ?, ?, 2000, 190)",
            (ODYLE, SERVER, iso(clock.at - timedelta(hours=hours_ago))),
        )
    views = service.item_views(db, SERVER, clock.at)
    found = service.build_alerts(db, views, [], [], clock.at)
    assert sorted(a.kind for a in found) == ["Price dip", "Undercut"]
    undercut = next(a for a in found if a.kind == "Undercut")
    assert "1,500" in undercut.message and "1,185" in undercut.message


def test_merge_mode_splits_history_into_before_and_after(filled):
    db, clock = filled
    db.set_watch(ODYLE)
    db.set_holding(ODYLE, 500, 900)
    for hours_ago in (72, 48):
        db.conn.execute(
            "INSERT INTO item_snapshots (item_key, server_id, captured_at, price, listings) "
            "VALUES (?, ?, ?, 800, 190)",
            (ODYLE, SERVER, iso(clock.at - timedelta(hours=hours_ago))),
        )
    views = service.item_views(db, SERVER, clock.at)
    [row] = service.watch_rows(db, views, SERVER)
    assert row.shift is None and row.holding.qty == 500
    assert row.holding.profit == pytest.approx(500 * (0.9 * 1184 - 900))

    service.set_merge_open(db, SERVER, True, clock.at - timedelta(hours=24))
    [row] = service.watch_rows(db, views, SERVER)
    assert (row.shift.pre_median, row.shift.post_median) == (800, 1185)
    assert row.shift.change_pct == pytest.approx(48.1, abs=0.1)
    assert [s["is_cross_server"] for s in db.servers() if s["id"] == SERVER] == [1]
    service.set_merge_open(db, SERVER, False)
    assert service.watch_rows(db, views, SERVER)[0].shift is None


def test_trade_checks_flag_sales_far_from_the_api(filled):
    db, clock = filled
    db.add_trade(ODYLE, "sell", 10, 2100, clock.at)
    db.add_trade(ODYLE, "sell", 10, 9500, clock.at)  # above the API's maximum sale of 9,000
    views = service.item_views(db, SERVER, clock.at)
    checks = {c.price: c for c in service.trade_checks(db, views, SERVER)}
    assert not checks[2100].gap.large
    assert checks[9500].gap.large and checks[9500].gap.outside_range


def test_server_prices_lists_reference_servers(filled):
    db, clock = filled
    prices = service.server_prices(db, SERVER, ODYLE, clock.at)
    assert len(prices) == len(db.reference_server_ids(SERVER)) and prices[0][1] == 1185


# -- tabs ------------------------------------------------------------------------------------


def test_new_tabs_are_present(window):
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == ["Opportunities", "Items", "Crafting", "Flips", "Watchlist and merge",
                      "Trades", "Tools", "Trust", "Settings"]


def test_trades_tab_shows_ledger_and_undercut(window):
    now = datetime.now(UTC)
    window.db.add_trade(ODYLE, "buy", 100, 1000, now - timedelta(days=2))
    window.db.add_trade(ODYLE, "sell", 60, 1500, now - timedelta(days=1))
    listing = window.db.add_trade(ODYLE, "sell", 40, 1600, now, done=False)
    window.state.reload()
    tab = window.trades
    assert tab.table.model_.rowCount() == 3
    assert "1 active listings" in tab.summary.text()
    assert "21,000" in tab.summary.text()  # 60 x (1,350 - 1,000)
    statuses = [tab._status(t) for t in tab.table.model_.rows]
    assert any("undercut" in s for s in statuses)
    assert any(a.kind == "Undercut" and a.ref == listing for a in window.state.alerts)

    tab.table.select_where(lambda t: t["id"] == listing)
    tab._sold()
    assert "0 active listings" in tab.summary.text()
    assert tab.weeks.model_.rowCount() >= 1 and tab.items.model_.rowCount() == 1


def test_alert_raises_one_toast_and_not_again(window):
    window.notified.clear()
    window.db.add_trade(ODYLE, "sell", 40, 1600, datetime.now(UTC), done=False)
    window.state.reload()
    assert len(window.notified) == 1 and "Undercut: Odyle" in window.notified[0]
    window.state.reload()
    assert len(window.notified) == 1  # the same alert is not repeated
    assert any(o.kind.startswith("Alert") for o in window.state.opportunities)


def test_alerts_pause_while_data_is_stale(window, qapp, api):
    import httpx

    from tests.conftest import break_api

    break_api(api, httpx.ConnectError("offline"))
    seen = window.state.result
    window.worker.request_refresh(force=True)
    pump(qapp, lambda: window.state.result is not seen)
    window.notified.clear()
    window.db.add_trade(ODYLE, "sell", 40, 1600, datetime.now(UTC), done=False)
    window.state.reload()
    assert window.state.alerts_paused and window.notified == []


def test_watchlist_tab_holdings(window):
    window.db.set_watch(ODYLE)
    window.db.set_holding(ODYLE, 200, 1000)
    window.state.reload()
    tab = window.watchlist
    assert tab.table.model_.rowCount() == 1
    assert "Holding 1 items" in tab.total.text()
    tab.table.selectRow(0)
    assert "Other servers" in tab.servers.text()
    tab._unwatch()
    assert tab.table.model_.rowCount() == 1  # still held, so it stays listed


def test_tools_tab_sell_vs_use_farming_and_events(window):
    tab = window.tools
    tab.sell_item.setCurrentIndex(tab.sell_item.findData(ODYLE))
    tab.alternative.setValue(5000)
    assert "Use or deliver it" in tab.verdict.text()
    tab.alternative.setValue(100)
    assert "Sell it" in tab.verdict.text()

    window.db.add_farm_run("Cave", datetime.now(UTC), 30, 1000, [(ODYLE, 100)])
    window.db.add_event("2026-10-10", "Patch 5.3", "patch")
    window.state.reload()
    [route] = tab.routes.model_.rows
    assert route.per_hour == pytest.approx(2 * (1000 + 100 * 0.9 * 1184))
    assert tab.events.model_.rowCount() == 1


def test_craft_results_are_compared_with_the_rate(window):
    recipe_id = window.db.save_recipe(
        {"output_key": ODYLE, "output_qty": 1, "success_rate": 0.9, "combo_probability": 0.0}, []
    )
    window.db.add_craft_outcome(recipe_id, 40, 24, 0, datetime.now(UTC))
    window.state.reload()
    [row] = window.state.recipes
    assert row.logged_attempts == 40 and row.logged.observed_rate == 0.6
    assert "results_below_expected" in row.flags
    assert "well below expected" in window.trust.text.toPlainText()


def test_timing_tab_reports_how_much_history_it_has(window, qapp):
    window.open_item(ODYLE)
    pump(qapp, lambda: window.items.status.text() == "")
    assert "need 2+ weeks" in window.items.timing_note.text()


# -- flip notifications and history backfill ------------------------------------------------


def test_backfill_fetches_each_item_once(filled, source, api):
    from app.external.scheduler import backfill_some

    db, _ = filled
    total = 0
    while (done := backfill_some(db, source, SERVER, 7)) > 0:
        total += done
    assert total == 20 and api["prices"].call_count == 20
    assert "from=" in str(api["prices"].calls.last.request.url)
    assert backfill_some(db, source, SERVER) == 0
    db.set_setting("history_backfill", 0)
    db.conn.execute("DELETE FROM history_backfill")
    assert backfill_some(db, source, SERVER) == 0


def flip_candidate(price, confidence="low"):
    from app.analysis.flips import FlipCandidate

    return FlipCandidate(ODYLE, "Odyle", price, 2000, 1800 - price, 50.0, 1, None, 0.0, confidence, "median_7d")


def test_every_accepted_flip_alerts_and_a_cheaper_listing_alerts_again(filled):
    db, clock = filled
    views = service.item_views(db, SERVER, clock.at)
    first = service.build_alerts(db, views, [flip_candidate(1000)], [], clock.at)
    again = service.build_alerts(db, views, [flip_candidate(900)], [], clock.at)
    [a], [b] = [x for x in first if x.kind == "Flip"], [x for x in again if x.kind == "Flip"]
    assert "listed at 1,000" in a.message and "low confidence" in a.message
    assert a.id != b.id  # a new, cheaper listing is a new alert


def test_flip_alert_modes(filled):
    db, clock = filled
    views = service.item_views(db, SERVER, clock.at)
    candidates = [flip_candidate(1000, "low")]
    db.set_setting("flip_alerts", "confident")
    assert not [a for a in service.build_alerts(db, views, candidates, [], clock.at) if a.kind == "Flip"]
    db.set_setting("flip_alerts", "off")
    assert not [a for a in service.build_alerts(db, views, [flip_candidate(1000, "high")], [], clock.at) if a.kind == "Flip"]
