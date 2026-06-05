"""Browser backend: same opening-range strategy, executed by driving the trading
site in a real browser instead of calling the API.

Two pieces:
  • BrowserEngine — the synchronous primitives (capture price, place, monitor,
    panic). These take a Playwright ``page`` and MUST run on the page's thread.
  • BrowserController — owns the Playwright lifecycle on ONE dedicated thread and
    drives the engine via a command queue, so the GUI/CLI stay responsive and we
    never touch a sync Playwright object from two threads.

The engine reuses the shared strategy math, scheduler, risk guard and logger, so
behaviour matches the API backend; only execution differs.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from ..config import Settings, config_dir
from ..models import Contract, StraddlePlan
from ..risk import RiskError, RiskGuard
from ..scheduler import next_fire_time, seconds_until
from ..strategy import StrategyParams, build_straddle, describe_plan
from .base import AdapterError, PlatformAdapter, make_adapter


def synth_contract(settings: Settings) -> Contract:
    """Browser mode has no contract lookup; synthesize one from settings."""
    return Contract(
        id=settings.contract_symbol, name=settings.contract_symbol,
        description="browser", tick_size=settings.browser_tick_size,
        tick_value=0.0, active=True, symbol_id=settings.contract_symbol,
    )


class BrowserEngine:
    def __init__(self, settings: Settings, adapter: PlatformAdapter, log=None) -> None:
        self.settings = settings
        self.adapter = adapter
        self.risk = RiskGuard(settings)
        self._log = log or (lambda *a, **k: None)
        self.contract = synth_contract(settings)

    def log(self, msg: str, level: str = "info") -> None:
        self._log(msg, level)

    def params(self) -> StrategyParams:
        s = self.settings
        return StrategyParams(s.entry_points, s.stop_loss_points, s.take_profit_points, s.contracts)

    def capture_price(self, page: Any) -> float:
        return self.adapter.read_price(page)

    def build_plan(self, ref: float) -> StraddlePlan:
        tag = "imbabot-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        return build_straddle(self.contract, ref, self.params(), tag_prefix=tag)

    def fire_open(self, page: Any) -> Tuple[Dict[str, str], Optional[StraddlePlan], bool]:
        """Capture price, build plan, place both legs (unless dry-run).

        Returns (handles, plan, placed). ``handles`` maps 'buy'/'sell' -> the
        cancel handle for that entry (used by the OCO step).
        """
        self.log("FIRE — capturing price from the browser chart.")
        ref = self.capture_price(page)
        self.log(f"Reference price captured: {ref:g}")
        plan = self.build_plan(ref)
        self.log(describe_plan(plan))

        if self.settings.dry_run:
            self.log("DRY RUN — no orders placed. Disable dry_run to trade live.", "warn")
            return {}, plan, False
        try:
            self.risk.check_can_send_orders()
        except RiskError as exc:
            self.log(f"Blocked: {exc}", "error")
            return {}, plan, False

        s = self.settings
        handles: Dict[str, str] = {}
        for leg, side in ((plan.long_leg, "buy"), (plan.short_leg, "sell")):
            try:
                h = self.adapter.place_stop_entry(
                    page, side=side, trigger_price=leg.stop_price, size=leg.size,
                    sl_points=s.stop_loss_points, tp_points=s.take_profit_points,
                    tick_size=self.contract.tick_size,
                )
                handles[side] = h
                self.log(f"Placed {side.upper()} STOP {leg.size} @ {leg.stop_price:g} (handle {h}).")
            except AdapterError as exc:
                self.log(f"Place {side.upper()} failed: {exc}", "error")
        if handles:
            self.risk.record_trade()
        return handles, plan, bool(handles)

    def monitor_step(self, page: Any, plan: StraddlePlan, handles: Dict[str, str]) -> bool:
        """One OCO poll. Returns True once a fill is handled (or nothing to do)."""
        try:
            net = self.adapter.read_net_position(page)
        except AdapterError as exc:
            self.log(f"position read failed: {exc}", "warn")
            return False
        if net == 0:
            return False
        other = "sell" if net > 0 else "buy"
        self.log(f"Fill detected ({'LONG' if net > 0 else 'SHORT'} {abs(net)}). "
                 f"Cancelling opposite {other.upper()} entry.")
        h = handles.get(other)
        if h:
            try:
                self.adapter.cancel_entry(page, h)
                self.log(f"Cancelled opposite {other.upper()} entry (handle {h}).")
            except AdapterError as exc:
                self.log(f"Cancel opposite failed: {exc}", "error")
        return True

    def emergency_stop(self, page: Any) -> None:
        self.log("EMERGENCY STOP — cancelling all orders and flattening.", "warn")
        try:
            self.adapter.cancel_all_orders(page)
            self.log("Cancel-all sent.")
        except AdapterError as exc:
            self.log(f"cancel_all failed: {exc}", "error")
        try:
            self.adapter.flatten_all(page)
            self.log("Flatten sent.")
        except AdapterError as exc:
            self.log(f"flatten failed: {exc}", "error")


class BrowserController:
    """Owns Playwright on a dedicated thread; drives BrowserEngine by command."""

    def __init__(self, settings: Settings, log=None) -> None:
        self.settings = settings
        self._log = log or (lambda *a, **k: None)
        self.adapter = make_adapter(settings.browser_platform, settings.browser_url_override)
        self.engine = BrowserEngine(settings, self.adapter, log=self._log)
        self._cmds: "queue.Queue[str]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # shared, read by the GUI thread (plain assignments are atomic enough here)
        self.last_price: Optional[float] = None
        self.logged_in: bool = False
        self.state: str = "idle"        # idle | armed | monitoring
        self.fire_at: Optional[datetime] = None
        self.error: Optional[str] = None

    def log(self, msg: str, level: str = "info") -> None:
        self._log(msg, level)

    # ---- public, thread-safe controls ----
    def launch(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="BrowserController", daemon=True)
        self._thread.start()

    def arm(self) -> None:
        self._cmds.put("arm")

    def disarm(self) -> None:
        self._cmds.put("disarm")

    def panic(self) -> None:
        self._cmds.put("panic")

    def fire_now(self) -> None:
        """Place the straddle immediately (Test section's 'Fire now')."""
        self._cmds.put("fire_now")

    def shutdown(self) -> None:
        self._stop.set()
        self._cmds.put("shutdown")
        if self._thread:
            self._thread.join(timeout=10)

    def next_fire(self) -> datetime:
        s = self.settings
        if s.test_mode and s.test_fire_time:
            from ..scheduler import next_local_fire

            return next_local_fire(s.test_fire_time)
        return next_fire_time(s.open_time(), s.capture_offset_seconds, s.market_tz)

    # ---- the dedicated thread ----
    def _run(self) -> None:
        from .drivers import open_driver

        user_dir = config_dir() / "browser" / self.settings.browser_platform
        user_dir.mkdir(parents=True, exist_ok=True)
        url = self.adapter.url()
        plan = None
        handles: Dict[str, str] = {}
        monitor_deadline: Optional[datetime] = None
        last_price_poll = datetime.min

        session = None
        try:
            try:
                session = open_driver(
                    self.settings.browser_driver, user_dir,
                    self.settings.browser_headless, self.settings.chrome_channel,
                )
            except Exception as exc:
                self.error = f"Could not launch browser ({self.settings.browser_driver}): {exc}"
                self.log(self.error, "error")
                return
            page = session.page
            if url:
                page.goto(url, wait_until="domcontentloaded")
            self.log(f"[{self.adapter.name}] Browser launched ({self.settings.browser_driver}). "
                     f"Log into your account, then click ARM/CONFIRM.")

            while not self._stop.is_set():
                # 1) handle queued commands
                try:
                    cmd = self._cmds.get(timeout=0.05)
                except queue.Empty:
                    cmd = None
                if cmd == "shutdown":
                    break
                elif cmd == "arm":
                    self.fire_at = self.next_fire()
                    self.state = "armed"
                    self.log(f"[{self.adapter.name}] ARMED. Fire at "
                             f"{self.fire_at.strftime('%H:%M:%S %Z')} "
                             f"(mode={self.settings.trade_mode}, dry_run={self.settings.dry_run}).")
                    if not self.logged_in:
                        self.log("Note: login not detected yet — make sure you're signed in.", "warn")
                elif cmd == "disarm":
                    self.state = "idle"
                    self.log(f"[{self.adapter.name}] Disarmed.")
                elif cmd == "panic":
                    self.engine.emergency_stop(page)
                    self.state = "idle"
                elif cmd == "fire_now":
                    self.log(f"[{self.adapter.name}] Manual TEST fire — placing now.")
                    handles, plan, placed = self.engine.fire_open(page)
                    if placed and self.settings.trade_mode == "one_trade":
                        self.state = "monitoring"
                        monitor_deadline = datetime.now() + timedelta(hours=1)
                    else:
                        self.state = "idle"

                # 2) periodic price/login poll (for the dashboard)
                now = datetime.now()
                if (now - last_price_poll).total_seconds() >= 3:
                    last_price_poll = now
                    try:
                        self.logged_in = self.adapter.is_logged_in(page)
                        if self.logged_in:
                            self.last_price = self.adapter.read_price(page)
                    except Exception:
                        pass

                # 3) fire when due
                if self.state == "armed" and self.fire_at and \
                        seconds_until(self.fire_at) <= 0:
                    handles, plan, placed = self.engine.fire_open(page)
                    if placed and self.settings.trade_mode == "one_trade":
                        self.state = "monitoring"
                        monitor_deadline = datetime.now() + timedelta(hours=1)
                    else:
                        self.state = "idle"

                # 4) OCO monitoring (one poll per loop -> stays responsive)
                if self.state == "monitoring":
                    if self.engine.monitor_step(page, plan, handles) or \
                            (monitor_deadline and datetime.now() > monitor_deadline):
                        self.state = "idle"

                # 5) tighten the loop near fire time for tick-accurate capture
                if self.state == "armed" and self.fire_at:
                    remaining = seconds_until(self.fire_at)
                    self._stop.wait(0.02 if remaining < 2 else 0.2)
                else:
                    self._stop.wait(0.2)
        except Exception as exc:
            self.error = str(exc)
            self.log(f"[{self.adapter.name}] Browser session error: {exc}", "error")
        finally:
            if session:
                session.close()
            self.log(f"[{self.adapter.name}] Browser session closed.")
