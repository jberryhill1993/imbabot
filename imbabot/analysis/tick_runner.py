"""Tick-data Morning analysis: ingest → simulate → label → assemble the Morning Plan.

Two entry points:
- ``analyze_ticks`` — research view over the cached/ingested tick days (per-day spike,
  volatility, straddle outcome, label) + validation vs the user's real trades.
- ``morning_plan`` — the live Morning Plan the GUI shows: **volatility level** + a
  **$TP-driven plan** (contracts + entry spread) from the predicted opening spike.

On the 4-day sample this is a validated *engine*, not a *predictor* — the spike model is
uncalibrated until the full tick history is loaded. Said plainly in the report header.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import calendar as econ
from .market_history import VIX_SYMBOL, by_date, load_daily, prior_value
from .sizing import SpikePlan, tp_plan_from_spike
from .spike_model import SpikeModel, load_spike_model
from .tick_data import TickDay, cached_dates, ingest_tbbo_zip, load_tickday
from .tick_features import SpikeMetrics, label_day, spike_metrics, volatility_level
from .tick_sim import TickOutcome, simulate_tick_straddle

# The user's live straddle settings (full NQ, $20/pt).
DEF_ENTRY, DEF_TP, DEF_SL = 12.0, 15.0, 8.0
DOLLARS_PER_POINT = 20.0


@dataclass
class TickDayAnalysis:
    date: str
    symbol: str
    prior_vix: Optional[float]
    news_label: str
    news_score: int
    volatility: str
    spike: Optional[SpikeMetrics]
    outcome: TickOutcome
    label: str             # clean-winner | whipsaw | no-trade


def analyze_day(td: TickDay, *, entry_points=DEF_ENTRY, tp_points=DEF_TP,
                sl_points=DEF_SL) -> TickDayAnalysis:
    vbd = by_date(load_daily(VIX_SYMBOL))
    pv = prior_value(vbd, td.date)
    prior_vix = pv.c if pv else None
    flag = econ.event_flag(td.date)
    sm = spike_metrics(td)
    out = simulate_tick_straddle(td, entry_points=entry_points, tp_points=tp_points,
                                 sl_points=sl_points)
    return TickDayAnalysis(
        date=td.date, symbol=td.symbol, prior_vix=prior_vix,
        news_label=flag.label, news_score=flag.score,
        volatility=volatility_level(prior_vix, flag.score),
        spike=sm, outcome=out, label=label_day(out))


def analyze_ticks(source: Optional[str | Path] = None, **kw) -> List[TickDayAnalysis]:
    """Analyze tick days. ``source`` = a Databento zip to ingest; else use cached days."""
    if source:
        days = ingest_tbbo_zip(source)
    else:
        days = [td for d in cached_dates() if (td := load_tickday(d))]
    return [analyze_day(td, **kw) for td in days]


# ----------------------------------------------------------------- Morning Plan
@dataclass
class MorningTickPlan:
    date: str
    volatility: str            # LOW | MEDIUM | HIGH | UNKNOWN
    prior_vix: Optional[float]
    news_label: str
    predicted_spike: float     # points
    calibrated: bool           # False until fit on full tick history
    plan: SpikePlan


def morning_plan(date: str, *, target_dollars: float, prior_vix: Optional[float] = None,
                 news_score: Optional[int] = None, dollars_per_point: float = DOLLARS_PER_POINT,
                 max_contracts: int = 10, sl_points: float = DEF_SL,
                 model: Optional[SpikeModel] = None) -> MorningTickPlan:
    """Assemble the Morning Plan: volatility level + $TP-driven contracts/spread."""
    if prior_vix is None:
        pv = prior_value(by_date(load_daily(VIX_SYMBOL)), date)
        prior_vix = pv.c if pv else None
    flag = econ.event_flag(date)
    if news_score is None:
        news_score = flag.score
    model = model or load_spike_model()
    S = model.predict(prior_vix, news_score)
    plan = tp_plan_from_spike(S, target_dollars, dollars_per_point=dollars_per_point,
                              max_contracts=max_contracts, sl_points=sl_points)
    return MorningTickPlan(
        date=date, volatility=volatility_level(prior_vix, news_score), prior_vix=prior_vix,
        news_label=flag.label, predicted_spike=S, calibrated=model.calibrated, plan=plan)


# ----------------------------------------------------------------- report
def analysis_report(rows: List[TickDayAnalysis], *, actual: Optional[dict] = None) -> str:
    actual = actual or {}
    out = [
        "IMBABOT — Tick Morning analysis (FOUNDATION)",
        "=" * 78,
        "Tick-accurate straddle on real tbbo data. On this small sample it VALIDATES the",
        "engine + sizing; the spike PREDICTOR stays uncalibrated until full tick history.",
        "",
        f"{'date':>10} {'vix':>5} {'vol':>6} {'thrust':>7} {'cntr':>5} {'label':>12} {'$ (3ct)':>9}  news",
    ]
    for r in rows:
        s = r.spike
        pnl3 = r.outcome.pnl_points * 3 * DOLLARS_PER_POINT
        val = ""
        if r.date in actual:
            val = f"   [actual ${actual[r.date]:+}]"
        out.append(
            f"{r.date:>10} {(r.prior_vix or 0):5.1f} {r.volatility:>6} "
            f"{(s.thrust if s else 0):7.1f} {(s.counter_poke if s else 0):5.1f} "
            f"{r.label:>12} {pnl3:9.0f}  {r.news_label}{val}")
    wins = sum(1 for r in rows if r.label == "clean-winner")
    out += ["", f"clean-winners {wins}/{len(rows)}; the rest whipsaw/no-trade. 4 days = NOT a pattern.",
            "DISCLAIMER: informational only; not financial advice; you decide the trade."]
    return "\n".join(out)
