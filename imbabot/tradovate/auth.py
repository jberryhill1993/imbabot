"""Tradovate authentication: credentials, access-token lifecycle, penalty handling.

Flow (https://partner.tradovate.com — verified 2026-07):
- POST /v1/auth/accesstokenrequest with {name, password, appId, appVersion,
  deviceId, cid, sec} -> {accessToken, expirationTime, userId, ...}. Tokens live
  ~90 minutes.
- Renewal is PROACTIVE: any call within RENEW_HEADROOM of expiry first hits
  /v1/auth/renewaccesstoken with the current Bearer token (never wait for a 401).
- Time penalty: a response carrying "p-ticket" means wait "p-time" seconds and
  re-send the SAME body plus {"p-ticket": ...}. "p-captcha": true cannot be
  automated -> clear error telling the user to log in once via the website.

Secrets policy: password/cid/sec are NEVER logged (log lines redact to cid=***),
never stored here — storage lives in imbabot.config (keyring / IMBABOT_TDV_* env).

The HTTP transport and clock are injectable so the whole lifecycle is testable
offline in the selftest (scripted responses + fake clock), matching the repo's
no-network selftest rule.
"""
from __future__ import annotations

import json
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from .. import __version__

# Renew this many seconds before expirationTime (docs advise ~15 min on a ~90 min
# token; 10 min keeps a fat margin without churning).
RENEW_HEADROOM = 600.0

_PENALTY_MAX_RETRIES = 3
_PENALTY_MAX_TOTAL_WAIT = 60.0


class TradovateAuthError(RuntimeError):
    """Authentication failed in a way that needs the user (bad creds, captcha)."""


@dataclass
class TradovateCredentials:
    username: str
    password: str
    cid: str            # API key "Client ID" (sent as given; server accepts str/int)
    sec: str            # API key secret
    app_id: str = "Imbabot"
    device_id: str = ""

    def body(self) -> Dict[str, Any]:
        b: Dict[str, Any] = {
            "name": self.username,
            "password": self.password,
            "appId": self.app_id,
            "appVersion": __version__,
        }
        if self.device_id:
            b["deviceId"] = self.device_id
        if self.cid:
            # cid arrives as a string from the UI/keyring; send an int when it is one
            # (the official examples send a number).
            b["cid"] = int(self.cid) if str(self.cid).isdigit() else self.cid
        if self.sec:
            b["sec"] = self.sec
        return b


def _parse_expiration(raw: Any) -> Optional[float]:
    """expirationTime ISO string -> unix seconds (None if absent/unparsable)."""
    if not raw:
        return None
    try:
        s = str(raw)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _default_http(method: str, url: str, body: Optional[dict],
                  headers: Dict[str, str], timeout: float) -> Tuple[int, dict]:
    """Real transport (requests). Returns (status_code, parsed-json-or-{})."""
    import requests

    resp = requests.request(method, url, json=body, headers=headers, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {"_raw": data}
    return resp.status_code, data


class TokenManager:
    """Owns the access token: acquire, cache, proactively renew.

    ``access_token()`` is the single entry point — every REST call and every WS
    (re)connect goes through it, so a token near expiry is renewed before use.
    Thread-safe (REST calls, the OCO monitor, and the WS heartbeat all consult it).
    """

    def __init__(
        self,
        base_url: str,
        credentials: TradovateCredentials,
        *,
        http: Optional[Callable[..., Tuple[int, dict]]] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        log: Optional[Callable[[str], None]] = None,
        timeout: float = 15.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._creds = credentials
        self._http = http or _default_http
        self._clock = clock or _time.time
        self._sleep = sleep or _time.sleep
        self._log = log or (lambda m: None)
        self._timeout = timeout
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._md_token: Optional[str] = None
        self._expiry: float = 0.0
        self.user_id: Optional[int] = None

    # ------------------------------------------------------------- public
    @property
    def authenticated(self) -> bool:
        return self._token is not None and self._clock() < self._expiry

    def access_token(self) -> str:
        """Return a token guaranteed fresh for at least RENEW_HEADROOM seconds."""
        with self._lock:
            now = self._clock()
            if self._token is None:
                self._acquire_locked()
            elif now >= self._expiry - RENEW_HEADROOM:
                self._renew_locked()
            return self._token  # type: ignore[return-value]

    def md_token(self) -> str:
        """Token for the market-data socket (falls back to the access token)."""
        tok = self.access_token()
        return self._md_token or tok

    def invalidate(self) -> None:
        """Drop the cached token (next call re-acquires)."""
        with self._lock:
            self._token = None
            self._md_token = None
            self._expiry = 0.0

    # ------------------------------------------------------------ internals
    def _store_locked(self, data: dict) -> None:
        self._token = data["accessToken"]
        md = data.get("mdAccessToken")
        if md:
            self._md_token = md
        exp = _parse_expiration(data.get("expirationTime"))
        # If the server omits/garbles expirationTime, assume the documented 90 min.
        self._expiry = exp if exp is not None else self._clock() + 90 * 60
        uid = data.get("userId")
        if uid is not None:
            self.user_id = uid

    def _acquire_locked(self) -> None:
        """Full accesstokenrequest, with the p-ticket penalty dance."""
        url = f"{self._base}/auth/accesstokenrequest"
        body = self._creds.body()
        waited = 0.0
        for attempt in range(_PENALTY_MAX_RETRIES + 1):
            status, data = self._http("POST", url, body,
                                      {"Content-Type": "application/json"}, self._timeout)
            if "p-ticket" in data:
                if data.get("p-captcha"):
                    raise TradovateAuthError(
                        "Tradovate requires a captcha for this login. Log in once at "
                        "trader.tradovate.com from this computer, then try again.")
                p_time = float(data.get("p-time") or 5)
                if attempt >= _PENALTY_MAX_RETRIES or waited + p_time > _PENALTY_MAX_TOTAL_WAIT:
                    raise TradovateAuthError(
                        "Tradovate keeps applying a login penalty. Check the username/"
                        "password/API key and try again in a few minutes.")
                self._log(f"Tradovate auth penalty: waiting {p_time:.0f}s before retry "
                          f"({attempt + 1}/{_PENALTY_MAX_RETRIES})...")
                self._sleep(p_time)
                waited += p_time
                body = dict(self._creds.body())
                body["p-ticket"] = data["p-ticket"]
                continue
            if status == 200 and data.get("accessToken"):
                self._store_locked(data)
                self._log("Tradovate: access token acquired "
                          f"(user={data.get('name', '?')} cid=***).")
                return
            err = data.get("errorText") or f"HTTP {status}"
            raise TradovateAuthError(f"Tradovate authentication failed: {err}")
        raise TradovateAuthError("Tradovate authentication failed (penalty retries exhausted).")

    def _renew_locked(self) -> None:
        """Proactive renewal; falls back to a full acquire on any failure.

        The docs don't pin the verb for /auth/renewaccesstoken — try GET first
        (matches the official JS example), then POST on 4xx/405.
        """
        url = f"{self._base}/auth/renewaccesstoken"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            status, data = self._http("GET", url, None, headers, self._timeout)
            if status in (404, 405):
                status, data = self._http("POST", url, {}, headers, self._timeout)
            if status == 200 and data.get("accessToken"):
                self._store_locked(data)
                self._log("Tradovate: access token renewed.")
                return
            self._log(f"Tradovate: token renew failed (HTTP {status}) — re-authenticating.")
        except TradovateAuthError:
            raise
        except Exception as exc:
            self._log(f"Tradovate: token renew errored ({type(exc).__name__}) — re-authenticating.")
        self._token = None
        self._acquire_locked()
