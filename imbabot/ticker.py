"""Lightweight live-quote feed for the header ticker.

Polls Yahoo Finance's public chart endpoint (no auth, no API key) for a futures
symbol — by default ``NQ=F``, the E-mini Nasdaq-100 front month — so the GUI can
show a live price the moment it opens, independent of the TopstepX API
connection. It is deliberately best-effort: any network/parse failure returns
``None`` and the UI just shows a dash. Nothing here can place an order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

# E-mini Nasdaq-100 front month. NQ and the micro MNQ the bot trades track the
# same index, so this price mirrors what you'll see on the chart.
DEFAULT_TICKER_SYMBOL = "NQ=F"

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Imbabot/1.0)"}


@dataclass
class Quote:
    symbol: str            # display symbol, e.g. "NQ"
    price: float           # latest traded price
    prev_close: float      # prior session close (for the change figure)
    market_state: str = "" # e.g. REGULAR / CLOSED / PRE / POST (may be empty)

    @property
    def change(self) -> float:
        return self.price - self.prev_close

    @property
    def change_pct(self) -> float:
        if self.prev_close:
            return (self.change / self.prev_close) * 100.0
        return 0.0


def _display_symbol(yahoo_symbol: str) -> str:
    """Strip Yahoo's futures suffix: 'NQ=F' -> 'NQ'."""
    return yahoo_symbol.split("=", 1)[0].upper()


def fetch_quote(symbol: str = DEFAULT_TICKER_SYMBOL, timeout: float = 8.0) -> Optional[Quote]:
    """Return a live :class:`Quote` for ``symbol``, or ``None`` on any failure."""
    try:
        resp = requests.get(
            _CHART_URL.format(symbol=symbol), headers=_HEADERS, timeout=timeout
        )
        if resp.status_code != 200:
            return None
        meta = resp.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose", meta.get("previousClose"))
        if price is None or prev is None:
            return None
        return Quote(
            symbol=_display_symbol(symbol),
            price=float(price),
            prev_close=float(prev),
            market_state=str(meta.get("marketState") or ""),
        )
    except Exception:
        return None
