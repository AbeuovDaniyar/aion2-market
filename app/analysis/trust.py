"""Trust checks. There is no second source, so the API is checked against itself and
against Danny's own results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.analysis.crafting import OutcomeCheck, outcome_check


@dataclass(frozen=True)
class Age:
    listings_stale: bool  # greyed out and excluded from opportunities
    sales_stale: bool
    listings_age: timedelta | None
    sales_age: timedelta | None


def data_age(
    now: datetime,
    captured_at: datetime | None,
    sales_registered: datetime | None,
    listings_hours: float = 2,
    sales_days: float = 2,
) -> Age:
    listings_age = now - captured_at if captured_at else None
    sales_age = now - sales_registered if sales_registered else None
    return Age(
        listings_age is None or listings_age > timedelta(hours=listings_hours),
        sales_age is None or sales_age > timedelta(days=sales_days),
        listings_age,
        sales_age,
    )


def sanity_flags(
    price: float,
    median_7d: float | None,
    sold_min: float | None = None,
    sold_avg: float | None = None,
    sold_max: float | None = None,
    *,
    jump_factor: float = 5,
    drop_factor: float = 0.2,
) -> tuple[str, ...]:
    """Flags a price that jumped more than 5x or fell below 0.2x its 7-day median, and
    sale statistics that contradict each other."""
    flags = []
    if price <= 0:
        flags.append("bad_price")
    if median_7d:
        if price > jump_factor * median_7d:
            flags.append("price_jump")
        elif price < drop_factor * median_7d:
            flags.append("price_drop")
    stats = (sold_min, sold_avg, sold_max)
    if all(s is not None for s in stats) and not sold_min <= sold_avg <= sold_max:
        flags.append("sales_inconsistent")
    elif any(s is not None and s < 0 for s in stats):
        flags.append("sales_inconsistent")
    return tuple(flags)


def low_coverage(
    samples: int | None, history_days: float, snapshots_24h: int | None = None,
    min_samples: int = 24, min_days: float = 3, min_snapshots_24h: int = 12,
) -> bool:
    """Thin data: the item gets a low-confidence tag."""
    return (
        (samples is not None and samples < min_samples)
        or history_days < min_days
        or (snapshots_24h is not None and snapshots_24h < min_snapshots_24h)
    )


@dataclass(frozen=True)
class TradeGap:
    gap_pct: float | None  # Danny's price against the API's sale average that day
    outside_range: bool  # outside the API's sale min..max
    large: bool


def trade_gap(
    trade_price: float,
    api_avg: float | None,
    api_min: float | None = None,
    api_max: float | None = None,
    large_pct: float = 25,
) -> TradeGap:
    """Reality check: one of Danny's sales against the API's numbers for that item and day."""
    gap = 100 * (trade_price - api_avg) / api_avg if api_avg else None
    outside = (api_min is not None and trade_price < api_min) or (
        api_max is not None and trade_price > api_max
    )
    return TradeGap(gap, outside, outside or (gap is not None and abs(gap) > large_pct))


def rate_gap(attempts: int, successes: int, api_rate: float) -> OutcomeCheck:
    """Reality check: Danny's craft outcomes against the API's success rate."""
    return outcome_check(attempts, successes, api_rate)
