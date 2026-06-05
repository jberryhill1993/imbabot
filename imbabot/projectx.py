"""Thin client for the ProjectX Gateway API (TopstepX).

Endpoints verified against https://gateway.docs.projectx.com :

    POST /api/Auth/loginKey        {userName, apiKey}            -> {token}
    POST /api/Auth/validate                                     -> {success}
    POST /api/Account/search       {onlyActiveAccounts}         -> {accounts[]}
    POST /api/Contract/search      {searchText, live}           -> {contracts[]}
    POST /api/History/retrieveBars {contractId, live, start...} -> {bars[]}
    POST /api/Order/place          {accountId, contractId, ...} -> {orderId}
    POST /api/Order/cancel         {accountId, orderId}         -> {success}
    POST /api/Order/searchOpen     {accountId}                  -> {orders[]}
    POST /api/Position/searchOpen  {accountId}                  -> {positions[]}

The token is a JWT, valid ~24h, sent as ``Authorization: Bearer <token>``.
All calls are POST with a JSON body. Different ProjectX-powered firms use
different hostnames; for Topstep the default below is correct.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from .models import (
    Account,
    Bar,
    Contract,
    OrderResult,
    OrderSide,
    OrderType,
    StraddleLeg,
)

DEFAULT_BASE_URL = "https://api.topstepx.com"


class ProjectXError(RuntimeError):
    """Raised when the API returns success=false or a transport error occurs."""

    def __init__(self, message: str, error_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class ProjectXClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()
        self._token: Optional[str] = None
        self._username: Optional[str] = None

    # ------------------------------------------------------------------ core
    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def _post(self, path: str, body: Dict[str, Any], auth: bool = True) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            if not self._token:
                raise ProjectXError("Not authenticated. Call authenticate() first.")
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = self._session.post(url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProjectXError(f"Network error calling {path}: {exc}") from exc

        if resp.status_code == 401:
            raise ProjectXError("Unauthorized (401) — token expired or invalid.", 401)
        if resp.status_code >= 400:
            raise ProjectXError(f"HTTP {resp.status_code} from {path}: {resp.text[:300]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ProjectXError(f"Non-JSON response from {path}: {resp.text[:200]}") from exc

        # Most endpoints return {success, errorCode, errorMessage, ...}
        if isinstance(data, dict) and data.get("success") is False:
            raise ProjectXError(
                data.get("errorMessage") or f"{path} failed (code {data.get('errorCode')})",
                data.get("errorCode"),
            )
        return data

    # ------------------------------------------------------------------ auth
    def authenticate(self, username: str, api_key: str) -> str:
        """Exchange username + API key for a session token."""
        data = self._post(
            "/api/Auth/loginKey",
            {"userName": username, "apiKey": api_key},
            auth=False,
        )
        token = data.get("token")
        if not token:
            raise ProjectXError("Authentication succeeded but no token was returned.")
        self._token = token
        self._username = username
        return token

    def validate(self) -> bool:
        """Return True if the current token is still valid."""
        if not self._token:
            return False
        try:
            self._post("/api/Auth/validate", {})
            return True
        except ProjectXError:
            return False

    # -------------------------------------------------------------- accounts
    def search_accounts(self, only_active: bool = True) -> List[Account]:
        data = self._post("/api/Account/search", {"onlyActiveAccounts": only_active})
        return [Account.from_api(a) for a in data.get("accounts", [])]

    # ------------------------------------------------------------- contracts
    def search_contracts(self, search_text: str, live: bool = False) -> List[Contract]:
        data = self._post(
            "/api/Contract/search", {"searchText": search_text, "live": live}
        )
        return [Contract.from_api(c) for c in data.get("contracts", [])]

    def resolve_contract(self, symbol: str, live: bool = False) -> Contract:
        """Resolve a symbol (e.g. 'MNQ') to the active front-month contract.

        Prefers an active contract whose name/symbol matches; falls back to the
        first active result, then the first result.
        """
        results = self.search_contracts(symbol, live=live)
        if not results:
            raise ProjectXError(f"No contracts found for '{symbol}'.")
        sym = symbol.upper()
        active = [c for c in results if c.active]
        for pool in (active, results):
            for c in pool:
                if c.name.upper().startswith(sym) or c.symbol_id.upper().endswith(sym):
                    return c
        return (active or results)[0]

    # ----------------------------------------------------------- market data
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
        """Retrieve OHLCV bars. unit: 1=Sec 2=Min 3=Hour 4=Day 5=Week 6=Month."""
        end_time = end_time or datetime.now(timezone.utc)
        if start_time is None:
            start_time = end_time - timedelta(minutes=max(limit * unit_number * 2, 30))
        body = {
            "contractId": contract_id,
            "live": live,
            "startTime": _iso(start_time),
            "endTime": _iso(end_time),
            "unit": unit,
            "unitNumber": unit_number,
            "limit": limit,
            "includePartialBar": include_partial_bar,
        }
        data = self._post("/api/History/retrieveBars", body)
        return [Bar.from_api(b) for b in data.get("bars", [])]

    def last_price(self, contract_id: str, live: bool = False) -> float:
        """Best-effort latest traded price via the most recent (partial) 1-min bar."""
        bars = self.retrieve_bars(
            contract_id, unit=2, unit_number=1, limit=2, live=live,
            include_partial_bar=True,
        )
        if not bars:
            raise ProjectXError("No bars returned; cannot determine last price.")
        return bars[-1].c

    def session_range(
        self, contract_id: str, start: datetime, end: datetime, live: bool = False
    ) -> Optional[Dict[str, float]]:
        """High/low across [start, end] (e.g. the overnight range). None if no data."""
        bars = self.retrieve_bars(
            contract_id, unit=2, unit_number=5, limit=5000, live=live,
            start_time=start, end_time=end, include_partial_bar=False,
        )
        if not bars:
            return None
        return {"high": max(b.h for b in bars), "low": min(b.l for b in bars)}

    # --------------------------------------------------------------- orders
    def place_order(
        self,
        *,
        account_id: int,
        contract_id: str,
        order_type: OrderType,
        side: OrderSide,
        size: int,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        trail_price: Optional[float] = None,
        custom_tag: Optional[str] = None,
        stop_loss_ticks: Optional[int] = None,
        take_profit_ticks: Optional[int] = None,
    ) -> OrderResult:
        body: Dict[str, Any] = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": int(order_type),
            "side": int(side),
            "size": int(size),
        }
        if limit_price is not None:
            body["limitPrice"] = limit_price
        if stop_price is not None:
            body["stopPrice"] = stop_price
        if trail_price is not None:
            body["trailPrice"] = trail_price
        if custom_tag is not None:
            body["customTag"] = custom_tag
        if stop_loss_ticks is not None:
            # type 4 == Stop for the protective leg
            body["stopLossBracket"] = {"ticks": int(stop_loss_ticks), "type": int(OrderType.STOP)}
        if take_profit_ticks is not None:
            # type 1 == Limit for the target leg
            body["takeProfitBracket"] = {"ticks": int(take_profit_ticks), "type": int(OrderType.LIMIT)}
        data = self._post("/api/Order/place", body)
        return OrderResult.from_api(data)

    def place_straddle_leg(
        self, account_id: int, contract_id: str, leg: StraddleLeg
    ) -> OrderResult:
        """Place one straddle leg as a STOP entry with attached brackets."""
        result = self.place_order(
            account_id=account_id,
            contract_id=contract_id,
            order_type=OrderType.STOP,
            side=leg.side,
            size=leg.size,
            stop_price=leg.stop_price,
            custom_tag=leg.custom_tag,
            stop_loss_ticks=leg.stop_loss_ticks,
            take_profit_ticks=leg.take_profit_ticks,
        )
        leg.order_id = result.order_id
        return result

    def cancel_order(self, account_id: int, order_id: int) -> bool:
        self._post("/api/Order/cancel", {"accountId": account_id, "orderId": order_id})
        return True

    def search_open_orders(self, account_id: int) -> List[Dict[str, Any]]:
        data = self._post("/api/Order/searchOpen", {"accountId": account_id})
        return data.get("orders", [])

    def search_open_positions(self, account_id: int) -> List[Dict[str, Any]]:
        data = self._post("/api/Position/searchOpen", {"accountId": account_id})
        return data.get("positions", [])


def _iso(dt: datetime) -> str:
    """ISO-8601 in UTC with a trailing Z, which the API accepts."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
