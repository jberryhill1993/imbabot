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
class CostSpec:
    """Real-world frictions, in index points per contract.

    ``slippage_points`` is the adverse fill slip on a STOP fill — charged on the entry
    (a stop entry fills past its trigger) and again on a stop-loss exit (stop-market).
    A take-profit is a LIMIT and does not slip. ``commission_points`` is the round-trip
    commission per contract expressed in points (commission_$ / $-per-point).
    """

    slippage_points: float = 0.0
    commission_points: float = 0.0


def _net(gross: float, *, stopped: bool, costs: "CostSpec", entry_slip: float) -> float:
    """Net a gross point result for entry slip + commission (+ stop-exit slip).

    ``entry_slip`` is the slippage charged on the ENTRY fill — full slippage for a
    stop entry, 0 for a stop-LIMIT entry (you got your price). A stop-loss exit always
    slips (it's a stop-market); the take-profit is a limit and never slips.
    """
    pnl = gross - entry_slip - costs.commission_points
    if stopped:
        pnl -= costs.slippage_points
    return pnl


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
    *, fine_grained: bool = False, costs: Optional[CostSpec] = None,
    entry_mode: str = "stop", limit_tolerance: float = 1.0,
    entry_window_seconds: Optional[int] = None,
) -> DayOutcome:
    """Simulate the one-trade straddle for a single day at a single spread, net of costs.

    ``fine_grained`` (1-second data): the entry bar honors actual wicks. ``costs``
    charges slippage + commission so the P&L is net.

    ``entry_mode``: "stop" (market stop entry — always fills if touched, pays entry
    slippage) or "stop_limit" (fills at the trigger price with NO slippage, but MISSES
    the day if price blows more than ``limit_tolerance`` points past the trigger on the
    crossing bar — modeling the adverse selection of limit orders: you skip the violent
    breakouts and only catch the calm ones). The protective stop-loss is a stop-market
    in both modes, so it still slips.

    ``entry_window_seconds``: if set, the entry may only TRIGGER within this many
    seconds of the 09:30:00 open (``OpenBar.minute`` holds the seconds offset for
    1-second data). This models the "opening-spike" strategy — only the first
    ``entry_window_seconds`` candle(s) can put you in; if the spike doesn't reach
    ±spread by then, there is no trade that day. A triggered position still resolves
    to its TP/SL over the FULL cached window (the live bot holds to its bracket), so
    analysis and live execution stay aligned. ``None`` = entry may trigger anytime in
    the window (the original behavior).
    """
    costs = costs or CostSpec()
    is_limit = entry_mode == "stop_limit"
    entry_slip = 0.0 if is_limit else costs.slippage_points
    ref = day.ref_price
    long_stop = round_to_tick(ref + spread, tick)
    short_stop = round_to_tick(ref - spread, tick)
    bars = sorted(day.open_bars, key=lambda b: b.minute)
    if not bars:
        return DayOutcome(spread, False, None, "none", False, 0.0)

    for i, b in enumerate(bars):
        # Entry may only trigger inside the opening window (e.g. the first 1s candle).
        # Past it the spike is over -> no trade. (Resolution below still runs full window.)
        if entry_window_seconds is not None and b.minute >= entry_window_seconds:
            break
        hit_long = b.h >= long_stop
        hit_short = b.l <= short_stop
        if not (hit_long or hit_short):
            continue

        # Both entries inside one bar's range => whipsaw. A limit order can't be caught
        # by a bar that violent, so it misses; a stop fills one side and is stopped.
        if hit_long and hit_short:
            if is_limit:
                return DayOutcome(spread, False, None, "miss", False, 0.0)
            return DayOutcome(spread, True, "long", "stop", True,
                              _net(-bracket.stop_points, stopped=True, costs=costs, entry_slip=entry_slip))

        side = "long" if hit_long else "short"
        entry = long_stop if hit_long else short_stop
        # Stop-limit: if the crossing bar shot more than the tolerance past the trigger,
        # the resting limit was skipped -> no fill that day (adverse selection).
        if is_limit:
            excursion = (b.h - long_stop) if side == "long" else (short_stop - b.l)
            if excursion > limit_tolerance:
                return DayOutcome(spread, False, None, "miss", False, 0.0)
        if side == "long":
            stop_level, target_level = entry - bracket.stop_points, entry + bracket.target_points
        else:
            stop_level, target_level = entry + bracket.stop_points, entry - bracket.target_points

        # Resolve from the trigger bar onward; adverse (stop) is checked before target.
        for j, b2 in enumerate(bars[i:]):
            # Entry bar (j==0): the pre-trigger excursion is ambiguous, so the stop
            # only counts on a close-through. Later bars: a resting stop is hit by the
            # wick. (``fine_grained`` is retained as an advisory "1-second data" flag.)
            wick = j > 0
            if side == "long":
                stopped = (b2.l <= stop_level) if wick else (b2.c <= stop_level)
                if stopped:
                    return DayOutcome(spread, True, side, "stop", True,
                                      _net(-bracket.stop_points, stopped=True, costs=costs, entry_slip=entry_slip))
                if b2.h >= target_level:
                    return DayOutcome(spread, True, side, "target", False,
                                      _net(bracket.target_points, stopped=False, costs=costs, entry_slip=entry_slip))
            else:
                stopped = (b2.h >= stop_level) if wick else (b2.c >= stop_level)
                if stopped:
                    return DayOutcome(spread, True, side, "stop", True,
                                      _net(-bracket.stop_points, stopped=True, costs=costs, entry_slip=entry_slip))
                if b2.l <= target_level:
                    return DayOutcome(spread, True, side, "target", False,
                                      _net(bracket.target_points, stopped=False, costs=costs, entry_slip=entry_slip))

        # Unresolved by window end: mark to the last close (flatten at market).
        last_c = bars[-1].c
        gross = (last_c - entry) if side == "long" else (entry - last_c)
        pnl = _net(gross, stopped=False, costs=costs, entry_slip=entry_slip)
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
    costs: Optional[CostSpec] = None,
    entry_window_seconds: Optional[int] = None,
) -> BacktestResult:
    """Sweep ``grid`` across all ``records``; aggregate P&L/whipsaw per spread."""
    grid = grid or spread_grid()
    res = BacktestResult(grid=grid)

    for sp in grid:
        outcomes = [simulate_day(d, sp, bracket, tick, fine_grained=fine_grained, costs=costs,
                                 entry_window_seconds=entry_window_seconds)
                    for d in records]
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
            o = simulate_day(d, sp, bracket, tick, fine_grained=fine_grained, costs=costs,
                             entry_window_seconds=entry_window_seconds)
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
    per_day_best: Dict[str, DayOutcome] = field(default_factory=dict)  # date->outcome at optimal
    cells_order: List[tuple] = field(default_factory=list)             # stable cell ordering
    per_day_cell_pnl: Dict[str, List[float]] = field(default_factory=dict)   # date->P&L per cell
    per_day_cell_whip: Dict[str, List[int]] = field(default_factory=dict)    # date->whipsaw per cell

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
    costs: Optional[CostSpec] = None,
    entry_window_seconds: Optional[int] = None,
) -> Backtest2D:
    """Sweep entry spread x stop distance (TP held at ``target_points``), net of costs."""
    spreads = spreads or spread_grid()
    stops = stops or stop_grid()
    res = Backtest2D(spreads=spreads, stops=stops, target_points=target_points)

    # Cache per (spread,stop) outcomes so per-day optimal reuses them.
    grid_outcomes: Dict[tuple, List[DayOutcome]] = {}
    for sp in spreads:
        for st in stops:
            br = BracketSpec(stop_points=st, target_points=target_points)
            outcomes = [simulate_day(d, sp, br, tick, fine_grained=fine_grained, costs=costs,
                                     entry_window_seconds=entry_window_seconds)
                        for d in records]
            grid_outcomes[(sp, st)] = outcomes
            trig = [o for o in outcomes if o.triggered]
            res.cells[(sp, st)] = CellStats(
                spread=sp, stop=st,
                mean_pnl=statistics.fmean(o.pnl_points for o in outcomes) if outcomes else 0.0,
                trigger_rate=(len(trig) / len(outcomes)) if outcomes else 0.0,
                whipsaw_rate=(sum(o.whipsaw for o in trig) / len(trig)) if trig else 0.0,
                n_days=len(outcomes),
            )

    res.cells_order = [(sp, st) for sp in spreads for st in stops]
    for idx, d in enumerate(records):
        best, best_key, best_outcome = None, None, None
        for sp in spreads:
            for st in stops:
                o = grid_outcomes[(sp, st)][idx]
                key = (o.pnl_points, sp, st)   # tie -> wider spread then wider stop
                if best_key is None or key > best_key:
                    best_key, best, best_outcome = key, (sp, st), o
        res.per_day_optimal[d.date] = best
        res.per_day_best[d.date] = best_outcome
        res.per_day_cell_pnl[d.date] = [grid_outcomes[c][idx].pnl_points for c in res.cells_order]
        res.per_day_cell_whip[d.date] = [int(grid_outcomes[c][idx].whipsaw) for c in res.cells_order]
    return res
