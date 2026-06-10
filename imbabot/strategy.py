"""Opening-range breakout straddle.

At the fire moment we capture a reference price and build two stop-entry orders:
a BUY stop ``points`` above and a SELL stop ``points`` below. Whichever way the
market breaks at the open, one side triggers. Each leg carries its own protective
stop-loss and take-profit (attached as ProjectX brackets, so they activate the
instant the entry fills).

This module is pure: it only computes the *plan*. Placing/cancelling/monitoring
lives in engine.py. Keeping it pure means we can unit-test the math offline.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import (
    Contract,
    OrderSide,
    StraddleLeg,
    StraddlePlan,
    points_to_ticks,
    round_to_tick,
)


@dataclass
class StrategyParams:
    """Per-platform strategy configuration (mirrors the guide's settings)."""

    entry_points: float = 12.0     # distance above/below reference for entries
    stop_loss_points: float = 12.0  # protective stop distance from fill
    take_profit_points: float = 12.0  # target distance from fill
    contracts: int = 2

    def validate(self) -> None:
        if self.entry_points <= 0:
            raise ValueError("entry_points must be > 0")
        if self.stop_loss_points <= 0:
            raise ValueError("stop_loss_points must be > 0")
        if self.take_profit_points <= 0:
            raise ValueError("take_profit_points must be > 0")
        if self.contracts < 1:
            raise ValueError("contracts must be >= 1")


def build_straddle(
    contract: Contract,
    reference_price: float,
    params: StrategyParams,
    tag_prefix: str,
) -> StraddlePlan:
    """Construct the two-leg straddle plan around ``reference_price``.

    All prices are snapped to the contract's tick grid; all bracket distances are
    expressed in whole ticks (what the ProjectX bracket fields expect).
    """
    params.validate()
    tick = contract.tick_size

    long_stop = round_to_tick(reference_price + params.entry_points, tick)
    short_stop = round_to_tick(reference_price - params.entry_points, tick)

    sl_ticks = points_to_ticks(params.stop_loss_points, tick)
    tp_ticks = points_to_ticks(params.take_profit_points, tick)

    long_leg = StraddleLeg(
        side=OrderSide.BUY,
        stop_price=long_stop,
        size=params.contracts,
        stop_loss_ticks=sl_ticks,
        take_profit_ticks=tp_ticks,
        custom_tag=f"{tag_prefix}-L",
    )
    short_leg = StraddleLeg(
        side=OrderSide.SELL,
        stop_price=short_stop,
        size=params.contracts,
        stop_loss_ticks=sl_ticks,
        take_profit_ticks=tp_ticks,
        custom_tag=f"{tag_prefix}-S",
    )
    return StraddlePlan(
        contract=contract,
        reference_price=reference_price,
        long_leg=long_leg,
        short_leg=short_leg,
    )


def describe_plan(plan: StraddlePlan) -> str:
    """Human-readable summary for logs and the confirm/arm step."""
    c = plan.contract
    L, S = plan.long_leg, plan.short_leg
    return (
        f"Straddle on {c.name} ({c.id})\n"
        f"  reference price : {plan.reference_price:,.2f}\n"
        f"  LONG  : BUY  STOP {L.size} @ {L.stop_price:,.2f}  "
        f"(SL {L.stop_loss_ticks}t / TP {L.take_profit_ticks}t)\n"
        f"  SHORT : SELL STOP {S.size} @ {S.stop_price:,.2f}  "
        f"(SL {S.stop_loss_ticks}t / TP {S.take_profit_ticks}t)"
    )
