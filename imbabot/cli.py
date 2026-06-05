"""Command-line interface — headless control + offline self-test.

    python -m imbabot.cli selftest                 # offline checks, no network
    python -m imbabot.cli login                     # store API key, verify auth
    python -m imbabot.cli accounts                  # list tradable accounts
    python -m imbabot.cli contracts MNQ             # resolve a symbol
    python -m imbabot.cli config --show             # show settings
    python -m imbabot.cli config --set contracts=2  # edit a setting
    python -m imbabot.cli run                        # connect, arm, wait for fire
    python -m imbabot.cli run --live-orders          # ⚠ actually send orders
    python -m imbabot.cli panic                      # cancel all + flatten all

Safety: `run` is dry-run unless you pass --live-orders, and even then the daily
trade-count guard and contract cap apply.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Optional

from .config import Settings, load_api_key, store_api_key, settings_path
from .logbus import Logger


def _logger() -> Logger:
    return Logger(sink=lambda line, level: print(line))


# ---------------------------------------------------------------- commands
def cmd_login(args: argparse.Namespace) -> int:
    s = Settings.load()
    username = args.username or input(f"Username [{s.username}]: ").strip() or s.username
    if not username:
        print("A username is required.")
        return 2
    api_key = getpass.getpass("API key (hidden): ").strip()
    if not api_key:
        print("No API key entered.")
        return 2
    backend = store_api_key(username, api_key)
    s.username = username
    s.save()
    print(f"Stored API key via {backend}. Verifying …")

    from .engine import BotEngine

    engine = BotEngine(s, log=_logger())
    try:
        engine.connect(api_key)
    except Exception as exc:
        print(f"Verification FAILED: {exc}")
        return 1
    print("Login verified. You're connected.")
    return 0


def _connected_engine(log: Optional[Logger] = None):
    s = Settings.load()
    api_key = load_api_key(s.username)
    if not api_key:
        print("No stored API key. Run: python -m imbabot.cli login")
        return None
    from .engine import BotEngine

    engine = BotEngine(s, log=log or _logger())
    engine.connect(api_key)
    return engine


def cmd_accounts(args: argparse.Namespace) -> int:
    engine = _connected_engine()
    if not engine:
        return 1
    for a in engine.list_accounts():
        flag = "TRADABLE" if a.can_trade else "locked"
        print(f"  id={a.id:<8} {a.name:<24} {flag}")
    return 0


def cmd_contracts(args: argparse.Namespace) -> int:
    s = Settings.load()
    api_key = load_api_key(s.username)
    if not api_key:
        print("No stored API key. Run: python -m imbabot.cli login")
        return 1
    from .engine import BotEngine

    engine = BotEngine(s, log=_logger())
    engine.client.authenticate(s.username, api_key)
    for c in engine.client.search_contracts(args.symbol, live=s.use_live_data):
        star = "*" if c.active else " "
        print(f" {star} {c.id:<22} {c.name:<8} tick={c.tick_size} ${c.tick_value} {c.description}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    s = Settings.load()
    if args.set:
        for pair in args.set:
            if "=" not in pair:
                print(f"Bad --set '{pair}', expected key=value")
                return 2
            key, val = pair.split("=", 1)
            if not hasattr(s, key):
                print(f"Unknown setting '{key}'")
                return 2
            cur = getattr(s, key)
            try:
                if isinstance(cur, bool):
                    newval: object = val.lower() in ("1", "true", "yes", "on")
                elif isinstance(cur, int) and not isinstance(cur, bool):
                    newval = int(val)
                elif isinstance(cur, float):
                    newval = float(val)
                else:
                    newval = val
            except ValueError:
                print(f"Bad value for {key}: {val}")
                return 2
            setattr(s, key, newval)
        s.save()
        print(f"Saved {settings_path()}")
    if args.show or not args.set:
        from dataclasses import asdict

        for k, v in asdict(s).items():
            print(f"  {k:24s} = {v}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    log = _logger()
    s = Settings.load()
    if args.live_orders:
        s.dry_run = False
    api_key = load_api_key(s.username)
    if not api_key:
        print("No stored API key. Run: python -m imbabot.cli login")
        return 1
    from .engine import BotEngine

    engine = BotEngine(s, log=log)
    try:
        engine.connect(api_key)
    except Exception as exc:
        print(f"Connect failed: {exc}")
        return 1

    if not s.dry_run:
        print("\n  ⚠️  LIVE ORDER MODE — this will place REAL orders on your account.")
        if input("  Type 'I UNDERSTAND' to proceed: ").strip() != "I UNDERSTAND":
            print("Aborted.")
            return 1

    try:
        fire = engine.arm(on_tick=None)
    except Exception as exc:
        print(f"Arm refused: {exc}")
        return 1

    print(f"\nArmed. Firing at {fire.strftime('%H:%M:%S %Z')}. Ctrl-C to disarm.\n")
    try:
        while engine.armed:
            d = engine.dashboard()
            sys.stdout.write(
                f"\r  {d['contract']} last={d['last_price']}  "
                f"countdown {d['countdown']}  mode={d['mode']} "
                f"dry_run={d['dry_run']}    "
            )
            sys.stdout.flush()
            time.sleep(1)
        # after fire, give the OCO monitor a moment in live one-trade mode
        if not s.dry_run and s.trade_mode == "one_trade":
            print("\nMonitoring for fill (Ctrl-C to stop)…")
            while engine._monitor_thread and engine._monitor_thread.is_alive():
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nDisarming…")
        engine.disarm()
    return 0


def cmd_panic(args: argparse.Namespace) -> int:
    engine = _connected_engine()
    if not engine:
        return 1
    engine.emergency_stop()
    return 0


def cmd_browser_run(args: argparse.Namespace) -> int:
    import time as _time

    from .scheduler import format_countdown, seconds_until

    s = Settings.load()
    s.backend = "browser"
    if args.platform:
        s.browser_platform = args.platform
    if args.headless:
        s.browser_headless = True
    if args.live_orders:
        s.dry_run = False
    s.save()

    from .browser import BrowserController

    ctrl = BrowserController(s, log=_logger())
    print(f"Launching browser for '{s.browser_platform}'. A window will open — log into "
          f"your account there.")
    ctrl.launch()
    if ctrl.error:
        print(f"Could not start browser: {ctrl.error}")
        return 1
    input("\nPress Enter once you are logged in and want to ARM… ")
    if not s.dry_run:
        print("\n  ⚠️  LIVE ORDER MODE — real orders will be placed at the open.")
        if input("  Type 'I UNDERSTAND' to proceed: ").strip() != "I UNDERSTAND":
            print("Aborted.")
            ctrl.shutdown()
            return 1
    ctrl.arm()
    print("\nArmed. Ctrl-C to stop.\n")
    try:
        while True:
            fire = ctrl.next_fire()
            sys.stdout.write(
                f"\r  state={ctrl.state:<10} logged_in={ctrl.logged_in} "
                f"price={ctrl.last_price}  countdown {format_countdown(seconds_until(fire))}   "
            )
            sys.stdout.flush()
            _time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down browser…")
        ctrl.shutdown()
    return 0


def cmd_browser_inspect(args: argparse.Namespace) -> int:
    """Open the platform site in the persistent profile so you can calibrate selectors."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"Playwright not installed: {exc}\n  pip install playwright && playwright install chromium")
        return 1
    from .browser import load_pack
    from .config import config_dir

    pack = load_pack(args.platform)
    url = args.url or pack.url
    user_dir = config_dir() / "browser" / args.platform
    user_dir.mkdir(parents=True, exist_ok=True)
    from .browser import base as _base

    pack_file = _base._PACK_DIR / f"{args.platform}.json"
    print(f"Opening {url} in a persistent browser profile.\n"
          f"Use DevTools (Cmd/Ctrl+Shift+C) to find selectors, then edit:\n  {pack_file}")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(user_dir), headless=False, no_viewport=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if url:
            page.goto(url)
        input("\nBrowser open. Press Enter here to close it… ")
        ctx.close()
    return 0


def cmd_browser_calibrate(args: argparse.Namespace) -> int:
    """Interactive point-and-click selector recorder for a real site."""
    from .browser.calibrate import run_calibration

    return run_calibration(args.platform, url_override=args.url or "")


def cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run_selftest

    return run_selftest()


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="imbabot", description="Imbabot — opening-range bot (TopstepX)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("login", help="store API key and verify connection")
    sp.add_argument("--username", help="ProjectX/TopstepX username")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("accounts", help="list active accounts")
    sp.set_defaults(func=cmd_accounts)

    sp = sub.add_parser("contracts", help="search contracts for a symbol")
    sp.add_argument("symbol")
    sp.set_defaults(func=cmd_contracts)

    sp = sub.add_parser("config", help="show or edit settings")
    sp.add_argument("--show", action="store_true")
    sp.add_argument("--set", action="append", metavar="key=value")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("run", help="connect, arm, and wait for the open")
    sp.add_argument("--live-orders", action="store_true", help="actually send orders")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("panic", help="cancel all orders and flatten all positions")
    sp.set_defaults(func=cmd_panic)

    sp = sub.add_parser("browser-run", help="run the BROWSER backend (launch, log in, arm)")
    sp.add_argument("--platform", choices=["projectx", "tradesea", "mock"],
                    help="which selector pack / site to drive")
    sp.add_argument("--headless", action="store_true", help="run headless (no manual login!)")
    sp.add_argument("--live-orders", action="store_true", help="actually place orders")
    sp.set_defaults(func=cmd_browser_run)

    sp = sub.add_parser("browser-inspect", help="open a site in the persistent profile to calibrate selectors")
    sp.add_argument("platform", choices=["projectx", "tradesea", "mock"])
    sp.add_argument("--url", help="override the URL to open")
    sp.set_defaults(func=cmd_browser_inspect)

    sp = sub.add_parser("browser-calibrate", help="point-and-click recorder to capture a site's real selectors")
    sp.add_argument("platform", choices=["projectx", "tradesea"])
    sp.add_argument("--url", help="override the URL to open")
    sp.set_defaults(func=cmd_browser_calibrate)

    sp = sub.add_parser("selftest", help="run offline checks (no network)")
    sp.set_defaults(func=cmd_selftest)
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
