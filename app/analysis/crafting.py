"""Crafting expected value, break-even, losing streaks, multi-step chains, outcome checks.

Failure loses all materials; COMBO gives more items or a higher grade.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.analysis.fees import Fees

RateSource = Literal["manual", "api", "unknown"]


@dataclass(frozen=True)
class Rates:
    success_rate: float | None
    combo_probability: float | None
    source: RateSource


def resolve_rates(
    manual: tuple[float | None, float | None] | None,
    api: tuple[float | None, float | None] | None,
) -> Rates:
    """Danny's own entry (matches his mastery) first, then the API's model, else unknown."""
    for source, pair in (("manual", manual), ("api", api)):
        if pair and pair[0] is not None:
            return Rates(pair[0], pair[1] or 0.0, source)
    return Rates(None, None, "unknown")


# -- single craft ---------------------------------------------------------------


def success_value(p_c: float, v_normal: float, v_combo: float, fees: Fees = Fees()) -> float:
    """Net value of one successful attempt, averaged over normal and COMBO results."""
    return (1 - p_c) * fees.net(v_normal) + p_c * fees.net(v_combo)


def craft_ev(
    p_s: float,
    p_c: float,
    v_normal: float,
    v_combo: float,
    materials: float,
    fee: float = 0.0,
    fees: Fees = Fees(),
) -> float:
    """EV = p_s x [(1 - p_c) x net(V_n) + p_c x net(V_c)] - C_m - C_f, per attempt."""
    return p_s * success_value(p_c, v_normal, v_combo, fees) - materials - fee


def break_even_rate(
    p_c: float, v_normal: float, v_combo: float, materials: float, fee: float = 0.0,
    fees: Fees = Fees(),
) -> float | None:
    """The success rate at which the craft earns nothing; compare with p_s for the margin."""
    value = success_value(p_c, v_normal, v_combo, fees)
    return (materials + fee) / value if value > 0 else None


def streak_risk(p: float, attempts: int) -> float:
    """Chance of zero successes in N attempts = (1 - p)^N."""
    return (1 - p) ** attempts


def streak_attempts(p: float, risk: float = 0.05) -> int | None:
    """Attempts needed so that the chance of not one success is at most `risk`."""
    if p <= 0:
        return None
    if p >= 1:
        return 1
    return max(1, math.ceil(math.log(risk) / math.log(1 - p) - 1e-9))


def streak_budget(p: float, cost_per_attempt: float, risk: float = 0.05) -> float | None:
    """Material budget to survive a 1-in-20 bad streak."""
    attempts = streak_attempts(p, risk)
    return None if attempts is None else attempts * cost_per_attempt


# -- multi-step chains ------------------------------------------------------------


@dataclass(frozen=True)
class ChainStep:
    success_rate: float
    combo_probability: float
    cost_per_attempt: float  # bought materials and fee; not the previous step's result
    # In the API's model the next tier only comes out of a COMBO; a plain success gives
    # the lower grade, valued here as byproduct_value (0 = ignored, as the API does).
    target_on: Literal["combo", "success"] = "combo"
    byproduct_value: float = 0.0
    inputs_per_attempt: float = 1.0  # units of the previous step's result consumed

    @property
    def target_rate(self) -> float:
        """Chance that one attempt yields the item the next step needs."""
        if self.target_on == "combo":
            return self.success_rate * self.combo_probability
        return self.success_rate


@dataclass(frozen=True)
class ChainResult:
    attempts: tuple[float, ...]  # expected attempts per step for ONE finished item
    step_costs: tuple[float, ...]
    cost: float  # expected total per finished item, byproducts credited
    final_rate: float


def chain_cost(steps: Sequence[ChainStep], fees: Fees = Fees()) -> ChainResult:
    """Expected attempts and cost per finished item. Works backwards from one final item:
    a step that fails, or does not COMBO, costs its expected re-tries of every step before."""
    attempts: list[float] = []
    need = 1.0
    for step in reversed(steps):
        tries = need / step.target_rate
        attempts.append(tries)
        need = tries * step.inputs_per_attempt
    attempts.reverse()

    costs = []
    for step, tries in zip(steps, attempts):
        cost = tries * step.cost_per_attempt
        if step.target_on == "combo" and step.byproduct_value:
            plain = tries * step.success_rate * (1 - step.combo_probability)
            cost -= plain * fees.net(step.byproduct_value)
        costs.append(cost)
    return ChainResult(tuple(attempts), tuple(costs), sum(costs), steps[-1].target_rate)


@dataclass(frozen=True)
class CraftProfit:
    cost: float  # expected cost per finished item
    sale_price: float  # estimated sale price, before fees
    profit: float  # net(sale_price) - cost
    margin_pct: float
    break_even_price: float  # sale price at which profit is zero
    streak_attempts: int | None  # last-step attempts to survive a 1-in-20 streak
    streak_budget: float | None


def craft_profit(
    cost: float,
    sale_price: float,
    fees: Fees = Fees(),
    final_rate: float | None = None,
    final_cost_per_attempt: float | None = None,
    risk: float = 0.05,
) -> CraftProfit:
    profit = fees.net(sale_price) - cost
    # net() is linear in price for every fee rule except 'flat'.
    unit_net = fees.net(1_000_000) / 1_000_000
    tries = streak_attempts(final_rate, risk) if final_rate else None
    return CraftProfit(
        cost,
        sale_price,
        profit,
        100 * profit / cost if cost else 0.0,
        cost / unit_net if unit_net > 0 else math.inf,
        tries,
        tries * final_cost_per_attempt if tries and final_cost_per_attempt else None,
    )


# -- outcome log ------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeCheck:
    observed_rate: float | None
    expected_rate: float
    p_value: float | None  # chance of doing this badly or worse if the expected rate is right
    below_expected: bool


def outcome_check(
    attempts: int, successes: int, expected_rate: float, alpha: float = 0.05
) -> OutcomeCheck:
    """Compares Danny's logged results with the expected rate (exact binomial lower tail)."""
    if attempts <= 0:
        return OutcomeCheck(None, expected_rate, None, False)
    p_value = sum(
        math.comb(attempts, k) * expected_rate**k * (1 - expected_rate) ** (attempts - k)
        for k in range(successes + 1)
    )
    return OutcomeCheck(successes / attempts, expected_rate, p_value, p_value < alpha)
