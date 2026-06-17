"""The bot engine: connect, arm, fire, manage, panic.

Sequence at fire time (09:29:57 ET by default):
  1. capture the reference price
  2. build the straddle plan (BUY stop above, SELL stop below + brackets)
  3. if dry_run -> log the plan and stop; else place both legs
  4. in One-Trade mode, monitor for a fill and cancel the opposite entry

The engine talks to a duck-typed client (ProjectXClient in production, a fake in
tests), so the whole flow is verifiable offline.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from .config import Settings
from .models import (
    Account,
    Contract,
    OrderSide,
    OrderType,
    StraddlePlan,
    TradeMode,
    round_to_tick,
)
from .risk import RiskError, RiskGuard
from .scheduler import (
    FireTimer,
    next_fire_time,
    seconds_until,
    format_countdown,
    MARKET_TZ,
)
from .strategy import StrategyParams, build_straddle, describe_plan

LogFn = Callable[..., None]


class BotEngine:
    def __init__(
        self,
        settings: Settings,
        client: Optional[Any] = None,
        log: Optional[LogFn] = None,
    ) -> None:
        self.settings = settings
        if client is None:
            from .projectx import ProjectXClient  # lazy: avoids requests on offline paths

            client = ProjectXClient(base_url=settings.base_url)
        self.client = client
        self.risk = RiskGuard(settings)
        self._log: LogFn = log or (lambda *a, **k: None)

        self.account: Optional[Account] = None
        self.contract: Optional[Contract] = None
        self.last_plan: Optional[StraddlePlan] = None

        self._timer: Optional[FireTimer] = None
        self._recurring = False                      # daily weekday auto-rearm
        self._on_tick: Optional[Callable[[float], None]] = None
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    # --------------------------------------------------------------- logging
    def log(self, msg: str, level: str = "info") -> None:
        self._log(msg, level)

    # ------------------------------------------------------------ connection
    def connect(self, api_key: str) -> None:
        s = self.settings
        self.log(f"Authenticating as {s.username} @ {s.base_url} …")
        self.client.authenticate(s.username, api_key)
        self.log("Authenticated. Token acquired.")
        self.account = self._pick_account()
        self.log(
            f"Account: {self.account.name} (id={self.account.id}, "
            f"canTrade={self.account.can_trade})"
        )
        self.refresh_contract()

    def _pick_account(self) -> Account:
        accounts = self.client.search_accounts(only_active=True)
        if not accounts:
            raise RuntimeError("No active accounts found for this login.")
        if self.settings.account_id is not None:
            for a in accounts:
                if a.id == self.settings.account_id:
                    return a
            self.log(
                f"Configured account_id={self.settings.account_id} not found; "
                "using first active account.",
                "warn",
            )
        tradable = [a for a in accounts if a.can_trade]
        chosen = (tradable or accounts)[0]
        self.settings.account_id = chosen.id
        self.settings.account_name = chosen.name
        return chosen

    def list_accounts(self) -> List[Account]:
        return self.client.search_accounts(only_active=True)

    def refresh_contract(self) -> Contract:
        s = self.settings
        self.contract = self.client.resolve_contract(s.contract_symbol, live=s.use_live_data)
        self.log(
            f"Contract: {self.contract.name} ({self.contract.id}) "
            f"tick={self.contract.tick_size} ${self.contract.tick_value}/tick"
        )
        return self.contract

    # ------------------------------------------------------------- dashboard
    def strategy_params(self) -> StrategyParams:
        s = self.settings
        return StrategyParams(
            entry_points=s.entry_points,
            stop_loss_points=s.stop_loss_points,
            take_profit_points=s.take_profit_points,
            contracts=s.contracts,
        )

    def next_fire(self, now: Optional[datetime] = None) -> datetime:
        s = self.settings
        if s.test_mode and s.test_fire_time:
            from .scheduler import next_local_fire

            return next_local_fire(s.test_fire_time, now=now)
        if s.strategy_fire_time:
            # Production daily schedule: recurring, weekday-only, local clock.
            from .scheduler import next_weekday_local_fire

            return next_weekday_local_fire(s.strategy_fire_time, now=now)
        return next_fire_time(
            s.open_time(),
            capture_offset_seconds=s.capture_offset_seconds,
            market_tz=s.market_tz,
            now=now,
        )

    def fire_now(self) -> None:
        """Immediately run the fire sequence once (Test section's 'Fire now')."""
        self.log("Manual TEST fire requested — running the fire sequence now.")
        threading.Thread(target=self._on_fire, name="TestFire", daemon=True).start()

    def last_price(self) -> Optional[float]:
        if not self.contract:
            return None
        try:
            return self.client.last_price(self.contract.id, live=self.settings.use_live_data)
        except Exception as exc:  # dashboard must never crash on a data hiccup
            self.log(f"last_price unavailable: {exc}", "warn")
            return None

    def overnight_range(self) -> Optional[Dict[str, float]]:
        if not self.contract:
            return None
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(self.settings.market_tz)
            open_dt = self.next_fire().astimezone(tz).replace(
                hour=self.settings.open_hour,
                minute=self.settings.open_minute,
                second=0,
                microsecond=0,
            )
            # Most recent completed open is today's (or next session's minus a day)
            end = open_dt if open_dt <= datetime.now(tz) else open_dt - timedelta(days=1)
            start = (end - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
            return self.client.session_range(
                self.contract.id, start, end, live=self.settings.use_live_data
            )
        except Exception as exc:
            self.log(f"overnight_range unavailable: {exc}", "warn")
            return None

    def dashboard(self) -> Dict[str, Any]:
        fire = self.next_fire()
        return {
            "account": self.account.name if self.account else "—",
            "contract": self.contract.name if self.contract else "—",
            "last_price": self.last_price(),
            "overnight_range": self.overnight_range(),
            "next_fire": fire,
            "countdown": format_countdown(seconds_until(fire)),
            "armed": self.armed,
            "mode": self.settings.trade_mode,
            "dry_run": self.settings.dry_run,
        }

    # ----------------------------------------------------------------- arm
    @property
    def armed(self) -> bool:
        return self._timer is not None and self._timer.armed

    def arm(self, on_tick: Optional[Callable[[float], None]] = None) -> datetime:
        if self.armed:
            raise RuntimeError("Already armed.")
        if not (self.account and self.contract):
            raise RuntimeError("Connect (account + contract) before arming.")
        self.risk.check_can_arm(self.account.can_trade)

        # Recurring daily schedule re-arms itself after each weekday fire.
        self._recurring = bool(self.settings.strategy_fire_time and not self.settings.test_mode)
        self._on_tick = on_tick
        fire = self.next_fire()
        self.log(
            f"ARMED. Fire at {fire.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"(capture {self.settings.capture_offset_seconds}s before "
            f"{self.settings.open_hour:02d}:{self.settings.open_minute:02d} open). "
            f"Mode={self.settings.trade_mode} dry_run={self.settings.dry_run}."
        )
        if self._recurring:
            self.log(
                "Daily schedule: stays armed and re-fires every weekday (Mon–Fri) "
                "at this time. Market holidays are NOT skipped — disarm on holidays.",
                "warn",
            )
        if self.settings.trade_mode == TradeMode.TWO_TRADE.value:
            self.log(
                "Two-Trade mode: both entries stay working (no auto-cancel). "
                "Set your TopStep trade limit to 2/day so both legs can fill.",
                "warn",
            )
        callback = self._on_scheduled_fire if self._recurring else self._on_fire
        self._timer = FireTimer(target=fire, on_fire=callback, on_tick=on_tick)
        self._timer.arm()
        return fire

    def _on_scheduled_fire(self) -> None:
        """Recurring-schedule callback: fire, then re-arm for the next weekday.

        Runs on the just-fired FireTimer thread (which then exits). We replace
        ``self._timer`` with a brand-new FireTimer, so ``armed`` stays True with
        no gap. ``disarm()`` clears ``self._recurring`` first, so a disarm during
        a fire won't re-arm."""
        self._on_fire()
        if not self._recurring:
            return
        try:
            fire = self.next_fire()
            self._timer = FireTimer(target=fire, on_fire=self._on_scheduled_fire, on_tick=self._on_tick)
            self._timer.arm()
            self.log(f"Re-armed for next weekday fire: {fire.strftime('%a %b %d %H:%M:%S')}.")
        except Exception as exc:
            self.log(f"re-arm failed: {exc} — daily schedule stopped, re-arm manually.", "error")

    def disarm(self) -> None:
        self._recurring = False          # stop recurrence before cancelling the timer
        if self._timer:
            self._timer.disarm()
            self._timer = None
        self.log("Disarmed.")

    # ---------------------------------------------------------------- fire
    def _ensure_session(self) -> None:
        """Re-validate the token right before it matters; re-auth if stale.

        Tokens last ~24h, so an app left connected overnight can hold a dead
        token at 09:29:57. Clients without validate() (the test fake) are
        assumed fresh.
        """
        validate = getattr(self.client, "validate", None)
        if validate is None:
            return
        try:
            if validate():
                return
        except Exception:
            pass
        from .config import load_api_key

        key = load_api_key(self.settings.username)
        if not key:
            raise RuntimeError(
                "Session token expired and no stored API key to re-authenticate."
            )
        self.client.authenticate(self.settings.username, key)
        self.log("Session token was stale — re-authenticated.", "warn")

    def _capture_reference_price(self, attempts: int = 2) -> float:
        """Capture the reference price, retrying once on a transient failure."""
        last_exc: Optional[Exception] = None
        for i in range(1, attempts + 1):
            try:
                return self.client.last_price(
                    self.contract.id, live=self.settings.use_live_data
                )
            except Exception as exc:
                last_exc = exc
                self.log(f"price capture attempt {i}/{attempts} failed: {exc}", "warn")
        raise RuntimeError(f"could not capture reference price: {last_exc}")

    def _on_fire(self) -> None:
        try:
            self._ensure_session()
            self.log("FIRE — capturing reference price.")
            ref = self._capture_reference_price()
            self.log(f"Reference price captured: {ref:,.2f}")

            tag = "imbabot-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            plan = build_straddle(self.contract, ref, self.strategy_params(), tag_prefix=tag)
            self.last_plan = plan
            self.log(describe_plan(plan))

            if self.settings.dry_run:
                self.log("DRY RUN — no orders sent. Disable dry_run to trade live.", "warn")
                return

            self._place_plan(plan)
            if self.settings.trade_mode == TradeMode.ONE_TRADE.value:
                self._start_monitor(plan)
        except Exception as exc:
            self.log(f"FIRE failed: {exc}", "error")

    def _place_plan(self, plan: StraddlePlan) -> None:
        self.risk.check_can_send_orders()
        acct = self.account.id
        cid = plan.contract.id
        placed: List = []
        failures: List[str] = []
        # Attempt every leg even if an earlier one fails: a lone leg is a
        # one-direction breakout entry whose risk is bounded by TopStep's
        # Position Bracket (attached to the position on fill), so it's worth
        # keeping per the operator's policy.
        for leg in plan.legs:
            try:
                res = self.client.place_straddle_leg(acct, cid, leg)
            except Exception as exc:
                failures.append(f"{leg.side.name}: {exc}")
                continue
            if res.success:
                placed.append(leg)
                self.log(
                    f"Placed {leg.side.name} STOP {leg.size}@{leg.stop_price:,.2f} "
                    f"-> orderId={res.order_id} tag={leg.custom_tag}"
                )
            else:
                failures.append(
                    f"{leg.side.name}: {res.error_message} (code {res.error_code})"
                )
        if not placed:
            raise RuntimeError("no orders placed — " + "; ".join(failures))
        if failures:
            self.log("REJECTED " + "; ".join(failures), "error")
            kept = ", ".join(
                f"{leg.side.name} STOP @ {leg.stop_price:,.2f} (orderId={leg.order_id})"
                for leg in placed
            )
            self.log(
                f"Straddle is ONE-SIDED: keeping {kept}. TopStep's Position "
                "Bracket will attach the SL/TP when it fills; cancel manually if "
                "you don't want the one-direction trade.",
                "warn",
            )
        # Don't let iterative test-mode fires pollute the real daily-trade count.
        if not self.settings.test_mode:
            self.risk.record_trade()

    # -------------------------------------------------------------- monitor
    def _start_monitor(self, plan: StraddlePlan) -> None:
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_oco, args=(plan,), name="OCOMonitor", daemon=True
        )
        self._monitor_thread.start()

    def _monitor_oco(self, plan: StraddlePlan, poll_seconds: float = 1.0) -> None:
        """One-Trade OCO: when one entry fills, cancel the remaining entries.

        We only act on a confirmed *fill* (a non-flat position on our
        contract), not on an external cancel. Rather than inferring which side
        filled from the position's direction, we cancel every plan entry that
        is still OPEN in the book — the filled leg is no longer open, so this
        is correct even if the position payload's direction encoding changes.
        TopStep's Position Bracket protects the filled side regardless.
        """
        self.log("OCO monitor started (One-Trade mode).")
        acct = self.account.id
        cid = plan.contract.id
        deadline = datetime.now() + timedelta(hours=1)
        while not self._monitor_stop.is_set() and datetime.now() < deadline:
            try:
                positions = self.client.search_open_positions(acct)
            except Exception as exc:
                self.log(f"position poll error: {exc}", "warn")
                self._monitor_stop.wait(poll_seconds)
                continue

            net = _net_position_for(positions, cid)
            if net != 0:
                self.log(
                    f"Fill detected ({'LONG' if net > 0 else 'SHORT'} {abs(net)}). "
                    "Cancelling any remaining entry legs."
                )
                try:
                    open_ids = {_order_id(o) for o in self.client.search_open_orders(acct)}
                except Exception as exc:
                    self.log(f"order lookup failed: {exc} — attempting cancels blind.", "warn")
                    open_ids = None
                for leg in plan.legs:
                    oid = leg.order_id
                    if oid is None:
                        continue
                    if open_ids is not None and oid not in open_ids:
                        continue  # this leg filled (or is already gone)
                    try:
                        self.client.cancel_order(acct, oid)
                        self.log(f"Cancelled remaining {leg.side.name} entry orderId={oid}.")
                    except Exception as exc:
                        self.log(f"Failed to cancel {leg.side.name} entry {oid}: {exc}", "error")
                return
            self._monitor_stop.wait(poll_seconds)
        self.log("OCO monitor stopped.")

    # ------------------------------------------------------- emergency stop
    def emergency_stop(self) -> None:
        """Panic: disarm, cancel ALL working orders, flatten ALL positions."""
        self.log("EMERGENCY STOP engaged.", "warn")
        self._recurring = False          # never re-arm after a panic
        self._monitor_stop.set()
        if self._timer:
            self._timer.disarm()
            self._timer = None
        if not self.account:
            self.log("No account bound; nothing to flatten.", "warn")
            return
        try:
            self._ensure_session()
        except Exception as exc:
            self.log(f"session refresh failed during panic: {exc} — attempting anyway.", "warn")
        acct = self.account.id
        self._cancel_all_orders(acct)
        self._flatten_positions(acct)
        self.log("Emergency stop complete.")

    def flatten_all(self) -> None:
        """Close every open position with a market order.

        Unlike emergency_stop this leaves working orders alone — it only takes
        you flat. Useful after a test fill or to exit at end of session while
        keeping (or separately managing) resting entries.
        """
        self.log("FLATTEN ALL positions requested.", "warn")
        if not self.account:
            self.log("No account bound; nothing to flatten.", "warn")
            return
        try:
            self._ensure_session()
        except Exception as exc:
            self.log(f"session refresh failed during flatten: {exc} — attempting anyway.", "warn")
        self._flatten_positions(self.account.id)
        self.log("Flatten complete.")

    # ----------------------------------------------------------- break-even
    def break_even(self) -> None:
        """Move the open position's protective stop to its entry price.

        Works off live API state (not last_plan) so it survives a restart. The
        bot no longer attaches its own ``-SL`` bracket; the protective stop is
        TopStep's Position Bracket, attached to the position on fill. We locate
        it by: (a) any ``-SL``-tagged stop, for back-compat; else (b) an open
        STOP order on this contract on the side *opposite* the net position
        (long -> protective SELL, short -> protective BUY). In One-Trade mode the
        opposite entry has already been cancelled, so this is unambiguous; if
        more than one candidate is found we refuse to guess and ask the operator
        to move it on TopStep.
        """
        self.log("BREAK-EVEN requested — moving the protective stop to entry.", "warn")
        if not (self.account and self.contract):
            self.log("Connect (account + contract) before break-even.", "warn")
            return
        try:
            self._ensure_session()
        except Exception as exc:
            self.log(f"session refresh failed during break-even: {exc} — attempting anyway.", "warn")
        acct = self.account.id
        cid = self.contract.id
        try:
            positions = self.client.search_open_positions(acct)
        except Exception as exc:
            self.log(f"position lookup failed: {exc}", "error")
            return
        net = _net_position_for(positions, cid)
        if net == 0:
            self.log("No open position on this contract; nothing to move.", "warn")
            return
        avg = _avg_entry_price(positions, cid)
        if avg is None:
            self.log("Could not read the position's entry price; aborting break-even.", "error")
            return
        target = round_to_tick(avg, self.contract.tick_size)
        try:
            orders = self.client.search_open_orders(acct)
        except Exception as exc:
            self.log(f"order lookup failed: {exc}", "error")
            return
        # (a) legacy bot-tagged bracket child, if any still exist
        stops = [
            o for o in orders
            if _order_contract(o) == cid and str(_order_tag(o) or "").endswith("-SL")
        ]
        # (b) fall back to TopStep's Position Bracket: a working STOP on this
        #     contract on the protective side (opposite the net position).
        if not stops:
            protective = OrderSide.SELL.value if net > 0 else OrderSide.BUY.value
            stops = [
                o for o in orders
                if _order_contract(o) == cid
                and _order_stop(o) is not None
                and _order_side(o) == protective
            ]
        if not stops:
            self.log(
                "No protective stop found for this position. If your stop is "
                "platform-managed, move it to break-even on TopStep manually.",
                "error",
            )
            return
        if len(stops) > 1:
            prices = ", ".join(str(_order_stop(o)) for o in stops)
            self.log(
                f"Found {len(stops)} candidate stops on the protective side "
                f"({prices}); refusing to guess which is the protective stop. "
                "Move it to break-even on TopStep manually.",
                "error",
            )
            return
        for o in stops:
            oid = _order_id(o)
            if oid is None:
                continue
            old = _order_stop(o)
            try:
                self.client.modify_order(acct, oid, stop_price=target)
                self.log(
                    f"Break-even: stop orderId={oid} moved {old} -> {target:,.2f} "
                    f"(entry {avg:,.2f})."
                )
            except Exception as exc:
                self.log(f"modify failed ({exc}); trying cancel+replace.", "warn")
                self._replace_stop(acct, cid, o, target, net)

    def _replace_stop(self, acct: int, cid: str, order: Dict[str, Any],
                      target: float, net: int) -> None:
        """Fallback when modify isn't available: cancel the stop and re-place it
        at ``target``. Leaves a brief unprotected window — logged loudly."""
        oid = _order_id(order)
        side = OrderSide.SELL if net > 0 else OrderSide.BUY  # protective side
        try:
            if oid is not None:
                self.client.cancel_order(acct, oid)
            res = self.client.place_order(
                account_id=acct, contract_id=cid, order_type=OrderType.STOP,
                side=side, size=abs(net), stop_price=target,
                custom_tag=(_order_tag(order) or "imbabot-be"),
            )
            if res.success:
                self.log(f"Break-even: replaced stop -> new orderId={res.order_id} @ {target:,.2f}.")
            else:
                self.log(
                    f"Replace stop REJECTED: {res.error_message} — position is UNPROTECTED, "
                    "act NOW.", "error",
                )
        except Exception as exc:
            self.log(f"cancel+replace failed: {exc} — position may be UNPROTECTED, act NOW.", "error")

    def _cancel_all_orders(self, acct: int) -> None:
        try:
            for o in self.client.search_open_orders(acct):
                oid = _order_id(o)
                if oid is not None:
                    try:
                        self.client.cancel_order(acct, oid)
                        self.log(f"Cancelled order {oid}.")
                    except Exception as exc:
                        self.log(f"Cancel {oid} failed: {exc}", "error")
        except Exception as exc:
            self.log(f"Order sweep failed: {exc}", "error")

    def _flatten_positions(self, acct: int) -> None:
        try:
            for p in self.client.search_open_positions(acct):
                cid = p.get("contractId") or p.get("contract_id")
                net = _net_position_value(p)
                if not cid or net == 0:
                    continue
                side = OrderSide.SELL if net > 0 else OrderSide.BUY
                try:
                    self.client.place_order(
                        account_id=acct, contract_id=cid,
                        order_type=OrderType.MARKET, side=side, size=abs(net),
                        custom_tag="imbabot-flatten-" + datetime.now().strftime("%H%M%S"),
                    )
                    self.log(f"Flattened {cid}: market {side.name} {abs(net)}.")
                except Exception as exc:
                    self.log(f"Flatten {cid} failed: {exc}", "error")
        except Exception as exc:
            self.log(f"Position sweep failed: {exc}", "error")


# ----------------------------------------------------------- field helpers
# Position/order payload field names can vary slightly between ProjectX firms;
# extract defensively and log raw shapes on first run if these need tuning.
def _net_position_value(p: Dict[str, Any]) -> int:
    for key in ("netPos", "size", "quantity", "positionSize", "netQuantity"):
        if key in p and p[key] is not None:
            try:
                val = int(p[key])
            except (TypeError, ValueError):
                continue
            # ProjectX PositionType enum: 1 = Long, 2 = Short; size is unsigned.
            # (Live-verified 2026-06-12 — type 1 was previously misread as
            # short, inverting the OCO monitor and the flatten direction.)
            t = p.get("type", p.get("side"))
            if t in (2, "2", "Short", "SHORT", "Sell", "sell"):
                return -abs(val)
            if t in (1, "1", "Long", "LONG", "Buy", "buy"):
                return abs(val)
            return val
    return 0


def _net_position_for(positions: List[Dict[str, Any]], contract_id: str) -> int:
    total = 0
    for p in positions:
        cid = p.get("contractId") or p.get("contract_id")
        if cid == contract_id:
            total += _net_position_value(p)
    return total


def _order_id(o: Dict[str, Any]) -> Optional[int]:
    for key in ("id", "orderId", "order_id"):
        if key in o and o[key] is not None:
            try:
                return int(o[key])
            except (TypeError, ValueError):
                return None
    return None


def _order_tag(o: Dict[str, Any]) -> Optional[str]:
    return o.get("customTag") or o.get("custom_tag")


def _order_stop(o: Dict[str, Any]):
    return o.get("stopPrice", o.get("stop_price"))


def _order_side(o: Dict[str, Any]) -> Optional[int]:
    """Order side as an int (0=BUY, 1=SELL), or None if absent."""
    v = o.get("side")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _order_contract(o: Dict[str, Any]) -> Optional[str]:
    return o.get("contractId") or o.get("contract_id")


def _avg_entry_price(positions: List[Dict[str, Any]], contract_id: str) -> Optional[float]:
    for p in positions:
        cid = p.get("contractId") or p.get("contract_id")
        if cid != contract_id:
            continue
        for key in ("averagePrice", "avgPrice", "averagePx", "entryPrice", "price"):
            v = p.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
    return None
