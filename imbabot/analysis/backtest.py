"""Straddle backtest over cached opening days.

For one day and one candidate spread we replay the open and ask: which entry
triggers, and does it follow through (target) or reverse into a stop (whipsaw)?
Sweeping a grid of spreads over ~12 months tells us which spread historically
maximized P&L while minimizing whipsaw — the number the daily model is fit to.

**Conservative intrabar assumption.** A 1-minute bar doesn't reveal whether its
high or low printed first, so we always resolve the *adverse* excursion first:
- if a single opening bar's range spans BOTH entries, we treat it as a whipsaw
  (filled one side, stopped the other);
- after an entry triggers, within each bar we check the STOP before the target.
This deliberately under-counts winners so the recommendation errs toward caution —
exactly the bias that protects against the too-narrow-spread reversal problem.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..models import round_to_tick
from .types import DayRecord


@dataclass
class BracketSpec:
    """Protective bracket applied after entry (points). Mirrors a fixed SL/TP."""

    stop_points: float
    target_points: float


@dataclass
class DayOutcome:
    spread: float
    triggered: bool
    side: Optional[str]          # "long" | "short" | None
    resolved: str                # "target" | "stop" | "window" | "none"
    whipsaw: bool
    pnl_points: float            # signed, in index points (one contract)


def simulate_day(
    day: DayRecord, spread: float, bracket: BracketSpec, tick: float = 0.25,
) -> DayOutcome:
    """Simulate the one-trade straddle for a single day at a single spread."""
    ref = day.ref_price
    long_stop = round_to_tick(ref + spread, tick)
    short_stop = round_to_tick(ref - spread, tick)
    bars = sorted(day.open_bars, key=lambda b: b.minute)
    if not bars:
        return DayOutcome(spread, False, None, "none", False, 0.0)

    for i, b in enumerate(bars):
        hit_long = b.h >= long_stop
        hit_short = b.l <= short_stop
        if not (hit_long or hit_short):
            continue

        # Both entries inside one bar's range => whipsaw (filled one, stopped other).
        if hit_long and hit_short:
            return DayOutcome(spread, True, "long", "stop", True, -bracket.stop_points)

        side = "long" if hit_long else "short"
        entry = long_stop if hit_long else short_stop
        if side == "long":
            stop_level, target_level = entry - bracket.stop_points, entry + bracket.target_points
        else:
            stop_level, target_level = entry + bracket.stop_points, entry - bracket.target_points

        # Resolve from the trigger bar onward; adverse (stop) is checked before the
        # target so wins are under-counted. On the ENTRY bar the stop only counts if
        # the bar CLOSES through it (the pre-trigger part of the range can't have hit
        # a stop that wasn't placed yet); on later bars a wick low/high counts, as a
        # resting stop order really would be filled by it.
        for j, b2 in enumerate(bars[i:]):
            entry_bar = j == 0
            if side == "long":
                stopped = (b2.c <= stop_level) if entry_bar else (b2.l <= stop_level)
                if stopped:
                    return DayOutcome(spread, True, side, "stop", True, -bracket.stop_points)
                if b2.h >= target_level:
                    return DayOutcome(spread, True, side, "target", False, bracket.target_points)
            else:
                stopped = (b2.c >= stop_level) if entry_bar else (b2.h >= stop_level)
                if stopped:
                    return DayOutcome(spread, True, side, "stop", True, -bracket.stop_points)
                if b2.l <= target_level:
                    return DayOutcome(spread, True, side, "target", False, bracket.target_points)

        # Unresolved by window end: mark to the last close.
        last_c = bars[-1].c
        pnl = (last_c - entry) if side == "long" else (entry - last_c)
        return DayOutcome(spread, True, side, "window", pnl < 0, pnl)

    return DayOutcome(spread, False, None, "none", False, 0.0)  # spread too wide


@dataclass
class SpreadStats:
    spread: float
    mean_pnl: float
    trigger_rate: float
    whipsaw_rate: float          # share of TRIGGERED days that were whipsawed
    n_days: int


@dataclass
class BacktestResult:
    grid: List[float]
    per_spread: Dict[float, SpreadStats] = field(default_factory=dict)
    per_day_optimal: Dict[str, float] = field(default_factory=dict)

    def best_spread(self) -> Optional[float]:
        """Grid spread with the highest mean P&L (tie -> wider = safer)."""
        if not self.per_spread:
            return None
        return max(self.per_spread.values(),
                   key=lambda s: (s.mean_pnl, s.spread)).spread


def spread_grid(lo: float = 6, hi: float = 30, step: float = 1) -> List[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 4) for i in range(n + 1)]


def backtest(
    records: List[DayRecord],
    *,
    bracket: BracketSpec,
    grid: Optional[List[float]] = None,
    tick: float = 0.25,
) -> BacktestResult:
    """Sweep ``grid`` across all ``records``; aggregate P&L/whipsaw per spread."""
    grid = grid or spread_grid()
    res = BacktestResult(grid=grid)

    for sp in grid:
        outcomes = [simulate_day(d, sp, bracket, tick) for d in records]
        triggered = [o for o in outcomes if o.triggered]
        res.per_spread[sp] = SpreadStats(
            spread=sp,
            mean_pnl=statistics.fmean(o.pnl_points for o in outcomes) if outcomes else 0.0,
            trigger_rate=(len(triggered) / len(outcomes)) if outcomes else 0.0,
            whipsaw_rate=(sum(o.whipsaw for o in triggered) / len(triggered)) if triggered else 0.0,
            n_days=len(outcomes),
        )

    # Per-day optimal spread (best single-day P&L; tie -> wider).
    for d in records:
        best, best_key = None, None
        for sp in grid:
            o = simulate_day(d, sp, bracket, tick)
            key = (o.pnl_points, sp)
            if best_key is None or key > best_key:
                best_key, best = key, sp
        res.per_day_optimal[d.date] = best
    return res
