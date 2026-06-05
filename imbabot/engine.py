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

        fire = self.next_fire()
        self.log(
            f"ARMED. Fire at {fire.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"(capture {self.settings.capture_offset_seconds}s before "
            f"{self.settings.open_hour:02d}:{self.settings.open_minute:02d} open). "
            f"Mode={self.settings.trade_mode} dry_run={self.settings.dry_run}."
        )
        self._timer = FireTimer(target=fire, on_fire=self._on_fire, on_tick=on_tick)
        self._timer.arm()
        return fire

    def disarm(self) -> None:
        if self._timer:
            self._timer.disarm()
            self._timer = None
        self.log("Disarmed.")

    # ---------------------------------------------------------------- fire
    def _on_fire(self) -> None:
        try:
            self.log("FIRE — capturing reference price.")
            ref = self.client.last_price(self.contract.id, live=self.settings.use_live_data)
            self.log(f"Reference price captured: {ref:g}")

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
        for leg in plan.legs:
            res = self.client.place_straddle_leg(acct, cid, leg)
            if res.success:
                self.log(
                    f"Placed {leg.side.name} STOP {leg.size}@{leg.stop_price:g} "
                    f"-> orderId={res.order_id} tag={leg.custom_tag}"
                )
            else:
                self.log(
                    f"REJECTED {leg.side.name} leg: {res.error_message} "
                    f"(code {res.error_code})",
                    "error",
                )
        self.risk.record_trade()

    # -------------------------------------------------------------- monitor
    def _start_monitor(self, plan: StraddlePlan) -> None:
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_oco, args=(plan,), name="OCOMonitor", daemon=True
        )
        self._monitor_thread.start()

    def _monitor_oco(self, plan: StraddlePlan, poll_seconds: float = 1.0) -> None:
        """One-Trade OCO: when one entry fills, cancel the opposite entry.

        We only cancel the opposite leg on a confirmed *fill* (a non-flat position
        on our contract), not on an external cancel. The per-leg brackets bound
        risk regardless, so a both-sides whipsaw is contained.
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
                filled = plan.long_leg if net > 0 else plan.short_leg
                other = plan.short_leg if net > 0 else plan.long_leg
                self.log(
                    f"Fill detected ({'LONG' if net > 0 else 'SHORT'} {abs(net)}). "
                    f"Cancelling opposite {other.side.name} entry."
                )
                if other.order_id is not None:
                    try:
                        self.client.cancel_order(acct, other.order_id)
                        self.log(f"Cancelled opposite entry orderId={other.order_id}.")
                    except Exception as exc:
                        self.log(f"Failed to cancel opposite entry: {exc}", "error")
                return
            self._monitor_stop.wait(poll_seconds)
        self.log("OCO monitor stopped.")

    # ------------------------------------------------------- emergency stop
    def emergency_stop(self) -> None:
        """Panic: disarm, cancel ALL working orders, flatten ALL positions."""
        self.log("EMERGENCY STOP engaged.", "warn")
        self._monitor_stop.set()
        if self._timer:
            self._timer.disarm()
            self._timer = None
        if not self.account:
            self.log("No account bound; nothing to flatten.", "warn")
            return
        acct = self.account.id
        # 1) cancel working orders
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
        # 2) flatten positions with market orders
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
        self.log("Emergency stop complete.")


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
            # Some feeds encode direction separately (0=long,1=short) with size >= 0
            t = p.get("type", p.get("side"))
            if val >= 0 and t in (1, "1", "Short", "SHORT", "sell"):
                return -abs(val)
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
