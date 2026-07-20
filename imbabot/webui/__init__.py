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
    """Open the dashboard window with the js_api bridge attached (Phase 2)."""
    try:
        import webview
    except ImportError:
        print("pywebview is not installed — falling back to the classic GUI "
              "(pip install pywebview).", file=sys.stderr)
        from ..gui import main as classic_main
        return classic_main()

    from .. import __version__
    from ..config import config_dir
    from .bridge import Api

    api = Api()
    # First line of every session -> the file log, so a session is never
    # invisible from outside (diagnosed 2026-07-19: a failed-connect session
    # left zero trace on disk).
    api.log(f"Imbabot {__version__} ready (web UI). Config: {config_dir()}")
    index = _static_dir() / "index.html"
    window = webview.create_window(
        f"Imbabot {__version__}",
        index.as_uri(),
        js_api=api,
        width=1280,
        height=860,
        # 980x720 floor: 980 sits inside the <=1100px breakpoint (stats 2x2, single-column
        # forms — verified to fit), 720 keeps the hero cards + action bar fully visible with
        # the middle region scrolling internally. Below this the layout would break.
        min_size=(980, 720),
        background_color="#0A0F1E",
    )
    window.events.closing += api.shutdown   # disarm + stop threads, same as gui.on_close
    webview.start()
    return 0
