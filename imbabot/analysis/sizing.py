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
