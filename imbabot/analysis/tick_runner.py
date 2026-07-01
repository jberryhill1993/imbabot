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
# Validated thresholds (full-year walk-forward): predicted spike >= TRADE_MIN beats the
# trade-all baseline OOS; >= BIG_MIN flags the high-conviction 30+ pt "money days".
# TRADE_MIN matches walkforward.evaluate's spike_min (20): the 258-day leave-one-out sweep
# showed 18 -> 20 lifts win-rate 49%->56% and $/day $61->$94 while filtering whipsaw days
# (e.g. 4/13, predicted 19.6) that sat just over the old line.
TRADE_MIN = 20.0
BIG_MIN = 28.0


@dataclass
class MorningTickPlan:
    date: str                  # the trading SESSION the plan is for
    session_date: str          # same as date (the resolved next open)
    market_closed_today: bool  # True if "today" was a weekend/holiday
    volatility: str            # LOW | MEDIUM | HIGH | UNKNOWN
    prior_vix: Optional[float]
    news_label: str            # named events with [IMPACT]
    predicted_spike: float     # points (first ~2s thrust)
    p_big: float               # P(30+ pt spike)
    calibrated: bool           # False until fit on full tick history
    decision: str              # TRADE | NO-TRADE
    conviction: str            # STRONG | MODERATE | LOW
    rationale: str
    plan: SpikePlan


def _recent_thrust(date: str, k: int = 10) -> float:
    """Mean opening thrust of the last ``k`` cached tick days before ``date`` (regime feature)."""
    prior = [d for d in cached_dates() if d < date][-k:]
    vals = []
    for d in prior:
        td = load_tickday(d)
        if td:
            sm = spike_metrics(td, window_s=2.0)
            if sm:
                vals.append(sm.thrust)
    return sum(vals) / len(vals) if vals else 14.0


def _features(date: str, prior_vix: Optional[float]) -> dict:
    from datetime import date as _d
    flag = econ.event_flag(date)
    vbd = by_date(load_daily(VIX_SYMBOL))
    pv = prior_value(vbd, date)
    vix_change = 0.0
    if pv:
        pv2 = prior_value(vbd, pv.date)
        if pv2:
            vix_change = pv.c - pv2.c
    return {
        "prior_vix": float(prior_vix) if prior_vix is not None else 17.0,
        "vix_change": float(vix_change),
        "news_score": float(flag.score),
        "fomc": 1.0 if flag.fomc else 0.0,
        "dow": float(_d.fromisoformat(date).weekday()),
        "recent_thrust": _recent_thrust(date),
    }


def morning_plan(date: str, *, target_dollars: float, prior_vix: Optional[float] = None,
                 dollars_per_point: float = DOLLARS_PER_POINT, max_contracts: int = 5,
                 sl_points: float = DEF_SL, model: Optional[SpikeModel] = None) -> MorningTickPlan:
    """Assemble the Morning Plan for the next real trading SESSION (``date`` = "as of today";
    weekends/holidays roll to the next open). Predict the spike -> volatility, TRADE/NO-TRADE, $TP."""
    from datetime import date as _date
    from ..market_calendar import is_trading_day, next_trading_session
    asof = _date.fromisoformat(date)
    session = next_trading_session(asof)
    market_closed_today = not is_trading_day(asof)
    date = session.isoformat()       # predict for the real session, not a closed day
    if prior_vix is None:
        pv = prior_value(by_date(load_daily(VIX_SYMBOL)), date)
        prior_vix = pv.c if pv else None
    flag = econ.event_flag(date)
    model = model or load_spike_model()
    feats = _features(date, prior_vix)
    pred = model.predict(feats)
    S = pred.expected_spike
    plan = tp_plan_from_spike(S, target_dollars, dollars_per_point=dollars_per_point,
                              max_contracts=max_contracts, sl_points=sl_points, min_spread=10.0)

    # TRADE/NO-TRADE by predicted spike SIZE (the walk-forward-validated signal). Win/loss
    # itself isn't predictable, so conviction reflects spike magnitude, not P(win).
    if not model.calibrated:
        decision, conviction = "NO-TRADE", "LOW"
        rationale = "Predictor uncalibrated (ingest the full tick history first)."
    elif not plan.feasible or S < TRADE_MIN:
        decision, conviction = "NO-TRADE", "LOW"
        rationale = (f"Predicted opening spike only ~{S:.0f} pts — too small/choppy to clear a "
                     f">=10pt entry + TP. Historically these days are a coin-flip; sit out.")
    else:
        decision = "TRADE"
        conviction = "STRONG" if S >= BIG_MIN else "MODERATE"
        big = " — BIG-SPIKE day likely (30+ pt; historically ~95% winners)" if S >= BIG_MIN else ""
        rationale = (f"Predicted opening spike ~{S:.0f} pts (P(30+)={pred.p_big*100:.0f}%){big}. "
                     f"Sized to hit ${target_dollars:,.0f}. Note: spike SIZE is predictable; the "
                     f"win/loss on any single day is not — respect the stop.")
    return MorningTickPlan(
        date=date, session_date=date, market_closed_today=market_closed_today,
        volatility=volatility_level(prior_vix, flag.score), prior_vix=prior_vix,
        news_label=flag.label, predicted_spike=S, p_big=pred.p_big, calibrated=model.calibrated,
        decision=decision, conviction=conviction, rationale=rationale, plan=plan)


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
