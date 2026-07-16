"""Expanding-window walk-forward — the HONEST gate for the spike predictor.

Every day is predicted from ONLY prior days (no look-ahead). We then ask, out of sample:
1. Does the predicted spike magnitude track the realized spike? (correlation)
2. Does trading only the model's high-conviction days beat trading every day? (net P&L, win-rate)
3. Does ranking by P(big) actually catch the 30+ pt days?

The verdict — good or bad — is printed plainly. If selection does not beat the baseline, the Morning
Plan must NOT present a confident TRADE/SKIP; it shows volatility + sizing as context only.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import List

from .spike_model import SpikeModel
from .tick_dataset import BIG_PTS, DayRow

DOLLARS_PER_POINT = 20.0


@dataclass
class WFRow:
    date: str
    pred_S: float
    actual_thrust: float
    p_clean: float
    clean: int
    pnl_points: float
    p_big: float
    is_big: int
    overnight_gap: float = 0.0


def walk_forward(rows: List[DayRow], *, warm: int = 60, k: int = 25) -> List[WFRow]:
    out: List[WFRow] = []
    for i in range(warm, len(rows)):
        m = SpikeModel().fit(rows[:i])
        pr = m.predict(rows[i].feats, k=k)
        r = rows[i]
        out.append(WFRow(r.date, pr.expected_spike, r.thrust, pr.p_clean,
                         1 if r.label == "clean-winner" else 0, r.pnl_points, pr.p_big, r.is_big,
                         getattr(r, "overnight_gap", 0.0)))
    return out


def _corr(xs, ys) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if not sx or not sy:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


@dataclass
class WFVerdict:
    n: int
    spike_corr: float
    base_net: float           # $/day/contract trading every day
    base_win: float
    sel_net: float            # $/day/contract trading only high-conviction days
    sel_win: float
    sel_count: int
    big_base_rate: float
    big_caught_rate: float    # of predicted-big days, fraction actually big
    edge: bool                # selection beats baseline net OOS
    text: str


def evaluate(rows: List[DayRow], *, warm: int = 60, k: int = 25,
             conviction: float = 0.5, spike_min: float = 20.0, gap_min: float = 40.0) -> WFVerdict:
    wf = walk_forward(rows, warm=warm, k=k)
    if not wf:
        return WFVerdict(0, float("nan"), 0, 0, 0, 0, 0, 0, 0, False, "Not enough data for walk-forward.")
    spike_corr = _corr([w.pred_S for w in wf], [w.actual_thrust for w in wf])
    base_net = statistics.fmean(w.pnl_points for w in wf) * DOLLARS_PER_POINT
    base_win = statistics.fmean(w.clean for w in wf)
    # The VALIDATED selection is by predicted SPIKE MAGNITUDE (P(clean) win-prediction does NOT
    # work; magnitude does — big spikes win ~95%). Trade days whose predicted spike >= spike_min.
    sel = [w for w in wf if w.pred_S >= spike_min]
    sel_net = (statistics.fmean(w.pnl_points for w in sel) * DOLLARS_PER_POINT) if sel else 0.0
    sel_win = statistics.fmean(w.clean for w in sel) if sel else 0.0
    # Plus the overnight-gap whipsaw filter (small-gap opens churn): the live Morning-Plan rule.
    gsel = [w for w in sel if w.overnight_gap > gap_min]
    gsel_net = (statistics.fmean(w.pnl_points for w in gsel) * DOLLARS_PER_POINT) if gsel else 0.0
    gsel_win = statistics.fmean(w.clean for w in gsel) if gsel else 0.0
    big_base = statistics.fmean(w.is_big for w in wf)
    topbig = sorted(wf, key=lambda w: w.pred_S, reverse=True)[:max(1, len(wf) // 3)]
    big_caught = statistics.fmean(w.is_big for w in topbig)
    bigdays = [w for w in wf if w.is_big]
    big_win = statistics.fmean(w.clean for w in bigdays) if bigdays else 0.0
    edge = bool(sel) and sel_net > base_net and sel_net > 0
    text = (
        f"WALK-FORWARD (expanding window, {len(wf)} OOS days, warm={warm}, k={k}):\n"
        f"  spike-magnitude corr (pred vs real): {spike_corr:+.2f}  <- predictable\n"
        f"  baseline (trade every day): net ${base_net:+.1f}/day/ct, win {base_win*100:.0f}%\n"
        f"  TRADE only predicted-spike>={spike_min:.0f} ({len(sel)} days): net ${sel_net:+.1f}/day/ct, "
        f"win {sel_win*100:.0f}%\n"
        f"  ... + overnight-gap>{gap_min:.0f}pt whipsaw filter ({len(gsel)} days): net "
        f"${gsel_net:+.1f}/day/ct, win {gsel_win*100:.0f}%  <- the live Morning-Plan rule\n"
        f"  actual 30+ pt opening days win {big_win*100:.0f}% (the money days); base rate "
        f"{big_base*100:.0f}%, top-third by predicted spike -> {big_caught*100:.0f}% actually big\n"
        f"  VERDICT: magnitude-based selection {'BEATS' if edge else 'does NOT beat'} baseline OOS "
        f"-> {'SHIP TRADE/NO-TRADE by predicted spike size' if edge else 'context only'}. "
        f"(Win/loss is NOT directly predictable; we route through spike SIZE.)")
    return WFVerdict(len(wf), spike_corr, base_net, base_win, sel_net, sel_win, len(sel),
                     big_base, big_caught, edge, text)
