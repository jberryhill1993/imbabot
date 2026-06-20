"""Feature assembly for the spread model.

Joins one trading day's open behavior with the daily VIX/NQ history and the
economic calendar into a fixed feature vector. The SAME primitives are used to
build training rows (from cached ``DayRecord``s, where the gap is the realized
open gap) and to build today's pre-open row in the daily runner (where overnight
range comes from the live session and the gap is estimated from the pre-open
price) — so train and serve stay aligned.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from . import calendar as econ
from .market_history import DailyBar, atr, by_date, prior_value
from .types import DayRecord

# Ordered feature names — the vector layout the model fits/serves.
FEATURE_NAMES: List[str] = [
    "prior_vix",         # prior session VIX close (fear gauge known pre-open)
    "vix_change",        # prior_vix minus the VIX close before it
    "overnight_range",   # Globex high-low into the open (volatility)
    "gap_abs",           # |open - prior RTH close| (overnight imbalance)
    "atr14",             # 14-day NQ daily range (regime volatility)
    "preopen_score",     # 0/1/2 scheduled 08:30 release intensity
    "fomc",              # 1 on FOMC decision days
]


def feature_row(
    date: str,
    overnight_range: Optional[float],
    gap: Optional[float],
    vix_by_date: Dict[str, DailyBar],
    nq_bars: List[DailyBar],
) -> Dict[str, float]:
    """Assemble the feature dict for ``date`` from primitives + daily history."""
    pv = prior_value(vix_by_date, date)
    prior_vix = pv.c if pv else 0.0
    # VIX close the session before the prior one, for the change feature.
    vix_change = 0.0
    if pv:
        pv2 = prior_value(vix_by_date, pv.date)
        if pv2:
            vix_change = pv.c - pv2.c
    flag = econ.event_flag(date)
    return {
        "prior_vix": prior_vix,
        "vix_change": vix_change,
        "overnight_range": float(overnight_range or 0.0),
        "gap_abs": abs(float(gap)) if gap is not None else 0.0,
        "atr14": atr(nq_bars, date, 14) or 0.0,
        "preopen_score": float(flag.preopen_score),
        "fomc": 1.0 if flag.fomc else 0.0,
    }


def row_from_record(
    rec: DayRecord, vix_by_date: Dict[str, DailyBar], nq_bars: List[DailyBar]
) -> Dict[str, float]:
    """Training-time feature row from a cached DayRecord (realized gap/overnight)."""
    return feature_row(rec.date, rec.overnight_range, rec.gap, vix_by_date, nq_bars)


def to_vector(row: Dict[str, float]) -> List[float]:
    return [row[name] for name in FEATURE_NAMES]
