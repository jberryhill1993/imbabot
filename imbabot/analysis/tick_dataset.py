"""Build the labeled, feature-tagged dataset the spike predictor fits on.

One row per trading day, in date order (so rolling features never leak the future):
- **Pre-open features** (all known before 9:30): prior VIX + VIX change, scheduled-news impact
  score + FOMC flag, day-of-week, and a rolling **recent realized opening thrust** (mean of the
  prior K days' first-second spikes — the volatility regime).
- **Targets** from the tick path: the realized **opening spike magnitude** (thrust over the first
  ~2 s — the burst the user trades, per the videos), a **is_big** flag (>= BIG_PTS), and the
  straddle **label** (clean-winner / whipsaw / no-trade) at the user's bracket.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import calendar as econ
from .market_history import VIX_SYMBOL, by_date, load_daily, prior_value
from .tick_data import TickDay, cached_dates, load_tickday
from .tick_features import label_day, spike_metrics
from .tick_sim import simulate_tick_straddle

SPIKE_WINDOW_S = 2.0     # the opening burst the user trades happens in ~1-2 s
BIG_PTS = 30.0           # "massive" opening spike the user wants to capitalize on
RECENT_K = 10            # rolling window for the recent-realized-vol regime feature

# Label bracket (the user's real config: ±12 entry, $900 TP=15pt, ~$500 SL=8pt).
LBL_ENTRY, LBL_TP, LBL_SL = 12.0, 15.0, 8.0

FEATURES = ["prior_vix", "vix_change", "news_score", "fomc", "dow", "recent_thrust"]


@dataclass
class DayRow:
    date: str
    feats: dict            # FEATURES -> value
    thrust: float          # realized first-~2s opening spike (points)
    counter_poke: float
    is_big: int            # 1 if thrust >= BIG_PTS
    label: str             # clean-winner | whipsaw | no-trade
    pnl_points: float
    news_label: str
    prior_vix: Optional[float]


def build_dataset(dates: Optional[List[str]] = None) -> List[DayRow]:
    """Assemble DayRows from the cached tick days, in date order (no look-ahead)."""
    dates = sorted(dates or cached_dates())
    vbd = by_date(load_daily(VIX_SYMBOL))
    rows: List[DayRow] = []
    recent: List[float] = []     # trailing realized thrusts for the regime feature
    for d in dates:
        td = load_tickday(d)
        if td is None or not td.ticks:
            continue
        sm = spike_metrics(td, window_s=SPIKE_WINDOW_S)
        if sm is None:
            continue
        out = simulate_tick_straddle(td, entry_points=LBL_ENTRY, tp_points=LBL_TP, sl_points=LBL_SL)
        pv = prior_value(vbd, d)
        prior_vix = pv.c if pv else None
        vix_change = 0.0
        if pv:
            pv2 = prior_value(vbd, pv.date)
            if pv2:
                vix_change = pv.c - pv2.c
        flag = econ.event_flag(d)
        recent_thrust = (sum(recent) / len(recent)) if recent else sm.thrust
        feats = {
            "prior_vix": float(prior_vix) if prior_vix is not None else 17.0,
            "vix_change": float(vix_change),
            "news_score": float(flag.score),
            "fomc": 1.0 if flag.fomc else 0.0,
            "dow": float(__import__("datetime").date.fromisoformat(d).weekday()),
            "recent_thrust": float(recent_thrust),
        }
        rows.append(DayRow(
            date=d, feats=feats, thrust=sm.thrust, counter_poke=sm.counter_poke,
            is_big=1 if sm.thrust >= BIG_PTS else 0, label=label_day(out),
            pnl_points=out.pnl_points, news_label=flag.label, prior_vix=prior_vix))
        recent.append(sm.thrust)
        if len(recent) > RECENT_K:
            recent.pop(0)
    return rows


def to_matrix(rows: List[DayRow]):
    """Return (X feature-matrix, dates) for the fixed FEATURES order."""
    return [[r.feats[f] for f in FEATURES] for r in rows], [r.date for r in rows]
