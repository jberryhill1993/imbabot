"""12-month daily history (VIX + NQ) from Yahoo for the analyzer's features.

The same public Yahoo chart endpoint the live ticker uses also returns daily bars
when asked with ``?interval=1d&range=1y`` — enough for the volatility/gap features
(prior-day VIX, VIX change, daily ATR, overnight gap context) that the spread model
keys off. Best-effort and cached to disk; any failure returns what's cached (or empty).
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from ..config import config_dir
from ..ticker import _CHART_URL, _HEADERS, VIX_SYMBOL, DEFAULT_TICKER_SYMBOL

NQ_SYMBOL = DEFAULT_TICKER_SYMBOL  # "NQ=F"


@dataclass
class DailyBar:
    date: str   # ISO date (UTC calendar date of the session)
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def true_range(self) -> float:
        return self.h - self.l

    def to_list(self) -> list:
        return [self.date, self.o, self.h, self.l, self.c, self.v]

    @classmethod
    def from_list(cls, x: list) -> "DailyBar":
        return cls(str(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))


def _daily_path(symbol: str) -> Path:
    safe = symbol.replace("=", "").replace("^", "")
    return config_dir() / "analysis" / f"{safe}_daily.json"


def fetch_daily(symbol: str, *, range_: str = "1y", timeout: float = 12.0) -> List[DailyBar]:
    """Fetch daily OHLCV bars for ``symbol`` from Yahoo (newest last)."""
    url = _CHART_URL.format(symbol=symbol) + f"?interval=1d&range={range_}"
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Yahoo daily fetch failed ({resp.status_code}) for {symbol}")
    result = resp.json()["chart"]["result"][0]
    ts = result.get("timestamp") or []
    q = result["indicators"]["quote"][0]
    opens, highs = q.get("open", []), q.get("high", [])
    lows, closes, vols = q.get("low", []), q.get("close", []), q.get("volume", [])
    bars: List[DailyBar] = []
    for i, t in enumerate(ts):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, l, c):
            continue  # Yahoo pads gaps with nulls
        date = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
        bars.append(DailyBar(date, float(o), float(h), float(l), float(c),
                             float(vols[i] or 0.0)))
    return bars


def refresh(symbol: str, *, range_: str = "1y") -> List[DailyBar]:
    """Fetch and cache daily bars; on failure fall back to the cache."""
    try:
        bars = fetch_daily(symbol, range_=range_)
        path = _daily_path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "symbol": symbol,
            "fetched_at": _now_iso(),
            "bars": [b.to_list() for b in bars],
        }), encoding="utf-8")
        return bars
    except Exception:
        return load_daily(symbol)


def load_daily(symbol: str) -> List[DailyBar]:
    path = _daily_path(symbol)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [DailyBar.from_list(x) for x in data.get("bars", [])]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------- features
def by_date(bars: List[DailyBar]) -> Dict[str, DailyBar]:
    return {b.date: b for b in bars}


def prior_value(bars_by_date: Dict[str, DailyBar], date: str) -> Optional[DailyBar]:
    """The most recent bar strictly before ``date`` (the info known pre-open)."""
    earlier = [d for d in bars_by_date if d < date]
    if not earlier:
        return None
    return bars_by_date[max(earlier)]


def atr(bars: List[DailyBar], end_date: str, n: int = 14) -> Optional[float]:
    """Average daily range over the n sessions ending strictly before ``end_date``."""
    prior = [b for b in bars if b.date < end_date][-n:]
    if not prior:
        return None
    return statistics.fmean(b.true_range for b in prior)
