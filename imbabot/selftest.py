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
    params = StrategyParams(entry_points=12, stop_loss_points=10, take_profit_points=14, contracts=2,
                            bot_stop_loss=True, bot_take_profit=True)
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

    # 2b) bot brackets OFF (default) -> 0 ticks (platform-managed, naked entries)
    params_off = StrategyParams(entry_points=2, stop_loss_points=3, take_profit_points=5, contracts=1)
    plan_off = build_straddle(contract, 21000.0, params_off, tag_prefix="t")
    _check("brackets off -> 0 SL/TP ticks in plan",
           plan_off.long_leg.stop_loss_ticks == 0 and plan_off.long_leg.take_profit_ticks == 0)

    # 2c) TP=0 with bot_take_profit on still disables the take-profit bracket
    params0 = StrategyParams(entry_points=2, stop_loss_points=3, take_profit_points=0, contracts=1,
                             bot_stop_loss=True, bot_take_profit=True)
    plan0 = build_straddle(contract, 21000.0, params0, tag_prefix="t")
    _check("TP=0 -> 0 TP ticks in plan",
           plan0.long_leg.take_profit_ticks == 0 and plan0.short_leg.take_profit_ticks == 0)
    _check("SL on with TP=0 -> SL ticks present",
           plan0.long_leg.stop_loss_ticks == 12, f"got {plan0.long_leg.stop_loss_ticks}")

    # 2d) stop-limit entries: limit caps slippage above the buy / below the sell
    sl_params = StrategyParams(entry_points=12, contracts=1,
                               entry_order_type="stop_limit", entry_limit_offset_ticks=4)
    sl_plan = build_straddle(contract, 21000.0, sl_params, tag_prefix="t")
    _check("stop-limit long: limit = stop + offset (1.0pt)",
           sl_plan.long_leg.limit_price == 21013.0, f"got {sl_plan.long_leg.limit_price}")
    _check("stop-limit short: limit = stop - offset",
           sl_plan.short_leg.limit_price == 20987.0, f"got {sl_plan.short_leg.limit_price}")
    plain = build_straddle(contract, 21000.0, StrategyParams(entry_points=12, contracts=1),
                           tag_prefix="t")
    _check("plain stop entry has no limit price", plain.long_leg.limit_price is None)
    # placement maps the limit price to a STOP_LIMIT order type
    f2 = FakeClient()
    f2.place_straddle_leg(1, "C", sl_plan.long_leg)
    _check("stop-limit leg placed as STOP_LIMIT type",
           int(f2.placed[-1]["order_type"]) == int(OrderType.STOP_LIMIT)
           and f2.placed[-1]["limit_price"] == 21013.0, f"placed={f2.placed[-1]}")

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

    # 4c) bot-managed brackets ON -> signed SL/TP are attached again
    eng, fake = make_engine(dry_run=False, trade_mode="one_trade",
                            bot_stop_loss=True, bot_take_profit=True, take_profit_points=12)
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    buy_rec = next(o for o in fake.placed if o["side"] == OrderSide.BUY)
    sell_rec = next(o for o in fake.placed if o["side"] == OrderSide.SELL)
    _check("bot SL+TP on: long brackets signed (SL<0 / TP>0)",
           buy_rec["stop_loss_ticks"] < 0 < buy_rec["take_profit_ticks"],
           f"SL={buy_rec['stop_loss_ticks']} TP={buy_rec['take_profit_ticks']}")
    _check("bot SL+TP on: short brackets signed (SL>0 / TP<0)",
           sell_rec["take_profit_ticks"] < 0 < sell_rec["stop_loss_ticks"],
           f"SL={sell_rec['stop_loss_ticks']} TP={sell_rec['take_profit_ticks']}")

    # 4d) only SL enabled -> SL attached, TP omitted entirely
    eng, fake = make_engine(dry_run=False, trade_mode="one_trade",
                            bot_stop_loss=True, bot_take_profit=False, take_profit_points=12)
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    _check("bot SL only: SL present, TP omitted",
           all(o.get("stop_loss_ticks") not in (None, 0) and o.get("take_profit_ticks") is None
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

    # 9b) One-Trade OCO race: a leg that fills AND closes (TP) between polls must
    # still cancel the opposite entry (the bug that let a 2nd trade fill on 06/24).
    eng, fake = make_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    acct, cid = eng.account.id, eng.contract.id
    seen = set()
    acted0 = eng._oco_scan(plan, acct, cid, seen)
    _check("OCO: no action while both entries rest",
           acted0 is False and fake.cancelled == [], f"acted={acted0} cx={fake.cancelled}")
    # short entry fills, then its bracket closes the position before the next poll:
    # the entry is gone from the book but there is NO open position.
    fake.orders.pop(plan.short_leg.order_id, None)
    acted1 = eng._oco_scan(plan, acct, cid, seen)
    _check("OCO: fast fill-then-flat still cancels the other entry",
           acted1 is True and plan.long_leg.order_id in fake.cancelled,
           f"acted={acted1} cancelled={fake.cancelled}")
    _check("OCO: exactly the one surviving entry was cancelled", len(fake.cancelled) == 1,
           f"cancelled={fake.cancelled}")

    # 10) analysis: Databento ingester, 2-D backtest, sizing, morning model
    from .analysis.types import DayRecord, OpenBar
    from .analysis.backtest import (simulate_day, backtest_2d, BracketSpec,
                                    spread_grid, stop_grid)
    from .analysis.sizing import size_for_target, point_value
    from .analysis.databento_csv import parse_databento_csv, build_day_records_1s
    from datetime import timezone, timedelta

    # 10a) Databento parse (ISO + decimal) -> ref price + second offsets
    ou = datetime(2026, 6, 22, 13, 30, 0, tzinfo=timezone.utc)
    rows = ["ts_event,open,high,low,close,volume,symbol"]
    for i, (o, h, l, c) in enumerate([(30823.25, 30823.5, 30811, 30812),
                                      (30812, 30819, 30811, 30818)]):
        t = (ou + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
        rows.append(f"{t},{o},{h},{l},{c},10,NQU6")
    dbp = os.path.join(tmp, "db.csv")
    open(dbp, "w").write("\n".join(rows) + "\n")
    recs = build_day_records_1s(parse_databento_csv(dbp), open_minutes=15)
    _check("databento parse: ref + second offsets",
           bool(recs) and recs[0].ref_price == 30823.25
           and [b.minute for b in recs[0].open_bars] == [0, 1], f"recs={recs}")

    # 10b) fine_grained honors entry-bar wicks (coarse uses close-proxy)
    wick = DayRecord(date="d", ref_price=21000, open_bars=[
        OpenBar(0, 21000, 21012, 21001, 21011, 1),   # long +10 fills; low 21001 <= SL(21002)
        OpenBar(1, 21011, 21025, 21010, 21024, 1)])  # later TP +13 = 21023
    br = BracketSpec(stop_points=8, target_points=13)
    _check("entry-bar pre-trigger low does not stop (resolves later to target)",
           simulate_day(wick, 10, br).resolved == "target",
           f"got {simulate_day(wick, 10, br).resolved}")

    # 10b2) stop-limit entry: misses a violent cross, fills a calm one (no entry slip)
    from imbabot.analysis.backtest import CostSpec
    calm = DayRecord(date="c", ref_price=21000, open_bars=[
        OpenBar(0, 21000, 21010.5, 20999, 21010.25, 1),    # crosses +10 by 0.5 (<= tol 1)
        OpenBar(1, 21010, 21025, 21009, 21024, 1)])         # -> TP +13
    violent = DayRecord(date="v", ref_price=21000, open_bars=[
        OpenBar(0, 21000, 21016, 20999, 21015, 1)])         # shoots +16, 6pt past trigger
    cst = CostSpec(slippage_points=2.0)
    o_calm = simulate_day(calm, 10, br, fine_grained=True, costs=cst,
                          entry_mode="stop_limit", limit_tolerance=1.0)
    o_viol = simulate_day(violent, 10, br, fine_grained=True, costs=cst,
                          entry_mode="stop_limit", limit_tolerance=1.0)
    o_stop = simulate_day(calm, 10, br, fine_grained=True, costs=cst, entry_mode="stop")
    _check("stop-limit misses the violent breakout", o_viol.resolved == "miss" and not o_viol.triggered)
    _check("stop-limit fills the calm cross", o_calm.triggered and o_calm.resolved == "target")
    _check("stop-limit fill has no entry slippage (beats stop by ~slip)",
           o_calm.pnl_points > o_stop.pnl_points, f"limit={o_calm.pnl_points} stop={o_stop.pnl_points}")

    # 10c) 2-D backtest: tight spread+stop is the worst whipsaw cell
    rev = DayRecord(date="r", ref_price=21000, open_bars=[
        OpenBar(0, 21000, 21001, 20988, 20990, 1),    # tags 12-spread short
        OpenBar(1, 20990, 21010, 20989, 21009, 1)])   # reverses up -> stop
    bt = backtest_2d([wick, rev], target_points=13, spreads=spread_grid(8, 20, 2),
                     stops=stop_grid(6, 16, 2), fine_grained=True)
    _check("2-D backtest produces a best cell", bt.best_cell() is not None)
    _check("2-D records per-day best outcome", len(bt.per_day_best) == 2)

    # 10d) sizing math: contracts, brackets, cap, honest downside
    dpp = point_value(5.0, 0.25)          # NQ $20/pt
    sp = size_for_target(1000, tp_points=13.3, stop_points=9, dollars_per_point=dpp,
                         winrate=0.6, max_contracts=10)
    _check("sizing: 4 contracts for $1000 target", sp.contracts == 4, f"got {sp.contracts}")
    _check("sizing: SL bracket = stop*ct*$/pt", round(sp.sl_bracket_dollars) == round(9 * 4 * dpp))
    _check("sizing: caps at max_contracts",
           size_for_target(5000, tp_points=13.3, stop_points=9, dollars_per_point=dpp,
                           winrate=0.6, max_contracts=10).capped)

    # 11) TICK engine: the core fix is resolving TP vs SL in true time order.
    from .analysis.tick_data import TickDay, Tick
    from .analysis.tick_sim import simulate_tick_straddle
    from .analysis.tick_features import label_day, volatility_level
    from .analysis.sizing import tp_plan_from_spike

    def _td(rows):  # rows = (t, price, bid, ask)
        return TickDay("t", "NQ", [Tick(*r) for r in rows])

    # 11a) TP printed BEFORE SL in time -> target (a 1-sec OHLCV bar would wrongly stop:
    # its high(105.5) and low(99) both inside one bar, adverse-first rule = stop).
    tp_first = _td([(-3, 100, 99.75, 100.25), (0, 100, 99.75, 100.25),
                    (1, 102, 101.75, 102.25),     # long triggers, fills ask 102.25
                    (2, 105.5, 105.25, 105.75),   # TP = 102.25+3 = 105.25 -> hit
                    (3, 99, 98.75, 99.25)])        # SL would be here, but LATER
    o = simulate_tick_straddle(tp_first, entry_points=2, tp_points=3, sl_points=2)
    _check("tick: TP-before-SL resolves to target", o.side == "long" and o.resolved == "target",
           f"got {o.side}/{o.resolved}")

    # 11b) SL printed BEFORE TP -> stop
    sl_first = _td([(-3, 100, 99.75, 100.25), (0, 100, 99.75, 100.25),
                    (1, 102, 101.75, 102.25),     # long entry 102.25
                    (2, 99, 98.75, 99.25),        # SL 100.25 -> hit first
                    (3, 106, 105.75, 106.25)])     # TP later
    o = simulate_tick_straddle(sl_first, entry_points=2, tp_points=3, sl_points=2)
    _check("tick: SL-before-TP resolves to stop", o.resolved == "stop", f"got {o.resolved}")

    # 11c) short whipsaw: triggers short, reverses up into the stop (the Jun-25 shape)
    whip = _td([(-3, 100, 99.75, 100.25), (0, 100, 99.75, 100.25),
                (1, 98, 97.75, 98.25),            # short triggers, fills bid 97.75
                (2, 100.5, 100.25, 100.75)])       # SL = 97.75+2 = 99.75 -> stop
    o = simulate_tick_straddle(whip, entry_points=2, tp_points=3, sl_points=2)
    _check("tick: short whipsaw -> stop, labeled whipsaw",
           o.side == "short" and o.resolved == "stop" and label_day(o) == "whipsaw",
           f"got {o.side}/{o.resolved}/{label_day(o)}")

    # 11d) no touch -> no trade
    calm = _td([(-3, 100, 99.75, 100.25), (0, 100.5, 100.25, 100.75), (1, 99.5, 99.25, 99.75)])
    o = simulate_tick_straddle(calm, entry_points=2, tp_points=3, sl_points=2)
    _check("tick: no touch -> no trade", not o.triggered and label_day(o) == "no-trade")

    # 11e) TP-driven sizing from a predicted spike
    sp = tp_plan_from_spike(20.0, 800.0, dollars_per_point=20.0, max_contracts=10,
                            counter_poke=4.0, slip_margin=3.0, min_spread=5.0)
    _check("sizing: spike 20 -> entry +/-5, feasible", sp.feasible and sp.entry_spread == 5.0,
           f"got {sp.entry_spread}/{sp.feasible}")
    _check("sizing: contracts = ceil(TP$/(T*$pt))", sp.contracts == 4,
           f"got {sp.contracts} (T={sp.tp_distance_points})")
    small = tp_plan_from_spike(6.0, 800.0, slip_margin=3.0, min_spread=5.0)
    _check("sizing: tiny spike -> not feasible", not small.feasible)

    # 11f) volatility bands + news bump
    _check("vol: low VIX -> LOW", volatility_level(12.0, 0) == "LOW")
    _check("vol: mid VIX -> MEDIUM", volatility_level(18.0, 0) == "MEDIUM")
    _check("vol: high VIX -> HIGH", volatility_level(25.0, 0) == "HIGH")
    _check("vol: news bumps a band", volatility_level(14.0, 2) == "MEDIUM")

    # 11g) entry floor 10 + scale-contracts-to-spike + NO-TRADE on too-small
    big = tp_plan_from_spike(35.0, 800.0, min_spread=10.0)        # big spike -> few contracts
    _check("sizing: entry floor 10 (big spike)", big.feasible and big.entry_spread == 10.0)
    _check("sizing: big spike -> few contracts", big.contracts <= 3, f"got {big.contracts}")
    smallsp = tp_plan_from_spike(16.0, 800.0, min_spread=10.0)    # small spike -> scale UP
    _check("sizing: small spike scales contracts up", smallsp.feasible and smallsp.contracts > big.contracts,
           f"small={smallsp.contracts} big={big.contracts}")
    notrade = tp_plan_from_spike(12.0, 800.0, min_spread=10.0)    # too small -> NO-TRADE
    _check("sizing: too-small spike -> NO-TRADE", not notrade.feasible)

    # 11g2) 5-contract cap alert + recommended TP $/SL $ (must be self-consistent)
    capd = tp_plan_from_spike(24.0, 2000.0, min_spread=10.0)      # $2k target exceeds the 5ct cap
    _check("sizing: contracts capped at 5", capd.contracts <= 5 and capd.max_contracts == 5,
           f"got {capd.contracts}/{capd.max_contracts}")
    _check("sizing: cap alert fires (wanted > cap)", capd.capped and capd.contracts_wanted > 5,
           f"capped={capd.capped} wanted={capd.contracts_wanted}")
    _check("sizing: recommended SL = 5 * 8pt * $20", capd.recommended_sl_dollars == 5 * 8.0 * 20.0,
           f"got {capd.recommended_sl_dollars}")
    _check("sizing: recommended TP <= true 5ct value", capd.recommended_tp_dollars <= 5 * capd.tp_distance_points * 20.0,
           f"got {capd.recommended_tp_dollars} vs {5*capd.tp_distance_points*20}")
    # The whole point: typing the recommended TP back in sizes to exactly 5 ct, NOT capped.
    reenter = tp_plan_from_spike(24.0, capd.recommended_tp_dollars, min_spread=10.0)
    _check("sizing: re-entering recommended TP -> exactly 5ct, no warning",
           reenter.contracts == 5 and not reenter.capped,
           f"contracts={reenter.contracts} capped={reenter.capped} rec={capd.recommended_tp_dollars}")

    # 11h) news calendar impact levels
    from .analysis import calendar as econ
    _check("calendar: NFP first Friday HIGH",
           any("Nonfarm" in n and imp == "high" for n, imp, *_ in econ.event_flag("2026-06-05").events))
    _check("calendar: Thursday jobless LOW",
           any("Jobless" in n and imp == "low" for n, imp, *_ in econ.event_flag("2026-06-25").events))
    _check("calendar: FOMC flag", econ.event_flag("2025-07-30").fomc)

    # 11i) spike model fit + predict round-trip (synthetic: high VIX -> bigger spike)
    from .analysis.spike_model import SpikeModel
    from .analysis.tick_dataset import DayRow
    syn = []
    for i in range(40):
        vix = 12.0 + (i % 20)          # spread of regimes
        thrust = vix * 1.5             # bigger VIX -> bigger spike (learnable)
        syn.append(DayRow(date=f"d{i}", feats={"prior_vix": vix, "vix_change": 0.0, "news_score": 0.0,
                          "fomc": 0.0, "dow": float(i % 5), "recent_thrust": thrust},
                          thrust=thrust, counter_poke=2.0, is_big=1 if thrust >= 30 else 0,
                          label="clean-winner" if thrust >= 20 else "whipsaw", pnl_points=0.0,
                          news_label="none", prior_vix=vix))
    mdl = SpikeModel().fit(syn)
    lo = mdl.predict({"prior_vix": 13.0, "vix_change": 0.0, "news_score": 0.0, "fomc": 0.0, "dow": 1.0, "recent_thrust": 19.5})
    hi = mdl.predict({"prior_vix": 29.0, "vix_change": 0.0, "news_score": 0.0, "fomc": 0.0, "dow": 1.0, "recent_thrust": 43.5})
    _check("spike model: calibrated after fit", mdl.calibrated and hi.calibrated)
    _check("spike model: higher VIX -> bigger predicted spike", hi.expected_spike > lo.expected_spike,
           f"lo={lo.expected_spike} hi={hi.expected_spike}")

    # 12) market calendar: weekends + US holidays, next trading session
    from datetime import date as _date, datetime as _dt, timezone as _tz2, timedelta as _td2
    from .market_calendar import is_trading_day, next_trading_session, is_holiday
    _check("calendar: Saturday is not a trading day", not is_trading_day(_date(2026, 6, 27)))
    _check("calendar: MLK Mon 2026-01-19 is a holiday", is_holiday(_date(2026, 1, 19)))
    _check("calendar: Good Friday 2026-04-03 is a holiday", is_holiday(_date(2026, 4, 3)))
    _check("calendar: normal weekday trades", is_trading_day(_date(2026, 6, 29)))
    _check("calendar: Sat -> next session Monday",
           next_trading_session(_date(2026, 6, 27)) == _date(2026, 6, 29))
    _check("calendar: rolls over a holiday (Jul 3 obs -> Jul 6)",
           next_trading_session(_date(2026, 7, 3)) == _date(2026, 7, 6))

    # 12b) auto-fire skips holidays: Fri 2026-01-16 after the time -> Tue (MLK Mon skipped)
    from .scheduler import next_weekday_local_fire
    fri = _dt(2026, 1, 16, 12, 0, 0, tzinfo=_tz2(_td2(hours=-5)))
    nf = next_weekday_local_fire("08:31:00", now=fri)
    _check("auto-fire skips weekend+MLK holiday -> Tuesday",
           nf.date() == _date(2026, 1, 20), f"got {nf.date()}")

    # 12c) morning_plan resolves a closed day to the next session
    from .analysis.tick_runner import morning_plan, TRADE_MIN
    mp = morning_plan("2026-06-27", target_dollars=800.0)   # a Saturday
    _check("morning plan: closed day -> next session + flag",
           mp.session_date == "2026-06-29" and mp.market_closed_today,
           f"got {mp.session_date}/{mp.market_closed_today}")

    # 12d) TRADE line set to the walk-forward-validated 20-pt cutoff (was 18; 18->20 lifts
    # win-rate 49%->56% and filters whipsaw days that sat just over the old line, e.g. 4/13).
    _check("morning trade line == 20 (matches walkforward spike_min)",
           TRADE_MIN == 20.0, f"got {TRADE_MIN}")

    # 12e) overnight-gap whipsaw filter: small-gap opens (<=40pt) churn -> a TRADE downgrades
    # to NO-TRADE (validated: win 50->58%, $65.7->$104.5/day/ct OOS; skips the live 6/29 loss).
    from .analysis.tick_runner import GAP_MIN
    _check("gap filter threshold == 40", GAP_MIN == 40.0, f"got {GAP_MIN}")
    mp_small = morning_plan("2026-06-24", target_dollars=800.0, overnight_gap=5.0)
    mp_big = morning_plan("2026-06-24", target_dollars=800.0, overnight_gap=200.0)
    # 6/24 predicts a tradeable spike; only the small-gap variant may downgrade it.
    _check("gap filter: small gap downgrades TRADE -> NO-TRADE",
           (not mp_small.gap_filtered) or mp_small.decision == "NO-TRADE",
           f"got {mp_small.decision}/{mp_small.gap_filtered}")
    _check("gap filter: big gap never gap-filters", not mp_big.gap_filtered,
           f"got {mp_big.decision}/{mp_big.gap_filtered}")
    if mp_big.decision == "TRADE":   # when the spike clears 20, verify the small-gap flip fires
        _check("gap filter: flips this TRADE day", mp_small.gap_filtered and mp_small.decision == "NO-TRADE",
               f"got {mp_small.decision}/{mp_small.gap_filtered}")

    # 12f) DayRow carries overnight_gap (the dataset field the walk-forward filter reads)
    from .analysis.tick_dataset import DayRow
    _check("DayRow has overnight_gap", "overnight_gap" in DayRow.__dataclass_fields__)

    # 12g) gap freshness: PAST sessions are always "fresh" (filter applies); the field exists so a
    # too-early recalc (>60min before a FUTURE open) skips the filter with a caveat instead.
    _check("gap freshness: past session counts as fresh (filter active)",
           mp_small.gap_fresh, f"got {mp_small.gap_fresh}")
    _check("MorningTickPlan has gap_fresh",
           "gap_fresh" in type(mp_small).__dataclass_fields__)

    # 12h) pre-open trigger fix: reference captured 1s before the open (was 3s — orders resting
    # ~2s pre-open cost −$4,660/yr at 4ct; 4 of 5 outcome-flips were winners turned losers).
    from .config import Settings as _S
    _check("capture offset default == 1s", _S().capture_offset_seconds == 1,
           f"got {_S().capture_offset_seconds}")
    import json as _json, tempfile as _tf, os as _os
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        fh.write(_json.dumps({"capture_offset_seconds": 3, "username": "x"}))
        _p = fh.name
    migrated = _S.load(__import__("pathlib").Path(_p))
    _os.unlink(_p)
    _check("capture offset migration: stored 3 -> 1", migrated.capture_offset_seconds == 1,
           f"got {migrated.capture_offset_seconds}")

    # 13) Tradovate second broker (0.2.5) — groundwork
    # 13a) BrokerAdapter protocol: the duck-typed engine contract, pinned. If the
    # engine ever grows a new client call, add it to broker.py and every backend.
    from .broker import BrokerAdapter
    from .projectx import ProjectXClient
    _check("FakeClient conforms to BrokerAdapter", isinstance(FakeClient(), BrokerAdapter))
    _check("ProjectXClient conforms to BrokerAdapter",
           isinstance(ProjectXClient(), BrokerAdapter))

    # 13b) Tradovate settings fields (non-secret) default sanely
    s_tdv = Settings()
    _check("tdv defaults: demo environment", s_tdv.tdv_environment == "demo",
           f"got {s_tdv.tdv_environment}")
    _check("tdv defaults: app id", s_tdv.tdv_app_id == "Imbabot")
    _check("tdv defaults: no username/device", s_tdv.tdv_username == "" and s_tdv.tdv_device_id == "")

    # 13c) credential blob round-trip via the 0600-file fallback. The real OS
    # keyring is deliberately bypassed so the selftest never writes to the
    # user's Windows Credential Manager.
    from . import config as _cfg
    _orig_keyring = _cfg._keyring
    _cfg._keyring = lambda: None
    try:
        secret = {"password": "hunter-pass-x", "cid": 123, "sec": "sec-uuid-x"}
        backend_used = _cfg.store_tradovate_credentials("selftest-user", secret)
        _check("tdv store falls back to file", backend_used == "file", f"got {backend_used}")
        blob = _cfg.load_tradovate_credentials("selftest-user")
        _check("tdv credentials round-trip", blob == secret, f"got {blob}")
        # settings.json must never carry the secrets
        stext = Settings(tdv_username="selftest-user").save().read_text(encoding="utf-8")
        _check("settings.json carries no tdv secrets",
               "hunter-pass-x" not in stext and "sec-uuid-x" not in stext)
        # tdv entries share the credentials file with the ProjectX key without clashing
        _cfg.store_api_key("selftest-user", "px-key-x")
        _check("tdv + projectx keys coexist in the file",
               _cfg.load_api_key("selftest-user") == "px-key-x"
               and _cfg.load_tradovate_credentials("selftest-user") == secret)
        _cfg.clear_tradovate_credentials("selftest-user")
        _cfg.clear_api_key("selftest-user")
        _check("tdv credentials cleared",
               _cfg.load_tradovate_credentials("selftest-user") is None)
        # env override wins over stored values
        os.environ["IMBABOT_TDV_PASSWORD"] = "env-pass-x"
        os.environ["IMBABOT_TDV_CID"] = "77"
        try:
            blob2 = _cfg.load_tradovate_credentials("whoever")
            _check("tdv env override wins",
                   bool(blob2) and blob2.get("password") == "env-pass-x" and blob2.get("cid") == "77",
                   f"got {blob2}")
        finally:
            del os.environ["IMBABOT_TDV_PASSWORD"]
            del os.environ["IMBABOT_TDV_CID"]
    finally:
        _cfg._keyring = _orig_keyring

    # 13d) Tradovate token lifecycle — offline, scripted transport + fake clock.
    from .tradovate.auth import (RENEW_HEADROOM, TokenManager, TradovateAuthError,
                                 TradovateCredentials)

    creds = TradovateCredentials(username="u", password="p", cid="123",
                                 sec="s", app_id="Imbabot", device_id="dev1")
    _check("tdv auth body: cid sent as int, appVersion present",
           creds.body()["cid"] == 123 and bool(creds.body()["appVersion"]))

    class _Script:
        """Scripted HTTP transport + clock + sleep recorder."""
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []          # (method, path, body)
            self.now = 1_000_000.0
            self.slept = []
        def http(self, method, url, body, headers, timeout):
            self.calls.append((method, url.rsplit("/", 1)[-1], body))
            return self.responses.pop(0)
        def clock(self):
            return self.now
        def sleep(self, s):
            self.slept.append(s)

    def _tok(exp_offset, token="tok-1", md=None, script_now=1_000_000.0):
        iso = datetime.fromtimestamp(script_now + exp_offset, tz=ZoneInfo("UTC")
                                     ).strftime("%Y-%m-%dT%H:%M:%SZ")
        d = {"accessToken": token, "expirationTime": iso, "userId": 9}
        if md:
            d["mdAccessToken"] = md
        return (200, d)

    # acquire parses token + expiry; md token falls back to access token when absent
    sc = _Script([_tok(90 * 60, "tok-A")])
    tm = TokenManager("https://demo.example/v1", creds,
                      http=sc.http, clock=sc.clock, sleep=sc.sleep)
    _check("tdv acquire returns token", tm.access_token() == "tok-A")
    _check("tdv acquire hit accesstokenrequest",
           sc.calls[0][1] == "accesstokenrequest" and sc.calls[0][0] == "POST")
    _check("tdv md token falls back to access token", tm.md_token() == "tok-A")
    _check("tdv cached: no extra http calls", len(sc.calls) == 1, f"{len(sc.calls)} calls")
    _check("tdv authenticated property", tm.authenticated)

    # near expiry -> proactive renew (GET renewaccesstoken), BEFORE any failure
    sc.now += 90 * 60 - RENEW_HEADROOM + 1
    sc.responses.append(_tok(90 * 60, "tok-B", md="md-B", script_now=sc.now))
    _check("tdv proactive renew returns fresh token", tm.access_token() == "tok-B")
    _check("tdv renew used GET renewaccesstoken",
           sc.calls[-1][0] == "GET" and sc.calls[-1][1] == "renewaccesstoken")
    _check("tdv renew captured mdAccessToken", tm.md_token() == "md-B")

    # renew failure falls back to a full re-acquire
    sc.now += 90 * 60
    sc.responses += [(500, {}), _tok(90 * 60, "tok-C", script_now=sc.now)]
    _check("tdv renew failure -> full re-acquire", tm.access_token() == "tok-C")
    _check("tdv re-acquire hit accesstokenrequest", sc.calls[-1][1] == "accesstokenrequest")

    # p-ticket penalty: wait p-time, retry with the ticket in the body
    sc2 = _Script([(200, {"p-ticket": "T-1", "p-time": 4}), _tok(90 * 60, "tok-P")])
    tm2 = TokenManager("https://demo.example/v1", creds,
                       http=sc2.http, clock=sc2.clock, sleep=sc2.sleep)
    _check("tdv p-ticket retry succeeds", tm2.access_token() == "tok-P")
    _check("tdv p-ticket waited p-time", sc2.slept == [4.0], f"got {sc2.slept}")
    _check("tdv p-ticket re-sent with ticket", sc2.calls[-1][2].get("p-ticket") == "T-1")

    # p-captcha cannot be automated -> clear user-facing error
    sc3 = _Script([(200, {"p-ticket": "T-2", "p-time": 2, "p-captcha": True})])
    tm3 = TokenManager("https://demo.example/v1", creds,
                       http=sc3.http, clock=sc3.clock, sleep=sc3.sleep)
    try:
        tm3.access_token()
        _check("tdv p-captcha raises", False, "no exception")
    except TradovateAuthError as exc:
        _check("tdv p-captcha raises", "captcha" in str(exc).lower())

    # bad credentials -> TradovateAuthError with the server's errorText
    sc4 = _Script([(401, {"errorText": "Incorrect username or password"})])
    tm4 = TokenManager("https://demo.example/v1", creds,
                       http=sc4.http, clock=sc4.clock, sleep=sc4.sleep)
    try:
        tm4.access_token()
        _check("tdv bad creds raise", False, "no exception")
    except TradovateAuthError as exc:
        _check("tdv bad creds raise", "Incorrect" in str(exc))

    # 13d2) transport must preserve list JSON — /account/list, /order/list and
    # /position/list return ARRAYS; wrapping them broke the first live connect
    # ("'str' object has no attribute 'get'"). Regression-pinned.
    from .tradovate.auth import _coerce_json
    _check("tdv http: list JSON preserved (live-connect regression)",
           _coerce_json([{"id": 1}]) == [{"id": 1}]
           and _coerce_json({"a": 1}) == {"a": 1}
           and _coerce_json("Access denied") == {"_raw": "Access denied"})

    # 13e) TradovateClient REST + order mapping — scripted transport, no sockets.
    from .tradovate.client import (ACTION_MAP, ORDER_TYPE_MAP, TradovateClient,
                                   TradovateError, bracket_prices)
    from .tradovate import safety as tdv_safety

    _check("tdv order-type map covers every engine type",
           set(ORDER_TYPE_MAP) == {OrderType.LIMIT, OrderType.MARKET,
                                   OrderType.STOP_LIMIT, OrderType.STOP,
                                   OrderType.TRAILING_STOP})
    _check("tdv action map", ACTION_MAP[OrderSide.BUY] == "Buy"
           and ACTION_MAP[OrderSide.SELL] == "Sell")

    # bracket math: BUY entry 21012, SL 48 ticks below, TP 56 above (0.25 tick)
    sl, tp = bracket_prices(OrderSide.BUY, 21012.0, 48, 56, 0.25)
    _check("tdv OSO bracket BUY: SL below / TP above",
           sl == 21000.0 and tp == 21026.0, f"got {sl}/{tp}")
    sl2, tp2 = bracket_prices(OrderSide.SELL, 20988.0, 48, 56, 0.25)
    _check("tdv OSO bracket SELL mirrored",
           sl2 == 21000.0 and tp2 == 20974.0, f"got {sl2}/{tp2}")
    _check("tdv OSO bracket zero ticks -> no bracket",
           bracket_prices(OrderSide.BUY, 21000.0, 0, 0, 0.25) == (None, None))

    class _Rest:
        """Scripted REST router for TradovateClient (records every call)."""
        def __init__(self):
            self.calls = []
        def __call__(self, method, url, body, headers, timeout):
            path = url.split("/v1/")[-1].split("?")[0]
            self.calls.append((method, path, body))
            if path == "auth/accesstokenrequest":
                return 200, {"accessToken": "tok", "userId": 5,
                             "expirationTime": "2099-01-01T00:00:00Z"}
            if path == "account/list":
                return 200, [{"id": 7001, "name": "DEMO7001", "active": True}]
            if path == "contract/suggest":
                t = url.split("t=")[-1].split("&")[0]      # requested root
                return 200, [{"id": 900, "name": f"{t}Z5", "contractMaturityId": 1},
                             {"id": 901, "name": f"{t}U6", "contractMaturityId": 2},
                             {"id": 902, "name": f"{t}Z6", "contractMaturityId": 3}]
            if path == "contractMaturity/item":
                exp = {1: "2025-12-19", 2: "2026-09-18", 3: "2026-12-18"}
                mid = int(url.split("id=")[-1])
                return 200, {"id": mid, "expirationDate": f"{exp[mid]}T13:30:00Z"}
            if path == "product/find":
                name = url.split("name=")[-1].split("&")[0]
                vpp = 2.0 if name == "MNQ" else 20.0       # MNQ $0.50/tick, NQ $5
                return 200, {"name": name, "tickSize": 0.25, "valuePerPoint": vpp}
            if path == "order/placeoso":
                return 200, {"orderId": 111, "oso1Id": 112, "oso2Id": 113}
            if path == "order/placeorder":
                return 200, {"orderId": 120}
            if path == "order/cancelorder":
                return 200, {"commandId": 55}
            if path == "order/liquidateposition":
                return 200, {"orderId": 130}
            if path == "order/list":
                return 200, [{"id": 111, "accountId": 7001, "ordStatus": "Working",
                              "action": "Buy", "contractId": 901},
                             {"id": 119, "accountId": 7001, "ordStatus": "Filled",
                              "action": "Sell", "contractId": 901}]
            if path == "position/list":
                return 200, [{"accountId": 7001, "contractId": 901, "netPos": -1,
                              "netPrice": 21001.5},
                             {"accountId": 7001, "contractId": 555, "netPos": 0}]
            return 404, {}

    rest = _Rest()
    warns = []
    s_c = Settings(backend="tradovate", tdv_environment="demo", tdv_username="u")
    cl = TradovateClient(s_c, log=lambda m, level="info": warns.append((level, m)),
                         http=rest, enable_ws=False)
    cl.authenticate("u", "pw-direct")
    _check("tdv client authenticates via password arg", cl.authenticated)
    _check("tdv client generated a stable device id", len(s_c.tdv_device_id) == 32)
    _check("tdv startup banner logged",
           any("TRADOVATE CONNECTED" in m and "env=DEMO" in m and
               "LIVE_TRADING=False" in m for _, m in warns))
    _check("tdv banner leaks no secrets",
           not any("pw-direct" in m for _, m in warns))

    accts = cl.search_accounts()
    _check("tdv accounts mapped", accts[0].id == 7001 and accts[0].name == "DEMO7001")

    con = cl.resolve_contract("MNQ")
    _check("tdv front month picked (nearest future expiry)",
           con.name == "MNQU6" and con.id == "901", f"got {con.name}/{con.id}")
    _check("tdv tick math from product", con.tick_size == 0.25 and con.tick_value == 0.5)

    # symbol parsing: TopStep-style names must resolve (live-found bugs:
    # 'NQU26' -> no contract; 'NQU6' -> root misread as 'NQU', tick lookup died)
    from .tradovate.client import split_symbol
    _check("tdv split_symbol: root only", split_symbol("MNQ") == ("MNQ", None, None))
    _check("tdv split_symbol: NQU6", split_symbol("NQU6") == ("NQ", "U", "6"))
    _check("tdv split_symbol: TopStep NQU26 -> NQ U 6",
           split_symbol("NQU26") == ("NQ", "U", "6"))
    _check("tdv split_symbol: MNQZ25", split_symbol("MNQZ25") == ("MNQ", "Z", "5"))
    con_ts = cl.resolve_contract("NQU26")
    _check("tdv TopStep-style symbol resolves to the Tradovate contract",
           con_ts.name == "NQU6" and con_ts.tick_size == 0.25
           and con_ts.tick_value == 5.0, f"got {con_ts.name}/{con_ts.tick_value}")
    con_tv = cl.resolve_contract("NQU6")
    _check("tdv explicit Tradovate name resolves", con_tv.name == "NQU6")
    con_root = cl.resolve_contract("NQ")
    _check("tdv bare NQ root resolves front month", con_root.name == "NQU6")
    cl.resolve_contract("MNQ")   # restore the MNQ mapping (the fake reuses ids)

    # bracketed straddle leg -> native OSO with absolute prices
    from .models import StraddleLeg
    leg = StraddleLeg(side=OrderSide.BUY, stop_price=21012.0, size=1,
                      stop_loss_ticks=48, take_profit_ticks=56, custom_tag="imba-t")
    res = cl.place_straddle_leg(7001, con.id, leg)
    oso = next(b for m, p, b in rest.calls if p == "order/placeoso")
    _check("tdv OSO placed + leg id written back",
           res.success and leg.order_id == 111)
    _check("tdv OSO body: symbol/action/type",
           oso["symbol"] == "MNQU6" and oso["action"] == "Buy"
           and oso["orderType"] == "Stop" and oso["stopPrice"] == 21012.0)
    _check("tdv OSO body: isAutomated + accountSpec, NO customTag50 (CME Tag 50 "
           "is a registered operator id — live-rejected 'Unregisted Tag50')",
           oso["isAutomated"] is True and oso["accountSpec"] == "DEMO7001"
           and "customTag50" not in oso)
    _check("tdv OSO bracket1 = protective sell stop @21000",
           oso["bracket1"] == {"action": "Sell", "orderType": "Stop",
                               "stopPrice": 21000.0}, f"got {oso['bracket1']}")
    _check("tdv OSO bracket2 = sell limit TP @21026",
           oso["bracket2"] == {"action": "Sell", "orderType": "Limit",
                               "price": 21026.0}, f"got {oso['bracket2']}")

    # naked leg -> plain placeorder + loud warning (no Position Brackets on Tradovate)
    naked = StraddleLeg(side=OrderSide.SELL, stop_price=20988.0, size=1,
                        stop_loss_ticks=0, take_profit_ticks=0, custom_tag="imba-n")
    cl.place_straddle_leg(7001, con.id, naked)
    po = [b for m, p, b in rest.calls if p == "order/placeorder"][-1]
    _check("tdv naked leg uses placeorder (no brackets)",
           "bracket1" not in po and po["orderType"] == "Stop")
    _check("tdv naked leg warns loudly",
           any(lvl == "warning" and "NAKED" in m for lvl, m in warns))

    # engine flatten path: opposing market order
    cl.place_order(account_id=7001, contract_id=con.id, order_type=OrderType.MARKET,
                   side=OrderSide.SELL, size=1, custom_tag="imbabot-flatten-x")
    mo = [b for m, p, b in rest.calls if p == "order/placeorder"][-1]
    _check("tdv market flatten body", mo["orderType"] == "Market"
           and mo["action"] == "Sell" and "stopPrice" not in mo)

    _check("tdv cancel order", cl.cancel_order(7001, 111) is True)
    _check("tdv liquidate position", cl.liquidate_position(7001, con.id).order_id == 130)

    # open orders/positions via REST fallback (no WS in this test)
    oo = cl.search_open_orders(7001)
    _check("tdv open orders: working only, id-keyed",
           len(oo) == 1 and oo[0]["id"] == 111, f"got {oo}")
    pp = cl.search_open_positions(7001)
    _check("tdv positions: signed netPos, zero rows dropped",
           len(pp) == 1 and pp[0]["netPos"] == -1 and "type" not in pp[0], f"got {pp}")
    from .engine import _net_position_value as _npv
    _check("tdv position shape reads correctly in the engine",
           _npv(pp[0]) == -1, f"got {_npv(pp[0])}")

    # Venue caps ship DISABLED (TopStep parity): a full 5-contract order — the
    # user's real TopStep sizing — passes the Tradovate guard (RiskGuard still
    # applies at the engine level, exactly as on TopStep).
    _check("tdv venue caps ship disabled (TopStep parity)",
           tdv_safety.MAX_POSITION_SIZE is None and tdv_safety.MAX_DAILY_LOSS is None)
    full = cl.place_order(account_id=7001, contract_id=con.id,
                          order_type=OrderType.MARKET, side=OrderSide.BUY, size=5)
    _check("tdv 5-contract order passes (same as TopStep)", full.success)

    # order failure mapping
    class _RestFail(_Rest):
        def __call__(self, method, url, body, headers, timeout):
            if "placeorder" in url:
                self.calls.append((method, url, body))
                return 200, {"failureReason": "InvalidPrice",
                             "failureText": "Price is invalid"}
            return super().__call__(method, url, body, headers, timeout)

    cl2 = TradovateClient(Settings(backend="tradovate", tdv_username="u"),
                          http=_RestFail(), enable_ws=False)
    cl2.authenticate("u", "pw")
    cl2.resolve_contract("MNQ")
    bad = cl2.place_order(account_id=7001, contract_id="901",
                          order_type=OrderType.MARKET, side=OrderSide.BUY, size=1)
    _check("tdv failure mapped to OrderResult",
           bad.success is False and bad.error_message == "Price is invalid")

    # live endpoint is un-constructable while LIVE_TRADING=False (source gate)
    _check("tdv safety: LIVE_TRADING ships False", tdv_safety.LIVE_TRADING is False)
    try:
        TradovateClient(Settings(backend="tradovate", tdv_environment="live"),
                        enable_ws=False)
        _check("tdv live endpoint blocked at construction", False, "no exception")
    except tdv_safety.SafetyError as exc:
        _check("tdv live endpoint blocked at construction", "LIVE" in str(exc).upper())

    # session_range / retrieve_bars are documented 0.2.5 limitations
    try:
        cl.session_range("901", None, None)
        _check("tdv session_range raises (documented limitation)", False)
    except TradovateError:
        _check("tdv session_range raises (documented limitation)", True)

    # 13f) Tradovate WebSocket codec + caches — canned frames, no sockets.
    from .tradovate.ws import (QuoteCache, UserSyncCache, encode_request,
                               md_ws_url, parse_frame, user_ws_url)

    _check("tdv ws urls: demo", user_ws_url("demo").startswith("wss://demo.")
           and md_ws_url("demo").startswith("wss://md-demo."))
    _check("tdv ws urls: live", user_ws_url("live").startswith("wss://live.")
           and md_ws_url("live").startswith("wss://md."))

    # authorize framing: RAW token in the BODY slot, never JSON-quoted, never
    # in the query slot (live-found 2026-07-19: both variants get 401).
    _check("tdv ws encode: authorize = raw token in body slot",
           encode_request("authorize", 1, body="tok-X") == "authorize\n1\n\ntok-X")
    _check("tdv ws encode: string bodies are never JSON-quoted",
           '"' not in encode_request("authorize", 1, body="tok-X"))
    _check("tdv ws encode: body json",
           encode_request("user/syncrequest", 2, body={"users": [5]})
           == 'user/syncrequest\n2\n\n{"users": [5]}')
    _check("tdv ws parse: open/heartbeat",
           parse_frame("o")[0] == "open" and parse_frame("h")[0] == "heartbeat")
    kind, msgs = parse_frame('a[{"s":200,"i":1,"d":{"ok":true}}]')
    _check("tdv ws parse: message array", kind == "messages" and msgs[0]["i"] == 1)
    kind, payload = parse_frame('c[1001,"gone"]')
    _check("tdv ws parse: close frame", kind == "close" and payload == [1001, "gone"])

    # UserSyncCache: snapshot ingest -> props events -> merged rows
    kills = []
    cache = UserSyncCache(on_kill=kills.append, max_daily_loss=500.0)
    cache.ingest_sync({
        "orders": [{"id": 111, "accountId": 7001, "ordStatus": "Working",
                    "action": "Buy", "contractId": 901}],
        "orderVersions": [{"id": 5001, "orderId": 111, "orderQty": 1,
                           "orderType": "Stop", "stopPrice": 21012.0}],
        "positions": [{"id": 61, "accountId": 7001, "contractId": 901, "netPos": 0}],
        "cashBalances": [{"id": 81, "accountId": 7001, "amount": 50000.0,
                          "tradeDate": {"year": 2026, "month": 7, "day": 16}}],
    })
    rows = cache.rows("orders")
    _check("tdv cache: order merged with version",
           rows[0]["id"] == 111 and rows[0]["stopPrice"] == 21012.0
           and rows[0]["orderQty"] == 1, f"got {rows}")

    # props: order fills (Updated ordStatus) + a new version + position update
    cache.apply_props("order", "Updated",
                      {"id": 111, "accountId": 7001, "ordStatus": "Filled",
                       "action": "Buy", "contractId": 901})
    cache.apply_props("orderVersion", "Created",
                      {"id": 5002, "orderId": 111, "orderQty": 1,
                       "orderType": "Stop", "stopPrice": 21015.0})
    cache.apply_props("position", "Updated",
                      {"id": 61, "accountId": 7001, "contractId": 901,
                       "netPos": 1, "netPrice": 21012.25})
    rows = cache.rows("orders")
    _check("tdv cache: props update order status + latest version wins",
           rows[0]["ordStatus"] == "Filled" and rows[0]["stopPrice"] == 21015.0)
    _check("tdv cache: position updated via props",
           cache.rows("positions")[0]["netPos"] == 1)
    cache.apply_props("order", "Deleted", {"id": 111})
    _check("tdv cache: props delete removes order", cache.rows("orders") == [])
    cache.apply_props("unknownEntity", "Created", {"id": 1})  # ignored, no crash
    _check("tdv cache: unknown entity types ignored", True)

    # daily-loss kill switch: amount-delta path (baseline 50k -> -520 breaches 500)
    _check("tdv kill: not tripped at baseline", kills == [])
    cache.apply_props("cashBalance", "Updated",
                      {"id": 81, "accountId": 7001, "amount": 49480.0,
                       "tradeDate": {"year": 2026, "month": 7, "day": 16}})
    _check("tdv kill: -520 trips the -500 switch",
           len(kills) == 1 and "-520" in kills[0], f"got {kills}")
    cache.apply_props("cashBalance", "Updated",
                      {"id": 81, "accountId": 7001, "amount": 49000.0,
                       "tradeDate": {"year": 2026, "month": 7, "day": 16}})
    _check("tdv kill: fires only once", len(kills) == 1)

    # realizedPnL path (preferred when the venue provides it)
    kills2 = []
    cache2 = UserSyncCache(on_kill=kills2.append, max_daily_loss=500.0)
    cache2.apply_props("cashBalance", "Updated",
                       {"id": 82, "accountId": 7001, "amount": 51000.0,
                        "realizedPnL": -501.0,
                        "tradeDate": {"year": 2026, "month": 7, "day": 16}})
    _check("tdv kill: realizedPnL field preferred", len(kills2) == 1, f"got {kills2}")

    # default construction (shipped MAX_DAILY_LOSS=None) -> watcher disabled,
    # even on a huge drawdown event (TopStep-parity guards only)
    kills3 = []
    cache3 = UserSyncCache(on_kill=kills3.append)
    cache3.apply_props("cashBalance", "Updated",
                       {"id": 83, "accountId": 7001, "amount": 51000.0,
                        "realizedPnL": -99999.0,
                        "tradeDate": {"year": 2026, "month": 7, "day": 16}})
    _check("tdv kill: disabled by default (parity — no venue loss cap)",
           kills3 == [], f"got {kills3}")

    # QuoteCache: trade price, bid/offer mid fallback, merge, staleness
    qclock = [1000.0]
    qc = QuoteCache(clock=lambda: qclock[0], stale_after=10.0)
    qc.update("MNQU6", {"Bid": {"price": 21010.0}, "Offer": {"price": 21010.5}})
    _check("tdv quotes: bid/offer mid when no trade",
           qc.last_price("MNQU6") == 21010.25)
    qc.update("MNQU6", {"Trade": {"price": 21011.0, "size": 2}})
    _check("tdv quotes: trade price preferred (merge kept bid/offer)",
           qc.last_price("MNQU6") == 21011.0)
    qclock[0] += 11.0
    _check("tdv quotes: stale after 10s -> None", qc.last_price("MNQU6") is None)
    _check("tdv quotes: unknown symbol -> None", qc.last_price("ESU6") is None)

    # 13f2) engine constructor routes backend="tradovate" to TradovateClient
    # (construction is offline — auth/network happen only at connect()).
    eng_tdv = BotEngine(Settings(backend="tradovate"))
    _check("engine builds TradovateClient for backend=tradovate",
           type(eng_tdv.client).__name__ == "TradovateClient",
           f"got {type(eng_tdv.client).__name__}")
    eng_px = BotEngine(Settings(backend="api"))
    _check("engine still builds ProjectXClient for backend=api",
           type(eng_px.client).__name__ == "ProjectXClient",
           f"got {type(eng_px.client).__name__}")

    # 13g) kill-switch machinery on the client (inert by default — the daily-loss
    # watcher is disabled while MAX_DAILY_LOSS is None — but _trip_kill must
    # still work when deliberately re-enabled for live).

    # kill switch: tripping it blocks new orders and sweeps the book
    n_before = len(rest.calls)
    cl._trip_kill("daily realized P&L -520.00 breached -$500")
    swept = [(m, p) for m, p, b in rest.calls[n_before:]]
    _check("tdv kill sweep cancels working orders",
           ("POST", "order/cancelorder") in swept, f"got {swept}")
    _check("tdv kill sweep liquidates open positions",
           ("POST", "order/liquidateposition") in swept, f"got {swept}")
    try:
        cl.place_order(account_id=7001, contract_id=con.id,
                       order_type=OrderType.MARKET, side=OrderSide.BUY, size=1)
        _check("tdv kill switch blocks new orders", False, "no exception")
    except tdv_safety.SafetyError as exc:
        _check("tdv kill switch blocks new orders", "Kill switch" in str(exc))

    # 13g3) reference-price source — the no-CME-license route (order routing
    # needs no data license; only streaming Tradovate quotes does, ~$290/mo).
    from .tradovate.pricefeed import ReferencePriceFeed
    _check("tdv price source default == topstep",
           Settings().tdv_price_source == "topstep")

    class _PxStub:
        authenticated = True
        def resolve_contract(self, symbol, live=False):
            return type("C", (), {"id": "PX1", "name": symbol + "Z6"})()
        def last_price(self, cid, live=False):
            return 21042.5

    class _PxDead:
        authenticated = True
        def resolve_contract(self, *a, **k):
            raise RuntimeError("px down")

    s_pf = Settings(backend="tradovate", tdv_price_source="topstep",
                    username="u", contract_symbol="MNQ")
    feed = ReferencePriceFeed(s_pf, px_client=_PxStub(), quote_fn=lambda: 21043.0)
    _check("tdv feed: TopStep source preferred when it agrees with the quote",
           feed.last_price() == 21042.5 and feed.last_source == "topstep")
    feed2 = ReferencePriceFeed(s_pf, px_client=_PxDead(), quote_fn=lambda: 21050.0)
    _check("tdv feed: falls back to the public quote",
           feed2.last_price() == 21050.0 and feed2.last_source == "public")
    feed3 = ReferencePriceFeed(s_pf, px_client=_PxDead(), quote_fn=lambda: None)
    try:
        feed3.last_price()
        _check("tdv feed: both sources dead raises", False, "no exception")
    except RuntimeError:
        _check("tdv feed: both sources dead raises", True)
    px_calls = []
    class _PxBoom:
        authenticated = True
        def resolve_contract(self, *a, **k):
            px_calls.append(1)
            raise RuntimeError("must not be called")
    feed4 = ReferencePriceFeed(Settings(backend="tradovate", tdv_price_source="public"),
                               px_client=_PxBoom(), quote_fn=lambda: 21060.0)
    _check("tdv feed: public source never touches TopStep",
           feed4.last_price() == 21060.0 and not px_calls)

    # feed hardening (live-found 2026-07-19): 429 backoff, one-time public
    # warning, and a short cache for the engine's back-to-back capture probes.
    px429 = {"n": 0}
    class _Px429:
        authenticated = True
        def resolve_contract(self, symbol, live=False):
            return type("C", (), {"id": "PX1", "name": "NQU6"})()
        def last_price(self, cid, live=False):
            px429["n"] += 1
            raise RuntimeError("HTTP 429 from /api/History/retrieveBars")
    warnsf = []
    fclock = [1000.0]
    feed5 = ReferencePriceFeed(s_pf, log=lambda m, level="info": warnsf.append(m),
                               px_client=_Px429(), quote_fn=lambda: 21070.0)
    feed5._clock = lambda: fclock[0]
    _check("tdv feed: 429 -> public fallback", feed5.last_price() == 21070.0)
    fclock[0] += 3.0
    feed5.last_price()
    fclock[0] += 3.0
    feed5.last_price()
    _check("tdv feed: 429 backoff stops hammering TopStep",
           px429["n"] == 1, f"px calls={px429['n']}")
    _check("tdv feed: public-quote warning logged once, not per poll",
           sum(1 for m in warnsf if "PUBLIC" in m) == 1)
    qcalls = {"n": 0}
    def _qf():
        qcalls["n"] += 1
        return 21080.0
    feed6 = ReferencePriceFeed(Settings(backend="tradovate", tdv_price_source="public"),
                               quote_fn=_qf)
    feed6._clock = lambda: 5000.0
    feed6.last_price()
    feed6.last_price()
    _check("tdv feed: short cache serves back-to-back capture probes",
           qcalls["n"] == 1, f"quote calls={qcalls['n']}")

    # Mon 7/20 rehearsal regressions: live-tier preference + divergence guard
    # (a stale sim bar 11pt under Tradovate's book filled the BUY pre-open).
    tiers = []
    class _PxTier:
        authenticated = True
        def resolve_contract(self, symbol, live=False):
            return type("C", (), {"id": "PX1", "name": "NQU6"})()
        def last_price(self, cid, live=False):
            tiers.append(live)
            return 21042.5
    feed7 = ReferencePriceFeed(s_pf, px_client=_PxTier(), quote_fn=lambda: 21042.0)
    feed7.last_price()
    _check("tdv feed: LIVE data tier probed first", tiers == [True], f"got {tiers}")

    tiers2 = []
    class _PxLiveDead:
        authenticated = True
        def resolve_contract(self, symbol, live=False):
            return type("C", (), {"id": "PX1", "name": "NQU6"})()
        def last_price(self, cid, live=False):
            tiers2.append(live)
            if live:
                raise RuntimeError("live data subscription required")
            return 21042.5
    feed8 = ReferencePriceFeed(s_pf, px_client=_PxLiveDead(), quote_fn=lambda: 21042.0)
    _check("tdv feed: live tier dead -> sim fallback still serves",
           feed8.last_price() == 21042.5 and tiers2 == [True, False], f"got {tiers2}")

    dwarns = []
    feed9 = ReferencePriceFeed(s_pf, log=lambda m, level="info": dwarns.append(m),
                               px_client=_PxTier(), quote_fn=lambda: 21055.0)
    p9 = feed9.last_price()
    _check("tdv feed: >5pt divergence -> the live quote wins",
           p9 == 21055.0 and feed9.last_source == "public", f"got {p9}")
    _check("tdv feed: divergence warned",
           any("away from the live public quote" in m for m in dwarns))

    # client dispatch: topstep source serves the price with the MD socket OFF
    warns_pf = []
    s_cd = Settings(backend="tradovate", tdv_username="u", tdv_price_source="topstep")
    cl_pf = TradovateClient(s_cd, log=lambda m, level="info": warns_pf.append(m),
                            http=_Rest(), enable_ws=False)
    cl_pf.authenticate("u", "pw")
    cl_pf._price_feed = ReferencePriceFeed(s_cd, px_client=_PxStub(),
                                           quote_fn=lambda: None)
    _check("tdv client: topstep price source works without the MD socket",
           cl_pf._md_ws is None and cl_pf.last_price("any-id") == 21042.5)
    _check("tdv banner states the price source",
           any("price_src=topstep" in m for m in warns_pf))

    # 13h) engine E2E on the Tradovate translation layer (FakeTradovate):
    # full fire -> OCO cancel -> panic, exercising signed netPos + OSO shapes.
    from ._fake_tradovate import FakeTradovate
    _check("FakeTradovate conforms to BrokerAdapter",
           isinstance(FakeTradovate(), BrokerAdapter))

    def make_tdv_engine(**overrides):
        s = Settings(backend="tradovate", tdv_username="tester",
                     contract_symbol="MNQ", entry_points=12, stop_loss_points=12,
                     take_profit_points=13.3, contracts=1,
                     bot_stop_loss=True, bot_take_profit=True,
                     max_trades_per_day=99, max_contracts=5)
        for k, v in overrides.items():
            setattr(s, k, v)
        ft = FakeTradovate(last=21000.0)
        e = BotEngine(s, client=ft, log=lambda *a, **k: None)
        e.connect("")
        return e, ft

    # dry-run gate covers the tradovate path (fires BEFORE any order call)
    eng, ft = make_tdv_engine(dry_run=True, trade_mode="one_trade")
    eng._on_fire()
    _check("tdv dry-run captured a plan, placed 0 orders",
           eng.last_plan is not None and len(ft.placed) == 0,
           f"placed {len(ft.placed)}")

    # live one-trade: 2 OSO entries with absolute bracket prices
    eng, ft = make_tdv_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    _check("tdv E2E: 2 OSO legs placed", len(ft.placed) == 2, f"{len(ft.placed)}")
    buy = next(o for o in ft.placed if o["side"] == OrderSide.BUY)
    sell = next(o for o in ft.placed if o["side"] == OrderSide.SELL)
    _check("tdv E2E: BUY leg brackets absolute + correctly ordered",
           buy["bracket1"]["stopPrice"] < buy["stopPrice"] < buy["bracket2"]["price"],
           f"got {buy['bracket1']}/{buy['stopPrice']}/{buy['bracket2']}")
    _check("tdv E2E: SELL leg brackets mirrored",
           sell["bracket2"]["price"] < sell["stopPrice"] < sell["bracket1"]["stopPrice"],
           f"got {sell['bracket2']}/{sell['stopPrice']}/{sell['bracket1']}")
    _check("tdv E2E: every order isAutomated",
           all(o.get("isAutomated") for o in ft.placed))

    # long fill (SIGNED netPos, no type key) -> OCO cancels the short entry
    long_oid, short_oid = plan.long_leg.order_id, plan.short_leg.order_id
    ft.simulate_fill(eng.contract.id, +1)
    eng._monitor_oco(plan, poll_seconds=0.01)
    _check("tdv E2E: long fill cancels the short entry",
           short_oid in ft.cancelled and long_oid not in ft.cancelled,
           f"cancelled={ft.cancelled}")

    # mirror: short fill cancels the long entry
    eng, ft = make_tdv_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    long_oid, short_oid = plan.long_leg.order_id, plan.short_leg.order_id
    ft.simulate_fill(eng.contract.id, -1)
    eng._monitor_oco(plan, poll_seconds=0.01)
    _check("tdv E2E: short fill cancels the long entry",
           long_oid in ft.cancelled and short_oid not in ft.cancelled,
           f"cancelled={ft.cancelled}")

    # Mon 7/20 rehearsal regression — CACHE LAG: the fill hit the FIRST 0.5s
    # scan before the venue's push cache listed the fresh entries. The old
    # visibility guard skipped the cancel and killed the monitor; now the
    # signed net names the filled side and the sibling is cancelled blind.
    eng, ft = make_tdv_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    long_oid, short_oid = plan.long_leg.order_id, plan.short_leg.order_id
    ft.positions = [{"contractId": eng.contract.id, "netPos": 1, "netPrice": 21012.0}]
    _orig_soo = ft.search_open_orders
    ft.search_open_orders = lambda acct: []      # push cache hasn't caught up
    acted = eng._oco_scan(plan, eng.account.id, eng.contract.id, set())
    ft.search_open_orders = _orig_soo
    _check("tdv OCO cache-lag: long fill cancels the INVISIBLE short entry",
           acted is True and short_oid in ft.cancelled and long_oid not in ft.cancelled,
           f"acted={acted} cancelled={ft.cancelled}")

    eng, ft = make_tdv_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    long_oid, short_oid = plan.long_leg.order_id, plan.short_leg.order_id
    ft.positions = [{"contractId": eng.contract.id, "netPos": -1, "netPrice": 20988.0}]
    _orig_soo = ft.search_open_orders
    ft.search_open_orders = lambda acct: []
    acted = eng._oco_scan(plan, eng.account.id, eng.contract.id, set())
    ft.search_open_orders = _orig_soo
    _check("tdv OCO cache-lag: short fill cancels the INVISIBLE long entry",
           acted is True and long_oid in ft.cancelled and short_oid not in ft.cancelled,
           f"acted={acted} cancelled={ft.cancelled}")

    # cancel error -> monitor retries next poll instead of dying
    eng, ft = make_tdv_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    short_oid = plan.short_leg.order_id
    ft.positions = [{"contractId": eng.contract.id, "netPos": 1, "netPrice": 21012.0}]
    _orig_cancel = ft.cancel_order
    _flaky = {"n": 0}
    def _flaky_cancel(acct, oid):
        _flaky["n"] += 1
        if _flaky["n"] == 1:
            raise RuntimeError("venue hiccup")
        return _orig_cancel(acct, oid)
    ft.cancel_order = _flaky_cancel
    r1 = eng._oco_scan(plan, eng.account.id, eng.contract.id, set())
    r2 = eng._oco_scan(plan, eng.account.id, eng.contract.id, set())
    ft.cancel_order = _orig_cancel
    _check("tdv OCO cancel error: first scan retries, second succeeds",
           r1 is False and r2 is True and short_oid in ft.cancelled,
           f"r1={r1} r2={r2} cancelled={ft.cancelled}")

    # full-size parity: the user's real 4-contract straddle places unchanged
    eng, ft = make_tdv_engine(dry_run=False, trade_mode="one_trade", contracts=4)
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    _check("tdv E2E: 4-contract legs place (TopStep-parity sizing)",
           len(ft.placed) == 2 and all(o["orderQty"] == 4 for o in ft.placed),
           f"got {[o.get('orderQty') for o in ft.placed]}")

    # emergency stop: cancel sweep + flatten via opposing market on signed netPos
    eng, ft = make_tdv_engine(dry_run=False, trade_mode="one_trade")
    plan = build_straddle(eng.contract, 21000.0, eng.strategy_params(), tag_prefix="t")
    eng._place_plan(plan)
    ft.simulate_fill(eng.contract.id, +1)   # long 1, short entry still resting
    n_orders = len(ft.placed)
    eng.emergency_stop()
    _check("tdv E2E: panic cancels the resting entry", len(ft.cancelled) >= 1,
           f"cancelled={ft.cancelled}")
    flat = ft.placed[n_orders:]
    _check("tdv E2E: panic flattens long 1 with a market SELL 1",
           len(flat) == 1 and flat[0]["side"] == OrderSide.SELL
           and flat[0]["size"] == 1
           and flat[0]["order_type"] == OrderType.MARKET, f"got {flat}")

    # 14) $-based SL/TP entry (TopStep Position-Brackets UX in the bot)
    from .models import dollars_per_point_for, dollars_to_points
    _check("$->pts: SL $600 at 4ct NQ = 7.5", dollars_to_points(600, 4, 20.0, 0.25) == 7.5)
    _check("$->pts: TP $550 floors to 6.75 (never demands more than typed)",
           dollars_to_points(550, 4, 20.0, 0.25) == 6.75)
    _check("$->pts: $480 = 6.0", dollars_to_points(480, 4, 20.0, 0.25) == 6.0)
    _check("$->pts: MNQ $2/pt case ($96 at 4ct = 12.0)",
           dollars_to_points(96, 4, 2.0, 0.25) == 12.0)
    _check("$->pts: tiny $ never floors to zero distance",
           dollars_to_points(1, 4, 20.0, 0.25) == 0.25)
    try:
        dollars_to_points(0, 4, 20.0, 0.25)
        _check("$->pts: non-positive raises", False, "no exception")
    except ValueError:
        _check("$->pts: non-positive raises", True)
    _check("$/pt lookup: NQ + full names + TopStep style",
           dollars_per_point_for("NQ") == 20.0
           and dollars_per_point_for("NQU6") == 20.0
           and dollars_per_point_for("NQU26") == 20.0
           and dollars_per_point_for("MNQ") == 2.0
           and dollars_per_point_for("XYZQ99") is None)
    s14 = Settings()
    _check("$ entry defaults: points mode, no dollars",
           s14.sl_tp_entry_mode == "points" and s14.stop_loss_dollars == 0.0)

    # bridge conversion path (the single authoritative implementation)
    from .webui.bridge import Api as _Api
    api14 = _Api.__new__(_Api)               # no window/threads — just settings
    api14.settings = Settings(contract_symbol="NQU26", contracts=4)
    api14.engine = None
    err = api14._apply_settings({"sl_tp_entry_mode": "dollars",
                                 "stop_loss_dollars": 600, "take_profit_dollars": 550})
    _check("bridge $ mode converts SL/TP to points",
           err is None and api14.settings.stop_loss_points == 7.5
           and api14.settings.take_profit_points == 6.75,
           f"err={err} sl={api14.settings.stop_loss_points} tp={api14.settings.take_profit_points}")
    _check("bridge $ mode preserves the typed dollars",
           api14.settings.stop_loss_dollars == 600.0
           and api14.settings.take_profit_dollars == 550.0)
    api14.settings = Settings(contract_symbol="ZZZ", contracts=4)
    err2 = api14._apply_settings({"sl_tp_entry_mode": "dollars",
                                  "stop_loss_dollars": 600})
    _check("bridge $ mode: unknown symbol root errors cleanly",
           isinstance(err2, str) and "ZZZ" in err2, f"got {err2}")

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1
