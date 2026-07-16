"""Ingest FirstRate-style 1-minute CSV into cached DayRecords.

FirstRate Data futures files are headerless CSV ``timestamp,open,high,low,close,volume``
with timestamps in **US Eastern** time marking each bar's START. We group bars by ET
session, distill each day's open behavior into a ``DayRecord``, and cache the result
as JSON so the 12-month ingest runs once.

The parser is deliberately tolerant: it skips a header row if present, accepts a couple
of datetime spellings, and ignores blank/short lines — so a TradingView/Barchart export
in the same shape also works.
"""
from __future__ import annotations

import csv as _csv
import json
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from ..config import config_dir
from .types import DayRecord, OpenBar

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ET = "America/New_York"
RTH_OPEN = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)
GLOBEX_OPEN = dtime(18, 0)          # prior-evening Globex open (ET)
DEFAULT_OPEN_MINUTES = 15          # bars after the open we keep for simulation

_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M")


@dataclass
class MinBar:
    dt: datetime   # tz-aware, ET
    o: float
    h: float
    l: float
    c: float
    v: float


def _tz():
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo unavailable. On Windows: pip install tzdata")
    return ZoneInfo(ET)


def _parse_dt(s: str) -> Optional[datetime]:
    s = s.strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_tz())
        except ValueError:
            continue
    return None


def parse_firstrate_csv(path: str | Path, limit: Optional[int] = None) -> List[MinBar]:
    """Parse a FirstRate-style 1-min CSV into ET-localized MinBars (ascending)."""
    bars: List[MinBar] = []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        for row in _csv.reader(fh):
            if len(row) < 6:
                continue
            dt = _parse_dt(row[0])
            if dt is None:
                continue  # header or junk line
            try:
                bars.append(MinBar(dt, float(row[1]), float(row[2]),
                                   float(row[3]), float(row[4]), float(row[5])))
            except ValueError:
                continue
            if limit and len(bars) >= limit:
                break
    bars.sort(key=lambda b: b.dt)
    return bars


def _rth_close_by_date(bars: List[MinBar]) -> Dict[str, float]:
    """Map ET date -> that session's RTH close (last bar at/at-or-before 16:00)."""
    closes: Dict[str, float] = {}
    for b in bars:
        if b.dt.time() <= RTH_CLOSE:
            closes[b.dt.date().isoformat()] = b.c  # ascending, so last wins up to 16:00
    return closes


def build_day_records(
    bars: List[MinBar], *, open_minutes: int = DEFAULT_OPEN_MINUTES
) -> List[DayRecord]:
    """Distill ascending ET MinBars into one DayRecord per session with a 09:30 open."""
    if not bars:
        return []
    by_date: Dict[str, List[MinBar]] = {}
    for b in bars:
        by_date.setdefault(b.dt.date().isoformat(), []).append(b)

    rth_close = _rth_close_by_date(bars)
    dates = sorted(by_date)
    prev_close_for: Dict[str, Optional[float]] = {}
    last_close: Optional[float] = None
    for d in dates:
        prev_close_for[d] = last_close
        if d in rth_close:
            last_close = rth_close[d]

    records: List[DayRecord] = []
    for d in dates:
        day_bars = by_date[d]
        tz = _tz()
        open_dt = datetime.combine(day_bars[0].dt.date(), RTH_OPEN, tzinfo=tz)
        # Reference = the 09:30 bar's open (the bot would straddle the opening print).
        open_bar = next((b for b in day_bars if b.dt == open_dt), None)
        if open_bar is None:
            continue  # no cash-session open this day (holiday/half-data) — skip
        ref = open_bar.o

        window_end = open_dt + timedelta(minutes=open_minutes)
        open_bars = [
            OpenBar(int((b.dt - open_dt).total_seconds() // 60), b.o, b.h, b.l, b.c, b.v)
            for b in day_bars if open_dt <= b.dt < window_end
        ]

        # Overnight Globex range: prior 18:00 ET -> this 09:30 ET.
        on_start = datetime.combine(open_dt.date() - timedelta(days=1), GLOBEX_OPEN, tzinfo=tz)
        on_bars = [b for b in bars if on_start <= b.dt < open_dt]
        on_high = max((b.h for b in on_bars), default=None)
        on_low = min((b.l for b in on_bars), default=None)

        records.append(DayRecord(
            date=d, ref_price=ref, open_bars=open_bars,
            overnight_high=on_high, overnight_low=on_low,
            prior_close=prev_close_for.get(d),
        ))
    return records


# ----------------------------------------------------------------- cache I/O
def history_path(symbol: str) -> Path:
    return config_dir() / "analysis" / "history" / f"{symbol.upper()}.json"


def save_records(symbol: str, records: List[DayRecord], *, source: str = "csv") -> Path:
    path = history_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol.upper(),
        "source": source,
        "count": len(records),
        "first_date": records[0].date if records else None,
        "last_date": records[-1].date if records else None,
        "records": [r.to_dict() for r in records],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def load_records(symbol: str) -> List[DayRecord]:
    path = history_path(symbol)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [DayRecord.from_dict(r) for r in data.get("records", [])]


def ingest_csv(
    path: str | Path, symbol: str, *, open_minutes: int = DEFAULT_OPEN_MINUTES
) -> List[DayRecord]:
    """Parse a FirstRate CSV, build DayRecords, cache them, and return them."""
    bars = parse_firstrate_csv(path)
    records = build_day_records(bars, open_minutes=open_minutes)
    save_records(symbol, records, source=f"csv:{Path(path).name}")
    return records
