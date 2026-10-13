"""Trade ledger: realized profit from Danny's own buys and sells, first in first out."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from app.analysis.fees import Fees
from app.db.repo import parse_iso


@dataclass(frozen=True)
class Realized:
    trade_id: int
    item_key: int
    done_at: datetime
    qty: int
    proceeds: float  # after fees
    cost: float  # of the matched buys
    profit: float
    unmatched_qty: int  # sold units with no recorded buy (crafted, farmed or older stock)


def sale_fee(trade: Mapping, fees: Fees) -> float:
    """The fee Danny recorded, or the fee rule applied to the sale."""
    if trade["fee_paid"] is not None:
        return trade["fee_paid"]
    gross = trade["qty"] * trade["unit_price"]
    return gross - trade["qty"] * fees.net(trade["unit_price"])


def realized_profit(trades: Iterable[Mapping], fees: Fees = Fees()) -> list[Realized]:
    """One row per completed sale. Buys are matched oldest first; a buy's fee is part of its
    cost. Listings that have not sold (done_at empty) are ignored."""
    done = sorted(
        (t for t in trades if t["done_at"]), key=lambda t: (t["done_at"], t["side"] == "sell", t["id"])
    )
    lots: dict[int, deque[list[float]]] = defaultdict(deque)  # item -> [qty left, unit cost]
    rows = []
    for trade in done:
        key = trade["item_key"]
        if trade["side"] == "buy":
            unit_cost = trade["unit_price"] + (trade["fee_paid"] or 0) / trade["qty"]
            lots[key].append([trade["qty"], unit_cost])
            continue
        need, cost = trade["qty"], 0.0
        while need and lots[key]:
            lot = lots[key][0]
            take = min(need, lot[0])
            cost += take * lot[1]
            lot[0] -= take
            need -= take
            if lot[0] == 0:
                lots[key].popleft()
        proceeds = trade["qty"] * trade["unit_price"] - sale_fee(trade, fees)
        rows.append(
            Realized(trade["id"], key, parse_iso(trade["done_at"]), trade["qty"], proceeds, cost,
                     proceeds - cost, need)
        )
    return rows


def week_label(when: datetime) -> str:
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def by_week(rows: Iterable[Realized]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[week_label(row.done_at)] += row.profit
    return dict(sorted(totals.items(), reverse=True))


def by_item(rows: Iterable[Realized]) -> dict[int, float]:
    totals: dict[int, float] = defaultdict(float)
    for row in rows:
        totals[row.item_key] += row.profit
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))
