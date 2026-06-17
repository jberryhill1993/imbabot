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

    from .models import OrderSide, OrderType, round_to_tick, points_to_ticks
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

    # 2b) TP=0 disables the take-profit bracket — no limit order is ever sent
    params0 = StrategyParams(entry_points=2, stop_loss_points=3, take_profit_points=0, contracts=1)
    plan0 = build_straddle(contract, 21000.0, params0, tag_prefix="t")
    _check("TP=0 -> 0 TP ticks in plan",
           plan0.long_leg.take_profit_ticks == 0 and plan0.short_leg.take_profit_ticks == 0)

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

    # 3c) weekday-only recurring schedule (production daily fire). 2026-06-04 is
    # a Thursday; Fri=06-05, Sat=06-06, next Monday=06-08.
    from imbabot.scheduler import next_weekday_local_fire
    thursday = local_now
    friday = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone(_td(hours=-5)))
    saturday = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone(_td(hours=-5)))
    wf_same = next_weekday_local_fire("19:40", now=thursday)
    _check("weekday fire later today stays today (Thu)",
           wf_same.date() == thursday.date() and wf_same.weekday() == 3)
    wf_fri = next_weekday_local_fire("06:00", now=friday)        # 06:00 passed Fri -> Mon
    _check("weekday fire after Fri rolls to Monday",
           wf_fri.weekday() == 0 and (wf_fri.hour, wf_fri.minute) == (6, 0))
    wf_sat = next_weekday_local_fire("19:00", now=saturday)      # Sat -> Mon
    _check("weekday fire on Saturday rolls to Monday", wf_sat.weekday() == 0)

    eng_w, _ = make_engine(strategy_fire_time="08:31:00")
    nfw = eng_w.next_fire(now=friday)                            # Fri 12:00 -> Mon 08:31
    _check("engine uses weekday schedule (Mon 08:31)",
           nfw.weekday() == 0 and (nfw.hour, nfw.minute) == (8, 31))
    eng_wt, _ = make_engine(test_mode=True, test_fire_time="19:40", strategy_fire_time="08:31:00")
    _check("test_mode overrides weekday schedule",
           (eng_wt.next_fire(now=local_now).hour, eng_wt.next_fire(now=local_now).minute) == (19, 40))
    eng_r, _ = make_engine(strategy_fire_time="08:31:00", dry_run=True)
    eng_r.arm(on_tick=None)
    _check("daily schedule sets recurring flag", eng_r._recurring is True)
    eng_r.disarm()
    _check("disarm clears recurring flag", eng_r._recurring is False)

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
    _check("BUY entry is a naked STOP above the reference",
           buy_rec["order_type"] == OrderType.STOP and buy_rec["stop_price"] > 21000.0,
           f"type={buy_rec.get('order_type')} stop={buy_rec.get('stop_price')}")
    _check("SELL entry is a naked STOP below the reference",
           sell_rec["order_type"] == OrderType.STOP and sell_rec["stop_price"] < 21000.0,
           f"type={sell_rec.get('order_type')} stop={sell_rec.get('stop_price')}")
    _check("no SL/TP bracket ticks on either entry (platform-managed)",
           all(o.get("stop_loss_ticks") is None and o.get("take_profit_ticks") is None
               for o in fake.placed),
           f"placed={fake.placed}")

    # 5a) OCO end-to-end: a fill cancels the opposite entry; brackets aren't involved
    eng, fake = make_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    _check("straddle rests exactly 2 orders (no bracket children)",
           len(fake.orders) == 2, f"open={list(fake.orders)}")
    long_oid, short_oid = plan.long_leg.order_id, plan.short_leg.order_id
    _check("both legs have order ids", long_oid is not None and short_oid is not None)
    fake.simulate_fill(eng.contract.id, +2)            # long side fills
    eng._monitor_oco(plan, poll_seconds=0.01)
    _check("opposite (short) entry cancelled", short_oid in fake.cancelled,
           f"cancelled={fake.cancelled}")
    _check("filled (long) entry NOT cancelled", long_oid not in fake.cancelled)

    # 5b) mirror image: short fill cancels the long entry
    eng, fake = make_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    long_oid, short_oid = plan.long_leg.order_id, plan.short_leg.order_id
    fake.simulate_fill(eng.contract.id, -2)            # short side fills
    eng._monitor_oco(plan, poll_seconds=0.01)
    _check("opposite (long) entry cancelled on short fill", long_oid in fake.cancelled,
           f"cancelled={fake.cancelled}")
    _check("filled (short) entry NOT cancelled", short_oid not in fake.cancelled)

    # 5c) ProjectX position payload decoding (type: 1=Long, 2=Short, unsigned size)
    from .engine import _net_position_value
    _check("position type 1 (Long) -> +size", _net_position_value({"type": 1, "size": 3}) == 3)
    _check("position type 2 (Short) -> -size", _net_position_value({"type": 2, "size": 3}) == -3)
    _check("legacy signed netPos passthrough", _net_position_value({"netPos": -2}) == -2)

    # 6) semi-auto: both placed, no monitor/cancel
    eng, fake = make_engine(dry_run=False, trade_mode="semi_auto")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    _check("semi-auto places 2 legs", len(fake.placed) == 2)
    _check("semi-auto cancels nothing", fake.cancelled == [])

    # 6b) one-sided straddle: if one leg is rejected, the placed leg is KEPT
    # (TopStep's Position Bracket bounds the risk on fill) and a warning logged
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

    # 10) break-even moves the protective '-SL' stop to the position's entry,
    # and leaves a still-working opposite entry untouched
    from .models import OrderType
    eng, fake = make_engine(dry_run=False, trade_mode="two_trade")
    cid = eng.contract.id
    # a protective stop (bracket child) + a still-working opposite entry
    sl = fake.place_order(account_id=42, contract_id=cid, order_type=OrderType.STOP,
                          side=OrderSide.SELL, size=1, stop_price=20990.0,
                          custom_tag="imbabot-x-L-SL")
    opp = fake.place_order(account_id=42, contract_id=cid, order_type=OrderType.STOP,
                           side=OrderSide.SELL, size=1, stop_price=20988.0,
                           custom_tag="imbabot-x-S")
    fake.simulate_fill(cid, +1, avg_price=21007.3)     # long fill at 21007.3
    eng.break_even()
    moved = fake.orders.get(sl.order_id, {})
    _check("break-even moved the -SL stop to entry tick",
           moved.get("stop_price") == 21007.25, f"got {moved.get('stop_price')}")
    _check("break-even left the opposite entry alone",
           fake.orders.get(opp.order_id, {}).get("stop_price") == 20988.0)
    _check("break-even used modify (not cancel)", fake.cancelled == [] and len(fake.modified) == 1,
           f"cancelled={fake.cancelled} modified={fake.modified}")

    # 10b) break-even with a PLATFORM-managed stop (no '-SL' tag): finds the
    # opposite-side STOP for the contract and moves it. (One-Trade: opposite
    # entry already cancelled, so it's unambiguous.)
    eng, fake = make_engine(dry_run=False, trade_mode="one_trade")
    cid = eng.contract.id
    ts_stop = fake.place_order(account_id=42, contract_id=cid, order_type=OrderType.STOP,
                               side=OrderSide.SELL, size=1, stop_price=20990.0,
                               custom_tag="")          # TopStep bracket: no -SL tag
    fake.simulate_fill(cid, +1, avg_price=21007.3)     # long fill at 21007.3
    eng.break_even()
    moved = fake.orders.get(ts_stop.order_id, {})
    _check("break-even moves platform stop (no -SL tag) to entry tick",
           moved.get("stop_price") == 21007.25, f"got {moved.get('stop_price')}")

    # 10c) ambiguous: two protective-side STOPs and no '-SL' tag -> refuse to guess
    eng, fake = make_engine(dry_run=False, trade_mode="two_trade")
    cid = eng.contract.id
    fake.place_order(account_id=42, contract_id=cid, order_type=OrderType.STOP,
                     side=OrderSide.SELL, size=1, stop_price=20990.0, custom_tag="")
    fake.place_order(account_id=42, contract_id=cid, order_type=OrderType.STOP,
                     side=OrderSide.SELL, size=1, stop_price=20988.0, custom_tag="")
    fake.simulate_fill(cid, +1, avg_price=21007.3)
    eng.break_even()
    _check("break-even refuses to guess when >1 candidate stop",
           fake.modified == [] and fake.cancelled == [],
           f"modified={fake.modified} cancelled={fake.cancelled}")

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1
