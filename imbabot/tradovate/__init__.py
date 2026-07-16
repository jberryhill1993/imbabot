"""Tradovate broker backend (REST + WebSocket).

Second, parallel execution venue next to TopstepX/ProjectX. The engine talks to
``TradovateClient`` through the same duck-typed surface (see imbabot/broker.py);
strategy/risk/OCO logic is unchanged.

Safety: demo endpoint by default. The live endpoint is hard-gated behind
``tradovate.safety.LIVE_TRADING`` (a source-level constant, not a setting).
"""
from __future__ import annotations

from .auth import TradovateAuthError, TradovateCredentials, TokenManager

__all__ = [
    "TradovateClient",
    "TradovateError",
    "TradovateAuthError",
    "TradovateCredentials",
    "TokenManager",
]


def __getattr__(name: str):
    # Lazy: keep `import imbabot.tradovate` light (auth-only) — the client pulls
    # in the websocket layer, which matters for startup time in the frozen exe.
    if name in ("TradovateClient", "TradovateError"):
        from . import client as _client

        return getattr(_client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
