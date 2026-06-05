"""Browser-automation backend (fallback to the API backend).

Drives the trading site in a real browser via Playwright, executing the same
opening-range strategy. Site-specific DOM details live in JSON selector packs
(`selectors/*.json`) so they can be calibrated without touching code.
"""
from __future__ import annotations

from .base import (
    AdapterError,
    ConfigurableAdapter,
    PlatformAdapter,
    SelectorPack,
    load_pack,
    make_adapter,
)
from .engine import BrowserController, BrowserEngine, synth_contract

__all__ = [
    "AdapterError",
    "ConfigurableAdapter",
    "PlatformAdapter",
    "SelectorPack",
    "load_pack",
    "make_adapter",
    "BrowserController",
    "BrowserEngine",
    "synth_contract",
]
