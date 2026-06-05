"""Test the calibration recorder's capture mechanism + pack assembly.

Injects the picker into the mock page (headless Chrome), programmatically clicks
elements, and verifies it captures correct selectors and suppresses the click.
Then assembles a pack from captured selectors and confirms the same adapter reads
the price back. Skips if Chrome/Selenium isn't available.
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

    os.environ["IMBABOT_CONFIG_DIR"] = "/tmp/imbabot-calibrate-test"
    from imbabot.browser.calibrate import _PICKER_JS, _assemble
    from imbabot.browser.base import SelectorPack, ConfigurableAdapter
    from imbabot.browser.drivers import open_selenium

    mock = Path(__file__).resolve().parent.parent / "imbabot" / "browser_mock" / "trader.html"
    profile = tempfile.mkdtemp(prefix="imba-cal-")

    print("Calibration recorder test\n-------------------------")
    try:
        session = open_selenium(Path(profile), headless=True)
    except Exception as exc:
        print(f"SKIP: could not launch Chrome ({exc}).")
        return 0

    driver = session.page.driver
    try:
        driver.get(mock.as_uri())
        driver.execute_script(_PICKER_JS)

        def click_capture(css):
            driver.execute_script("window.__imbaActive=true; window.__imbaPicked=null;")
            driver.execute_script(f"document.querySelector('{css}').click();")
            return driver.execute_script("return window.__imbaPicked;")

        check("captures #price by id", click_capture("#price") == "#price")
        check("captures #buy-tab by id", click_capture("#buy-tab") == "#buy-tab")
        check("captures #quantity by id", click_capture("#quantity") == "#quantity")

        # click suppression: capturing the buy tab must NOT have toggled it active
        # (the handler preventDefault/stopImmediatePropagation before the onclick runs)
        active = driver.execute_script(
            "return document.getElementById('buy-tab').classList.contains('active');")
        check("click was suppressed (buy tab not toggled)", active is False, f"active={active}")

        # Skip button
        driver.execute_script("window.__imbaActive=true; window.__imbaPicked=null;")
        driver.execute_script("document.getElementById('__imbaSkip').click();")
        check("Skip button yields __SKIP__", driver.execute_script("return window.__imbaPicked;") == "__SKIP__")

        # Assemble a pack from captured selectors and confirm it drives the page
        caught = {
            "chart_ready": "#chart", "price": "#price", "position": "#position",
            "buy": "#buy-tab", "sell": "#sell-tab", "order_type": "#order-type",
            "stop_price": "#stop-price", "quantity": "#quantity",
            "submit": "#submit", "cancel_all": "#cancel-all", "flatten": "#flatten",
        }
        pack_dict = _assemble("mock", mock.as_uri(), caught)
        check("assembled buy has set_value(stop_price)",
              any(s.get("action") == "set_value" and s.get("value") == "$trigger_price"
                  for s in pack_dict["actions"]["buy"]))
        check("assembled has price/position/logged_in",
              pack_dict["price"] == "#price" and pack_dict["position_size"] == "#position"
              and pack_dict["logged_in"] == "#chart")

        adapter = ConfigurableAdapter(SelectorPack.from_dict(pack_dict))
        check("assembled pack reads price via adapter", adapter.read_price(session.page) == 29464.75)
        check("assembled pack detects ready", adapter.is_logged_in(session.page) is True)
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
