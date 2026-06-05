"""Run every test suite and report a combined result.

    python tests/run_all.py

Browser suites self-skip if Playwright/Chromium aren't installed.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

SUITES = [
    ("engine self-test (offline)", [PY, "-m", "imbabot.cli", "selftest"]),
    ("API client unit tests", [PY, os.path.join(HERE, "test_projectx_client.py")]),
    ("browser backend (mock page)", [PY, os.path.join(HERE, "test_browser_mock.py")]),
    ("browser controller (threaded)", [PY, os.path.join(HERE, "test_browser_controller.py")]),
    ("selenium driver (real Chrome)", [PY, os.path.join(HERE, "test_browser_selenium.py")]),
    ("calibration recorder", [PY, os.path.join(HERE, "test_calibrate.py")]),
]


def main() -> int:
    failures = 0
    for name, cmd in SUITES:
        print(f"\n===== {name} =====")
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            failures += 1
            print(f"  >> {name} FAILED (exit {result.returncode})")
    print("\n" + "=" * 40)
    if failures:
        print(f"{failures} suite(s) failed.")
        return 1
    print("All suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
