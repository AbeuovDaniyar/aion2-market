"""Alerts and the small Kina tools: undercut, supply squeeze, price dip, sell vs. use."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from app.analysis.fees import Fees
from app.analysis.pricing import Obs


@dataclass(frozen=True)
class Alert:
    kind: str  # 'Undercut' | 'Price dip' | 'Supply squeeze' | 'Flip' | 'Craft'
    item_key: int
    name: str
    message: str
    ref: int | None = None  # trade id for undercuts

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.item_key}:{self.ref or ''}"


def is_undercut(my_price: float, cheapest: float | None) -> bool:
    """Someone lists the same item cheaper than Danny's active listing."""
    return cheapest is not None and cheapest < my_price


def price_dip(cheapest: float, median_7d: float | None, dip_pct: float = 20) -> float | None:
    """How far (in %) the current price is below the 7-day median, if at least dip_pct."""
    if not median_7d:
        return None
    below = 100 * (median_7d - cheapest) / median_7d
    return below if below >= dip_pct else None


def supply_squeeze(
    history: Sequence[Obs], now: datetime, drop_pct: float = 40, days: float = 3
) -> float | None:
    """The listing count fell by drop_pct or more over `days` while the median price rose.
    Returns the drop in %, or None. Often a good time to sell into, or to stop crafting for."""
    recent = [o for o in history if o.listings is not None and now - o.ts <= timedelta(days=days)]
    if len(recent) < 4:
        return None
    span = (recent[-1].ts - recent[0].ts).total_seconds() / 86400
    if span < 0.8 * days or recent[0].listings <= 0:
        return None
    drop = 100 * (recent[0].listings - recent[-1].listings) / recent[0].listings
    half = len(recent) // 2
    rose = median(o.price for o in recent[half:]) > median(o.price for o in recent[:half])
    return drop if drop >= drop_pct and rose else None


@dataclass(frozen=True)
class SellOrUse:
    sell_net: float
    alternative: float
    better: str  # 'sell' | 'alternative'
    difference: float


def sell_vs_alternative(sale_price: float, alternative_value: float, fees: Fees = Fees()) -> SellOrUse:
    """Selling on the Marketplace (after fees) against what an alternative pays: an Abyss
    supply-order reward, or the value of keeping a duplicate for soul engraving."""
    sell_net = fees.net(sale_price)
    better = "sell" if sell_net >= alternative_value else "alternative"
    return SellOrUse(sell_net, alternative_value, better, abs(sell_net - alternative_value))
