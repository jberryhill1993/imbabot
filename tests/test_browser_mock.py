"""End-to-end test of the browser backend against a local mock trading page.

This drives the REAL adapter + BrowserEngine (the same code paths used for Project
X / TradeSea) through a headless Chromium, proving the mechanics:
capture price -> place straddle -> detect a fill -> cancel the opposite entry ->
emergency flatten. Only the selector pack differs for real sites.

Run:  python tests/test_browser_mock.py     (needs `playwright install chromium`)
"""
from __future__ import annotations

import os
import sys
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
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"SKIP: Playwright not installed ({exc}). `pip install playwright && playwright install chromium`")
        return 0

    os.environ["IMBABOT_CONFIG_DIR"] = "/tmp/imbabot-browser-test"
    from imbabot.config import Settings
    from imbabot.browser import make_adapter, BrowserEngine

    mock = Path(__file__).resolve().parent.parent / "imbabot" / "browser_mock" / "trader.html"
    if not mock.exists():
        print(f"FAIL: mock page missing at {mock}")
        return 1

    settings = Settings(
        backend="browser", browser_platform="mock", browser_tick_size=0.25,
        entry_points=12, stop_loss_points=10, take_profit_points=14, contracts=2,
        dry_run=False, max_trades_per_day=99, max_contracts=5,
    )
    adapter = make_adapter("mock")
    logs = []
    engine = BrowserEngine(settings, adapter, log=lambda m, l="info": logs.append(m))

    print("Browser backend mock test\n--------------------------")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(mock.as_uri(), wait_until="domcontentloaded")

            check("adapter detects logged-in/ready", adapter.is_logged_in(page) is True)

            price = engine.capture_price(page)
            check("reads live price from DOM", price == 29464.75, f"got {price}")

            # dry-run places nothing
            settings.dry_run = True
            handles, plan, placed = engine.fire_open(page)
            rows = page.locator("tr.order").count()
            check("dry-run places no orders", placed is False and rows == 0, f"placed={placed} rows={rows}")

            # live: places both straddle legs
            settings.dry_run = False
            handles, plan, placed = engine.fire_open(page)
            rows = page.locator("tr.order").count()
            check("live places 2 entry legs", placed and rows == 2, f"placed={placed} rows={rows}")
            check("long handle = ref+12 snapped", handles.get("buy") == "29476.75", f"got {handles.get('buy')}")
            check("short handle = ref-12 snapped", handles.get("sell") == "29452.75", f"got {handles.get('sell')}")

            # mock DOM received the right values
            buy_row = page.locator("tr.order[data-side='buy']")
            check("buy row price in DOM", buy_row.get_attribute("data-price") == "29476.75")
            check("buy row qty in DOM", buy_row.get_attribute("data-qty") == "2")

            # no fill yet -> monitor does nothing
            check("monitor_step no-op while flat", engine.monitor_step(page, plan, handles) is False)

            # simulate a LONG fill -> opposite (short) entry must be cancelled
            page.evaluate("window.simulateFill('buy')")
            check("position now long 2", adapter.read_net_position(page) == 2,
                  f"got {adapter.read_net_position(page)}")
            handled = engine.monitor_step(page, plan, handles)
            check("monitor_step handles the fill", handled is True)
            remaining = page.locator("tr.order").count()
            check("opposite (short) entry cancelled -> 0 working orders", remaining == 0,
                  f"rows={remaining}")

            # emergency stop flattens
            engine.emergency_stop(page)
            check("emergency stop flattens position", adapter.read_net_position(page) == 0,
                  f"pos={adapter.read_net_position(page)}")

            browser.close()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"\nERROR during browser test: {exc}")
        return 1

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
