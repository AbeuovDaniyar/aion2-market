"""Server comparison: this server against the rest of its region, and before/after a merge.

Older servers in the region are a reference market. An item priced well below
the regional median here has room to rise as this server matures or merges;
it is a signal, not a forecast.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Literal

from app.analysis.fees import Fees

Verdict = Literal["cheap", "expensive", "in_line", "not_listed_here", "unknown"]


@dataclass(frozen=True)
class RegionComparison:
    item_key: int
    my_price: float | None
    regional_median: float | None
    regional_low: float | None
    regional_high: float | None
    servers: int  # reference servers listing the item
    gap_pct: float | None  # my price against the regional median; negative = cheaper here
    verdict: Verdict
    reference_units_per_day: float | None  # median sales rate on the reference servers


def compare_with_region(
    item_key: int,
    my_price: float | None,
    other_prices: Mapping[int, float],
    other_units_sold: Mapping[int, int] | None = None,
    *,
    gap_pct: float = 15,
    min_servers: int = 3,
    window_days: float = 28,
) -> RegionComparison:
    """other_prices / other_units_sold are keyed by server id."""
    prices = [p for p in other_prices.values() if p and p > 0]
    sold = [u for u in (other_units_sold or {}).values() if u is not None]
    rate = median(sold) / window_days if sold else None
    if len(prices) < min_servers:
        return RegionComparison(item_key, my_price, None, None, None, len(prices), None,
                                "unknown", rate)
    mid = median(prices)
    if my_price is None:
        return RegionComparison(item_key, None, mid, min(prices), max(prices), len(prices),
                                None, "not_listed_here", rate)
    gap = 100 * (my_price - mid) / mid
    verdict: Verdict = "cheap" if gap <= -gap_pct else "expensive" if gap >= gap_pct else "in_line"
    return RegionComparison(item_key, my_price, mid, min(prices), max(prices), len(prices),
                            gap, verdict, rate)


@dataclass(frozen=True)
class DemandReference:
    """What an established server with sales statistics says about an item. A different
    market: use it for which items move and roughly what they settle at, not as this
    server's numbers."""

    units_per_day: float | None
    sold_avg: float | None  # average real sale price there
    price: float | None  # cheapest listing there
    servers: int


def demand_reference(
    rows: Sequence[tuple[float | None, int | None, float | None]], window_days: float = 28
) -> DemandReference | None:
    """rows = (cheapest price, units_sold, sold_avg) per demand-reference server."""
    if not rows:
        return None
    sold = [u for _, u, _ in rows if u is not None]
    averages = [a for _, _, a in rows if a]
    prices = [p for p, _, _ in rows if p]
    return DemandReference(
        median(sold) / window_days if sold else None,
        median(averages) if averages else None,
        median(prices) if prices else None,
        len(rows),
    )


@dataclass(frozen=True)
class MergeShift:
    pre_median: float | None
    post_median: float | None
    change_pct: float | None


def merge_shift(pre_prices: Sequence[float], post_prices: Sequence[float]) -> MergeShift:
    """Median price before and after the cross-server market opened."""
    pre = median(pre_prices) if pre_prices else None
    post = median(post_prices) if post_prices else None
    change = 100 * (post - pre) / pre if pre and post is not None else None
    return MergeShift(pre, post, change)


@dataclass(frozen=True)
class Holding:
    qty: int
    cost: float
    value: float  # net of fees at the estimated sale price
    profit: float
    profit_pct: float | None


def hold_value(qty: int, avg_cost: float | None, sale_price: float, fees: Fees = Fees()) -> Holding:
    cost = qty * (avg_cost or 0)
    value = qty * fees.net(sale_price)
    return Holding(qty, cost, value, value - cost, 100 * (value - cost) / cost if cost else None)
