"""Shared data types for the spread analyzer.

A ``DayRecord`` is the analyzer's unit of history: everything the backtest needs
about one trading day's open, distilled from raw 1-minute bars. These are cached
to disk as JSON (see ``csv_history``) so the heavy ingest runs once.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class OpenBar:
    """One 1-minute bar of the opening window, offset from the 09:30 ET open."""

    minute: int          # 0 = the 09:30 bar, 1 = 09:31, ...
    o: float
    h: float
    l: float
    c: float
    v: float

    def to_list(self) -> list:
        return [self.minute, self.o, self.h, self.l, self.c, self.v]

    @classmethod
    def from_list(cls, x: list) -> "OpenBar":
        return cls(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))


@dataclass
class DayRecord:
    """Distilled open-behavior for one trading day (ET).

    ``ref_price`` is the opening print (the 09:30 bar's open) — the price the bot
    would straddle. ``open_bars`` is the first N minutes after the open, used to
    simulate which entry triggers and whether it reverses (whipsaw). Overnight
    range and the prior RTH close feed the volatility/gap features.
    """

    date: str                         # ISO date of the ET cash session (YYYY-MM-DD)
    ref_price: float
    open_bars: List[OpenBar]
    overnight_high: Optional[float] = None
    overnight_low: Optional[float] = None
    prior_close: Optional[float] = None

    @property
    def overnight_range(self) -> Optional[float]:
        if self.overnight_high is None or self.overnight_low is None:
            return None
        return self.overnight_high - self.overnight_low

    @property
    def gap(self) -> Optional[float]:
        """Open minus prior RTH close (signed); the overnight gap."""
        if self.prior_close is None:
            return None
        return self.ref_price - self.prior_close

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "ref_price": self.ref_price,
            "open_bars": [b.to_list() for b in self.open_bars],
            "overnight_high": self.overnight_high,
            "overnight_low": self.overnight_low,
            "prior_close": self.prior_close,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DayRecord":
        return cls(
            date=d["date"],
            ref_price=float(d["ref_price"]),
            open_bars=[OpenBar.from_list(x) for x in d.get("open_bars", [])],
            overnight_high=d.get("overnight_high"),
            overnight_low=d.get("overnight_low"),
            prior_close=d.get("prior_close"),
        )
