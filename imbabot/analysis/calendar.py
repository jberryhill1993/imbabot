"""US economic-event calendar with IMPACT LEVELS (ForexFactory-style) for the Morning Plan.

Classifies each trading day by the scheduled macro releases that move the open and tags each
with an impact level (high/medium/low):
- **Pre-open releases** (08:30 ET, ~1h before the 09:30 cash open) inflate the opening spike:
  monthly **NFP** (first Friday, HIGH, derived), weekly **jobless claims** (Thursday, LOW, derived),
  and curated **CPI / Core PCE / PPI / Retail Sales / GDP** from ``data/econ_events.json``.
- **Same-day regime** events: **FOMC** decision (14:00 ET, HIGH) and **ISM** (10:00 ET) — they don't
  hit the 08:30 open but raise the day's volatility regime.

``event_flag(date)`` returns named events + impact + numeric scores the predictor keys off.
Curated dates come from BLS/BEA/FRB schedules; the derived rules always work; and upcoming
dates auto-refresh from ForexFactory's weekly feed cache (see ``newsfeed.py`` — curated wins ties).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}
# Curated event types -> (display name, impact, time_et, pre_open?). Dates live in the JSON.
_CURATED = {
    "fomc":   ("FOMC decision", "high", "14:00", False),
    "cpi":    ("CPI", "high", "08:30", True),
    "pce":    ("Core PCE Price Index", "high", "08:30", True),
    "ppi":    ("PPI", "medium", "08:30", True),
    "retail": ("Retail Sales", "medium", "08:30", True),
    "gdp":    ("GDP", "medium", "08:30", True),
    "ism_mfg": ("ISM Manufacturing PMI", "medium", "10:00", False),
    "ism_svc": ("ISM Services PMI", "medium", "10:00", False),
}


def _data_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "imbabot" / "analysis" / "data" / "econ_events.json"
    return Path(__file__).with_name("data") / "econ_events.json"


_CACHE: Optional[dict] = None


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_data_path().read_text(encoding="utf-8"))
        except Exception:
            _CACHE = {}
    return _CACHE


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)  # Friday = weekday 4


@dataclass
class EventFlag:
    date: str
    events: List[Tuple[str, str, str, bool]] = field(default_factory=list)  # (name, impact, time_et, preopen)

    @property
    def has_event(self) -> bool:
        return bool(self.events)

    @property
    def labels(self) -> List[str]:
        return [e[0] for e in self.events]

    @property
    def label(self) -> str:
        if not self.events:
            return "none"
        return ", ".join(f"{n} [{imp.upper()}]" for n, imp, _t, _p in self.events)

    @property
    def max_impact(self) -> str:
        if not self.events:
            return "none"
        return max((e[1] for e in self.events), key=lambda i: _IMPACT_RANK.get(i, 0))

    @property
    def preopen_impact(self) -> str:
        """Highest impact among events that hit BEFORE the 09:30 open (08:30 releases)."""
        pre = [e[1] for e in self.events if e[3]]
        return max(pre, key=lambda i: _IMPACT_RANK.get(i, 0)) if pre else "none"

    @property
    def preopen_score(self) -> int:
        return _IMPACT_RANK.get(self.preopen_impact, 0)

    @property
    def fomc(self) -> bool:
        return any("FOMC" in e[0] for e in self.events)

    @property
    def score(self) -> int:
        """Combined catalyst intensity 0-3 (pre-open weighted; FOMC/high regime adds)."""
        return min(3, max(self.preopen_score, _IMPACT_RANK.get(self.max_impact, 0)))


def event_flag(d: str) -> EventFlag:
    """Classify the macro events scheduled for ISO date ``d`` (YYYY-MM-DD), with impact levels."""
    data = _load()
    dt = datetime.strptime(d, "%Y-%m-%d").date()
    flag = EventFlag(date=d)

    # --- derived regular releases ---
    if dt == _first_friday(dt.year, dt.month):
        flag.events.append(("Nonfarm Payrolls", "high", "08:30", True))
    if dt.weekday() == 3:  # Thursday
        flag.events.append(("Jobless Claims", "low", "08:30", True))

    # --- curated irregular releases (dates in JSON) ---
    seen_keys = set()
    for key, (name, impact, time_et, preopen) in _CURATED.items():
        block = data.get(key) or {}
        if d in (block.get("dates") or []):
            imp = block.get("impact", impact)
            flag.events.append((name, imp, block.get("time_et", time_et), preopen))
            seen_keys.add(key)

    # --- auto-fetched feed (ForexFactory weekly XML cache; curated JSON wins ties) ---
    try:
        from . import newsfeed
        for key, feed_time in newsfeed.cached_events(d):
            if key in seen_keys or key not in _CURATED:
                continue
            name, impact, def_time, _ = _CURATED[key]
            t = feed_time or def_time
            flag.events.append((name, impact, t, t < "09:30"))
            seen_keys.add(key)
    except Exception:
        pass  # a bad cache must never break the plan

    return flag


def upcoming_fomc(after: str, limit: int = 1) -> List[str]:
    dates = sorted((_load().get("fomc") or {}).get("dates") or [])
    return [d for d in dates if d >= after][:limit]
