from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analysis import crafting, flips, merge, pricing, trust
from app.analysis.fees import Fees, net
from app.analysis.pricing import Liquidity, Obs, SalesStats
from tests.conftest import load

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def series(listings: list[int], hours: float = 24, price: float = 100) -> list[Obs]:
    return [Obs(T0 + timedelta(hours=hours * i), price, n) for i, n in enumerate(listings)]


# -- fees -------------------------------------------------------------------------


def test_net_default_is_ten_percent_commission():
    assert net(1000) == 900


@pytest.mark.parametrize(
    "fees, expected",
    [
        (Fees(10, "flat", 50), 850),
        (Fees(10, "percent", 2), 880),
        (Fees(0), 1000),
        (Fees(12.5), 875),
    ],
)
def test_net_fee_rules(fees, expected):
    assert fees.net(1000) == pytest.approx(expected)


def test_fees_from_settings(db):
    db.set_setting("sales_tax_pct", 8)
    db.set_setting("listing_fee_rule", "flat")
    db.set_setting("listing_fee_value", 100)
    assert Fees.from_db(db).net(1000) == pytest.approx(820)


# -- spike filter and estimated sale price ------------------------------------------


def test_spike_is_replaced_by_the_median():
    prices = [100, 102, 98, 101, 99, 100, 500]
    assert pricing.spike_filter(prices) == (100, True)


def test_spike_downwards_too():
    assert pricing.spike_filter([100, 102, 98, 101, 99, 100, 20]) == (100, True)


def test_move_that_persists_three_refreshes_is_accepted():
    prices = [100, 102, 98, 101, 99, 100, 500, 510, 505]
    assert pricing.spike_filter(prices) == (505, False)


def test_normal_noise_and_short_history_are_not_spikes():
    assert pricing.spike_filter([100, 102, 98, 101, 99, 100, 103]) == (103, False)
    assert pricing.spike_filter([100, 500]) == (500, False)


def test_flat_series_does_not_flag_a_one_kina_change():
    assert pricing.spike_filter([1000] * 8 + [1001]) == (1001, False)


def test_depth_price():
    levels = [(100, 4), (110, 4), (150, 10)]
    assert pricing.depth_price(levels, 10) == pytest.approx((400 + 440 + 300) / 10)
    assert pricing.depth_price(levels, 100) is None


def test_estimate_takes_the_lowest_candidate():
    sales = SalesStats(units_sold=500, avg_price=900)
    est = pricing.estimate_sale_price(1000, [1000] * 6, sales, depth_unit_price=1050, history_days=5)
    assert (est.price, est.basis, est.confidence) == (900, "sales_avg", "high")

    est = pricing.estimate_sale_price(
        1000, [1000] * 6, SalesStats(500, 1500), depth_unit_price=1050, history_days=5
    )
    assert (est.price, est.basis) == (999, "undercut")


def test_overpriced_listing_that_never_sells_does_not_set_the_price():
    """The only listing is at 50,000 but real sales average 9,000."""
    est = pricing.estimate_sale_price(50_000, [50_000] * 6, SalesStats(40, 9_000), history_days=6)
    assert est.price == 9_000 and est.basis == "sales_avg"


def test_few_sales_do_not_anchor():
    est = pricing.estimate_sale_price(1000, [1000] * 6, SalesStats(3, 200), history_days=6)
    assert est.price == 999 and "few_sales" in est.flags and est.confidence == "medium"


def test_spike_uses_median_and_depth_can_win():
    history = [100, 102, 98, 101, 99, 100, 400]
    est = pricing.estimate_sale_price(400, history, history_days=6, depth_unit_price=95)
    assert est.spike and est.reference == 100 and (est.price, est.basis) == (95, "depth")


def test_no_sales_data_is_low_confidence_but_still_priced():
    est = pricing.estimate_sale_price(1000, [990, 1000, 1010, 1000, 1000, 1000], history_days=6)
    assert est.price == 999 and est.confidence == "low" and "no_sales_data" in est.flags


def test_illiquid_item_falls_back_on_sales_and_is_low_confidence():
    est = pricing.estimate_sale_price(
        5000, [5000] * 6, SalesStats(20, 6000), illiquid=True, history_days=6
    )
    assert (est.price, est.basis, est.confidence) == (6000, "sales_avg", "low")


def test_undercut_step_setting():
    assert pricing.estimate_sale_price(1000, undercut_step=10).price == 990
    assert pricing.estimate_sale_price(1, undercut_step=10).price == 1


def test_is_illiquid():
    stuck = [(12, 40), (12, 40), (13, 40), (12, 40)]
    selling = [(12, 40), (12, 55), (13, 70), (12, 90)]
    assert pricing.is_illiquid(stuck) is True
    assert pricing.is_illiquid(selling) is False
    assert pricing.is_illiquid(stuck[:2]) is None
    assert pricing.is_illiquid([(12, None)] * 5) is None  # no sales data: cannot tell


# -- sell-through -------------------------------------------------------------------


def test_sell_through_from_sales():
    liq = pricing.sell_through(SalesStats(units_sold=280, avg_price=10))
    assert (liq.units_per_day, liq.source) == (10, "sales")


def test_sell_through_trend_flags():
    assert "sales_rising" in pricing.sell_through(SalesStats(1000, 10, deltas=(150, 150))).flags
    assert "sales_falling" in pricing.sell_through(SalesStats(1000, 10, deltas=(-300,))).flags


def test_sell_through_falls_back_to_listing_turnover():
    history = series([20, 14, 16, 10])  # drops of 6 and 6 over three days
    liq = pricing.sell_through(history=history, reference_units_per_day=99)
    assert liq.source == "listings_proxy" and liq.units_per_day == pytest.approx(4)
    assert "low_confidence" in liq.flags


def test_sell_through_reference_then_unknown():
    short = series([20, 10], hours=1)  # an hour of data is not enough
    assert pricing.sell_through(history=short, reference_units_per_day=7) == Liquidity(
        7, "reference", ("other_servers",)
    )
    assert pricing.sell_through().source == "unknown"


def test_days_to_sell_counts_competing_units():
    assert pricing.days_to_sell(20, 10) == 2
    assert pricing.days_to_sell(20, 10, ahead=30) == 5
    assert pricing.days_to_sell(20, 0) is None and pricing.days_to_sell(20, None) is None


def test_horizon_profit_caps_at_what_the_market_absorbs():
    assert pricing.horizon_profit(100, qty=50, units_per_day=10, horizon_days=3) == 3000
    assert pricing.horizon_profit(100, qty=5, units_per_day=10, horizon_days=3) == 500
    assert pricing.horizon_profit(100, qty=50, units_per_day=10, horizon_days=3, ahead=25) == 500
    assert pricing.horizon_profit(100, qty=50, units_per_day=None) == 0


def test_big_margin_that_barely_sells_ranks_below_small_margin_that_moves():
    slow = pricing.horizon_profit(1_000_000, qty=10, units_per_day=0.01)
    fast = pricing.horizon_profit(5_000, qty=10, units_per_day=20)
    assert fast > slow


# -- crafting -------------------------------------------------------------------------


def test_craft_ev_formula():
    # 0.8 x [0.75 x 900 + 0.25 x 1800] - 500 - 20
    assert crafting.craft_ev(0.8, 0.25, 1000, 2000, 500, 20) == pytest.approx(380)


def test_lucky_case_craft_is_a_loss_in_expectation():
    """Looks profitable if it always succeeds (900 > 800), loses money at 50%."""
    assert crafting.craft_ev(1.0, 0, 1000, 1000, 800) > 0
    assert crafting.craft_ev(0.5, 0, 1000, 1000, 800) < 0


def test_break_even_rate_is_where_ev_is_zero():
    rate = crafting.break_even_rate(0.25, 1000, 2000, 500, 20)
    assert rate == pytest.approx(520 / 1125)
    assert crafting.craft_ev(rate, 0.25, 1000, 2000, 500, 20) == pytest.approx(0)
    assert crafting.break_even_rate(0, 0, 0, 500) is None


def test_streak_risk_and_budget():
    assert crafting.streak_risk(0.2, 10) == pytest.approx(0.8**10)
    assert crafting.streak_attempts(0.2) == 14  # 0.8^14 = 4.4%, 0.8^13 = 5.5%
    assert crafting.streak_budget(0.2, 1000) == 14_000
    assert crafting.streak_attempts(1.0) == 1 and crafting.streak_attempts(0) is None


def test_resolve_rates_prefers_danny_then_api():
    assert crafting.resolve_rates((0.9, 0.3), (0.8, 0.25)).source == "manual"
    assert crafting.resolve_rates(None, (0.8, 0.25)) == crafting.Rates(0.8, 0.25, "api")
    assert crafting.resolve_rates((None, None), None).source == "unknown"


def test_chain_matches_the_api_for_a_recorded_craft():
    """Our chain maths reproduces the API's attempts and cost for a real three-step craft."""
    detail = load("craft_detail")
    steps = [
        crafting.ChainStep(
            s["success_rate"],
            s["combo_probability"],
            sum(mat["per_attempt"] * mat["unit_price"] for mat in s["materials"]),
        )
        for s in detail["steps"]
    ]
    chain = crafting.chain_cost(steps)
    assert chain.attempts == pytest.approx([s["attempts"] for s in detail["steps"]])
    assert chain.step_costs == pytest.approx([s["step_cost"] for s in detail["steps"]])
    assert chain.cost == pytest.approx(detail["latest"]["cost"], rel=1e-6)


def test_chain_failed_intermediate_costs_its_retries():
    one = crafting.chain_cost([crafting.ChainStep(0.5, 0, 100, target_on="success")])
    assert one.attempts == (2,) and one.cost == 200
    two = crafting.chain_cost(
        [
            crafting.ChainStep(0.5, 0, 100, target_on="success"),
            crafting.ChainStep(0.25, 0, 10, target_on="success"),
        ]
    )
    assert two.attempts == (8, 4) and two.cost == 840


def test_chain_credits_byproducts():
    plain = crafting.chain_cost([crafting.ChainStep(1.0, 0.25, 100)])
    credited = crafting.chain_cost([crafting.ChainStep(1.0, 0.25, 100, byproduct_value=50)])
    assert plain.cost == 400
    assert credited.cost == pytest.approx(400 - 3 * 45)  # three plain results at net 45


def test_craft_profit():
    p = crafting.craft_profit(8000, 10_000, final_rate=0.25, final_cost_per_attempt=1000)
    assert p.profit == pytest.approx(1000) and p.margin_pct == pytest.approx(12.5)
    assert p.break_even_price == pytest.approx(8000 / 0.9)
    assert p.streak_attempts == 11 and p.streak_budget == 11_000


def test_outcome_check_flags_results_well_below_expected():
    bad = crafting.outcome_check(40, 28, 0.93)
    fine = crafting.outcome_check(40, 37, 0.93)
    assert bad.below_expected and bad.observed_rate == 0.7
    assert not fine.below_expected
    assert not crafting.outcome_check(0, 0, 0.93).below_expected


# -- flips ----------------------------------------------------------------------------


def flip(**kw) -> flips.FlipInput:
    base = dict(
        item_key=1, name="x", buy_price=60_000, median_7d=100_000, history_days=7,
        avg_listings=20, sales=SalesStats(300, 95_000), liquidity=Liquidity(10, "sales"),
        second_cheapest=98_000, units_at_price=2,
    )
    return flips.FlipInput(**(base | kw))


def test_flip_target_is_capped_by_median_second_listing_and_sales():
    [c] = flips.find_flips([flip()])
    assert (c.resale_target, c.basis) == (95_000, "sales_avg")
    assert c.profit == pytest.approx(0.9 * 95_000 - 60_000)
    assert c.roi_pct == pytest.approx(42.5) and c.confidence == "high"
    assert c.days_to_sell == pytest.approx(0.2) and c.horizon_profit == pytest.approx(51_000)

    [c] = flips.find_flips([flip(second_cheapest=80_000)])
    assert (c.resale_target, c.basis) == (79_999, "second_cheapest")


def test_flip_thresholds():
    assert flips.find_flips([flip(buy_price=84_000)]) == []  # profit 1,500 < 5,000
    assert flips.find_flips([flip(buy_price=80_000)]) == []  # ROI 6.9% < 8%
    assert len(flips.find_flips([flip(buy_price=84_000)], min_profit=1000, min_roi_pct=1)) == 1


def test_flip_liquidity_filter():
    assert flips.find_flips([flip(history_days=2)]) == []
    assert flips.find_flips([flip(avg_listings=4)]) == []


def test_flip_without_sales_data_is_low_confidence():
    [c] = flips.find_flips(
        [flip(sales=SalesStats(), liquidity=Liquidity(3, "listings_proxy"), second_cheapest=None)]
    )
    assert c.basis == "median_7d" and c.confidence == "low"


def test_flips_rank_by_profit_within_the_horizon():
    slow = flip(item_key=1, buy_price=10_000, liquidity=Liquidity(0.1, "sales"))
    fast = flip(item_key=2, buy_price=70_000, liquidity=Liquidity(50, "sales"))
    assert [c.item_key for c in flips.find_flips([slow, fast])] == [2, 1]


# -- region comparison and merge ----------------------------------------------------------


def test_compare_with_region():
    others = {1: 1000, 2: 1100, 3: 900, 4: 1000}
    cheap = merge.compare_with_region(7, 600, others, {1: 280, 2: 560, 3: 840})
    assert cheap.verdict == "cheap" and cheap.gap_pct == pytest.approx(-40)
    assert cheap.regional_median == 1000 and cheap.reference_units_per_day == 20
    assert merge.compare_with_region(7, 1300, others).verdict == "expensive"
    assert merge.compare_with_region(7, 1050, others).verdict == "in_line"
    assert merge.compare_with_region(7, None, others).verdict == "not_listed_here"
    assert merge.compare_with_region(7, 600, {1: 1000}).verdict == "unknown"


def test_merge_shift_and_hold_value():
    shift = merge.merge_shift([100, 110, 90], [150, 140, 160])
    assert (shift.pre_median, shift.post_median, shift.change_pct) == (100, 150, 50)
    assert merge.merge_shift([], [150]).change_pct is None
    held = merge.hold_value(10, 500, 1000)
    assert (held.cost, held.value, held.profit, held.profit_pct) == (5000, 9000, 4000, 80)


# -- trust --------------------------------------------------------------------------------


def test_sanity_flags():
    assert trust.sanity_flags(100, 100, 50, 90, 200) == ()
    assert trust.sanity_flags(600, 100) == ("price_jump",)
    assert trust.sanity_flags(15, 100) == ("price_drop",)
    assert trust.sanity_flags(100, 100, 200, 90, 300) == ("sales_inconsistent",)
    assert trust.sanity_flags(0, None) == ("bad_price",)


def test_data_age():
    now = T0
    age = trust.data_age(now, now - timedelta(hours=1), now - timedelta(days=1))
    assert not age.listings_stale and not age.sales_stale
    age = trust.data_age(now, now - timedelta(hours=3), now - timedelta(days=3))
    assert age.listings_stale and age.sales_stale
    assert trust.data_age(now, None, None).listings_stale


def test_low_coverage():
    assert trust.low_coverage(samples=5, history_days=10)
    assert trust.low_coverage(samples=100, history_days=1)
    assert trust.low_coverage(samples=100, history_days=10, snapshots_24h=3)
    assert not trust.low_coverage(samples=100, history_days=10, snapshots_24h=24)


def test_reality_checks():
    assert not trust.trade_gap(1000, 950, 500, 2000).large
    assert trust.trade_gap(1500, 1000, 500, 2000).large  # 50% above the average
    assert trust.trade_gap(2500, 2400, 500, 2000).outside_range
    assert trust.trade_gap(1000, None).gap_pct is None
    assert trust.rate_gap(40, 28, 0.93).below_expected
