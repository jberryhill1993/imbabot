"""Entry point bundled into the app. Double-clicking launches the GUI.

Also accepts a couple of args (handy for the frozen binary):
    Imbabot cli <...>        # run any CLI command from the packaged app
    Imbabot selenium-smoke   # verify the bundled Selenium can drive your Chrome
"""
from __future__ import annotations

import sys


def _selenium_smoke() -> int:
    """Launch headless Chrome via the bundled Selenium and read a value back."""
    import tempfile
    from pathlib import Path

    try:
        from imbabot.browser.drivers import open_selenium
    except Exception as exc:
        print(f"SELENIUM_SMOKE_FAIL: import error: {exc}")
        return 1
    profile = tempfile.mkdtemp(prefix="imba-smoke-")
    try:
        session = open_selenium(Path(profile), headless=True)
    except Exception as exc:
        print(f"SELENIUM_SMOKE_FAIL: could not launch Chrome: {exc}")
        return 1
    try:
        page = session.page
        page.goto("data:text/html,<h1 id=t>imba</h1><span id=p>42</span>")
        val = page.locator("#p").inner_text()
        ua = page.evaluate("navigator.userAgent")
        ok = val == "42" and "Chrome" in str(ua)
        print(f"SELENIUM_SMOKE_{'OK' if ok else 'FAIL'}: read={val!r} ua={ua}")
        return 0 if ok else 1
    finally:
        session.close()


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "cli":
        from imbabot.cli import main as cli_main

        return cli_main(args[1:])
    if args and args[0] == "selenium-smoke":
        return _selenium_smoke()
    if args and args[0] == "pack-check":
        import os
        import tempfile

        os.environ["IMBABOT_CONFIG_DIR"] = tempfile.mkdtemp(prefix="imba-packcheck-")  # ignore user override
        from imbabot.browser.base import load_pack, _PACK_DIR

        try:
            p = load_pack("projectx")
            steps = [s.get("action") for s in p.actions.get("buy", [])]
            print(f"PACK_CHECK_OK name={p.name} buy={steps} price_js={bool(p.price_js)} dir={_PACK_DIR}")
            return 0
        except Exception as exc:
            print(f"PACK_CHECK_FAIL {exc} dir={_PACK_DIR}")
            return 1
    if args and args[0] == "--classic":
        # Rollback path: the original Tkinter GUI, fully functional.
        from imbabot.gui import main as gui_main
        return gui_main()
    # Default on the UI branch: the glass web dashboard (falls back to the
    # classic GUI automatically if pywebview isn't available).
    from imbabot.webui import run_webui
    return run_webui()


if __name__ == "__main__":
    raise SystemExit(main())
