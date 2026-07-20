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
            # Lazy imports: avoid pulling requests/websocket on offline paths.
            if settings.backend == "tradovate":
                from .tradovate import TradovateClient

                client = TradovateClient(settings, log=log or (lambda *a, **k: None))
            else:
                from .projectx import ProjectXClient

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
        if s.backend == "tradovate":
            user, venue = s.tdv_username, f"Tradovate {s.tdv_environment.upper()}"
        else:
            user, venue = s.username, s.base_url
        self.log(f"Authenticating as {user} @ {venue} …")
        self.client.authenticate(user, api_key)
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
            bot_stop_loss=s.bot_stop_loss,
            bot_take_profit=s.bot_take_profit,
            entry_order_type=s.entry_order_type,
            entry_limit_offset_ticks=s.entry_limit_offset_ticks,
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
            # Log once, not per 5s poll (a Tradovate session spammed 6,699
            # copies of the "not supported" warning on 2026-07-19).
            if not getattr(self, "_range_warned", False):
                self._range_warned = True
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
        """Capture the reference price, auto-detecting the data feed.

        Tries the preferred feed (``use_live_data`` = live, else sim) first, then
        falls back to the other if it returns nothing. This makes eval accounts
        (sim-only) and funded accounts (live) both work without flipping a toggle —
        a single hard-coded feed would fail price capture on the wrong account type.
        Logs which feed produced the price.
        """
        preferred = bool(self.settings.use_live_data)
        last_exc: Optional[Exception] = None
        for live in (preferred, not preferred):
            for i in range(1, attempts + 1):
                try:
                    px = self.client.last_price(self.contract.id, live=live)
                    self.log(f"Reference price via {'LIVE' if live else 'sim'} feed.")
                    return px
                except Exception as exc:
                    last_exc = exc
                    self.log(f"{'LIVE' if live else 'sim'} feed attempt {i}/{attempts} "
                             f"failed: {exc}", "warn")
            if live == preferred:
                self.log(f"{'LIVE' if preferred else 'sim'} feed unavailable; "
                         f"falling back to {'sim' if preferred else 'LIVE'} feed.", "warn")
        raise RuntimeError(f"could not capture reference price on either feed: {last_exc}")

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

    def _oco_scan(self, plan: StraddlePlan, acct: int, cid: str, seen_open: set) -> bool:
        """One OCO check. Returns True once it has acted (a leg filled → others
        cancelled) so the monitor can stop.

        A fill is detected by an entry ORDER leaving the open-order book *after*
        we've confirmed it resting — NOT by a currently-open position. A filled
        order stays gone; a position can open and close (TP/SL) between polls, which
        is exactly how the old position-only check let the opposite entry survive and
        fill later. A live position is also accepted as a fill signal (belt-and-braces).
        ``seen_open`` accumulates our entries we've observed resting, so we never treat
        a not-yet-propagated entry's absence as a fill.
        """
        our_ids = {leg.order_id for leg in plan.legs if leg.order_id is not None}
        try:
            open_ids = {_order_id(o) for o in self.client.search_open_orders(acct)}
        except Exception as exc:
            self.log(f"OCO order poll error: {exc}", "warn")
            return False
        seen_open |= (our_ids & open_ids)          # confirm which entries are resting
        gone = {oid for oid in seen_open if oid not in open_ids}  # a confirmed entry filled
        try:
            net = _net_position_for(self.client.search_open_positions(acct), cid)
        except Exception:
            net = 0
        if not gone and net == 0:
            return False                            # both entries still resting

        self.log(f"Fill detected (entry filled; net={net}). Cancelling the other entry.")
        for leg in plan.legs:
            oid = leg.order_id
            if oid is None or oid not in open_ids:
                continue                            # this leg filled or is already gone
            try:
                self.client.cancel_order(acct, oid)
                self.log(f"Cancelled remaining {leg.side.name} entry orderId={oid}.")
            except Exception as exc:
                self.log(f"Failed to cancel {leg.side.name} entry {oid}: {exc}", "error")
        return True

    def _monitor_oco(self, plan: StraddlePlan, poll_seconds: float = 0.5) -> None:
        """One-Trade OCO: the instant one entry fills, cancel the other so only one
        position is ever taken (no second trade that day). See ``_oco_scan``."""
        self.log("OCO monitor started (One-Trade mode).")
        acct = self.account.id
        cid = plan.contract.id
        seen_open: set = set()
        deadline = datetime.now() + timedelta(hours=1)
        while not self._monitor_stop.is_set() and datetime.now() < deadline:
            if self._oco_scan(plan, acct, cid, seen_open):
                return
            self._monitor_stop.wait(poll_seconds)
        self.log("OCO monitor stopped.")
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


