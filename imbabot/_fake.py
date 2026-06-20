"""An in-memory fake of ProjectXClient for offline testing.

Implements the same duck-typed surface BotEngine uses, records placed/cancelled
orders, and can simulate a fill so the One-Trade OCO path is verifiable without a
live account or network.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .models import Account, Bar, Contract, OrderResult, OrderSide, OrderType, StraddleLeg


def _parse_iso(t: str) -> datetime:
    """Parse an API/Bar ISO timestamp (trailing 'Z' allowed) to aware UTC."""
    s = t.replace("Z", "+00:00") if t.endswith("Z") else t
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, last: float = 21000.0, tick_size: float = 0.25) -> None:
        self._last = last
        self._tick = tick_size
        self._next_id = 1000
        self.orders: Dict[int, Dict[str, Any]] = {}      # id -> order dict (working)
        self.placed: List[Dict[str, Any]] = []           # full history
        self.cancelled: List[int] = []
        self.positions: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.authenticated = False
        self.reject_sides: set = set()  # OrderSides whose entry placement is rejected
        self.modified: List[Dict[str, Any]] = []  # record of modify_order calls
        # --- historical-bar simulation (analyzer/backtest tests) ---
        # If `scripted_bars` is set, retrieve_bars returns those within the window.
        # Otherwise it synthesizes a flat 1-min series, but returns nothing for
        # windows older than `history_since` (mimics finite TopStep retention).
        self.scripted_bars: Optional[List[Bar]] = None
        self.history_since: Optional[datetime] = None

    # --- auth / account / contract ---
    def authenticate(self, username: str, api_key: str) -> str:
        self.authenticated = True
        return "fake-token"

    def search_accounts(self, only_active: bool = True) -> List[Account]:
        return [Account(id=42, name="FAKE-COMBINE", can_trade=True, is_visible=True)]

    def resolve_contract(self, symbol: str, live: bool = False) -> Contract:
        return Contract(
            id=f"CON.F.US.{symbol.upper()}.Z25",
            name=symbol.upper(),
            description=f"{symbol.upper()} (fake)",
            tick_size=self._tick,
            tick_value=0.5,
            active=True,
            symbol_id=f"F.US.{symbol.upper()}",
        )

    # --- market data ---
    def last_price(self, contract_id: str, live: bool = False) -> float:
        return self._last

    def session_range(self, contract_id, start, end, live: bool = False):
        return {"high": self._last + 30, "low": self._last - 25}

    def retrieve_bars(
        self,
        contract_id: str,
        *,
        unit: int = 2,
        unit_number: int = 1,
        limit: int = 5,
        live: bool = False,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        include_partial_bar: bool = True,
    ) -> List[Bar]:
        """Offline stand-in for ProjectXClient.retrieve_bars (newest-first).

        Two modes: replay ``scripted_bars`` within the window, or synthesize a flat
        1-minute series — empty if the window predates ``history_since`` so the
        depth probe and retention behavior are testable without a network.
        """
        end_time = end_time or datetime.now(timezone.utc)
        if start_time is None:
            start_time = end_time - timedelta(minutes=max(limit * unit_number * 2, 30))
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)

        if self.scripted_bars is not None:
            sel = [b for b in self.scripted_bars
                   if start_time <= _parse_iso(b.t) < end_time]
            sel.sort(key=lambda b: b.t, reverse=True)  # newest-first, like the API
            return sel[:limit] if limit else sel

        if self.history_since is not None and start_time < self.history_since:
            return []  # outside the simulated retention window

        # Synthesize a deterministic flat 1-min series across the window.
        step = timedelta(minutes=unit_number) if unit == 2 else timedelta(minutes=1)
        bars: List[Bar] = []
        t = start_time
        while t < end_time and len(bars) < (limit or 100000):
            iso = t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            bars.append(Bar(t=iso, o=self._last, h=self._last, l=self._last,
                            c=self._last, v=0.0))
            t += step
        bars.sort(key=lambda b: b.t, reverse=True)  # newest-first
        return bars

    # --- orders ---
    def place_order(self, **kw) -> OrderResult:
        with self._lock:
            oid = self._next_id
            self._next_id += 1
            rec = dict(kw)
            rec["id"] = oid
            self.orders[oid] = rec
            self.placed.append(rec)
        return OrderResult(order_id=oid, success=True, error_code=0, error_message=None)

    def place_straddle_leg(self, account_id: int, contract_id: str, leg: StraddleLeg) -> OrderResult:
        if leg.side in self.reject_sides:
            return OrderResult(order_id=None, success=False, error_code=99,
                               error_message="rejected (fake)")
        # Naked by default; attach a SIGNED bracket only when the leg opts in
        # (non-zero ticks), mirroring the real client.
        sl_ticks = tp_ticks = None
        if leg.stop_loss_ticks:
            sl_ticks = -leg.stop_loss_ticks if leg.side == OrderSide.BUY else leg.stop_loss_ticks
        if leg.take_profit_ticks:
            tp_ticks = leg.take_profit_ticks if leg.side == OrderSide.BUY else -leg.take_profit_ticks
        res = self.place_order(
            account_id=account_id, contract_id=contract_id,
            order_type=OrderType.STOP, side=leg.side, size=leg.size,
            stop_price=leg.stop_price, custom_tag=leg.custom_tag,
            stop_loss_ticks=sl_ticks, take_profit_ticks=tp_ticks,
        )
        leg.order_id = res.order_id
        return res

    def modify_order(self, account_id: int, order_id: int, *, stop_price=None,
                     limit_price=None, size=None, trail_price=None) -> bool:
        with self._lock:
            rec = self.orders.get(order_id)
            if rec is None:
                return False
            if stop_price is not None:
                rec["stop_price"] = rec["stopPrice"] = stop_price
            if limit_price is not None:
                rec["limit_price"] = rec["limitPrice"] = limit_price
            if size is not None:
                rec["size"] = int(size)
            self.modified.append({"order_id": order_id, "stop_price": stop_price,
                                  "limit_price": limit_price, "size": size})
        return True

    def cancel_order(self, account_id: int, order_id: int) -> bool:
        with self._lock:
            self.orders.pop(order_id, None)
            self.cancelled.append(order_id)
        return True

    def search_open_orders(self, account_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.orders.values())

    def search_open_positions(self, account_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.positions)

    # --- test helpers ---
    def simulate_fill(self, contract_id: str, net: int, avg_price: float = None) -> None:
        """Pretend an entry filled: open a position of `net` (>0 long, <0 short).

        Mirrors the real ProjectX payload (type: 1=Long 2=Short, unsigned size,
        averagePrice) and removes the filled entry from the open-order book, as
        live does.
        """
        with self._lock:
            self.positions = [{
                "contractId": contract_id,
                "type": 1 if net > 0 else 2,
                "size": abs(net),
                "averagePrice": self._last if avg_price is None else avg_price,
            }]
            filled_side = OrderSide.BUY if net > 0 else OrderSide.SELL
            for oid, rec in list(self.orders.items()):
                if rec.get("side") == filled_side:
                    self.orders.pop(oid)
