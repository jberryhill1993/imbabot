"""Tradovate DEMO integration check — run manually AFTER the onboarding checklist.

Proves the whole Tradovate stack end-to-end against demo.tradovateapi.com with
1-contract orders placed far from the market. It never touches the live
endpoint (TradovateClient refuses live while safety.LIVE_TRADING is False).

Prerequisites (see README "Tradovate onboarding"):
  1. Tradovate demo account + the API Access add-on (cid + sec generated).
  2. CME market-data subscription valid for API usage.
  3. Credentials stored: either connect once in the app with "Remember", or set
     IMBABOT_TDV_USER / IMBABOT_TDV_PASSWORD / IMBABOT_TDV_CID / IMBABOT_TDV_SEC.

Run:
  python scripts/tdv_demo_check.py            # uses stored/env credentials
  python scripts/tdv_demo_check.py --symbol MNQ

Steps (each prints PASS/FAIL; aborts on the first hard failure):
  1  auth: token acquired, expiry in the future (p-ticket handling logged)
  2  sockets: user WS syncs (accounts/orders/positions), MD WS authorizes
  3  contract: resolve front month, tick 0.25 / $0.50 for MNQ
  4  quote: a fresh price arrives within 15s
  5  OSO: 1-lot entry stop ~200pt away w/ brackets -> visible as Working
  6  modify: entry moved 10pt further -> cache shows the new stopPrice
  7  cancel: order leaves the working set (the OCO monitor's fill/cancel signal)
  8  reconnect: socket force-closed -> auto reconnect + resync restores caches
  9  liquidate guard: no position expected, liquidate is a no-op sweep
Optional (--fill): place a marketable 1-lot limit, watch the fill arrive over
the WS, then liquidate. Off by default — it costs demo commissions only, but
it IS a real demo trade.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imbabot.config import Settings  # noqa: E402
from imbabot.models import OrderSide, OrderType  # noqa: E402
from imbabot.tradovate import TradovateClient  # noqa: E402

PASS = "  PASS "
FAIL = "  FAIL "
_failures = 0


def check(name: str, cond: bool, detail: str = "", fatal: bool = True) -> bool:
    global _failures
    print((PASS if cond else FAIL) + name + ("" if cond else f"  {detail}"))
    if not cond:
        _failures += 1
        if fatal:
            print("\nAborting (fatal step failed).")
            sys.exit(1)
    return cond


def log(msg: str, level: str = "info") -> None:
    print(f"    [{level}] {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Tradovate DEMO integration check")
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--username", default="", help="Tradovate username "
                    "(default: settings/tdv env var)")
    ap.add_argument("--fill", action="store_true",
                    help="ALSO place a marketable 1-lot and liquidate (real demo trade)")
    args = ap.parse_args()

    s = Settings.load()
    s.backend = "tradovate"
    s.tdv_environment = "demo"          # this script never runs live
    if args.username:
        s.tdv_username = args.username

    print("Tradovate DEMO check\n--------------------")
    cl = TradovateClient(s, log=log)

    # 1 — auth
    cl.authenticate(s.tdv_username, "")
    check("1 auth: token acquired", cl.authenticated)
    check("1 auth: validate() self-heals", cl.validate())

    # 2 — sockets
    deadline = time.time() + 15
    while time.time() < deadline and not (cl._user_ws and cl._user_ws.healthy):
        time.sleep(0.5)
    check("2 user socket healthy + synced", bool(cl._user_ws and cl._user_ws.healthy))
    while time.time() < deadline and not (cl._md_ws and cl._md_ws.healthy):
        time.sleep(0.5)
    check("2 MD socket healthy", bool(cl._md_ws and cl._md_ws.healthy))

    accounts = cl.search_accounts()
    check("2 accounts listed", len(accounts) >= 1,
          "no active demo account visible")
    acct = accounts[0].id
    print(f"    using account {accounts[0].name} (id={acct})")

    # 3 — contract
    con = cl.resolve_contract(args.symbol)
    print(f"    resolved {con.name} (id={con.id}) tick={con.tick_size} "
          f"${con.tick_value}/tick")
    check("3 front month resolved", con.name.upper().startswith(args.symbol.upper()))
    if args.symbol.upper() == "MNQ":
        check("3 MNQ tick math", con.tick_size == 0.25 and con.tick_value == 0.5,
              f"{con.tick_size}/{con.tick_value}")

    # 4 — quote
    price = None
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            price = cl.last_price(con.id)
            break
        except Exception:
            time.sleep(1.0)
    check("4 fresh quote received", price is not None,
          "no quote in 15s — is the CME market-data subscription active for API?")
    print(f"    last price {price}")

    # 5 — far-from-market OSO (1 lot, brackets, will never fill)
    from imbabot.models import StraddleLeg
    entry = round((price + 200) / con.tick_size) * con.tick_size
    leg = StraddleLeg(side=OrderSide.BUY, stop_price=entry, size=1,
                      stop_loss_ticks=48, take_profit_ticks=53,
                      custom_tag="imba-demo-check")
    res = cl.place_straddle_leg(acct, con.id, leg)
    check("5 OSO placed", res.success and res.order_id is not None,
          str(res.error_message))
    oid = res.order_id
    deadline = time.time() + 10
    seen = False
    while time.time() < deadline and not seen:
        seen = any(o["id"] == oid for o in cl.search_open_orders(acct))
        time.sleep(0.5)
    check("5 OSO visible as Working via WS cache", seen)

    # 6 — modify 10pt further away
    new_entry = entry + 10
    ok = cl.modify_order(acct, oid, stop_price=new_entry)
    check("6 modify accepted", ok, fatal=False)
    if ok:
        deadline = time.time() + 10
        moved = False
        while time.time() < deadline and not moved:
            for o in (cl._user_ws.cache.rows("orders") if cl._user_ws else []):
                if o.get("id") == oid and o.get("stopPrice") == new_entry:
                    moved = True
            time.sleep(0.5)
        check("6 cache shows the new stopPrice", moved, fatal=False)

    # 7 — cancel (this disappearance is exactly the OCO monitor's signal)
    check("7 cancel accepted", cl.cancel_order(acct, oid))
    deadline = time.time() + 10
    gone = False
    while time.time() < deadline and not gone:
        gone = all(o["id"] != oid for o in cl.search_open_orders(acct))
        time.sleep(0.5)
    check("7 order left the working set", gone)

    # 8 — forced disconnect -> auto reconnect + resync
    cl._user_ws._ws.close()
    deadline = time.time() + 90
    recovered = False
    while time.time() < deadline and not recovered:
        recovered = cl._user_ws.healthy
        time.sleep(1.0)
    check("8 user socket auto-reconnected + resynced", recovered)

    # 9 — liquidate sweep is a safe no-op with no position
    pos = cl.search_open_positions(acct)
    check("9 no unexpected open position", pos == [], f"got {pos}", fatal=False)

    if args.fill:
        print("    --fill: placing a marketable 1-lot limit (REAL demo trade)...")
        r = cl.place_order(account_id=acct, contract_id=con.id,
                           order_type=OrderType.LIMIT, side=OrderSide.BUY, size=1,
                           limit_price=price + 5 * con.tick_size,
                           custom_tag="imba-demo-fill")
        check("F fill order accepted", r.success, str(r.error_message))
        deadline = time.time() + 20
        net = 0
        while time.time() < deadline and net == 0:
            for p in cl.search_open_positions(acct):
                net = int(p.get("netPos") or 0)
            time.sleep(0.5)
        check("F WS shows netPos +1", net == 1, f"net={net}")
        lr = cl.liquidate_position(acct, con.id)
        check("F liquidated", lr.success, str(lr.error_message))
        deadline = time.time() + 15
        flat = False
        while time.time() < deadline and not flat:
            flat = cl.search_open_positions(acct) == []
            time.sleep(0.5)
        check("F flat after liquidate", flat)

    cl.close()
    print(f"\n{'ALL CHECKS PASSED' if _failures == 0 else f'{_failures} FAILURES'} "
          f"— demo environment {'is ready for the engine rehearsal' if _failures == 0 else 'needs attention'}.")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
