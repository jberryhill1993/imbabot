"""Selenium driver test — the path that bundles into the .exe/.app.

Drives the SAME mock page through the SeleniumPage shim + the same selector pack
and BrowserEngine used for real sites, in real (headless) Google Chrome. Proves
capture -> place -> fill -> cancel-opposite -> flatten works on the Selenium
driver, so the packaged app's "Launch → control Chrome" button is real.

Skips cleanly if Chrome/Selenium isn't available.

Run:  python tests/test_browser_selenium.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    try:
        import selenium  # noqa: F401
    except Exception as exc:
        print(f"SKIP: selenium not installed ({exc}).")
        return 0

    os.environ["IMBABOT_CONFIG_DIR"] = "/tmp/imbabot-selenium-test"
    from imbabot.config import Settings
    from imbabot.browser import make_adapter, BrowserEngine
    from imbabot.browser.drivers import open_selenium

    mock = Path(__file__).resolve().parent.parent / "imbabot" / "browser_mock" / "trader.html"
    profile = tempfile.mkdtemp(prefix="imba-sel-profile-")

    print("Selenium driver test (real Chrome)\n----------------------------------")
    try:
        session = open_selenium(Path(profile), headless=True)
    except Exception as exc:
        print(f"SKIP: could not launch Chrome via Selenium ({exc}).")
        return 0

    settings = Settings(
        backend="browser", browser_driver="selenium", browser_platform="mock",
        browser_tick_size=0.25, entry_points=12, stop_loss_points=10,
        take_profit_points=14, contracts=2, dry_run=False,
        max_trades_per_day=99, max_contracts=5,
    )
    adapter = make_adapter("mock")
    engine = BrowserEngine(settings, adapter, log=lambda *a, **k: None)

    try:
        page = session.page
        page.goto(mock.as_uri())

        check("adapter detects ready (Selenium)", adapter.is_logged_in(page) is True)
        price = engine.capture_price(page)
        check("reads price from DOM (Selenium)", price == 29464.75, f"got {price}")

        settings.dry_run = True
        _, _, placed = engine.fire_open(page)
        check("dry-run places nothing", placed is False and page.locator("tr.order").count() == 0)

        settings.dry_run = False
        handles, plan, placed = engine.fire_open(page)
        check("live places 2 legs", placed and page.locator("tr.order").count() == 2,
              f"placed={placed}")
        check("handles computed", handles.get("buy") == "29476.75" and handles.get("sell") == "29452.75",
              f"handles={handles}")

        check("monitor no-op while flat", engine.monitor_step(page, plan, handles) is False)

        page.evaluate("window.simulateFill('buy')")
        check("position long 2 after fill", adapter.read_net_position(page) == 2,
              f"net={adapter.read_net_position(page)}")
        check("monitor handles fill", engine.monitor_step(page, plan, handles) is True)
        check("opposite entry cancelled -> 0 orders", page.locator("tr.order").count() == 0,
              f"rows={page.locator('tr.order').count()}")

        engine.emergency_stop(page)
        check("emergency flatten -> position 0", adapter.read_net_position(page) == 0)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"ERROR: {exc}")
        session.close()
        return 1
    finally:
        session.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
