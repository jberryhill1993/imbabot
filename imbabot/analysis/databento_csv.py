"""Ingest Databento `ohlcv-1s` (1-second) CSV into cached DayRecords.

Databento exports OHLCV bars as CSV with a header row. We key off the header names
(robust to column order / extra metadata columns) and tolerate the two formatting
choices the portal offers:
- **timestamps** (`ts_event`) as ISO-8601 *or* integer nanoseconds since epoch (UTC),
- **prices** as decimals *or* integer fixed-point scaled by 1e-9.

1-second bars resolve the intrabar high/low *sequence* in the opening minute — the
whipsaw the 1-minute FirstRate feed can't see. Output reuses the same ``DayRecord``
cache as the 1-minute path; ``OpenBar.minute`` holds the **seconds** offset from the
09:30 ET open for 1-second bars.
"""
from __future__ import annotations

import csv as _csv
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .csv_history import MinBar, _tz, RTH_OPEN, RTH_CLOSE, GLOBEX_OPEN, DEFAULT_OPEN_MINUTES
from .csv_history import save_records  # reuse the symbol-keyed JSON cache
from .types import DayRecord, OpenBar

# Header aliases Databento uses (lowercased). ts_event = bar start time.
_TS_KEYS = ("ts_event", "ts_recv", "timestamp", "time")
_O, _H, _L, _C, _V = "open", "high", "low", "close", "volume"
_PX_FIXED_THRESHOLD = 1e7   # any NQ price above this must be 1e-9 fixed-point


def _parse_ts(raw: str) -> Optional[datetime]:
    """Databento ts_event -> aware UTC datetime (ISO string or epoch nanoseconds)."""
    s = raw.strip()
    if not s:
        return None
    if s.isdigit():  # integer nanoseconds since epoch
        return datetime.fromtimestamp(int(s) / 1e9, tz=timezone.utc)
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_px(raw: str) -> Optional[float]:
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return None
    return v / 1e9 if abs(v) > _PX_FIXED_THRESHOLD else v


def parse_databento_csv(path: str | Path, limit: Optional[int] = None) -> List[MinBar]:
    """Parse a Databento ohlcv CSV into ET-localized MinBars (ascending)."""
    tz = _tz()
    bars: List[MinBar] = []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = _csv.reader(fh)
        header = next(reader, None)
        if not header:
            return []
        cols = {name.strip().lower(): i for i, name in enumerate(header)}
        ts_i = next((cols[k] for k in _TS_KEYS if k in cols), None)
        try:
            o_i, h_i, l_i, c_i, v_i = (cols[_O], cols[_H], cols[_L], cols[_C], cols[_V])
        except KeyError:
            raise ValueError("CSV missing open/high/low/close/volume columns — "
                             "is this a Databento OHLCV export?")
        if ts_i is None:
            raise ValueError("CSV has no recognizable timestamp column (ts_event).")
        for row in reader:
            if len(row) <= max(ts_i, o_i, h_i, l_i, c_i, v_i):
                continue
            ts = _parse_ts(row[ts_i])
            o, h, l, c = (_parse_px(row[o_i]), _parse_px(row[h_i]),
                          _parse_px(row[l_i]), _parse_px(row[c_i]))
            if ts is None or None in (o, h, l, c):
                continue
            try:
                v = float(row[v_i] or 0.0)
            except ValueError:
                v = 0.0
            bars.append(MinBar(ts.astimezone(tz), o, h, l, c, v))
            if limit and len(bars) >= limit:
                break
    bars.sort(key=lambda b: b.dt)
    return bars


def build_day_records_1s(
    bars: List[MinBar], *, open_minutes: int = DEFAULT_OPEN_MINUTES
) -> List[DayRecord]:
    """Distill ascending ET 1-second MinBars into one DayRecord per RTH session.

    ``OpenBar.minute`` carries the integer **seconds** offset from 09:30:00 ET.
    Overnight range / prior close are filled only if the export spans them (a
    narrow opening-window export leaves them None — gap is derived elsewhere).
    """
    if not bars:
        return []
    tz = _tz()
    by_date: Dict[str, List[MinBar]] = {}
    for b in bars:
        by_date.setdefault(b.dt.date().isoformat(), []).append(b)

    # Prior RTH close per date (last bar at/at-or-before 16:00), if present.
    rth_close: Dict[str, float] = {}
    for b in bars:
        if b.dt.time() <= RTH_CLOSE:
            rth_close[b.dt.date().isoformat()] = b.c
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
        open_dt = datetime.combine(day_bars[0].dt.date(), RTH_OPEN, tzinfo=tz)
        open_bar = next((b for b in day_bars if b.dt == open_dt), None)
        if open_bar is None:
            continue
        ref = open_bar.o
        window_end = open_dt + timedelta(minutes=open_minutes)
        open_bars = [
            OpenBar(int((b.dt - open_dt).total_seconds()), b.o, b.h, b.l, b.c, b.v)
            for b in day_bars if open_dt <= b.dt < window_end
        ]
        on_start = datetime.combine(open_dt.date() - timedelta(days=1), GLOBEX_OPEN, tzinfo=tz)
        on_bars = [b for b in bars if on_start <= b.dt < open_dt]
        records.append(DayRecord(
            date=d, ref_price=ref, open_bars=open_bars,
            overnight_high=max((b.h for b in on_bars), default=None),
            overnight_low=min((b.l for b in on_bars), default=None),
            prior_close=prev_close_for.get(d),
        ))
    return records


def ingest_databento_csv(
    path: str | Path, symbol: str, *, open_minutes: int = DEFAULT_OPEN_MINUTES
) -> List[DayRecord]:
    """Parse a small Databento ohlcv-1s CSV, build DayRecords, cache, return.

    For large multi-contract `.csv`/`.csv.zst` exports (the `NQ.FUT` parent), use
    ``ingest_databento_stream`` instead — it streams, windows to the open, and picks
    the front-month contract per day.
    """
    bars = parse_databento_csv(path)
    records = build_day_records_1s(bars, open_minutes=open_minutes)
    save_records(symbol, records, source=f"databento:{Path(path).name}")
    return records


def _open_text(path: str | Path):
    """Open a Databento export, transparently decompressing `.zst` (Python 3.14 stdlib)."""
    sp = str(path)
    if sp.endswith(".zst"):
        from compression import zstd  # py3.14+ stdlib
        return zstd.open(sp, "rt", encoding="utf-8")
    return open(sp, "r", newline="", encoding="utf-8")


def ingest_databento_stream(
    path: str | Path, symbol: str, *, open_minutes: int = DEFAULT_OPEN_MINUTES,
    pre_pad_min: int = 1,
) -> List[DayRecord]:
    """Stream a (possibly huge, possibly `.zst`, multi-contract) Databento ohlcv-1s
    export, keep only the opening-window bars, pick the **front-month** contract per
    day (highest opening volume), and cache one DayRecord per session.

    Memory stays low: a cheap UTC-hour string prefilter drops ~22/24 of the day before
    any timezone math, then ET-window bars are kept per (date, contract).
    """
    from collections import defaultdict

    tz = _tz()
    open_t, end_t = RTH_OPEN, dtime(RTH_OPEN.hour, RTH_OPEN.minute + open_minutes) \
        if RTH_OPEN.minute + open_minutes < 60 else dtime(RTH_OPEN.hour + 1,
                                                          (RTH_OPEN.minute + open_minutes) % 60)
    lo_t = dtime(open_t.hour, open_t.minute - pre_pad_min) if open_t.minute >= pre_pad_min \
        else dtime(open_t.hour - 1, 60 - pre_pad_min)
    # 09:30 ET is 13:30 UTC (EDT) or 14:30 UTC (EST) -> these UTC hours bracket the window.
    _UTC_HOURS = {"13", "14"}

    # date -> symbol -> {offset_sec: (o,h,l,c,v)} ; date -> symbol -> total volume
    bars: dict = defaultdict(lambda: defaultdict(dict))
    vol: dict = defaultdict(lambda: defaultdict(float))

    with _open_text(path) as fh:
        reader = _csv.reader(fh)
        header = next(reader)
        cols = {n.strip().lower(): i for i, n in enumerate(header)}
        ts_i = next((cols[k] for k in _TS_KEYS if k in cols), 0)
        o_i, h_i, l_i, c_i, v_i = cols["open"], cols["high"], cols["low"], cols["close"], cols["volume"]
        sym_i = cols.get("symbol")
        for row in reader:
            ts = row[ts_i]
            if len(ts) < 16 or ts[11:13] not in _UTC_HOURS:  # cheap prefilter
                continue
            dt = _parse_ts(ts)
            if dt is None:
                continue
            et = dt.astimezone(tz)
            t = et.time()
            if not (lo_t <= t < end_t):
                continue
            sym = row[sym_i] if sym_i is not None else symbol
            o, h, l, c = (_parse_px(row[o_i]), _parse_px(row[h_i]),
                          _parse_px(row[l_i]), _parse_px(row[c_i]))
            if None in (o, h, l, c):
                continue
            try:
                v = float(row[v_i] or 0.0)
            except ValueError:
                v = 0.0
            open_dt = datetime.combine(et.date(), RTH_OPEN, tzinfo=tz)
            off = int((et - open_dt).total_seconds())
            d = et.date().isoformat()
            bars[d][sym][off] = (o, h, l, c, v)
            vol[d][sym] += v

    records: List[DayRecord] = []
    for d in sorted(bars):
        # front month = the contract with the most opening-window volume that day
        front = max(vol[d], key=lambda s: vol[d][s])
        day = bars[d][front]
        if 0 not in day:          # need the exact 09:30:00 bar as the reference
            continue
        ref = day[0][0]           # 09:30:00 open
        open_bars = [OpenBar(off, *day[off]) for off in sorted(day) if 0 <= off < open_minutes * 60]
        records.append(DayRecord(date=d, ref_price=ref, open_bars=open_bars,
                                 overnight_high=None, overnight_low=None, prior_close=None))
    save_records(symbol, records, source=f"databento:{Path(path).name}")
    return records
