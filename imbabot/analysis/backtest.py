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
    *, fine_grained: bool = False,
) -> DayOutcome:
    """Simulate the one-trade straddle for a single day at a single spread.

    ``fine_grained`` (1-second data): the pre-trigger part of the entry bar is
    sub-second, so the entry bar honors actual wicks like every other bar — exact
    whipsaw detection. For coarse 1-minute bars (``False``) the entry-bar stop only
    counts on a close-through (the pre-trigger minute can't have hit a not-yet-placed
    stop), which conservatively under-counts winners.
    """
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
        # target so wins are under-counted.
        for j, b2 in enumerate(bars[i:]):
            wick = fine_grained or j > 0   # entry bar uses close-proxy only when coarse
            if side == "long":
                stopped = (b2.l <= stop_level) if wick else (b2.c <= stop_level)
                if stopped:
                    return DayOutcome(spread, True, side, "stop", True, -bracket.stop_points)
                if b2.h >= target_level:
                    return DayOutcome(spread, True, side, "target", False, bracket.target_points)
            else:
                stopped = (b2.h >= stop_level) if wick else (b2.c >= stop_level)
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
    fine_grained: bool = False,
) -> BacktestResult:
    """Sweep ``grid`` across all ``records``; aggregate P&L/whipsaw per spread."""
    grid = grid or spread_grid()
    res = BacktestResult(grid=grid)

    for sp in grid:
        outcomes = [simulate_day(d, sp, bracket, tick, fine_grained=fine_grained) for d in records]
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
            o = simulate_day(d, sp, bracket, tick, fine_grained=fine_grained)
            key = (o.pnl_points, sp)
            if best_key is None or key > best_key:
                best_key, best = key, sp
        res.per_day_optimal[d.date] = best
    return res


# ----------------------------------------------- 2-D sweep (spread x stop)
@dataclass
class CellStats:
    spread: float
    stop: float
    mean_pnl: float
    trigger_rate: float
    whipsaw_rate: float
    n_days: int


@dataclass
class Backtest2D:
    spreads: List[float]
    stops: List[float]
    target_points: float
    cells: Dict[tuple, CellStats] = field(default_factory=dict)        # (spread,stop)->stats
    per_day_optimal: Dict[str, tuple] = field(default_factory=dict)    # date->(spread,stop)

    def best_cell(self) -> Optional[tuple]:
        """(spread, stop) with the highest mean P&L (tie -> wider spread, wider stop)."""
        if not self.cells:
            return None
        c = max(self.cells.values(), key=lambda s: (s.mean_pnl, s.spread, s.stop))
        return (c.spread, c.stop)

    def whipsaw_at(self, spread: float, stop: float) -> float:
        if not self.cells:
            return 0.0
        key = min(self.cells, key=lambda k: (abs(k[0] - spread), abs(k[1] - stop)))
        return self.cells[key].whipsaw_rate


def stop_grid(lo: float = 4, hi: float = 20, step: float = 1) -> List[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 4) for i in range(n + 1)]


def backtest_2d(
    records: List[DayRecord],
    *,
    target_points: float,
    spreads: Optional[List[float]] = None,
    stops: Optional[List[float]] = None,
    tick: float = 0.25,
    fine_grained: bool = False,
) -> Backtest2D:
    """Sweep entry spread x stop distance (TP held at ``target_points``)."""
    spreads = spreads or spread_grid()
    stops = stops or stop_grid()
    res = Backtest2D(spreads=spreads, stops=stops, target_points=target_points)

    # Cache per (spread,stop) outcomes so per-day optimal reuses them.
    grid_outcomes: Dict[tuple, List[DayOutcome]] = {}
    for sp in spreads:
        for st in stops:
            br = BracketSpec(stop_points=st, target_points=target_points)
            outcomes = [simulate_day(d, sp, br, tick, fine_grained=fine_grained) for d in records]
            grid_outcomes[(sp, st)] = outcomes
            trig = [o for o in outcomes if o.triggered]
            res.cells[(sp, st)] = CellStats(
                spread=sp, stop=st,
                mean_pnl=statistics.fmean(o.pnl_points for o in outcomes) if outcomes else 0.0,
                trigger_rate=(len(trig) / len(outcomes)) if outcomes else 0.0,
                whipsaw_rate=(sum(o.whipsaw for o in trig) / len(trig)) if trig else 0.0,
                n_days=len(outcomes),
            )

    for idx, d in enumerate(records):
        best, best_key = None, None
        for sp in spreads:
            for st in stops:
                o = grid_outcomes[(sp, st)][idx]
                key = (o.pnl_points, sp, st)   # tie -> wider spread then wider stop
                if best_key is None or key > best_key:
                    best_key, best = key, (sp, st)
        res.per_day_optimal[d.date] = best
    return res
