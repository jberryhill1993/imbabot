"""Offline self-test: exercises the strategy math, scheduler, and the full engine
fire/OCO/panic flow against the in-memory FakeClient. No network, no real account.

Run via:  python -m imbabot.cli selftest
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, time as dtime

from zoneinfo import ZoneInfo

_PASS = 0
_FAIL = 0


def _check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def run_selftest() -> int:
    # Redirect all config/state to a throwaway dir so we never touch real settings.
    tmp = tempfile.mkdtemp(prefix="imbabot-selftest-")
    os.environ["IMBABOT_CONFIG_DIR"] = tmp

    from .models import OrderSide, round_to_tick, points_to_ticks
    from .strategy import StrategyParams, build_straddle
    from .scheduler import next_fire_time, seconds_until
    from .config import Settings
    from .engine import BotEngine
    from .risk import RiskError
    from ._fake import FakeClient

    print("Imbabot self-test\n-----------------")

    # 1) tick math
    _check("round_to_tick snaps to grid", round_to_tick(21000.07, 0.25) == 21000.0,
           f"got {round_to_tick(21000.07, 0.25)}")
    _check("round_to_tick rounds up", round_to_tick(21000.13, 0.25) == 21000.25,
           f"got {round_to_tick(21000.13, 0.25)}")
    _check("points_to_ticks", points_to_ticks(12, 0.25) == 48,
           f"got {points_to_ticks(12, 0.25)}")
    _check("points_to_ticks floor 1", points_to_ticks(0.0, 0.25) == 1)

    # 2) straddle construction
    contract = FakeClient().resolve_contract("MNQ")
    params = StrategyParams(entry_points=12, stop_loss_points=10, take_profit_points=14, contracts=2)
    plan = build_straddle(contract, 21000.0, params, tag_prefix="t")
    _check("long entry = ref + points", plan.long_leg.stop_price == 21012.0,
           f"got {plan.long_leg.stop_price}")
    _check("short entry = ref - points", plan.short_leg.stop_price == 20988.0,
           f"got {plan.short_leg.stop_price}")
    _check("long side is BUY", plan.long_leg.side == OrderSide.BUY)
    _check("short side is SELL", plan.short_leg.side == OrderSide.SELL)
    _check("SL ticks from points", plan.long_leg.stop_loss_ticks == 40,
           f"got {plan.long_leg.stop_loss_ticks}")
    _check("TP ticks from points", plan.long_leg.take_profit_ticks == 56,
           f"got {plan.long_leg.take_profit_ticks}")
    _check("tags differ", plan.long_leg.custom_tag != plan.short_leg.custom_tag)

    # 3) scheduler: fixed 'now' before the open today -> fire is 09:29:57 today
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 6, 4, 8, 0, 0, tzinfo=tz)
    fire = next_fire_time(dtime(9, 30), capture_offset_seconds=3, now=now)
    _check("fire is 09:29:57", (fire.hour, fire.minute, fire.second) == (9, 29, 57),
           f"got {fire.time()}")
    _check("fire is today", fire.date() == now.date())
    now_after = datetime(2026, 6, 4, 10, 0, 0, tzinfo=tz)
    fire2 = next_fire_time(dtime(9, 30), capture_offset_seconds=3, now=now_after)
    _check("rolls to next day after open", fire2.date() > now_after.date())
    _check("seconds_until positive", seconds_until(fire2, now=now_after) > 0)

    # 3b) test-mode scheduling (custom local fire time)
    from datetime import timezone, timedelta as _td

    from imbabot.scheduler import parse_hms, next_local_fire

    _check("parse_hms HH:MM", parse_hms("19:40") == dtime(19, 40, 0))
    _check("parse_hms HH:MM:SS", parse_hms("9:30:57") == dtime(9, 30, 57))
    local_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone(_td(hours=-5)))
    nf = next_local_fire("19:40", now=local_now)
    _check("test fire later today", (nf.hour, nf.minute) == (19, 40) and nf.date() == local_now.date())
    nf2 = next_local_fire("06:00", now=local_now)
    _check("test fire earlier rolls tomorrow", nf2.date() > local_now.date())

    # helper to build a connected engine on a fake broker
    def make_engine(**overrides):
        s = Settings(username="tester", contract_symbol="MNQ", entry_points=12,
                     stop_loss_points=12, take_profit_points=12, contracts=2,
                     max_trades_per_day=99, max_contracts=5)
        for k, v in overrides.items():
            setattr(s, k, v)
        fake = FakeClient(last=21000.0)
        eng = BotEngine(s, client=fake, log=lambda *a, **k: None)
        eng.connect("fake-key")
        return eng, fake

    eng_t, _ = make_engine(test_mode=True, test_fire_time="19:40")
    _check("engine honors test_mode fire time",
           (eng_t.next_fire(now=local_now).hour, eng_t.next_fire(now=local_now).minute) == (19, 40))

    # 4) dry-run fire places nothing
    eng, fake = make_engine(dry_run=True, trade_mode="one_trade")
    eng._on_fire()
    _check("dry-run captured a plan", eng.last_plan is not None)
    _check("dry-run placed 0 orders", len(fake.placed) == 0, f"placed {len(fake.placed)}")

    # 5) live one-trade: both legs placed, long fill cancels the short entry
    eng, fake = make_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    _check("placed 2 entry legs", len(fake.placed) == 2, f"placed {len(fake.placed)}")
    buy_rec = next(o for o in fake.placed if o["side"] == OrderSide.BUY)
    sell_rec = next(o for o in fake.placed if o["side"] == OrderSide.SELL)
    _check("long brackets signed (SL<0 / TP>0)",
           buy_rec["stop_loss_ticks"] < 0 < buy_rec["take_profit_ticks"],
           f"SL={buy_rec['stop_loss_ticks']} TP={buy_rec['take_profit_ticks']}")
    _check("short brackets signed (SL>0 / TP<0)",
           sell_rec["take_profit_ticks"] < 0 < sell_rec["stop_loss_ticks"],
           f"SL={sell_rec['stop_loss_ticks']} TP={sell_rec['take_profit_ticks']}")
    long_oid, short_oid = plan.long_leg.order_id, plan.short_leg.order_id
    _check("both legs have order ids", long_oid is not None and short_oid is not None)
    fake.simulate_fill(eng.contract.id, +2)            # long side fills
    eng._monitor_oco(plan, poll_seconds=0.01)
    _check("opposite (short) entry cancelled", short_oid in fake.cancelled,
           f"cancelled={fake.cancelled}")
    _check("filled (long) entry NOT cancelled", long_oid not in fake.cancelled)

    # 6) semi-auto: both placed, no monitor/cancel
    eng, fake = make_engine(dry_run=False, trade_mode="semi_auto")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    _check("semi-auto places 2 legs", len(fake.placed) == 2)
    _check("semi-auto cancels nothing", fake.cancelled == [])

    # 6b) one-sided straddle: if one leg is rejected, the placed leg is KEPT
    # (its SL/TP bracket bounds the risk platform-side) and a warning logged
    eng, fake = make_engine(dry_run=False, trade_mode="semi_auto")
    fake.reject_sides = {OrderSide.SELL}
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    _check("one-sided: lone long leg kept",
           plan.long_leg.order_id is not None and plan.long_leg.order_id in fake.orders,
           f"orders={list(fake.orders)}")
    _check("one-sided: nothing cancelled", fake.cancelled == [],
           f"cancelled={fake.cancelled}")

    # 6b2) total placement failure: nothing reached the book -> fire fails loudly
    eng, fake = make_engine(dry_run=False, trade_mode="semi_auto")
    fake.reject_sides = {OrderSide.BUY, OrderSide.SELL}
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    try:
        eng._place_plan(plan)
        _check("total placement failure raises", False, "no error raised")
    except RuntimeError:
        _check("total placement failure raises", True)
    _check("total failure placed nothing", len(fake.placed) == 0,
           f"placed={len(fake.placed)}")

    # 6c) reference-price capture retries once on a transient failure
    eng, fake = make_engine(dry_run=True)
    calls = {"n": 0}
    orig_lp = fake.last_price

    def flaky(cid, live=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient data hiccup")
        return orig_lp(cid, live=live)

    fake.last_price = flaky
    ref = eng._capture_reference_price()
    _check("price capture retries transient failure",
           ref == 21000.0 and calls["n"] == 2, f"ref={ref} calls={calls['n']}")

    # 7) risk guard blocks oversize / dry-run send
    eng, fake = make_engine(dry_run=True, contracts=10, max_contracts=5)
    try:
        eng.risk.check_can_arm(True)
        _check("oversize contracts blocked", False, "no RiskError raised")
    except RiskError:
        _check("oversize contracts blocked", True)
    try:
        eng.risk.check_can_send_orders()
        _check("dry_run blocks sending", False, "no RiskError raised")
    except RiskError:
        _check("dry_run blocks sending", True)

    # 8) emergency stop cancels working orders and flattens positions
    eng, fake = make_engine(dry_run=False, trade_mode="semi_auto")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    fake.simulate_fill(eng.contract.id, +2)
    before_orders = len(fake.placed)
    eng.emergency_stop()
    _check("panic cancelled the working entries", len(fake.cancelled) >= 1,
           f"cancelled={fake.cancelled}")
    flatten = [o for o in fake.placed[before_orders:] if o.get("custom_tag", "").startswith("imbabot-flatten")]
    _check("panic sent a flattening market order", len(flatten) == 1, f"flatten={flatten}")
    _check("flatten is opposite side (SELL of long)",
           bool(flatten) and int(flatten[0]["side"]) == int(OrderSide.SELL))

    # 9) flatten_all closes positions but leaves working orders untouched
    eng, fake = make_engine(dry_run=False, trade_mode="semi_auto")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    fake.simulate_fill(eng.contract.id, +2)
    before = len(fake.placed)
    eng.flatten_all()
    flat = [o for o in fake.placed[before:] if o.get("custom_tag", "").startswith("imbabot-flatten")]
    _check("flatten_all sent a market close", len(flat) == 1 and int(flat[0]["side"]) == int(OrderSide.SELL),
           f"flat={flat}")
    _check("flatten_all left working orders alone", fake.cancelled == [],
           f"cancelled={fake.cancelled}")

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1
