"""Realistic sale price and sell-through. Plain functions: no database, no network.

Sales statistics are used when the API has them. A server without them (a new
one) still gets an estimate from listings, tagged low-confidence, and its
liquidity comes from how fast listings disappear between snapshots.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Literal

Confidence = Literal["high", "medium", "low"]
LiquiditySource = Literal["sales", "listings_proxy", "reference", "unknown"]


@dataclass(frozen=True)
class Obs:
    """One observation of an item's cheapest price and listing count."""

    ts: datetime
    price: float
    listings: int | None = None


@dataclass(frozen=True)
class SalesStats:
    """The API's rolling-window sale statistics; all None when the server has none."""

    units_sold: int | None = None
    avg_price: float | None = None
    min_price: float | None = None
    max_price: float | None = None
    window_days: float = 28.0
    deltas: tuple[int, ...] = ()  # day-to-day change of units_sold, oldest first

    @property
    def available(self) -> bool:
        return self.units_sold is not None

    def anchored(self, min_units: int = 10) -> bool:
        return bool(self.units_sold and self.units_sold >= min_units and self.avg_price)


@dataclass(frozen=True)
class PriceEstimate:
    price: float  # what Danny can realistically sell one unit for (before fees)
    basis: str  # 'sales_avg' | 'depth' | 'undercut'
    cheapest: float
    reference: float  # the cheapest price after the spike filter
    median_7d: float | None
    spike: bool
    illiquid: bool | None  # None = cannot tell (no sales data)
    confidence: Confidence
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Liquidity:
    units_per_day: float | None
    source: LiquiditySource
    flags: tuple[str, ...] = ()


# -- series helpers -------------------------------------------------------------


def window(history: Sequence[Obs], now: datetime, days: float = 7) -> list[Obs]:
    start = now - timedelta(days=days)
    return [o for o in history if start <= o.ts <= now]


def days_of_data(history: Sequence[Obs]) -> float:
    if len(history) < 2:
        return 0.0
    return (history[-1].ts - history[0].ts).total_seconds() / 86400


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation."""
    mid = median(values)
    return median(abs(v - mid) for v in values)


def spike_filter(prices: Sequence[float], k: float = 3.0, persist: int = 3) -> tuple[float, bool]:
    """Returns (price to trust, was the current price a spike).

    prices is the 7-day series, oldest first, ending with the current price. A
    move of more than k MADs is replaced by the median until the last `persist`
    observations all sit beyond the band on the same side.
    """
    current = prices[-1]
    if len(prices) < persist + 3:
        return current, False  # too little history to call anything a spike
    base = prices[:-1]
    mid = median(base)
    spread = max(mad(base), 0.01 * mid)  # a flat series would make any move infinite MADs

    def beyond(price: float) -> int:
        deviation = (price - mid) / spread
        return (deviation > k) - (deviation < -k)

    side = beyond(current)
    if side == 0:
        return current, False
    if all(beyond(p) == side for p in prices[-persist:]):
        return current, False  # the move persisted: it is the new price
    return mid, True


def depth_price(levels: Sequence[tuple[float, int]], units: int = 10) -> float | None:
    """Average unit price of the cheapest `units` units. levels = (unit_price, units), cheapest
    first. None when fewer units are listed."""
    need, cost = units, 0.0
    for unit_price, available in levels:
        take = min(need, available)
        cost += take * unit_price
        need -= take
        if need == 0:
            return cost / units
    return None


def is_illiquid(days: Sequence[tuple[int | None, int | None]], min_days: int = 3) -> bool | None:
    """Stuck listings: over the last `min_days`+ days the listing count stayed flat while
    units_sold did not grow. days = (listings, units_sold) per day, oldest first.
    None when there is not enough data or no sales data to judge."""
    recent = [d for d in days if d[0] is not None and d[1] is not None][-(min_days + 1) :]
    if len(recent) < min_days + 1:
        return None
    listings = [d[0] for d in recent]
    flat = max(listings) - min(listings) <= max(1, 0.1 * max(listings))
    no_sales = recent[-1][1] <= recent[0][1]
    return flat and no_sales and max(listings) > 0


# -- estimated sale price ---------------------------------------------------------


def estimate_sale_price(
    cheapest: float,
    prices_7d: Sequence[float] = (),
    sales: SalesStats = SalesStats(),
    depth_unit_price: float | None = None,
    illiquid: bool | None = None,
    history_days: float = 0.0,
    *,
    undercut_step: float = 1,
    min_sales_units: int = 10,
    spike_mads: float = 3.0,
    spike_persist: int = 3,
) -> PriceEstimate:
    """The lowest of: the sale average (if anchored), the depth price, and the cheapest
    listing minus one undercut step. prices_7d ends with the current cheapest price."""
    series = list(prices_7d) or [cheapest]
    if series[-1] != cheapest:
        series.append(cheapest)
    reference, spike = spike_filter(series, spike_mads, spike_persist)
    median_7d = median(series) if len(series) >= 2 else None

    candidates = {"undercut": max(1.0, reference - undercut_step)}
    if depth_unit_price is not None:
        candidates["depth"] = depth_unit_price
    anchored = sales.anchored(min_sales_units)
    if anchored:
        candidates["sales_avg"] = sales.avg_price
    # An illiquid item's listing price is not trusted: fall back on real sales if any.
    if illiquid and anchored:
        candidates = {"sales_avg": sales.avg_price}
    basis = min(candidates, key=candidates.get)

    flags = []
    if spike:
        flags.append("spike")
    if illiquid:
        flags.append("illiquid")
    if not sales.available:
        flags.append("no_sales_data")
    elif not anchored:
        flags.append("few_sales")
    if history_days < 3:
        flags.append("thin_history")

    if illiquid or history_days < 3:
        confidence: Confidence = "low"
    elif anchored:
        confidence = "high"
    else:
        confidence = "medium" if sales.available else "low"

    return PriceEstimate(
        candidates[basis], basis, cheapest, reference, median_7d, spike, illiquid, confidence,
        tuple(flags),
    )


# -- sell-through ---------------------------------------------------------------


def listing_turnover_per_day(history: Sequence[Obs], min_hours: float = 6) -> float | None:
    """Listings that disappeared per day: the sum of drops in the listing count between
    snapshots. A proxy for sales when the API has none. It also counts cancelled and
    expired listings (too high) and treats a stack as one sale (too low)."""
    points = [o for o in history if o.listings is not None]
    if len(points) < 2:
        return None
    span_hours = (points[-1].ts - points[0].ts).total_seconds() / 3600
    if span_hours < min_hours:
        return None
    drops = sum(max(0, a.listings - b.listings) for a, b in zip(points, points[1:]))
    return drops / (span_hours / 24)


def sell_through(
    sales: SalesStats = SalesStats(),
    history: Sequence[Obs] = (),
    reference_units_per_day: float | None = None,
) -> Liquidity:
    """Units per day, from the best source available."""
    if sales.available and sales.window_days > 0:
        flags = []
        # units_sold is a rolling total, so its day-to-day change shows the trend.
        if sales.deltas and sales.units_sold:
            trend = sum(sales.deltas) / sales.units_sold
            if trend > 0.2:
                flags.append("sales_rising")
            elif trend < -0.2:
                flags.append("sales_falling")
        return Liquidity(sales.units_sold / sales.window_days, "sales", tuple(flags))
    proxy = listing_turnover_per_day(history)
    if proxy is not None:
        return Liquidity(proxy, "listings_proxy", ("low_confidence",))
    if reference_units_per_day is not None:
        return Liquidity(reference_units_per_day, "reference", ("other_servers",))
    return Liquidity(None, "unknown")


def days_to_sell(qty: float, units_per_day: float | None, ahead: float = 0) -> float | None:
    """Days to sell qty units; `ahead` = competing units listed below Danny's price."""
    if not units_per_day or units_per_day <= 0:
        return None
    return (qty + ahead) / units_per_day


def horizon_profit(
    profit_per_unit: float,
    qty: float,
    units_per_day: float | None,
    horizon_days: float = 3,
    ahead: float = 0,
) -> float:
    """Expected profit within the horizon: a big margin on an item that barely sells ranks low."""
    if not units_per_day or units_per_day <= 0:
        return 0.0
    absorbed = max(0.0, units_per_day * horizon_days - ahead)
    return profit_per_unit * min(qty, absorbed)
