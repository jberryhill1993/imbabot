"""Imbabot — a local opening-range breakout bot for TopstepX / ProjectX.

This is a clean-room rebuild driven by the official ProjectX Gateway API
(https://gateway.docs.projectx.com) instead of browser automation. It runs
locally, places a stop-entry straddle a few seconds before the cash open, and
(in One-Trade mode) cancels the opposite entry once one side fills.

Nothing in here is trading or financial advice. You are fully responsible for
any orders this software places. See README.md for the rules that apply to
automated trading on Topstep.
"""

__version__ = "0.2.4.1"
__all__ = ["__version__"]
