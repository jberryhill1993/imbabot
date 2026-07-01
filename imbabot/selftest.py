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

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1
