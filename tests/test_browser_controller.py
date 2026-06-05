"""Threaded BrowserController end-to-end smoke test against the mock page.

Verifies the risky orchestration bits: Playwright launches on the controller's own
thread, price/login polling populates, ARM schedules a fire a few seconds out, the
fire executes on schedule (dry-run -> logs a plan), and shutdown is clean. No
cross-thread Playwright access (the controller owns the page).

Run:  python tests/test_browser_controller.py   (needs `playwright install chromium`)
"""
from __future__ import annotations

import os
import sys
import time
from datetime import timedelta
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
        import playwright  # noqa: F401
        from zoneinfo import ZoneInfo
    except Exception as exc:
        print(f"SKIP: prerequisites missing ({exc}).")
        return 0

    os.environ["IMBABOT_CONFIG_DIR"] = "/tmp/imbabot-browser-ctrl"
    from datetime import datetime

    from imbabot.config import Settings
    from imbabot.browser import BrowserController

    mock = Path(__file__).resolve().parent.parent / "imbabot" / "browser_mock" / "trader.html"

    # Schedule the "open" ~20s out, capture 3s before -> fire ~17s from now.
    # Wide margin so a slow Chromium launch can't push arm past the fire moment
    # (which would roll next_fire to tomorrow).
    et = ZoneInfo("America/New_York")
    target_open = datetime.now(et) + timedelta(seconds=20)

    s = Settings(
        backend="browser", browser_platform="mock", browser_url_override=mock.as_uri(),
        browser_tick_size=0.25, browser_headless=True,
        browser_driver="playwright", chrome_channel="chromium",
        entry_points=12, stop_loss_points=10, take_profit_points=14, contracts=2,
        trade_mode="one_trade", dry_run=True, max_trades_per_day=99, max_contracts=5,
        open_hour=target_open.hour, open_minute=target_open.minute,
        capture_offset_seconds=3,
    )
    # next_fire uses open h:m:s; align seconds too via a tiny monkeypatch-free path:
    s.open_time = lambda: __import__("datetime").time(target_open.hour, target_open.minute, target_open.second)  # type: ignore

    logs = []
    ctrl = BrowserController(s, log=lambda m, l="info": logs.append(m))

    print("Browser controller smoke test\n-----------------------------")
    ctrl.launch()

    # wait for the controller thread to launch chromium + poll a price
    t0 = time.time()
    while ctrl.last_price is None and time.time() - t0 < 12:
        time.sleep(0.3)
    check("controller launched & polled price", ctrl.last_price == 29464.75,
          f"last_price={ctrl.last_price} err={ctrl.error}")
    check("login/ready detected", ctrl.logged_in is True)

    ctrl.arm()
    time.sleep(0.5)
    check("ARM set state", ctrl.state in ("armed", "monitoring", "idle"))

    # wait for the scheduled fire (dry-run -> a FIRE + DRY RUN log)
    t0 = time.time()
    fired = False
    while time.time() - t0 < 24:
        if any("FIRE" in m for m in logs):
            fired = True
            break
        time.sleep(0.3)
    check("fired on schedule", fired, f"logs tail={logs[-3:]}")
    check("dry-run logged a plan, placed nothing",
          any("DRY RUN" in m for m in logs) and any("Straddle on" in m for m in logs))

    ctrl.shutdown()
    time.sleep(0.5)
    check("clean shutdown", not (ctrl._thread and ctrl._thread.is_alive()))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
