"""Glassmorphic web UI (pywebview + HTML/CSS/JS) — presentation layer only.

Phase 1 (current): static shell with placeholder data for visual review.
Phase 2 (after visual approval): a thin js_api bridge exposing the EXISTING engine
operations (the ones imbabot/gui.py already calls) — zero trading-logic changes.

The classic Tkinter GUI remains in-tree (`--classic`) as instant rollback.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _static_dir() -> Path:
    """Static assets dir — source tree or the PyInstaller bundle."""
    if getattr(sys, "frozen", False):  # frozen exe: bundled under _MEIPASS
        return Path(sys._MEIPASS) / "imbabot" / "webui" / "static"  # type: ignore[attr-defined]
    return Path(__file__).parent / "static"


def run_webui() -> int:
    """Open the dashboard window. Phase 1: no bridge — the page uses placeholder data."""
    try:
        import webview
    except ImportError:
        print("pywebview is not installed — falling back to the classic GUI "
              "(pip install pywebview).", file=sys.stderr)
        from ..gui import main as classic_main
        return classic_main()

    from .. import __version__
    index = _static_dir() / "index.html"
    webview.create_window(
        f"Imbabot {__version__}",
        index.as_uri(),
        width=1280,
        height=860,
        min_size=(1080, 700),
        background_color="#0A0F1E",
    )
    webview.start()
    return 0
