"""Profit-target position sizing — honest calculator.

Given a dollar target and the day's recommended spread/stop, compute how many
contracts it takes to net the target *on a winning morning*, and surface the
**symmetric downside** and the **dollar brackets to set in TopStep** so the
point-stop holds at that size.

Crucial honesty: **contracts scale the outcome, not the odds.** More size doubles a
win AND a loss; it does not raise the probability of a winning morning. This module
never implies otherwise — it shows size, the win, the loss, and the expected value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def point_value(tick_value: float, tick_size: float) -> float:
    """Dollars per index point per contract (NQ: $5/0.25 = $20; MNQ: $0.50/0.25 = $2)."""
    return tick_value / tick_size if tick_size else 0.0


@dataclass
class SizingPlan:
    target_dollars: float
    contracts: int
    capped: bool                  # target needs more than max_contracts
    gross_win_dollars: float      # net on a winning (TP) morning at this size
    downside_dollars: float       # loss on a stopped morning at this size
    tp_bracket_dollars: float     # $ take-profit to set in TopStep for this size
    sl_bracket_dollars: float     # $ stop-loss to set in TopStep for this size
    expected_value_dollars: float # winrate*win - (1-winrate)*loss (rough)
    winrate: float
    note: str


def size_for_target(
    target_dollars: float,
    *,
    tp_points: float,
    stop_points: float,
    dollars_per_point: float,
    winrate: float,
    max_contracts: int = 10,
) -> SizingPlan:
    """Contracts needed to reach ``target_dollars`` on a winning morning, with the
    downside and the $ brackets to set. Size scales outcome, not probability."""
    per_contract_win = tp_points * dollars_per_point
    raw = (target_dollars / per_contract_win) if per_contract_win > 0 else float(max_contracts)
    want = max(1, math.ceil(raw))
    capped = want > max_contracts
    contracts = min(want, max_contracts)

    gross = contracts * tp_points * dollars_per_point
    downside = contracts * stop_points * dollars_per_point
    ev = winrate * gross - (1.0 - winrate) * downside

    if capped:
        note = (f"Target ${target_dollars:,.0f} needs ~{want} contracts; capped at "
                f"{max_contracts}. A winning morning nets ${gross:,.0f} (short of target). "
                f"Size scales the outcome, not the odds.")
    else:
        note = (f"{contracts} contract(s): a winning morning nets ~${gross:,.0f}, "
                f"a stopped morning loses ~${downside:,.0f}. Size scales the outcome, "
                f"not the probability of winning.")

    return SizingPlan(
        target_dollars=target_dollars, contracts=contracts, capped=capped,
        gross_win_dollars=gross, downside_dollars=downside,
        tp_bracket_dollars=gross, sl_bracket_dollars=downside,
        expected_value_dollars=ev, winrate=winrate, note=note,
    )


@dataclass
class SpikePlan:
    """TP-driven plan from a PREDICTED opening spike: entry spread + contracts to hit $TP."""
    feasible: bool
    predicted_spike: float       # points
    entry_spread: float          # ±X points
    tp_distance_points: float    # T = room in the spike for the take-profit
    contracts: int
    capped: bool
    target_dollars: float
    achievable_dollars: float    # $ at the (possibly capped) contracts
    recommended_tp_dollars: float  # realistic max $TP at the contract cap on this predicted spike
    contracts_wanted: int        # contracts the entered target WOULD need (pre-cap) -> drives the alert
    max_contracts: int           # the cap in force (e.g. 5 on TopStep)
    tp_bracket_dollars: float
    sl_points: float
    sl_bracket_dollars: float
    note: str


def tp_plan_from_spike(
    predicted_spike: float,
    target_dollars: float,
    *,
    dollars_per_point: float = 20.0,
    max_contracts: int = 5,
    counter_poke: float = 4.0,
    slip_margin: float = 3.0,
    min_spread: float = 10.0,
    sl_points: float = 8.0,
    min_tp: float = 3.0,
    max_tp: float = 15.0,
) -> SpikePlan:
    """Recommend entry spread (±X) and # contracts to hit a $TP, scaled to the predicted spike.

    Entry ±X >= ``min_spread`` (10 by default — a tighter entry can trigger pre-open when the
    brackets are placed at 8:29:59) and above the typical counter-poke, but inside the spike.
    The reachable TP distance T = the room the spike leaves past entry, clamped to [min_tp, max_tp]:
    a BIG spike -> standard T, few contracts; a SMALL spike -> tighter T, MORE contracts so the $TP
    is still reachable; too small to clear entry + min_tp -> NO-TRADE.
    """
    X = max(min_spread, counter_poke + 1.0)
    room = predicted_spike - X - slip_margin           # points the move extends past entry
    if room < min_tp:
        return SpikePlan(
            feasible=False, predicted_spike=predicted_spike, entry_spread=round(X, 1),
            tp_distance_points=0.0, contracts=0, capped=False, target_dollars=target_dollars,
            achievable_dollars=0.0, recommended_tp_dollars=0.0, contracts_wanted=0,
            max_contracts=max_contracts, tp_bracket_dollars=0.0, sl_points=sl_points,
            sl_bracket_dollars=0.0,
            note=("Predicted spike too small to clear a >=10pt entry + take-profit after slippage "
                  "-> NO-TRADE (low conviction)."))
    T = max(min_tp, min(max_tp, room))
    want = max(1, math.ceil(target_dollars / (T * dollars_per_point)))
    capped = want > max_contracts
    contracts = min(want, max_contracts)
    achievable = contracts * T * dollars_per_point
    sl_dollars = contracts * sl_points * dollars_per_point
    # Realistic max $TP at the contract cap on this predicted spike (TP reachable inside the
    # first-second thrust) — what to actually aim for on a >=5-cap prop account.
    recommended = max_contracts * T * dollars_per_point
    if capped:
        note = (f"To hit ${target_dollars:,.0f} needs ~{want} contracts; capped at {max_contracts}. "
                f"At {contracts} a clean open nets ~${achievable:,.0f} (TP {T:.0f}pt).")
    else:
        note = (f"{contracts} contract(s), entry +/-{X:.0f}, TP {T:.0f}pt: a clean open hits "
                f"~${achievable:,.0f}; a stop loses ~${sl_dollars:,.0f}. Size scales outcome, not odds.")
    return SpikePlan(
        feasible=True, predicted_spike=predicted_spike, entry_spread=round(X, 1),
        tp_distance_points=round(T, 1), contracts=contracts, capped=capped,
        target_dollars=target_dollars, achievable_dollars=round(achievable, 0),
        recommended_tp_dollars=round(recommended, 0), contracts_wanted=want,
        max_contracts=max_contracts, tp_bracket_dollars=round(achievable, 0), sl_points=sl_points,
        sl_bracket_dollars=round(sl_dollars, 0), note=note)
