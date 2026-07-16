"""Ingest Databento **tbbo** (Trade + BBO) tick data for the opening-spike analysis.

`tbbo` gives one row per trade with the best bid/offer at that instant — enough to
reconstruct the exact sub-second price path AND model realistic fills (a buy-stop
fills at the **ask**, a sell-stop / short-cover at the **bid**). A 1-second OHLCV bar
can't resolve a trade that lives <1s; ticks can. Built to stream day-by-day so a full
year is tractable.
"""
from __future__ import annotations

import csv as _csv
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from ..config import config_dir
from .csv_history import _tz, RTH_OPEN          # ET tz + 09:30 open time
from .databento_csv import _parse_ts, _parse_px  # tolerant ts/px parsers

# Opening window kept per day, in seconds from the 09:30:00 ET open. Covers the
# pre-open reference capture (-3s) and resolution (the user's trades resolve in
# <1s..~30s; keep 3 min of margin).
WINDOW_START = -15.0
WINDOW_END = 180.0
# 09:30 ET is 13:30 UTC (EDT) or 14:30 UTC (EST) — cheap string prefilter before tz math.
_UTC_HOURS = {"13", "14"}
_TS_KEYS = ("ts_event", "ts_recv", "timestamp", "time")


@dataclass
class Tick:
    t: float       # seconds from the 09:30:00 ET open (negative = pre-open)
    price: float
    bid: float
    ask: float


@dataclass
class TickDay:
    date: str          # ISO ET session date
    symbol: str        # front-month contract (e.g. NQU6)
    ticks: List[Tick]  # ordered by time, opening window only

    def price_at(self, t: float) -> Optional[float]:
        """Last trade price at or before offset ``t`` — the reference capture."""
        last = None
        for tk in self.ticks:
            if tk.t <= t:
                last = tk.price
            else:
                break
        return last

    def to_dict(self) -> dict:
        return {"date": self.date, "symbol": self.symbol,
                "ticks": [[tk.t, tk.price, tk.bid, tk.ask] for tk in self.ticks]}

    @classmethod
    def from_dict(cls, d: dict) -> "TickDay":
        return cls(d["date"], d["symbol"],
                   [Tick(float(a), float(b), float(c), float(e)) for a, b, c, e in d["ticks"]])


def _parse_member(raw: bytes) -> List[TickDay]:
    """Decompress one `*.tbbo.csv.zst` member and distill its opening-window TickDays.

    Front month per session = the NQ contract with the most opening-window trade volume
    (spreads, with '-' in the symbol, are excluded).
    """
    from compression import zstd  # py3.14 stdlib

    text = zstd.decompress(raw).decode("utf-8", "replace")
    lines = text.splitlines()
    if not lines:
        return []
    hdr = {n.strip().lower(): i for i, n in enumerate(lines[0].split(","))}
    ti = next((hdr[k] for k in _TS_KEYS if k in hdr), None)
    try:
        pi, szi = hdr["price"], hdr["size"]
        bidi, aski = hdr["bid_px_00"], hdr["ask_px_00"]
    except KeyError:
        raise ValueError("not a tbbo CSV — missing price/size/bid_px_00/ask_px_00")
    syi = hdr.get("symbol")
    acti = hdr.get("action")
    tz = _tz()

    # date -> symbol -> list[Tick] ; date -> symbol -> volume
    by: Dict[str, Dict[str, List[Tick]]] = {}
    vol: Dict[str, Dict[str, float]] = {}
    for ln in lines[1:]:
        # cheap prefilter: only the ET-open UTC hours
        if len(ln) < 16 or ln[11:13] not in _UTC_HOURS:
            continue
        r = ln.split(",")
        if acti is not None and r[acti] != "T":   # trades only
            continue
        sym = r[syi] if syi is not None else "NQ"
        if "-" in sym or not sym.startswith("NQ"):  # skip spreads / non-NQ
            continue
        dt = _parse_ts(r[ti])
        if dt is None:
            continue
        et = dt.astimezone(tz)
        open_dt = datetime.combine(et.date(), RTH_OPEN, tzinfo=tz)
        off = (et - open_dt).total_seconds()
        if not (WINDOW_START <= off <= WINDOW_END):
            continue
        price = _parse_px(r[pi]); bid = _parse_px(r[bidi]); ask = _parse_px(r[aski])
        if price is None:
            continue
        d = et.date().isoformat()
        by.setdefault(d, {}).setdefault(sym, []).append(
            Tick(off, price, bid if bid is not None else price, ask if ask is not None else price))
        try:
            v = float(r[szi] or 0.0)
        except ValueError:
            v = 0.0
        vol.setdefault(d, {})[sym] = vol.setdefault(d, {}).get(sym, 0.0) + v

    out: List[TickDay] = []
    for d in sorted(by):
        front = max(vol[d], key=lambda s: vol[d][s])
        ticks = sorted(by[d][front], key=lambda tk: tk.t)
        out.append(TickDay(date=d, symbol=front, ticks=ticks))
    return out


def ingest_tbbo_zip(zip_path: str | Path, *, cache: bool = True) -> List[TickDay]:
    """Ingest every `*.tbbo.csv.zst` in a Databento zip → cached TickDays (one per session)."""
    days: List[TickDay] = []
    with zipfile.ZipFile(zip_path) as z:
        for name in sorted(z.namelist()):
            if not name.endswith(".tbbo.csv.zst"):
                continue
            for td in _parse_member(z.read(name)):
                if cache:
                    save_tickday(td)
                days.append(td)
    return days


def ingest_tbbo_file(path: str | Path, *, cache: bool = True) -> List[TickDay]:
    """Ingest a single `.tbbo.csv.zst` (or `.csv`) file → TickDays."""
    p = Path(path)
    raw = p.read_bytes()
    if p.suffix != ".zst":  # plain csv: wrap as if decompressed
        from compression import zstd
        raw = zstd.compress(raw)
    days = _parse_member(raw)
    if cache:
        for td in days:
            save_tickday(td)
    return days


# ------------------------------------------------------------------ cache I/O
def ticks_dir() -> Path:
    p = config_dir() / "analysis" / "ticks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_tickday(td: TickDay) -> Path:
    path = ticks_dir() / f"{td.date}.json"
    path.write_text(json.dumps(td.to_dict()), encoding="utf-8")
    return path


def load_tickday(date: str) -> Optional[TickDay]:
    path = ticks_dir() / f"{date}.json"
    if not path.exists():
        return None
    return TickDay.from_dict(json.loads(path.read_text(encoding="utf-8")))


def cached_dates() -> List[str]:
    return sorted(p.stem for p in ticks_dir().glob("*.json"))
