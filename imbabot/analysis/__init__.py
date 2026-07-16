"""Daily recommended-spread analyzer (Imbabot 0.2.1).

A backtest-calibrated advisor that suggests a daily entry point-spread from VIX,
overnight volatility, and the scheduled economic calendar — shown next to the VIX
in the HUD. It is **informational only**: the bot never changes ``entry_points``
automatically; the user reviews the recommendation and adjusts by hand.

Everything here is pure-Python (no numpy/pandas) so it stays out of the lean
packaged exe's excluded-deps list, and isolated from the shipped 0.2.0 strategy
and engine paths.
"""
from __future__ import annotations

__all__ = ["probe_depth", "ProbeResult"]

from .history import ProbeResult, probe_depth
