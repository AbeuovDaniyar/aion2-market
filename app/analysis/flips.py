"""Flip finder: listings priced far enough below what the item resells for."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.analysis.fees import Fees
from app.analysis.pricing import Confidence, Liquidity, SalesStats, days_to_sell, horizon_profit


@dataclass(frozen=True)
class FlipInput:
    item_key: int
    name: str | None
    buy_price: float  # the listing Danny would buy
    median_7d: float | None
    history_days: float
    avg_listings: float
    sales: SalesStats = SalesStats()
    liquidity: Liquidity = Liquidity(None, "unknown")
    second_cheapest: float | None = None  # the next listing up, from the depth ladder
    units_at_price: int = 1
    spike: bool = False


@dataclass(frozen=True)
class FlipCandidate:
    item_key: int
    name: str | None
    buy_price: float
    resale_target: float
    profit: float  # per unit, after fees
    roi_pct: float
    units: int
    days_to_sell: float | None
    horizon_profit: float
    confidence: Confidence
    basis: str  # what capped the resale target


def resale_target(
    flip: FlipInput, undercut_step: float = 1, min_sales_units: int = 10
) -> tuple[float, str] | None:
    """What the item resells for once the cheap listing is gone: the sale average if
    anchored, never above the 7-day median or the second-cheapest current listing."""
    caps: dict[str, float] = {}
    if flip.median_7d is not None:
        caps["median_7d"] = flip.median_7d
    if flip.second_cheapest is not None:
        caps["second_cheapest"] = max(1.0, flip.second_cheapest - undercut_step)
    if flip.sales.anchored(min_sales_units):
        caps["sales_avg"] = flip.sales.avg_price
    if not caps:
        return None
    basis = min(caps, key=caps.get)
    return caps[basis], basis


def find_flips(
    inputs: Iterable[FlipInput],
    fees: Fees = Fees(),
    *,
    min_profit: float = 5000,
    min_roi_pct: float = 8,
    min_days: float = 3,
    min_listings: float = 5,
    horizon_days: float = 3,
    undercut_step: float = 1,
    min_sales_units: int = 10,
) -> list[FlipCandidate]:
    """Ranked by expected profit within the horizon, then by profit per unit."""
    found = []
    for flip in inputs:
        # Liquidity filter: thin history or a thin market cannot support a resale price.
        if flip.history_days < min_days or flip.avg_listings < min_listings:
            continue
        target = resale_target(flip, undercut_step, min_sales_units)
        if target is None:
            continue
        price, basis = target
        profit = fees.net(price) - flip.buy_price
        roi_pct = 100 * profit / flip.buy_price
        if profit < min_profit or roi_pct < min_roi_pct:
            continue

        rate = flip.liquidity.units_per_day
        if flip.sales.anchored(min_sales_units) and flip.second_cheapest is not None:
            confidence: Confidence = "high" if flip.history_days >= 7 else "medium"
        elif flip.sales.anchored(min_sales_units) or flip.second_cheapest is not None:
            confidence = "medium"
        else:
            confidence = "low"
        if flip.spike or flip.liquidity.source in ("listings_proxy", "reference", "unknown"):
            confidence = "low" if confidence != "high" else "medium"

        found.append(
            FlipCandidate(
                flip.item_key,
                flip.name,
                flip.buy_price,
                price,
                profit,
                roi_pct,
                flip.units_at_price,
                days_to_sell(flip.units_at_price, rate),
                horizon_profit(profit, flip.units_at_price, rate, horizon_days),
                confidence,
                basis,
            )
        )
    return sorted(found, key=lambda c: (c.horizon_profit, c.profit), reverse=True)
