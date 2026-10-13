"""Farming value per hour, and timing insights (when prices tend to be high)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import mean

from app.analysis.fees import Fees

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class RunValue:
    value: float  # drops at net market prices, plus raw Kina
    per_hour: float | None
    unpriced: int  # drop lines with no market price


def run_value(
    minutes: float,
    kina: float,
    drops: Iterable[tuple[int, int]],
    price_of: Callable[[int], float | None],
    fees: Fees = Fees(),
) -> RunValue:
    """drops = (item_key, qty); price_of gives the estimated sale price of an item."""
    value, unpriced = float(kina or 0), 0
    for key, qty in drops:
        price = price_of(key)
        if price is None:
            unpriced += 1
        else:
            value += qty * fees.net(price)
    return RunValue(value, value * 60 / minutes if minutes and minutes > 0 else None, unpriced)


@dataclass(frozen=True)
class RouteValue:
    route: str
    runs: int
    hours: float
    value: float
    per_hour: float | None


def rank_routes(runs: Iterable[tuple[str, float, RunValue]]) -> list[RouteValue]:
    """runs = (route, minutes, value). Kina per hour over all of a route's runs, best first."""
    totals: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    for route, minutes, value in runs:
        entry = totals[route or "(unnamed)"]
        entry[0] += 1
        entry[1] += (minutes or 0) / 60
        entry[2] += value.value
    routes = [
        RouteValue(route, int(n), hours, value, value / hours if hours > 0 else None)
        for route, (n, hours, value) in totals.items()
    ]
    return sorted(routes, key=lambda r: r.per_hour or 0, reverse=True)


@dataclass(frozen=True)
class TimingProfile:
    days: float
    by_weekday: dict[str, float]  # average price, Mon..Sun (local time)
    by_hour: dict[int, float]  # average price per hour of the day (local time)
    best_weekday: str  # when prices run highest: the time to list
    best_hour: int
    weekday_spread_pct: float  # best weekday against the overall average


def timing_profile(points: Sequence[tuple[datetime, float]], min_days: float = 14) -> TimingProfile | None:
    """Average price by weekday and time of day. None until min_days of data exist."""
    if len(points) < 2:
        return None
    span = (points[-1][0] - points[0][0]).total_seconds() / 86400
    if span < min_days:
        return None
    weekday: dict[int, list[float]] = defaultdict(list)
    hour: dict[int, list[float]] = defaultdict(list)
    for ts, price in points:
        local = ts.astimezone()
        weekday[local.weekday()].append(price)
        hour[local.hour].append(price)
    by_weekday = {WEEKDAYS[d]: mean(v) for d, v in sorted(weekday.items())}
    by_hour = {h: mean(v) for h, v in sorted(hour.items())}
    overall = mean(p for _, p in points)
    best_day = max(by_weekday, key=by_weekday.get)
    return TimingProfile(
        span, by_weekday, by_hour, best_day, max(by_hour, key=by_hour.get),
        100 * (by_weekday[best_day] - overall) / overall if overall else 0.0,
    )
