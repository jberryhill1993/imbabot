"""Reference-price sources for the Tradovate backend that need NO CME license.

Context (verified 2026-07-18): streaming real-time CME quotes over Tradovate's
API requires CME sub-vendor registration (~$290/mo paid to CME) — but ORDER
ROUTING does not. The bot only consumes one price from the data path: the
reference captured ~1s before the open. So the default Tradovate configuration
takes that price from elsewhere:

- **TopStep/ProjectX feed** (``tdv_price_source="topstep"``, default): the SAME
  feed and code path the validated forward-test used — a ProjectXClient
  authenticated with the already-stored TopStep API key, reading 1s bars.
- **Public NQ quote** (fallback, or ``tdv_price_source="public"``): the header
  ticker's public feed. May lag a few seconds at the bell — logged loudly
  whenever it is the source actually used.

``tdv_price_source="tradovate"`` bypasses this module entirely and uses the
market-data WebSocket (for accounts that DO hold the CME sub-vendor license).
"""
from __future__ import annotations

import time as _time
from typing import Any, Callable, Optional

# Serve a just-fetched price for this long (the engine's capture probes the
# feed twice back-to-back; the dashboard polls every 5s so it always refetches).
_CACHE_SECONDS = 2.0
# After a TopStep HTTP 429 (rate limit), serve the public quote for this long
# instead of hammering ProjectX.
_PX_BACKOFF_SECONDS = 60.0
# If the TopStep bar price and the live public quote disagree by more than this
# many points, trust the QUOTE. Live 2026-07-20: a stale pre-open sim bar was
# 11+ pts below Tradovate's real book — the straddle centered low and the BUY
# stop filled instantly, a second before the open.
_DIVERGENCE_MAX_POINTS = 5.0


class ReferencePriceFeed:
    """Serves last_price() for the Tradovate backend from non-Tradovate sources.

    ``px_client`` and ``quote_fn`` are injectable for offline tests; production
    builds a real ProjectXClient lazily on first use.
    """

    def __init__(self, settings: Any, log: Optional[Callable[..., None]] = None,
                 *, px_client: Any = None,
                 quote_fn: Optional[Callable[[], Optional[float]]] = None) -> None:
        self._settings = settings
        self._log = log or (lambda msg, level="info": None)
        self._px = px_client
        self._quote_fn = quote_fn or self._default_quote
        self._px_state = "unset"          # unset | ready | unavailable
        self._px_contract: Any = None
        self.last_source: Optional[str] = None   # "topstep" | "public"
        self._clock = _time.time
        self._cache: Optional[tuple] = None      # (ts, price)
        self._px_backoff_until = 0.0
        self._public_warned = False
        self._px_live: Optional[bool] = None     # data tier: live preferred, sim fallback
        self._last_div_log = 0.0                 # divergence lines: max one per 30s

    # ------------------------------------------------------------ sources
    def _ensure_px(self) -> Any:
        """Authenticated ProjectX client + resolved contract, or None."""
        if self._px_state == "unavailable":
            return None
        if self._px_state == "ready":
            return self._px
        s = self._settings
        try:
            if self._px is None:
                from ..projectx import ProjectXClient
                self._px = ProjectXClient(base_url=s.base_url)
            if not getattr(self._px, "authenticated", False):
                from ..config import load_api_key
                key = load_api_key(s.username) if s.username else None
                if not key:
                    raise RuntimeError(
                        "no TopStep API key stored (Connect once on the API "
                        "backend, or set tdv_price_source to 'public')")
                self._px.authenticate(s.username, key)
            self._px_contract = self._px.resolve_contract(
                s.contract_symbol, live=s.use_live_data)
            self._px_state = "ready"
            self._log(f"Reference price source: TopStep feed "
                      f"({self._px_contract.name}).")
            return self._px
        except Exception as exc:
            self._px_state = "unavailable"
            self._log(f"TopStep price feed unavailable ({exc}) — will use the "
                      f"public NQ quote instead.", "warning")
            return None

    def _px_price(self, px: Any) -> float:
        """Bars from the LIVE data tier when available (real-time CME), the sim
        tier otherwise. The tier is probed once and remembered."""
        if self._px_live is None:
            try:
                price = px.last_price(self._px_contract.id, live=True)
                self._px_live = True
                self._log("TopStep bars: LIVE data tier.")
                return price
            except Exception as exc:
                if "429" in str(exc):
                    raise               # rate limit says nothing about the tier
                self._px_live = False
                self._log("TopStep bars: live tier unavailable — using the sim "
                          "tier (cross-checked against the public quote).",
                          "warning")
        return px.last_price(self._px_contract.id, live=self._px_live)

    @staticmethod
    def _default_quote() -> Optional[float]:
        from ..ticker import fetch_quote
        q = fetch_quote()
        return float(q.price) if q and q.price else None

    # ------------------------------------------------------------- public
    def last_price(self) -> float:
        s = self._settings
        now = self._clock()
        if self._cache is not None and now - self._cache[0] < _CACHE_SECONDS:
            return self._cache[1]
        source = getattr(s, "tdv_price_source", "topstep") or "topstep"
        if source != "public" and now >= self._px_backoff_until:
            px = self._ensure_px()
            if px is not None:
                try:
                    price = float(self._px_price(px))
                    # Cross-venue sanity: the straddle executes on Tradovate's
                    # REAL book, so a stale TopStep bar must never center it.
                    public = self._quote_fn()
                    if public is not None and \
                            abs(price - float(public)) > _DIVERGENCE_MAX_POINTS:
                        # Rate-limited to 30s, NOT once-per-episode: the fire-
                        # time capture must always log its own decision (the
                        # 7/21 fire's silent source pick cost diagnosis time).
                        if now - self._last_div_log >= 30.0:
                            self._last_div_log = now
                            self._log(
                                f"TopStep bars are {abs(price - float(public)):.1f}pt "
                                f"away from the live public quote ({price:,.2f} vs "
                                f"{float(public):,.2f}) — using the QUOTE for the "
                                f"reference.", "warning")
                        self.last_source = "public"
                        self._cache = (now, float(public))
                        return float(public)
                    self.last_source = "topstep"
                    self._cache = (now, price)
                    self._public_warned = False
                    return price
                except Exception as exc:
                    if "429" in str(exc):
                        # Rate-limited: stop hammering ProjectX for a while.
                        self._px_backoff_until = now + _PX_BACKOFF_SECONDS
                        self._log(f"TopStep feed rate-limited (429) — using the "
                                  f"public quote for {_PX_BACKOFF_SECONDS:.0f}s.",
                                  "warning")
                    else:
                        self._log(f"TopStep price fetch failed ({exc}) — trying "
                                  f"the public quote.", "warning")
        price = self._quote_fn()
        if price is not None:
            self.last_source = "public"
            if not self._public_warned:   # once per outage, not per 5s poll
                self._public_warned = True
                self._log("Reference price via PUBLIC NQ quote — may lag a few "
                          "seconds at the bell.", "warning")
            self._cache = (now, float(price))
            return float(price)
        raise RuntimeError(
            "No reference price available (TopStep feed and the public NQ "
            "quote both failed).")

    def describe(self) -> str:
        return {"topstep": "TopStep feed",
                "public": "public NQ quote"}.get(self.last_source or "", "none yet")
