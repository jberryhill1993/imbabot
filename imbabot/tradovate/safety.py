"""Live-trading environment gate + optional venue caps for the Tradovate backend.

These are SOURCE-LEVEL constants on purpose — not Settings fields, not
UI-editable — so flipping them requires editing this file and re-running the
selftest; it can never happen by mis-click or a bad settings file.

Guard parity (user directive 2026-07-18): the Tradovate path runs under the
SAME guard set as the TopStep path — ``Settings.max_contracts``, RiskGuard's
``max_trades_per_day``, and ``dry_run``. The venue-specific caps below ship
DISABLED (= None) so the exact TopStep-sim strategy (4–5 contracts, ~$600
stop) executes unchanged on Tradovate demo. Tradovate's own account-level
risk settings are the platform-side backstop.

Before enabling LIVE_TRADING, deliberately decide whether to re-enable the
caps (set integers/floats instead of None) — on a personal account there is
no prop-firm daily-loss net above you.
"""
from __future__ import annotations

from typing import Optional

# Must be flipped to True IN SOURCE before the live endpoint can even be
# constructed. Demo (demo.tradovateapi.com) never needs it.
LIVE_TRADING: bool = False

# Optional hard ceiling on any single order size. None = disabled (parity with
# TopStep: the engine-level max_contracts/RiskGuard still applies).
MAX_POSITION_SIZE: Optional[int] = None

# Optional realized-daily-loss (USD) kill switch: block new orders, cancel
# working orders, liquidate; resets only on restart + a new day. None = disabled.
MAX_DAILY_LOSS: Optional[float] = None


class SafetyError(RuntimeError):
    """A hard safeguard refused the operation."""


def assert_live_allowed(environment: str) -> None:
    """Raise unless the environment is permitted by the compiled-in gate."""
    if environment == "live" and not LIVE_TRADING:
        raise SafetyError(
            "Tradovate LIVE endpoint is disabled in this build. "
            "Live trading requires editing imbabot/tradovate/safety.py "
            "(LIVE_TRADING = True) and re-running the selftest — after the "
            "demo integration check passes."
        )
    if environment not in ("demo", "live"):
        raise SafetyError(f"Unknown Tradovate environment: {environment!r}")
