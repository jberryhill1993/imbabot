"""US economic-event calendar for the spread analyzer.

Classifies each trading day by the scheduled macro releases that move the open:
- **Pre-open releases** (08:30 ET — one hour before the 09:30 cash open) directly
  inflate the opening candle: monthly **NFP** (first Friday), weekly **jobless claims**
  (Thursday), and **CPI/PPI**. NFP and jobless are derived by rule; CPI/PPI come from
  the bundled curated list (``data/econ_events.json``).
- **Same-day** events that don't hit the open but raise the regime: **FOMC** (decision
  at 14:00 ET), from the curated list.

``event_flag(date)`` returns an :class:`EventFlag` with both a human label and numeric
features (``preopen_score``, ``fomc``) the spread model keys off. The curated JSON is
refreshable; the derived rules always work even if the JSON is stale.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

_PREOPEN_IMPACT = {"high": 2, "medium": 1, "low": 1}


def _data_path() -> Path:
    """Locate econ_events.json from source or a frozen exe (mirrors browser packs)."""
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
    labels: List[str] = field(default_factory=list)
    preopen_score: int = 0     # 0 none, 1 medium (jobless/PPI), 2 high (NFP/CPI)
    fomc: bool = False         # FOMC decision day (same-day regime, not pre-open)

    @property
    def has_event(self) -> bool:
        return bool(self.labels)

    @property
    def label(self) -> str:
        return ", ".join(self.labels) if self.labels else "none"

    @property
    def score(self) -> int:
        """Combined intensity for the model/report (pre-open weighted, FOMC adds 1)."""
        return min(3, self.preopen_score + (1 if self.fomc else 0))


def event_flag(d: str) -> EventFlag:
    """Classify the macro events scheduled for ISO date ``d`` (YYYY-MM-DD)."""
    data = _load()
    dt = datetime.strptime(d, "%Y-%m-%d").date()
    flag = EventFlag(date=d)

    # --- derived regular releases (08:30 ET, pre-open) ---
    if dt == _first_friday(dt.year, dt.month):
        flag.labels.append("NFP (08:30)")
        flag.preopen_score = max(flag.preopen_score, 2)
    if dt.weekday() == 3:  # Thursday
        flag.labels.append("Jobless claims (08:30)")
        flag.preopen_score = max(flag.preopen_score, 1)

    # --- curated irregular releases ---
    for key in ("cpi", "ppi"):
        block = data.get(key) or {}
        if d in (block.get("dates") or []):
            impact = block.get("impact", "high")
            flag.labels.append(f"{key.upper()} ({block.get('time_et', '08:30')})")
            flag.preopen_score = max(flag.preopen_score, _PREOPEN_IMPACT.get(impact, 2))

    fomc = data.get("fomc") or {}
    if d in (fomc.get("dates") or []):
        flag.fomc = True
        flag.labels.append(f"FOMC ({fomc.get('time_et', '14:00')})")

    return flag


def upcoming_fomc(after: str, limit: int = 1) -> List[str]:
    """Next FOMC decision dates on/after ISO date ``after`` (for the report)."""
    dates = sorted((_load().get("fomc") or {}).get("dates") or [])
    return [d for d in dates if d >= after][:limit]
