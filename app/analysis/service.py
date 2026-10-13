"""Glue between SQLite and the analysis functions: reads stored data, returns ready rows.

Everything here reads the database only; nothing waits on the network.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import sqlite3
from statistics import mean, median, pstdev

from app.analysis import alerts, crafting, flips, ledger, merge, pricing, trust
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
    output_key: int
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
    logged: crafting.OutcomeCheck | None = None  # Danny's logged results against the rate
    logged_attempts: int = 0


def recipe_rows(db: Database, views: list[ItemView]) -> list[RecipeRow]:
    fees = Fees.from_db(db)
    risk = db.get_float("streak_risk")
    by_key = {v.item_key: v for v in views}
    outcomes = db.craft_outcome_totals()
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
        logged, attempts = None, 0
        total = outcomes.get(recipe["id"])
        if total and total["attempts"]:
            attempts = total["attempts"]
            if p_s is not None:
                logged = crafting.outcome_check(attempts, total["successes"] or 0, p_s)
                if logged.below_expected:
                    flags.append("results_below_expected")
        rows.append(
            RecipeRow(recipe["id"], recipe["output_key"], name, p_s, p_c, materials, ev, margin, per_hour, even,
                      budget, saturated, tuple(flags), logged, attempts)
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


# -- milestone 4: alerts, watchlist and merge, trust gaps ---------------------------------


def build_alerts(
    db: Database,
    views: list[ItemView],
    flip_rows: list[flips.FlipCandidate],
    craft_rows: list[CraftRow],
    now: datetime | None = None,
) -> list[alerts.Alert]:
    """Everything worth a toast right now. The caller pauses them while data is stale."""
    now = now or utcnow()
    by_key = {v.item_key: v for v in views}
    watched = set(db.watched_keys())
    found = []
    for trade in db.trades():
        view = by_key.get(trade["item_key"])
        if trade["side"] != "sell" or trade["done_at"] or view is None or not view.usable:
            continue
        if alerts.is_undercut(trade["unit_price"], view.cheapest):
            found.append(
                alerts.Alert(
                    "Undercut", view.item_key, view.name or str(view.item_key),
                    f"your listing at {trade['unit_price']:,.0f} is undercut: cheapest is now "
                    f"{view.cheapest:,.0f}", trade["id"],
                )
            )
    dip_pct = db.get_float("dip_pct")
    drop_pct, days = db.get_float("squeeze_drop_pct"), db.get_float("squeeze_days")
    for view in views:
        if not view.usable:
            continue
        name = view.name or str(view.item_key)
        if view.item_key in watched and view.history_days >= 1:
            dip = alerts.price_dip(view.cheapest, view.estimate.median_7d, dip_pct)
            if dip is not None:
                found.append(
                    alerts.Alert(
                        "Price dip", view.item_key, name,
                        f"{view.cheapest:,.0f} is {dip:.0f}% below its 7-day median "
                        f"({view.estimate.median_7d:,.0f})",
                    )
                )
        squeeze = alerts.supply_squeeze(view.history, now, drop_pct, days)
        if squeeze is not None:
            found.append(
                alerts.Alert(
                    "Supply squeeze", view.item_key, name,
                    f"listings down {squeeze:.0f}% in {days:.0f} days while the price rose",
                )
            )
    threshold = db.get_float("alert_min_profit")
    for flip in flip_rows:
        if flip.confidence != "low" and max(flip.profit, flip.horizon_profit) >= threshold:
            found.append(
                alerts.Alert(
                    "Flip", flip.item_key, flip.name or str(flip.item_key),
                    f"buy at {flip.buy_price:,.0f}, resell near {flip.resale_target:,.0f} "
                    f"({flip.profit:,.0f} profit per unit)",
                )
            )
    for row in craft_rows:
        if row.profit and row.profit.profit >= threshold and "low_confidence_price" not in row.flags:
            found.append(
                alerts.Alert(
                    "Craft", row.target_key, row.name or str(row.target_key),
                    f"expected profit {row.profit.profit:,.0f} per finished item",
                )
            )
    return found


@dataclass(frozen=True)
class WatchRow:
    view: ItemView
    watched: bool
    holding: merge.Holding | None
    avg_cost: float | None
    shift: merge.MergeShift | None  # before and after the cross-server market opened


def merge_opened_at(db: Database) -> datetime | None:
    if db.get_setting("cross_server_open") != "1":
        return None
    return parse_iso(db.get_setting("cross_server_opened_at"))


def set_merge_open(db: Database, server_id: int, is_open: bool, now: datetime | None = None) -> None:
    """Marks the cross-server market as open from now on; data after this is post-merge."""
    db.set_setting("cross_server_open", int(is_open))
    if is_open and not db.get_setting("cross_server_opened_at"):
        db.set_setting("cross_server_opened_at", (now or utcnow()).isoformat())
    if not is_open:
        db.set_setting("cross_server_opened_at", "")
    db.conn.execute("UPDATE servers SET is_cross_server = ? WHERE id = ?", (int(is_open), server_id))
    db.conn.commit()


def watch_rows(db: Database, views: list[ItemView], server_id: int) -> list[WatchRow]:
    """Watched and held items with their current value and merge comparison."""
    fees = Fees.from_db(db)
    opened = merge_opened_at(db)
    held = db.holdings()
    rows = []
    for view in views:
        entry = held.get(view.item_key)
        if entry is None:
            continue
        qty = entry["hold_qty"] or 0
        holding = (
            merge.hold_value(qty, entry["hold_avg_cost"], view.estimate.price, fees) if qty else None
        )
        shift = None
        if opened:
            history = db.full_price_history(server_id, view.item_key)
            pre = [h["price"] for h in history if parse_iso(h["ts"]) < opened]
            post = [h["price"] for h in history if parse_iso(h["ts"]) >= opened]
            shift = merge.merge_shift(pre, post)
        rows.append(WatchRow(view, bool(entry["watch"]), holding, entry["hold_avg_cost"], shift))
    return rows


def server_prices(db: Database, server_id: int, item_key: int, now: datetime | None = None):
    """(server name, cheapest price, listings) for this item on every reference server."""
    now = now or utcnow()
    names = {s["id"]: s["name"] for s in db.servers()}
    ids = db.reference_server_ids(server_id) + db.demand_server_ids(server_id)
    found = []
    for other in dict.fromkeys(ids):
        row = db.conn.execute(
            "SELECT price, listings, captured_at FROM item_snapshots WHERE server_id = ? AND "
            "item_key = ? ORDER BY captured_at DESC LIMIT 1",
            (other, item_key),
        ).fetchone()
        if row and now - parse_iso(row["captured_at"]) <= REFERENCE_MAX_AGE:
            found.append((names.get(other, str(other)), row["price"], row["listings"]))
    return sorted(found, key=lambda r: r[1])


@dataclass(frozen=True)
class TradeCheck:
    trade_id: int
    name: str
    price: float
    api_avg: float | None
    gap: trust.TradeGap


def trade_checks(db: Database, views: list[ItemView], server_id: int) -> list[TradeCheck]:
    """Danny's completed sales against the API's sale statistics for the same item and day."""
    by_key = {v.item_key: v for v in views}
    large = db.get_float("trade_gap_pct")
    found = []
    for trade in db.trades():
        if trade["side"] != "sell" or not trade["done_at"]:
            continue
        day = trade["done_at"][:10]
        stats = None
        for row in db.sales_days(server_id, trade["item_key"]):
            if row["registered"][:10] <= day:
                stats = (row["avg_price"], row["min_price"], row["max_price"])
        view = by_key.get(trade["item_key"])
        if stats is None and view and view.sales.available:
            stats = (view.sales.avg_price, view.sales.min_price, view.sales.max_price)
        if stats is None or stats[0] is None:
            continue
        found.append(
            TradeCheck(
                trade["id"], trade["name"] or str(trade["item_key"]), trade["unit_price"], stats[0],
                trust.trade_gap(trade["unit_price"], *stats, large_pct=large),
            )
        )
    return found


def realized(db: Database) -> list[ledger.Realized]:
    return ledger.realized_profit(db.trades(), Fees.from_db(db))