"""BrokerAdapter — the duck-typed contract every broker client must satisfy.

BotEngine never imports a concrete client type; it calls this surface on whatever
object it is given (ProjectXClient, TradovateClient, or the offline fakes). This
Protocol writes that implicit contract down in one place and lets the selftest
pin it: if the engine ever grows a new client call, conformance checks fail
loudly instead of one backend silently breaking.

Notes:
- ``@runtime_checkable`` isinstance() checks verify method PRESENCE only, not
  signatures — the selftest's behavioral checks cover semantics.
- ``validate()`` is optional on purpose (the engine probes it with getattr);
  clients without it simply re-authenticate via the engine's fallback.
- No existing class inherits from this. Conformance is structural.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .models import Account, Bar, Contract, OrderResult, StraddleLeg


@runtime_checkable
class BrokerAdapter(Protocol):
    """Everything BotEngine calls on a broker client.

    Return-shape conventions the engine relies on (see engine.py helpers):
    - ``search_open_orders`` dicts must carry the order id under ``"id"``
      (or ``orderId``/``order_id`` — ``_order_id`` tries all three).
    - ``search_open_positions`` dicts either carry a SIGNED net under
      ``netPos``/``netQuantity`` (no ``type`` key), or ProjectX-style
      ``type`` (1=Long/2=Short) + unsigned ``size`` — ``_net_position_value``
      handles both.
    """

    # --- auth / account / contract ---
    def authenticate(self, username: str, api_key: str) -> str: ...

    def search_accounts(self, only_active: bool = True) -> List[Account]: ...

    def resolve_contract(self, symbol: str, live: bool = False) -> Contract: ...

    # --- market data ---
    def last_price(self, contract_id: str, live: bool = False) -> float: ...

    def session_range(
        self, contract_id: str, start: datetime, end: datetime, live: bool = False
    ) -> Optional[Dict[str, float]]: ...

    def retrieve_bars(self, contract_id: str, **kwargs: Any) -> List[Bar]: ...

    # --- orders / positions ---
    def place_order(self, **kwargs: Any) -> OrderResult: ...

    def place_straddle_leg(
        self, account_id: int, contract_id: str, leg: StraddleLeg
    ) -> OrderResult: ...

    def modify_order(self, account_id: int, order_id: int, **kwargs: Any) -> bool: ...

    def cancel_order(self, account_id: int, order_id: int) -> bool: ...

    def search_open_orders(self, account_id: int) -> List[Dict[str, Any]]: ...

    def search_open_positions(self, account_id: int) -> List[Dict[str, Any]]: ...
