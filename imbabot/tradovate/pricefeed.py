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

from typing import Any, Callable, Optional


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

    @staticmethod
    def _default_quote() -> Optional[float]:
        from ..ticker import fetch_quote
        q = fetch_quote()
        return float(q.price) if q and q.price else None

    # ------------------------------------------------------------- public
    def last_price(self) -> float:
        s = self._settings
        source = getattr(s, "tdv_price_source", "topstep") or "topstep"
        if source != "public":
            px = self._ensure_px()
            if px is not None:
                try:
                    price = float(px.last_price(self._px_contract.id,
                                                live=s.use_live_data))
                    self.last_source = "topstep"
                    return price
                except Exception as exc:
                    self._log(f"TopStep price fetch failed ({exc}) — trying "
                              f"the public quote.", "warning")
        price = self._quote_fn()
        if price is not None:
            self.last_source = "public"
            self._log("Reference price via PUBLIC NQ quote — may lag a few "
                      "seconds at the bell.", "warning")
            return float(price)
        raise RuntimeError(
            "No reference price available (TopStep feed and the public NQ "
            "quote both failed).")

    def describe(self) -> str:
        return {"topstep": "TopStep feed",
                "public": "public NQ quote"}.get(self.last_source or "", "none yet")
