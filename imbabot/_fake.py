"""An in-memory fake of ProjectXClient for offline testing.

Implements the same duck-typed surface BotEngine uses, records placed/cancelled
orders, and can simulate a fill so the One-Trade OCO path is verifiable without a
live account or network.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import Account, Contract, OrderResult, OrderSide, OrderType, StraddleLeg


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
        # Naked STOP entry — no attached brackets, mirroring the real client.
        # The straddle rests exactly two orders; SL/TP are platform-managed
        # Position Brackets on TopStep, not per-order brackets.
        res = self.place_order(
            account_id=account_id, contract_id=contract_id,
            order_type=OrderType.STOP, side=leg.side, size=leg.size,
            stop_price=leg.stop_price, custom_tag=leg.custom_tag,
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
