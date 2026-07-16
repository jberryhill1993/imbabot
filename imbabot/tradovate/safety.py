"""Hard-coded live-trading safeguards for the Tradovate backend.

These are SOURCE-LEVEL constants on purpose — not Settings fields, not
UI-editable. Flipping to live trading requires editing this file and re-running
the selftest, so it can never happen by accident, by mis-click, or by a bad
settings file. They sit BELOW the user-facing guards (Settings.dry_run,
Settings.max_contracts, RiskGuard) as defense in depth.

Also configure Tradovate's own account-level risk settings as the
platform-side backstop — these client-side guards stop the bot, not the venue.
"""
from __future__ import annotations

# Must be flipped to True IN SOURCE before the live endpoint can even be
# constructed. Demo (demo.tradovateapi.com) never needs it.
LIVE_TRADING: bool = False

# Hard ceiling on any single order size AND on the projected absolute net
# position. Independent of (and lower than) Settings.max_contracts.
MAX_POSITION_SIZE: int = 2

# Realized daily loss (USD) that trips the kill switch: block new orders,
# cancel working orders, liquidate. Resets only on restart + a new day.
MAX_DAILY_LOSS: float = 500.0


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
