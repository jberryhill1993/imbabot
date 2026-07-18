"""TradovateClient — the Tradovate implementation of the BrokerAdapter surface.

Same duck-typed contract BotEngine already speaks (see imbabot/broker.py), so the
engine, strategy, risk and OCO-monitor code run unchanged. Differences from
ProjectX, handled entirely inside this module:

- Auth is username/password/API-key (cid+sec) with ~90-min tokens
  (tradovate.auth.TokenManager handles acquisition/renewal/penalties).
- Brackets are NATIVE server-side OSO (POST /order/placeoso, absolute prices):
  they survive a bot crash or disconnect. The two-entry cancel race stays with
  the engine's battle-tested client-side OCO monitor.
- Fills/orders/positions arrive over the user-sync WebSocket; quotes over the
  market-data WebSocket (tradovate/ws.py). ``search_open_orders``/
  ``search_open_positions`` read those caches, with a REST fallback when the
  socket is unhealthy, so the 0.5s OCO poll never goes blind.
- Live endpoint is hard-gated by tradovate/safety.py (source-level constant).

Every order body carries ``isAutomated: true`` (required by Tradovate for
non-manual orders). Secrets are never logged.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from ..models import (Account, Bar, Contract, OrderResult, OrderSide, OrderType,
                      StraddleLeg, round_to_tick)
from . import safety
from .auth import TokenManager, TradovateCredentials, _default_http

DEMO_BASE_URL = "https://demo.tradovateapi.com/v1"
LIVE_BASE_URL = "https://live.tradovateapi.com/v1"

# ProjectX integer enums -> Tradovate order-type strings.
ORDER_TYPE_MAP: Dict[OrderType, str] = {
    OrderType.LIMIT: "Limit",
    OrderType.MARKET: "Market",
    OrderType.STOP_LIMIT: "StopLimit",
    OrderType.STOP: "Stop",
    OrderType.TRAILING_STOP: "TrailingStop",
}
ACTION_MAP: Dict[OrderSide, str] = {OrderSide.BUY: "Buy", OrderSide.SELL: "Sell"}

# Order states that count as "resting on the book" for the OCO monitor.
WORKING_STATUSES = {"Working", "Suspended", "PendingNew", "PendingReplace",
                    "PendingCancel", "Received"}

# Tick-size sanity table for the contracts this bot actually trades. Used to
# cross-check what the API reports (a wrong tick size would corrupt every
# bracket price).
_KNOWN_TICKS = {"MNQ": (0.25, 0.5), "NQ": (0.25, 5.0),
                "MES": (0.25, 1.25), "ES": (0.25, 12.5)}


class TradovateError(RuntimeError):
    """REST call failed (network, HTTP, or failureReason from the venue)."""


def bracket_prices(side: OrderSide, entry_price: float, sl_ticks: int,
                   tp_ticks: int, tick_size: float
                   ) -> Tuple[Optional[float], Optional[float]]:
    """Absolute, tick-snapped SL/TP prices for an OSO, anchored at the entry.

    BUY entry: SL below (protective sell stop), TP above (sell limit).
    SELL entry mirrors. A zero tick distance means "no bracket on that side".
    """
    sl = tp = None
    sign = 1 if side == OrderSide.BUY else -1
    if sl_ticks:
        sl = round_to_tick(entry_price - sign * sl_ticks * tick_size, tick_size)
    if tp_ticks:
        tp = round_to_tick(entry_price + sign * tp_ticks * tick_size, tick_size)
    return sl, tp


def _exit_action(side: OrderSide) -> str:
    """Brackets close the position -> the opposite action of the entry."""
    return "Sell" if side == OrderSide.BUY else "Buy"


class TradovateClient:
    """Duck-typed broker client for Tradovate (REST + WS).

    ``http`` is injectable for offline tests (same signature as
    tradovate.auth._default_http). ``enable_ws=False`` skips the sockets
    (offline tests and the pure-REST demo probe).
    """

    def __init__(
        self,
        settings: Any,
        log: Optional[Callable[..., None]] = None,
        *,
        http: Optional[Callable[..., Tuple[int, dict]]] = None,
        enable_ws: bool = True,
        timeout: float = 15.0,
    ) -> None:
        env = getattr(settings, "tdv_environment", "demo") or "demo"
        safety.assert_live_allowed(env)          # gate #1: cannot construct live
        self._env = env
        self._settings = settings
        self._log = log or (lambda msg, level="info": None)
        self._http = http or _default_http
        self._enable_ws = enable_ws
        self._timeout = timeout
        self._lock = threading.Lock()
        self._tokens: Optional[TokenManager] = None
        self.authenticated = False
        self._account_specs: Dict[int, str] = {}   # accountId -> accountSpec (name)
        self._contracts: Dict[str, Dict[str, Any]] = {}  # our str id -> {id, name, tick}
        self._user_ws = None    # tradovate.ws.TdvSocket (user sync)
        self._md_ws = None      # tradovate.ws.TdvSocket (market data)
        self._kill_reason: Optional[str] = None    # daily-loss kill switch (ws.py sets)
        # Transient credentials for THIS session only (UI passes cid/sec here when
        # the user declines "remember" — nothing touches the keyring/disk).
        self.session_secrets: Dict[str, Any] = {}

    # ------------------------------------------------------------ plumbing
    def _base_url(self) -> str:
        safety.assert_live_allowed(self._env)     # gate #2: re-check at every use
        return LIVE_BASE_URL if self._env == "live" else DEMO_BASE_URL

    def _request(self, method: str, path: str, *, body: Optional[dict] = None,
                 params: Optional[dict] = None) -> Any:
        if self._tokens is None:
            raise TradovateError("Not authenticated (connect first).")
        url = self._base_url() + path
        if params:
            url += "?" + urlencode(params)
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._tokens.access_token()}",
                       "Content-Type": "application/json"}
            status, data = self._http(method, url, body, headers, self._timeout)
            if status == 401 and attempt == 0:
                self._tokens.invalidate()          # stale token -> re-auth once
                continue
            if status != 200:
                detail = ""
                if isinstance(data, dict):
                    detail = data.get("errorText") or data.get("failureText") or ""
                raise TradovateError(f"{method} {path} -> HTTP {status} {detail}".strip())
            return data
        raise TradovateError(f"{method} {path} failed after re-auth.")

    @staticmethod
    def _order_result(data: dict) -> OrderResult:
        """Map a placeorder/placeoso/liquidate response onto OrderResult."""
        reason = data.get("failureReason")
        oid = data.get("orderId")
        ok = oid is not None and reason in (None, "Success")
        return OrderResult(
            order_id=int(oid) if oid is not None else None,
            success=ok,
            error_code=0 if ok else -1,
            error_message=None if ok else (data.get("failureText") or reason or "rejected"),
        )

    def _contract_info(self, contract_id: Any) -> Dict[str, Any]:
        info = self._contracts.get(str(contract_id))
        if info is None:
            raise TradovateError(
                f"Unknown contract id {contract_id!r} — resolve_contract() first.")
        return info

    def _guard_order(self, account_id: int, size: int) -> None:
        """Kill-switch check + OPTIONAL venue size cap.

        With safety.MAX_POSITION_SIZE = None (the shipped default), sizing is
        governed by the same guards as the TopStep path: Settings.max_contracts
        via RiskGuard at the engine level. The cap here is a deliberate opt-in
        for live (see safety.py). NOTE: the cap is per-ORDER only — a projected-
        position check was removed because it cannot tell risk-reducing orders
        (flatten) from risk-adding ones and would refuse a full-size flatten.
        """
        if self._kill_reason:
            raise safety.SafetyError(
                f"Kill switch is tripped ({self._kill_reason}) — order refused.")
        cap = safety.MAX_POSITION_SIZE
        if cap is not None and size > cap:
            raise safety.SafetyError(
                f"Order size {size} exceeds the hard Tradovate cap "
                f"MAX_POSITION_SIZE={cap} (safety.py).")

    # --------------------------------------------------------------- auth
    def authenticate(self, username: str, api_key: str) -> str:
        """Engine-compatible signature. ``api_key`` is the Tradovate PASSWORD
        when freshly entered in the UI, or "" meaning: load the stored blob
        (keyring / IMBABOT_TDV_* env) for ``username``."""
        from ..config import load_tradovate_credentials

        username = username or getattr(self._settings, "tdv_username", "")
        if not username:
            raise TradovateError("Tradovate username is required.")
        blob = dict(load_tradovate_credentials(username) or {})
        blob.update({k: v for k, v in self.session_secrets.items() if v})
        password = api_key or blob.get("password") or ""
        if not password:
            raise TradovateError(
                "No Tradovate password stored for this username — enter it in "
                "the Connect panel.")
        device_id = getattr(self._settings, "tdv_device_id", "") or uuid.uuid4().hex
        try:  # persist the device id so every session looks like one machine
            if getattr(self._settings, "tdv_device_id", "") != device_id:
                self._settings.tdv_device_id = device_id
        except Exception:
            pass
        creds = TradovateCredentials(
            username=username,
            password=password,
            cid=str(blob.get("cid", "") or ""),
            sec=str(blob.get("sec", "") or ""),
            app_id=blob.get("app_id") or getattr(self._settings, "tdv_app_id", "Imbabot"),
            device_id=device_id,
        )
        self._tokens = TokenManager(self._base_url(), creds, http=self._http,
                                    log=lambda m: self._log(m), timeout=self._timeout)
        token = self._tokens.access_token()
        self.authenticated = True
        if self._enable_ws:
            self._connect_sockets()
        # Gate #2 banner: unambiguous statement of which venue is armed.
        dry = getattr(self._settings, "dry_run", True)
        cap = safety.MAX_POSITION_SIZE
        loss = safety.MAX_DAILY_LOSS
        self._log(
            f"TRADOVATE CONNECTED — env={self._env.upper()} "
            f"endpoint={self._base_url().split('//')[1].split('/')[0]} "
            f"LIVE_TRADING={safety.LIVE_TRADING} dry_run={dry} "
            f"venue_caps={'off (TopStep-parity guards)' if cap is None and loss is None else f'max_pos={cap} max_daily_loss=${loss}'}",
            "warning" if self._env == "live" else "info",
        )
        return token

    def validate(self) -> bool:
        """Self-healing: TokenManager renews/re-acquires internally."""
        if self._tokens is None:
            return False
        try:
            self._tokens.access_token()
            return True
        except Exception:
            return False

    def _connect_sockets(self) -> None:
        from . import ws

        base = self._base_url()
        self._user_ws = ws.TdvSocket(
            ws.user_ws_url(self._env), self._tokens, kind="user",
            log=self._log, on_kill=self._trip_kill)
        self._user_ws.start()
        self._md_ws = ws.TdvSocket(
            ws.md_ws_url(self._env), self._tokens, kind="md", log=self._log)
        self._md_ws.start()

    def _trip_kill(self, reason: str) -> None:
        """Daily-loss kill switch: block orders, sweep the book, liquidate."""
        self._kill_reason = reason
        self._log(f"KILL SWITCH: {reason} — cancelling all orders and liquidating.",
                  "error")
        try:
            for acct in list(self._account_specs):
                for o in self.search_open_orders(acct):
                    try:
                        self.cancel_order(acct, int(o["id"]))
                    except Exception:
                        pass
                for p in self.search_open_positions(acct):
                    try:
                        self.liquidate_position(acct, p.get("contractId"))
                    except Exception:
                        pass
        except Exception as exc:
            self._log(f"Kill-switch sweep failed: {exc}", "error")

    def close(self) -> None:
        for sock in (self._user_ws, self._md_ws):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    # ---------------------------------------------------- account / contract
    def search_accounts(self, only_active: bool = True) -> List[Account]:
        data = self._request("GET", "/account/list")
        accounts: List[Account] = []
        for a in data or []:
            active = bool(a.get("active", True))
            if only_active and not active:
                continue
            acct = Account(id=int(a["id"]), name=str(a.get("name", "")),
                           can_trade=active, is_visible=True)
            self._account_specs[acct.id] = acct.name
            accounts.append(acct)
        return accounts

    def _account_spec(self, account_id: int) -> str:
        spec = self._account_specs.get(int(account_id))
        if spec:
            return spec
        self.search_accounts(only_active=False)
        spec = self._account_specs.get(int(account_id))
        if not spec:
            raise TradovateError(f"Unknown Tradovate account id {account_id}.")
        return spec

    def resolve_contract(self, symbol: str, live: bool = False) -> Contract:
        """Resolve a root symbol (MNQ) or explicit contract (MNQU6) to the
        front-month Contract, with tick math from the product definition."""
        symbol = (symbol or "").upper().strip()
        root = "".join(ch for ch in symbol if ch.isalpha())
        # Explicit contract name (root + month code + year digit)?
        explicit = len(symbol) > len(root) and symbol[:len(root)] == root

        hits = self._request("GET", "/contract/suggest",
                             params={"t": symbol if explicit else root, "l": 20}) or []
        candidates = [c for c in hits
                      if str(c.get("name", "")).upper().startswith(root)]
        if explicit:
            candidates = [c for c in candidates
                          if str(c.get("name", "")).upper() == symbol] or candidates
        if not candidates:
            raise TradovateError(f"No Tradovate contract found for {symbol!r}.")

        # Front month = the candidate with the nearest FUTURE maturity.
        def _maturity(c: dict) -> str:
            mid = c.get("contractMaturityId")
            if mid is None:
                return "9999"
            try:
                m = self._request("GET", "/contractMaturity/item",
                                  params={"id": int(mid)}) or {}
                return str(m.get("expirationDate") or "9999")
            except TradovateError:
                return "9999"

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        dated = sorted(((_maturity(c), c) for c in candidates), key=lambda t: t[0])
        future = [t for t in dated if t[0] >= now_iso] or dated
        chosen = future[0][1]

        tick_size, tick_value = self._product_ticks(root)
        contract = Contract(
            id=str(chosen["id"]),
            name=str(chosen.get("name", symbol)),
            description=f"{chosen.get('name', symbol)} (Tradovate {self._env})",
            tick_size=tick_size,
            tick_value=tick_value,
            active=True,
            symbol_id=root,
        )
        self._contracts[contract.id] = {
            "id": int(chosen["id"]), "name": contract.name, "tick": tick_size,
        }
        return contract

    def _product_ticks(self, root: str) -> Tuple[float, float]:
        tick_size = value_per_point = None
        try:
            prod = self._request("GET", "/product/find", params={"name": root}) or {}
            if isinstance(prod, list):
                prod = prod[0] if prod else {}
            ts = prod.get("tickSize")
            vpp = prod.get("valuePerPoint")
            tick_size = float(ts) if ts else None
            value_per_point = float(vpp) if vpp else None
        except TradovateError:
            pass
        known = _KNOWN_TICKS.get(root)
        if tick_size is None or value_per_point is None:
            if known is None:
                raise TradovateError(
                    f"Cannot determine tick size for {root!r} from the API.")
            self._log(f"Tradovate: product lookup incomplete for {root}; "
                      f"using known tick constants.", "warning")
            return known
        tick_value = tick_size * value_per_point
        if known and (abs(tick_size - known[0]) > 1e-9
                      or abs(tick_value - known[1]) > 1e-9):
            self._log(
                f"Tradovate: {root} tick {tick_size}/{tick_value} differs from "
                f"expected {known[0]}/{known[1]} — VERIFY before trading.", "warning")
        return tick_size, tick_value

    # ---------------------------------------------------------- market data
    def last_price(self, contract_id: str, live: bool = False) -> float:
        info = self._contract_info(contract_id)
        if self._md_ws is None:
            raise TradovateError("Market-data socket not connected.")
        self._md_ws.subscribe_quote(info["name"], info["id"])
        price = self._md_ws.quotes.last_price(info["name"])
        if price is None:
            raise TradovateError(
                f"No fresh Tradovate quote for {info['name']} (check the CME "
                f"market-data subscription on the API).")
        return price

    def session_range(self, contract_id, start, end, live: bool = False):
        raise TradovateError("session_range is not supported on the Tradovate "
                             "backend yet (0.2.5 limitation).")

    def retrieve_bars(self, contract_id: str, **kwargs: Any) -> List[Bar]:
        raise TradovateError("retrieve_bars is not supported on the Tradovate "
                             "backend yet (0.2.5 limitation).")

    # -------------------------------------------------------------- orders
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
        custom_tag: Optional[str] = None,
        **_ignored: Any,
    ) -> OrderResult:
        self._guard_order(account_id, int(size))
        info = self._contract_info(contract_id)
        body: Dict[str, Any] = {
            "accountSpec": self._account_spec(account_id),
            "accountId": int(account_id),
            "action": ACTION_MAP[side],
            "symbol": info["name"],
            "orderQty": int(size),
            "orderType": ORDER_TYPE_MAP[order_type],
            "isAutomated": True,
        }
        if limit_price is not None:
            body["price"] = limit_price
        if stop_price is not None:
            body["stopPrice"] = stop_price
        if custom_tag:
            body["customTag50"] = str(custom_tag)[:50]
        data = self._request("POST", "/order/placeorder", body=body)
        return self._order_result(data)

    def place_straddle_leg(self, account_id: int, contract_id: str,
                           leg: StraddleLeg) -> OrderResult:
        """Entry stop with NATIVE OSO brackets (absolute prices). Cancelling an
        unfilled OSO entry kills the whole structure — the engine's OCO monitor
        needs no special casing."""
        self._guard_order(account_id, int(leg.size))
        info = self._contract_info(contract_id)
        entry_type = OrderType.STOP_LIMIT if leg.limit_price is not None else OrderType.STOP
        sl_price, tp_price = bracket_prices(
            leg.side, leg.stop_price, leg.stop_loss_ticks, leg.take_profit_ticks,
            info["tick"])

        body: Dict[str, Any] = {
            "accountSpec": self._account_spec(account_id),
            "accountId": int(account_id),
            "action": ACTION_MAP[leg.side],
            "symbol": info["name"],
            "orderQty": int(leg.size),
            "orderType": ORDER_TYPE_MAP[entry_type],
            "stopPrice": leg.stop_price,
            "isAutomated": True,
        }
        if leg.limit_price is not None:
            body["price"] = leg.limit_price
        if leg.custom_tag:
            body["customTag50"] = str(leg.custom_tag)[:50]

        exit_action = _exit_action(leg.side)
        brackets: List[Dict[str, Any]] = []
        if sl_price is not None:
            brackets.append({"action": exit_action, "orderType": "Stop",
                             "stopPrice": sl_price})
        if tp_price is not None:
            brackets.append({"action": exit_action, "orderType": "Limit",
                             "price": tp_price})

        if brackets:
            body["bracket1"] = brackets[0]
            if len(brackets) > 1:
                body["bracket2"] = brackets[1]
            data = self._request("POST", "/order/placeoso", body=body)
        else:
            # Tradovate has NO server-side Position Brackets (unlike TopStep):
            # a naked entry here is truly naked until you act.
            self._log(
                "Tradovate: placing a NAKED entry (no SL/TP brackets). Enable "
                "'Bot stop-loss/take-profit' for OSO protection on Tradovate.",
                "warning")
            data = self._request("POST", "/order/placeorder", body=body)

        res = self._order_result(data)
        leg.order_id = res.order_id
        return res

    def modify_order(self, account_id: int, order_id: int, *,
                     stop_price: Optional[float] = None,
                     limit_price: Optional[float] = None,
                     size: Optional[int] = None,
                     trail_price: Optional[float] = None) -> bool:
        snap = self._order_snapshot(int(order_id))
        body: Dict[str, Any] = {
            "orderId": int(order_id),
            # modifyorder requires orderQty + orderType re-stated.
            "orderQty": int(size if size is not None else snap.get("orderQty") or 1),
            "orderType": snap.get("orderType") or "Stop",
            "isAutomated": True,
        }
        if stop_price is not None:
            body["stopPrice"] = stop_price
        elif snap.get("stopPrice") is not None:
            body["stopPrice"] = snap["stopPrice"]
        if limit_price is not None:
            body["price"] = limit_price
        elif snap.get("price") is not None:
            body["price"] = snap["price"]
        data = self._request("POST", "/order/modifyorder", body=body)
        return data.get("failureReason") in (None, "Success")

    def cancel_order(self, account_id: int, order_id: int) -> bool:
        data = self._request("POST", "/order/cancelorder",
                             body={"orderId": int(order_id), "isAutomated": True})
        return data.get("failureReason") in (None, "Success")

    def liquidate_position(self, account_id: int, contract_id: Any) -> OrderResult:
        """Venue-side flatten for one contract (kill switch / demo probe).
        The engine's normal flatten path uses opposing market orders instead."""
        info = self._contract_info(contract_id)
        data = self._request("POST", "/order/liquidateposition",
                             body={"accountId": int(account_id),
                                   "contractId": info["id"],
                                   "admin": False, "isAutomated": True})
        return self._order_result(data)

    # ----------------------------------------------- open orders / positions
    def search_open_orders(self, account_id: int) -> List[Dict[str, Any]]:
        rows = self._book_rows("orders", "/order/list")
        out = []
        for o in rows:
            if int(o.get("accountId", account_id) or account_id) != int(account_id):
                continue
            if str(o.get("ordStatus", "Working")) not in WORKING_STATUSES:
                continue
            out.append({"id": int(o["id"]),
                        "ordStatus": o.get("ordStatus", "Working"),
                        "action": o.get("action"),
                        "contractId": str(o.get("contractId", ""))})
        return out

    def search_open_positions(self, account_id: int) -> List[Dict[str, Any]]:
        rows = self._book_rows("positions", "/position/list")
        out = []
        for p in rows:
            if int(p.get("accountId", account_id) or account_id) != int(account_id):
                continue
            net = int(p.get("netPos") or 0)
            if net == 0:
                continue
            # SIGNED netPos with no "type" key -> engine._net_position_value
            # returns it as-is (long > 0, short < 0).
            out.append({"contractId": str(p.get("contractId", "")),
                        "netPos": net,
                        "netPrice": p.get("netPrice")})
        return out

    def _book_rows(self, kind: str, rest_path: str) -> List[Dict[str, Any]]:
        """WS cache when healthy; REST fallback so OCO polling never goes blind."""
        if self._user_ws is not None and self._user_ws.healthy:
            return self._user_ws.cache.rows(kind)
        return list(self._request("GET", rest_path) or [])

    def _order_snapshot(self, order_id: int) -> Dict[str, Any]:
        if self._user_ws is not None and self._user_ws.healthy:
            for o in self._user_ws.cache.rows("orders"):
                if int(o.get("id", -1)) == order_id:
                    return o
        try:
            versions = self._request("GET", "/orderVersion/deps",
                                     params={"masterid": order_id}) or []
            if versions:
                return versions[-1]
        except TradovateError:
            pass
        return {}
