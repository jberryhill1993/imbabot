"""Tick-accurate straddle simulator.

Replays the opening-range straddle against the real tbbo tick path so TP/SL resolve in
**true time order** (the thing 1-second OHLCV can't do) and fills cross the real spread:
a buy-stop / long-TP-sell fills at the **ask**/**bid** actually quoted, a sell-stop /
short-cover at the **bid**/**ask** — so slippage is *measured*, not assumed.

Mechanics (mirrors how TopStep position brackets behave):
- Reference captured at ``capture_offset`` seconds (default −3s, as the live bot does).
- Stops: buy ref+X, sell ref−X. Trigger on the trade price; the OCO cancels the other.
- Bracket from the actual fill: TP is a LIMIT (no worse than its price), SL a stop-MARKET
  (fills at the crossed quote, can slip). First level reached in tick time wins; if one
  tick reaches both, the adverse (stop) is taken — conservative.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .tick_data import TickDay


@dataclass
class TickOutcome:
    date: str
    triggered: bool
    side: Optional[str]        # "long" | "short" | None
    entry: Optional[float]
    exit: Optional[float]
    resolved: str              # "target" | "stop" | "window" | "none"
    pnl_points: float          # signed, one contract
    seconds: float             # entry -> exit, in seconds
    ref: float


def simulate_tick_straddle(
    td: TickDay, *, entry_points: float, tp_points: float, sl_points: float,
    capture_offset: float = -3.0, entry_from: float = 0.0,
) -> TickOutcome:
    """Simulate the one-trade straddle for one day on its ticks. Points are per contract."""
    ref = td.price_at(capture_offset)
    if ref is None:
        return TickOutcome(td.date, False, None, None, None, "none", 0.0, 0.0, 0.0)
    long_stop = ref + entry_points
    short_stop = ref - entry_points

    # --- entry: first stop touched at/after the open. Fill by crossing the spread, but
    # never better than the trade price that triggered it (guards against crossed/stale
    # quotes in the feed): short sells at min(bid, price), long buys at max(ask, price). ---
    side = entry = etime = None
    ei = 0
    for i, tk in enumerate(td.ticks):
        if tk.t < entry_from:
            continue
        if tk.price >= long_stop:
            side, entry, etime, ei = "long", max(tk.ask, tk.price), tk.t, i
            break
        if tk.price <= short_stop:
            side, entry, etime, ei = "short", min(tk.bid, tk.price), tk.t, i
            break
    if side is None:
        return TickOutcome(td.date, False, None, None, None, "none", 0.0, 0.0, ref)

    if side == "long":
        tp_level, sl_level = entry + tp_points, entry - sl_points
    else:
        tp_level, sl_level = entry - tp_points, entry + sl_points

    # --- resolve from the tick AFTER entry, in true tick order. The stop-MARKET fills at
    # the crossed quote (can slip past the level); the TP is a LIMIT that fills exactly at
    # its price. The adverse (stop) is checked first if a single tick reaches both. ---
    for tk in td.ticks[ei + 1:]:
        if side == "long":
            if tk.price <= sl_level:                 # sell stop-market at the bid
                px = min(tk.bid, tk.price)
                return _out(td, side, entry, px, "stop", (px - entry), tk.t - etime, ref)
            if tk.price >= tp_level:                 # sell limit at tp_level
                return _out(td, side, entry, tp_level, "target", tp_points, tk.t - etime, ref)
        else:
            if tk.price >= sl_level:                 # cover stop-market at the ask
                px = max(tk.ask, tk.price)
                return _out(td, side, entry, px, "stop", (entry - px), tk.t - etime, ref)
            if tk.price <= tp_level:                 # cover limit at tp_level
                return _out(td, side, entry, tp_level, "target", tp_points, tk.t - etime, ref)

    # unresolved by the window end -> flatten at the last trade
    last = td.ticks[-1]
    px = last.price
    pnl = (px - entry) if side == "long" else (entry - px)
    return _out(td, side, entry, px, "window", pnl, last.t - etime, ref)


def _out(td, side, entry, exit_px, resolved, pnl, seconds, ref) -> TickOutcome:
    return TickOutcome(td.date, True, side, entry, exit_px, resolved,
                       round(pnl, 4), round(seconds, 4), ref)
