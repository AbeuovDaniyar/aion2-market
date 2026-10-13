"""Glue between SQLite and the analysis functions: reads stored data, returns ready rows.

Everything here reads the database only; nothing waits on the network.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import sqlite3
from statistics import mean, median, pstdev

from app.analysis import crafting, flips, merge, pricing, trust
from app.analysis.fees import Fees
from app.db.repo import Database, parse_iso, utcnow
from app.external import models as m

REFERENCE_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class ItemView:
    item_key: int
    name: str | None
    section: str | None
    category_name: str | None
    captured_at: datetime
    cheapest: float
    listings: int
    sales: pricing.SalesStats
    history: tuple[pricing.Obs, ...]  # 7 days, oldest first
    history_days: float
    estimate: pricing.PriceEstimate
    liquidity: pricing.Liquidity
    age: trust.Age
    sanity: tuple[str, ...]
    region: merge.RegionComparison | None = None
    demand: merge.DemandReference | None = None  # established servers with sales statistics

    @property
    def usable(self) -> bool:
        """Fresh and sane enough to appear in opportunities."""
        return not self.age.listings_stale and not self.sanity


def _history(db: Database, server_id: int, now: datetime) -> dict[int, list[pricing.Obs]]:
    """Price points (watched items) overlaid with our own snapshots, per item."""
    since = now - timedelta(days=7)
    merged: dict[int, dict[datetime, pricing.Obs]] = defaultdict(dict)
    for row in db.price_point_history(server_id, since):
        ts = parse_iso(row["ts"])
        merged[row["item_key"]][ts] = pricing.Obs(ts, row["price"], row["listings"])
    for row in db.snapshot_history(server_id, since):
        ts = parse_iso(row["captured_at"])
        merged[row["item_key"]][ts] = pricing.Obs(ts, row["price"], row["listings"])
    return {key: [obs[ts] for ts in sorted(obs) if ts <= now] for key, obs in merged.items()}


def _daily_listings_and_sales(db: Database, server_id: int, now: datetime):
    """(listings, units_sold) per day and item, for the stuck-listing check."""
    days: dict[int, dict[str, tuple]] = defaultdict(dict)
    for row in db.snapshot_history(server_id, now - timedelta(days=7)):
        days[row["item_key"]][row["captured_at"][:10]] = (row["listings"], row["units_sold"])
    return {key: [by_day[d] for d in sorted(by_day)] for key, by_day in days.items()}


def _region(db: Database, server_id: int, now: datetime, my_prices: dict[int, float]):
    prices: dict[int, dict[int, float]] = defaultdict(dict)
    sold: dict[int, dict[int, int]] = defaultdict(dict)
    for other in db.reference_server_ids(server_id):
        for row in db.latest_snapshots(other):
            if now - parse_iso(row["captured_at"]) > REFERENCE_MAX_AGE:
                continue
            prices[row["item_key"]][other] = row["price"]
            if row["units_sold"] is not None:
                sold[row["item_key"]][other] = row["units_sold"]
    gap = db.get_float("region_gap_pct")
    window_days = db.get_float("sales_window_days")
    return {
        key: merge.compare_with_region(
            key, my_prices.get(key), prices[key], sold.get(key), gap_pct=gap,
            window_days=window_days,
        )
        for key in prices
    }


def _demand(db: Database, server_id: int, now: datetime):
    rows: dict[int, list[tuple]] = defaultdict(list)
    for other in db.demand_server_ids(server_id):
        for row in db.latest_snapshots(other):
            if now - parse_iso(row["captured_at"]) <= REFERENCE_MAX_AGE:
                rows[row["item_key"]].append((row["price"], row["units_sold"], row["sold_avg"]))
    window_days = db.get_float("sales_window_days")
    return {key: merge.demand_reference(found, window_days) for key, found in rows.items()}


def item_views(
    db: Database,
    server_id: int,
    now: datetime | None = None,
    sales_batch_at: datetime | None = None,
) -> list[ItemView]:
    """Every item on the server with its estimate, liquidity and trust flags.
    sales_batch_at is the last sales batch day (Freshness.sales_batch_at)."""
    now = now or utcnow()
    history = _history(db, server_id, now)
    daily = _daily_listings_and_sales(db, server_id, now)
    rows = db.latest_snapshots(server_id)
    region = _region(db, server_id, now, {r["item_key"]: r["price"] for r in rows})
    demand = _demand(db, server_id, now)
    window_days = db.get_float("sales_window_days")
    min_units = int(db.get_float("min_sales_units"))

    views = []
    for row in rows:
        key = row["item_key"]
        series = history.get(key, [])
        span = pricing.days_of_data(series)
        sales = pricing.SalesStats(
            row["units_sold"], row["sold_avg"], row["sold_min"], row["sold_max"], window_days,
            tuple(d["units_delta"] for d in db.sales_days(server_id, key) if d["units_delta"]),
        )
        comparison = region.get(key)
        reference = demand.get(key)
        # Demand borrowed from elsewhere: the region first, then the established servers.
        borrowed = comparison.reference_units_per_day if comparison else None
        if borrowed is None and reference:
            borrowed = reference.units_per_day
        estimate = pricing.estimate_sale_price(
            row["price"],
            [o.price for o in series],
            sales,
            illiquid=pricing.is_illiquid(daily.get(key, [])),
            history_days=span,
            undercut_step=db.get_float("undercut_step"),
            min_sales_units=min_units,
            spike_mads=db.get_float("spike_mads"),
            spike_persist=int(db.get_float("spike_persist_refreshes")),
        )
        captured_at = parse_iso(row["captured_at"])
        views.append(
            ItemView(
                key, row["name"], row["section"], row["category_name"], captured_at,
                row["price"], row["listings"], sales, tuple(series), span, estimate,
                pricing.sell_through(sales, series, borrowed),
                trust.data_age(
                    now, captured_at, sales_batch_at if sales.available else None,
                    db.get_float("stale_listings_hours"), db.get_float("stale_sales_days"),
                ),
                trust.sanity_flags(
                    row["price"], estimate.median_7d, row["sold_min"], row["sold_avg"],
                    row["sold_max"], jump_factor=db.get_float("sanity_jump_factor"),
                    drop_factor=db.get_float("sanity_drop_factor"),
                ),
                comparison,
                reference,
            )
        )
    return views


@dataclass(frozen=True)
class PriceStats:
    median_7d: float | None
    low_7d: float | None
    high_7d: float | None
    change_vs_prev_week_pct: float | None
    volatility: float | None  # coefficient of variation over 7 days


def price_stats(points: list[pricing.Obs], now: datetime) -> PriceStats:
    """The Items tab stats line. points may span more than 7 days."""
    week = [o.price for o in pricing.window(points, now, 7)]
    previous = [o.price for o in points if now - timedelta(days=14) <= o.ts < now - timedelta(days=7)]
    if not week:
        return PriceStats(None, None, None, None, None)
    mid = median(week)
    change = 100 * (mid - median(previous)) / median(previous) if previous else None
    spread = pstdev(week) / mean(week) if len(week) > 1 and mean(week) else None
    return PriceStats(mid, min(week), max(week), change, spread)


def chart_points(db: Database, server_id: int, item_key: int) -> list[sqlite3.Row]:
    """Stored hourly price buckets for one item, oldest first (filled by load_item)."""
    return db.conn.execute(
        "SELECT ts, low, high, avg, close, listings FROM price_points "
        "WHERE server_id = ? AND item_key = ? AND bucket = ? ORDER BY ts",
        (server_id, item_key, "1h"),
    ).fetchall()


@dataclass(frozen=True)
class RecipeRow:
    """A recipe Danny entered by hand, valued with the single-craft EV formula."""

    recipe_id: int
    name: str
    success_rate: float | None
    combo_probability: float
    materials: float | None  # per attempt
    ev: float | None  # per attempt
    margin_pct: float | None
    per_hour: float | None
    break_even_rate: float | None
    streak_budget: float | None
    saturated: bool
    flags: tuple[str, ...]


def recipe_rows(db: Database, views: list[ItemView]) -> list[RecipeRow]:
    fees = Fees.from_db(db)
    risk = db.get_float("streak_risk")
    by_key = {v.item_key: v for v in views}
    rows = []
    for recipe, inputs in db.recipes():
        flags = []
        output = by_key.get(recipe["output_key"])
        combo = by_key.get(recipe["combo_output_key"]) if recipe["combo_output_key"] else output
        materials: float | None = 0.0
        for line in inputs:
            view = by_key.get(line["item_key"])
            market = view.cheapest if view else None
            # The cheaper of the Marketplace price and the NPC price.
            options = [p for p in (market, line["npc_price"]) if p is not None]
            if not options:
                materials = None
                flags.append("materials_not_listed")
                break
            materials += line["qty"] * min(options)
        p_s, p_c = recipe["success_rate"], recipe["combo_probability"] or 0.0
        if p_s is None:
            flags.append("rate_unknown")
        if output is None:
            flags.append("not_listed_here")
        ev = margin = per_hour = even = budget = None
        if p_s is not None and output is not None and materials is not None:
            fee = recipe["kina_cost"] or 0
            v_normal = output.estimate.price * (recipe["output_qty"] or 1)
            v_combo = (combo or output).estimate.price * (
                recipe["combo_output_qty"] or recipe["output_qty"] or 1
            )
            ev = crafting.craft_ev(p_s, p_c, v_normal, v_combo, materials, fee, fees)
            cost = materials + fee
            margin = 100 * ev / cost if cost else None
            if recipe["craft_seconds"]:
                per_hour = ev * 3600 / recipe["craft_seconds"]
            even = crafting.break_even_rate(p_c, v_normal, v_combo, materials, fee, fees)
            budget = crafting.streak_budget(p_s, cost, risk)
            if output.estimate.confidence == "low":
                flags.append("low_confidence_price")
        # Saturation: the output already has many listings or its price is falling.
        saturated = bool(output) and (
            output.listings >= 50
            or (len(output.history) >= 4 and output.history[-1].price < 0.9 * output.history[0].price)
        )
        name = (output.name if output else None) or f"item {recipe['output_key']}"
        rows.append(
            RecipeRow(recipe["id"], name, p_s, p_c, materials, ev, margin, per_hour, even,
                      budget, saturated, tuple(flags))
        )
    return sorted(rows, key=lambda r: r.ev if r.ev is not None else float("-inf"), reverse=True)


def region_gaps(views: list[ItemView], verdict: str = "cheap") -> list[ItemView]:
    """Items priced well below (or above) the regional median, biggest gap first."""
    # A lone listing at a silly price says nothing about this server being dear; a lone
    # cheap listing is still something Danny can buy.
    min_listings = 3 if verdict == "expensive" else 1
    hits = [
        v
        for v in views
        if v.region and v.region.verdict == verdict and v.usable and v.listings >= min_listings
    ]
    return sorted(hits, key=lambda v: abs(v.region.gap_pct), reverse=True)


def flip_candidates(
    db: Database, views: list[ItemView], ladders: dict[int, m.DepthLadder] | None = None
) -> list[flips.FlipCandidate]:
    """ladders (optional, per item) supply the second-cheapest listing and the units on offer."""
    inputs = []
    for view in views:
        if not view.usable:
            continue
        second, units = None, 1
        ladder = (ladders or {}).get(view.item_key)
        if ladder and ladder.levels:
            units = ladder.levels[0].units
            second = ladder.levels[1].unit_price if len(ladder.levels) > 1 else None
        listed = [o.listings for o in view.history if o.listings is not None]
        inputs.append(
            flips.FlipInput(
                view.item_key, view.name, view.cheapest, view.estimate.median_7d,
                view.history_days, mean(listed) if listed else view.listings, view.sales,
                view.liquidity, second, units, view.estimate.spike,
            )
        )
    return flips.find_flips(
        inputs,
        Fees.from_db(db),
        min_profit=db.get_float("flip_min_profit"),
        min_roi_pct=db.get_float("flip_min_roi_pct"),
        min_days=db.get_float("flip_min_days"),
        min_listings=db.get_float("flip_min_listings"),
        horizon_days=db.get_float("horizon_days"),
        undercut_step=db.get_float("undercut_step"),
        min_sales_units=int(db.get_float("min_sales_units")),
    )


@dataclass(frozen=True)
class CraftRow:
    target_key: int
    name: str | None
    skill: str
    attempts: tuple[float, ...]
    api_cost: float | None  # the API's expected cost (uses the cheapest route per material)
    buy_cost: float | None  # our own chain, buying every material at this server's prices
    sale_price: float | None  # estimated sale price here; None = not listed on this server
    profit: crafting.CraftProfit | None
    rate_source: str
    flags: tuple[str, ...]


def craft_table(
    db: Database, model: m.CraftModelInfo, crafts: m.CraftList, views: list[ItemView]
) -> list[CraftRow]:
    """The API's craft targets valued at this server's estimated sale prices."""
    fees = Fees.from_db(db)
    risk = db.get_float("streak_risk")
    by_key = {v.item_key: v for v in views}
    api_cost = {t.target_key: t.cost for t in crafts.targets}
    rows = []
    for profile in model.profiles:
        steps, missing = [], False
        for step in profile.steps:
            cost = 0.0
            for material in step.materials:
                view = by_key.get(material.material_key)
                if view is None:
                    missing = True
                else:
                    cost += material.per_attempt * view.cheapest
            steps.append(crafting.ChainStep(step.success_rate, step.combo_probability, cost))
        chain = crafting.chain_cost(steps, fees) if steps else None
        for member in profile.members:
            flags = []
            buy_cost = None if missing or chain is None else chain.cost
            if missing:
                flags.append("materials_not_listed")
            cost = api_cost.get(member.target_key)
            if cost is None:
                cost = buy_cost
            elif buy_cost and abs(buy_cost - cost) > 0.25 * cost:
                flags.append("cost_differs_from_api")
            view = by_key.get(member.target_key)
            profit = None
            if view is None:
                flags.append("not_listed_here")
            elif cost is not None and chain is not None:
                if view.estimate.confidence == "low":
                    flags.append("low_confidence_price")
                # One last-step attempt burns its materials and one intermediate item.
                attempt_cost = steps[-1].cost_per_attempt + (
                    sum(chain.step_costs[:-1]) / chain.attempts[-1]
                )
                profit = crafting.craft_profit(
                    cost, view.estimate.price, fees, chain.final_rate, attempt_cost, risk
                )
            rows.append(
                CraftRow(
                    member.target_key, member.name, profile.skill,
                    chain.attempts if chain else (), api_cost.get(member.target_key), buy_cost,
                    view.estimate.price if view else None, profit, "api", tuple(flags),
                )
            )
    return sorted(rows, key=lambda r: r.profit.profit if r.profit else float("-inf"), reverse=True)
