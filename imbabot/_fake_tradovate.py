"""An in-memory fake of TradovateClient for offline testing.

Counterpart of ``_fake.FakeClient`` (the ProjectX fake), but with the
Tradovate-shaped surfaces the engine must digest correctly:
- positions carry a SIGNED ``netPos`` and NO ``type`` key,
- open orders are ``id``-keyed with an ``ordStatus``,
- bracketed straddle legs record an OSO-style placement (absolute bracket
  prices) instead of signed tick offsets.

Used by the selftest to run the engine's full fire -> OCO-cancel -> panic flow
against the Tradovate translation layer without a network.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .models import (Account, Contract, OrderResult, OrderSide, OrderType,
                     StraddleLeg)
from .tradovate.client import ACTION_MAP, ORDER_TYPE_MAP, bracket_prices, _exit_action


class FakeTradovate:
    def __init__(self, last: float = 21000.0, tick_size: float = 0.25) -> None:
        self._last = last
        self._tick = tick_size
        self._next_id = 2000
        self.orders: Dict[int, Dict[str, Any]] = {}     # id -> working order
        self.placed: List[Dict[str, Any]] = []          # full history (OSO bodies)
        self.cancelled: List[int] = []
        self.liquidated: List[str] = []
        self.positions: List[Dict[str, Any]] = []       # Tradovate-shaped rows
        self.statuses: Dict[int, str] = {}              # id -> ordStatus (venue view)
        self._lock = threading.Lock()
        self.authenticated = False
        self.session_secrets: Dict[str, Any] = {}

    # --- auth / account / contract ---
    def authenticate(self, username: str, api_key: str) -> str:
        self.authenticated = True
        return "fake-tdv-token"

    def validate(self) -> bool:
        return self.authenticated

    def search_accounts(self, only_active: bool = True) -> List[Account]:
        return [Account(id=7001, name="TDV-DEMO", can_trade=True, is_visible=True)]

    def resolve_contract(self, symbol: str, live: bool = False) -> Contract:
        return Contract(
            id="901",
            name=f"{symbol.upper()}U6",
            description=f"{symbol.upper()} front month (fake Tradovate)",
            tick_size=self._tick,
            tick_value=0.5,
            active=True,
            symbol_id=symbol.upper(),
        )

    # --- market data ---
    def last_price(self, contract_id: str, live: bool = False) -> float:
        return self._last

    def session_range(self, contract_id, start, end, live: bool = False):
        raise RuntimeError("session_range not supported on Tradovate (0.2.5)")

    def retrieve_bars(self, contract_id: str, **kw) -> list:
        raise RuntimeError("retrieve_bars not supported on Tradovate (0.2.5)")

    # --- orders ---
    def place_order(self, **kw) -> OrderResult:
        with self._lock:
            oid = self._next_id
            self._next_id += 1
            rec = dict(kw)
            rec["id"] = oid
            rec["ordStatus"] = "Working"
            self.orders[oid] = rec
            self.placed.append(rec)
            self.statuses[oid] = "Working"
        return OrderResult(order_id=oid, success=True, error_code=0, error_message=None)

    def place_straddle_leg(self, account_id: int, contract_id: str,
                           leg: StraddleLeg) -> OrderResult:
        # Mirror TradovateClient: absolute OSO bracket prices from tick offsets.
        sl, tp = bracket_prices(leg.side, leg.stop_price, leg.stop_loss_ticks,
                                leg.take_profit_ticks, self._tick)
        entry_type = OrderType.STOP_LIMIT if leg.limit_price is not None else OrderType.STOP
        body: Dict[str, Any] = {
            "action": ACTION_MAP[leg.side],
            "side": leg.side,
            "symbol": "MNQU6",
            "contractId": contract_id,
            "orderQty": leg.size,
            "orderType": ORDER_TYPE_MAP[entry_type],
            "stopPrice": leg.stop_price,
            "isAutomated": True,
        }
        if sl is not None:
            body["bracket1"] = {"action": _exit_action(leg.side),
                                "orderType": "Stop", "stopPrice": sl}
        if tp is not None:
            key = "bracket2" if "bracket1" in body else "bracket1"
            body[key] = {"action": _exit_action(leg.side),
                         "orderType": "Limit", "price": tp}
        res = self.place_order(**body)
        leg.order_id = res.order_id
        return res

    def modify_order(self, account_id: int, order_id: int, *, stop_price=None,
                     limit_price=None, size=None, trail_price=None) -> bool:
        with self._lock:
            rec = self.orders.get(order_id)
            if rec is None:
                return False
            if stop_price is not None:
                rec["stopPrice"] = stop_price
            if limit_price is not None:
                rec["price"] = limit_price
            if size is not None:
                rec["orderQty"] = int(size)
        return True

    def cancel_order(self, account_id: int, order_id: int) -> bool:
        with self._lock:
            self.orders.pop(order_id, None)
            self.cancelled.append(order_id)
            self.statuses[order_id] = "Canceled"
        return True

    def entry_status(self, order_id: int):
        """Mirror TradovateClient.entry_status (venue ordStatus or None)."""
        return self.statuses.get(int(order_id))

    def liquidate_position(self, account_id: int, contract_id: Any) -> OrderResult:
        with self._lock:
            self.liquidated.append(str(contract_id))
            self.positions = [p for p in self.positions
                              if str(p.get("contractId")) != str(contract_id)]
        return OrderResult(order_id=self._next_id, success=True,
                           error_code=0, error_message=None)

    def search_open_orders(self, account_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(o) for o in self.orders.values()
                    if o.get("ordStatus") == "Working"]

    def search_open_positions(self, account_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self.positions]

    # --- test helpers ---
    def simulate_fill(self, contract_id: str, net: int,
                      avg_price: Optional[float] = None) -> None:
        """A Tradovate fill: SIGNED netPos (no type key), and the filled entry
        leaves the working set — exactly what the live user-sync cache yields."""
        with self._lock:
            self.positions = [{
                "contractId": str(contract_id),
                "netPos": int(net),
                "netPrice": self._last if avg_price is None else avg_price,
            }]
            filled_side = OrderSide.BUY if net > 0 else OrderSide.SELL
            for oid, rec in list(self.orders.items()):
                if rec.get("side") == filled_side:
                    self.orders.pop(oid)
                    self.statuses[oid] = "Filled"

    def simulate_reject(self, order_id: int) -> None:
        """The venue rejects an entry ASYNC (accepted by REST, then Rejected) —
        the Tue 2026-07-21 live scenario."""
        with self._lock:
            self.orders.pop(int(order_id), None)
            self.statuses[int(order_id)] = "Rejected"
