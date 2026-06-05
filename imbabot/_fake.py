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
        res = self.place_order(
            account_id=account_id, contract_id=contract_id,
            order_type=OrderType.STOP, side=leg.side, size=leg.size,
            stop_price=leg.stop_price, custom_tag=leg.custom_tag,
            stop_loss_ticks=leg.stop_loss_ticks, take_profit_ticks=leg.take_profit_ticks,
        )
        leg.order_id = res.order_id
        return res

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
    def simulate_fill(self, contract_id: str, net: int) -> None:
        """Pretend an entry filled: open a position of `net` (>0 long, <0 short)."""
        with self._lock:
            self.positions = [{"contractId": contract_id, "netPos": net}]
