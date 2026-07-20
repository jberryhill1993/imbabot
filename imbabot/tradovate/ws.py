"""Tradovate WebSocket layer: user-sync (fills/orders/positions) + market data.

Protocol (SockJS-flavored; verified against the official example-api-js):
- Server frames: ``o`` open, ``h`` heartbeat, ``a[...]`` array of JSON messages,
  ``c[code,"reason"]`` close.
- Client requests are text: ``endpoint\\n<id>\\n<query>\\n<json-body>``.
  Correlated responses come back as ``{"s": <status>, "i": <id>, "d": ...}``;
  push events as ``{"e": "props"|"md"|"shutdown"|"clock", "d": ...}``.
- The CLIENT must heartbeat: send ``[]`` every ~2.5s or the server drops the
  socket at ~5s of silence.

Thread model matches the repo (no asyncio): one daemon thread per socket runs
connect -> authorize -> resync -> receive loop, with reconnection and
exponential backoff (1..60s + jitter). On every reconnect, strictly BEFORE the
socket is marked healthy again: fresh token authorize, ``user/syncrequest``
full-snapshot resync (heals anything missed while down), and quote
re-subscription. The heartbeat also pings TokenManager once a minute so token
renewal stays proactive even when no REST call happens for hours.

Caches are lock-guarded and read by other threads (the engine's 0.5s OCO poll
reads UserSyncCache through TradovateClient.search_open_orders/positions; the
fire path reads QuoteCache via last_price). The daily-loss kill switch watches
cashBalance updates and fires the client's on_kill callback once per day.

Everything below the socket (codec + caches) is pure and covered offline by the
selftest; the socket thread itself is exercised by scripts/tdv_demo_check.py.
"""
from __future__ import annotations

import json
import random
import threading
import time as _time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import safety

HEARTBEAT_INTERVAL = 2.5      # client -> server "[]" cadence (server drops ~5s)
TOKEN_PING_INTERVAL = 60.0    # consult TokenManager (proactive renewal clock)
RECONNECT_MAX_DELAY = 60.0
REQUEST_TIMEOUT = 10.0
QUOTE_STALE_AFTER = 10.0      # seconds before a cached quote is unusable


def user_ws_url(environment: str) -> str:
    host = "live" if environment == "live" else "demo"
    return f"wss://{host}.tradovateapi.com/v1/websocket"


def md_ws_url(environment: str) -> str:
    # Verified: demo market data runs on the md-demo host.
    host = "md" if environment == "live" else "md-demo"
    return f"wss://{host}.tradovateapi.com/v1/websocket"


# ------------------------------------------------------------------ codec
def encode_request(endpoint: str, req_id: int, query: str = "",
                   body: Any = None) -> str:
    """``endpoint\\n<id>\\n<query>\\n<body>``.

    Dict/list bodies are JSON-encoded; STRING bodies are sent RAW — the
    ``authorize`` endpoint requires the bare token in the body slot without
    JSON quoting (verified live 2026-07-19: a quoted or query-slot token gets
    401 "Access is denied").
    """
    parts = [endpoint, str(req_id), query or ""]
    if body is None:
        parts.append("")
    elif isinstance(body, str):
        parts.append(body)
    else:
        parts.append(json.dumps(body))
    return "\n".join(parts)


def parse_frame(raw: str) -> Tuple[str, Any]:
    """Classify one raw frame -> (kind, payload).

    kinds: "open" | "heartbeat" | "messages" (payload = list of dicts) |
           "close" (payload = [code, reason]) | "unknown".
    """
    if not raw:
        return "unknown", None
    head = raw[0]
    if head == "o":
        return "open", None
    if head == "h":
        return "heartbeat", None
    if head == "a":
        try:
            return "messages", json.loads(raw[1:])
        except Exception:
            return "unknown", raw
    if head == "c":
        try:
            return "close", json.loads(raw[1:])
        except Exception:
            return "close", None
    return "unknown", raw


# ------------------------------------------------------------------ caches
class UserSyncCache:
    """Orders / orderVersions / positions / cashBalances from the user socket.

    ``ingest_sync`` replaces everything wholesale (the reconnect resync);
    ``apply_props`` applies incremental Created/Updated/Deleted events. Order
    rows are merged with their latest orderVersion so price/qty/type are
    available for modifyorder re-statement.
    """

    _SYNC_MAP = {  # syncrequest snapshot key -> entity kind
        "orders": "order", "orderVersions": "orderVersion",
        "positions": "position", "cashBalances": "cashBalance",
    }

    def __init__(self, *, on_kill: Optional[Callable[[str], None]] = None,
                 max_daily_loss: Optional[float] = safety.MAX_DAILY_LOSS) -> None:
        # max_daily_loss=None (the shipped safety.py default) disables the
        # daily-loss kill switch entirely — TopStep-parity guards only. Tests
        # pass an explicit value to exercise the machinery.
        self._lock = threading.Lock()
        self._store: Dict[str, Dict[int, Dict[str, Any]]] = {
            "order": {}, "orderVersion": {}, "position": {}, "cashBalance": {},
        }
        self._on_kill = on_kill
        self._max_daily_loss = max_daily_loss
        self._day_baseline: Dict[str, float] = {}
        self._killed = False

    # -- ingestion -----------------------------------------------------
    def ingest_sync(self, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            for kind in self._store:
                self._store[kind].clear()
            for key, kind in self._SYNC_MAP.items():
                for ent in snapshot.get(key) or []:
                    if isinstance(ent, dict) and "id" in ent:
                        self._store[kind][int(ent["id"])] = ent
        for cb in snapshot.get("cashBalances") or []:
            self._watch_cash(cb)

    def apply_props(self, entity_type: str, event_type: str,
                    entity: Dict[str, Any]) -> None:
        kind = entity_type[0].lower() + entity_type[1:] if entity_type else ""
        if kind not in self._store or not isinstance(entity, dict):
            return  # unknown entity types are ignored on purpose
        eid = entity.get("id")
        if eid is None:
            return
        with self._lock:
            if str(event_type).lower() == "deleted":
                self._store[kind].pop(int(eid), None)
            else:  # Created / Updated
                self._store[kind][int(eid)] = entity
        if kind == "cashBalance":
            self._watch_cash(entity)

    # -- reads ---------------------------------------------------------
    def rows(self, kind: str) -> List[Dict[str, Any]]:
        with self._lock:
            if kind == "orders":
                # merge each order with its latest orderVersion (highest id wins)
                latest: Dict[int, Dict[str, Any]] = {}
                for v in self._store["orderVersion"].values():
                    oid = v.get("orderId")
                    if oid is None:
                        continue
                    cur = latest.get(int(oid))
                    if cur is None or int(v.get("id", 0)) >= int(cur.get("id", 0)):
                        latest[int(oid)] = v
                out = []
                for o in self._store["order"].values():
                    row = dict(o)
                    v = latest.get(int(o["id"]))
                    if v:
                        for f in ("orderQty", "orderType", "price", "stopPrice"):
                            if v.get(f) is not None:
                                row[f] = v[f]
                    out.append(row)
                return out
            if kind == "positions":
                return [dict(p) for p in self._store["position"].values()]
            return [dict(e) for e in self._store.get(kind, {}).values()]

    # -- daily-loss kill switch -----------------------------------------
    def _watch_cash(self, cb: Dict[str, Any]) -> None:
        """Trip on_kill when the day's realized P&L breaches -MAX_DAILY_LOSS.

        Prefers the venue's own realizedPnL field; falls back to the delta of
        ``amount`` against the first balance seen for that trade date.
        """
        if self._killed or self._on_kill is None or self._max_daily_loss is None:
            return
        day = json.dumps(cb.get("tradeDate"), sort_keys=True, default=str)
        loss: Optional[float] = None
        rp = cb.get("realizedPnL")
        if rp is not None:
            try:
                loss = float(rp)
            except (TypeError, ValueError):
                loss = None
        if loss is None:
            try:
                amt = float(cb.get("amount"))
            except (TypeError, ValueError):
                return
            baseline = self._day_baseline.setdefault(day, amt)
            loss = amt - baseline
        if loss <= -self._max_daily_loss:
            self._killed = True
            self._on_kill(f"daily realized P&L {loss:+.2f} breached "
                          f"-${self._max_daily_loss:.0f}")


class QuoteCache:
    """Latest quote entries per key (symbol), with a staleness window."""

    def __init__(self, clock: Optional[Callable[[], float]] = None,
                 stale_after: float = QUOTE_STALE_AFTER) -> None:
        self._clock = clock or _time.time
        self._stale = stale_after
        self._lock = threading.Lock()
        self._quotes: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def update(self, key: str, entries: Dict[str, Any]) -> None:
        with self._lock:
            # merge so a Bid/Offer-only tick doesn't wipe the last Trade
            ts, cur = self._quotes.get(key, (0.0, {}))
            merged = dict(cur)
            merged.update(entries or {})
            self._quotes[key] = (self._clock(), merged)

    def last_price(self, key: str) -> Optional[float]:
        with self._lock:
            ts, entries = self._quotes.get(key, (0.0, {}))
        if not entries or self._clock() - ts > self._stale:
            return None
        trade = entries.get("Trade") or {}
        if trade.get("price") is not None:
            return float(trade["price"])
        bid = (entries.get("Bid") or {}).get("price")
        offer = (entries.get("Offer") or {}).get("price")
        if bid is not None and offer is not None:
            return (float(bid) + float(offer)) / 2.0
        return None


# ------------------------------------------------------------------ socket
class TdvSocket(threading.Thread):
    """One Tradovate WebSocket (user-sync or market-data) on a daemon thread.

    kind="user": authorize -> user/syncrequest -> feed UserSyncCache.
    kind="md":   authorize (mdAccessToken) -> md/subscribeQuote -> QuoteCache.
    """

    def __init__(self, url: str, tokens: Any, *, kind: str,
                 log: Optional[Callable[..., None]] = None,
                 on_kill: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(daemon=True, name=f"TdvSocket-{kind}")
        self.url = url
        self.kind = kind
        self._tokens = tokens
        self._log = log or (lambda msg, level="info": None)
        self.cache = UserSyncCache(on_kill=on_kill) if kind == "user" else None
        self.quotes = QuoteCache()
        self.healthy = False
        self._had_session = False
        self._closing = False
        self._ws: Any = None
        self._req_id = 0
        self._send_lock = threading.Lock()
        self._subs: Dict[str, Optional[str]] = {}   # symbol -> contractId str

    # -- public ----------------------------------------------------------
    def subscribe_quote(self, symbol: str, contract_id: Any = None) -> None:
        if contract_id is not None:
            self._subs[symbol] = str(contract_id)
        elif symbol not in self._subs:
            self._subs[symbol] = None
        if self.healthy:
            try:
                self._send(encode_request("md/subscribeQuote", self._next_id(),
                                          body={"symbol": symbol}))
            except Exception as exc:
                self._log(f"Tradovate MD subscribe failed: {exc}", "warning")

    def close(self) -> None:
        self._closing = True
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    # -- thread body -------------------------------------------------------
    def run(self) -> None:  # pragma: no cover (network; demo probe exercises it)
        attempt = 0
        auth_failures = 0
        while not self._closing:
            self._had_session = False
            try:
                self._session()
            except Exception as exc:
                msg = str(exc)
                self._log(f"Tradovate {self.kind} socket dropped: {msg}", "warning")
                if "401" in msg or "access is denied" in msg.lower():
                    # Stale/superseded token (e.g. a re-connect acquired a new
                    # one). Invalidate ONCE so the next attempt re-acquires;
                    # give up after 3 strikes — each re-acquire spends one of
                    # Tradovate's ~5 auth attempts/hour, and the client's REST
                    # fallback keeps orders/positions visible without us.
                    auth_failures += 1
                    if auth_failures == 1:
                        try:
                            self._tokens.invalidate()
                        except Exception:
                            pass
                    elif auth_failures >= 3:
                        self._log(
                            f"Tradovate {self.kind} socket stopping after repeated "
                            f"authorize failures — REST fallback stays active. "
                            f"Reconnect the bot to retry.", "error")
                        break
            if self._had_session:                 # we got healthy this round
                attempt = 0
                auth_failures = 0
            self.healthy = False
            if self._closing:
                break
            attempt += 1
            delay = min(2 ** (attempt - 1), RECONNECT_MAX_DELAY)
            delay *= 1.0 + random.random() * 0.25
            self._log(f"Tradovate {self.kind} socket reconnecting in {delay:.0f}s "
                      f"(attempt {attempt})...", "warning" if attempt > 1 else "info")
            _time.sleep(delay)

    def _session(self) -> None:  # pragma: no cover (network)
        import websocket  # websocket-client (pure Python, thread-friendly)

        ws = websocket.create_connection(self.url, timeout=REQUEST_TIMEOUT)
        self._ws = ws
        try:
            self._await_open(ws)
            # ALWAYS a fresh token — the cached string may be stale after a
            # long disconnect; TokenManager renews as needed.
            token = (self._tokens.md_token() if self.kind == "md"
                     else self._tokens.access_token())
            self._call(ws, "authorize", body=token)   # RAW token in the body slot
            if self.kind == "user":
                uid = getattr(self._tokens, "user_id", None)
                body = {"users": [uid]} if uid is not None else {"users": []}
                snap = self._call(ws, "user/syncrequest", body=body)
                if self.cache is not None and isinstance(snap, dict):
                    self.cache.ingest_sync(snap)
            else:
                for symbol in list(self._subs):
                    self._call(ws, "md/subscribeQuote", body={"symbol": symbol})
            self.healthy = True
            self._had_session = True              # reached healthy -> reset backoff
            self._log(f"Tradovate {self.kind} socket connected.", "info")
            self._receive_loop(ws)
        finally:
            self.healthy = False
            try:
                ws.close()
            except Exception:
                pass

    def _receive_loop(self, ws: Any) -> None:  # pragma: no cover (network)
        ws.settimeout(0.5)
        last_hb = last_token = _time.monotonic()
        while not self._closing:
            now = _time.monotonic()
            if now - last_hb >= HEARTBEAT_INTERVAL:
                self._send("[]")
                last_hb = now
            if now - last_token >= TOKEN_PING_INTERVAL:
                try:  # proactive renewal clock (see auth.py)
                    self._tokens.access_token()
                except Exception as exc:
                    self._log(f"Tradovate token ping failed: {exc}", "warning")
                last_token = now
            try:
                raw = ws.recv()
            except Exception as exc:
                if "timed out" in str(exc).lower():
                    continue
                raise
            kind, payload = parse_frame(raw if isinstance(raw, str) else
                                        raw.decode("utf-8", "replace"))
            if kind == "messages":
                for msg in payload or []:
                    self._dispatch(msg)
            elif kind == "close":
                raise ConnectionError(f"server closed the socket: {payload}")

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        event = msg.get("e")
        if event == "props" and self.cache is not None:
            d = msg.get("d") or {}
            self.cache.apply_props(str(d.get("entityType", "")),
                                   str(d.get("eventType", "")),
                                   d.get("entity") or {})
        elif event == "md":
            for q in (msg.get("d") or {}).get("quotes") or []:
                cid = str(q.get("contractId", ""))
                key = None
                for sym, mapped in self._subs.items():
                    if mapped == cid:
                        key = sym
                        break
                self.quotes.update(key or cid, q.get("entries") or {})
        elif event == "shutdown":
            self._log(f"Tradovate {self.kind} socket: server shutdown notice "
                      f"{msg.get('d')}", "warning")

    # -- wire helpers ------------------------------------------------------
    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _send(self, text: str) -> None:
        with self._send_lock:
            if self._ws is not None:
                self._ws.send(text)

    def _await_open(self, ws: Any) -> None:  # pragma: no cover (network)
        deadline = _time.monotonic() + REQUEST_TIMEOUT
        while _time.monotonic() < deadline:
            kind, _ = parse_frame(ws.recv())
            if kind == "open":
                return
        raise ConnectionError("no open frame from Tradovate socket")

    def _call(self, ws: Any, endpoint: str, *, query: str = "",
              body: Any = None) -> Any:  # pragma: no cover (network)
        """Send one request and block for its correlated response, dispatching
        any push events that arrive in between."""
        rid = self._next_id()
        self._send(encode_request(endpoint, rid, query, body))
        deadline = _time.monotonic() + REQUEST_TIMEOUT
        while _time.monotonic() < deadline:
            kind, payload = parse_frame(ws.recv())
            if kind != "messages":
                if kind == "close":
                    raise ConnectionError("socket closed during request")
                continue
            for msg in payload or []:
                if isinstance(msg, dict) and msg.get("i") == rid:
                    status = msg.get("s")
                    if status != 200:
                        raise ConnectionError(
                            f"{endpoint} -> status {status}: {msg.get('d')}")
                    return msg.get("d")
                self._dispatch(msg)
        raise ConnectionError(f"{endpoint}: no response within {REQUEST_TIMEOUT}s")
