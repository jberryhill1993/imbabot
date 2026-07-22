"""js_api bridge — thin adapters over the SAME engine operations imbabot/gui.py calls.

Zero trading logic lives here: every method mirrors a gui.py handler 1:1
(_connect_worker, _collect_settings/_on_save, _on_arm, _on_schedule_strategy,
_on_schedule_autofire, _on_fire_now, _on_flatten, _on_panic, _morning_recalc_worker,
_tick_countdown, _ticker_worker, _poll_worker). Confirm dialogs are the frontend's
job (JS confirm()); this layer never prompts.

SECURITY: the API key is WRITE-ONLY. It is accepted by connect(), passed to the
engine / keyring exactly like the classic GUI, and never returned, logged, or
included in any state payload (get_settings exposes only `has_key`). The same
rule covers the Tradovate secrets (password/cid/sec): they arrive in the
connect() payload under "tdv_secrets", go straight to the keyring (if remember)
or to the client's session-only override, and never appear in settings.json,
logs, or any response.
"""
from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ..config import (Settings, load_api_key, load_tradovate_credentials,
                      store_api_key, store_tradovate_credentials)
from ..logbus import Logger
from ..models import Account

_SETTINGS_FIELDS = [
    # (payload key, coercion) — mirrors gui._collect_settings
    ("backend", str), ("browser_platform", str), ("test_mode", bool),
    ("test_fire_time", str), ("strategy_fire_time", str), ("base_url", str),
    ("username", str), ("contract_symbol", lambda v: str(v).strip().upper()),
    ("entry_points", float), ("stop_loss_points", float), ("take_profit_points", float),
    ("sl_tp_entry_mode", str), ("stop_loss_dollars", float), ("take_profit_dollars", float),
    ("contracts", int), ("bot_stop_loss", bool), ("bot_take_profit", bool),
    ("trade_mode", str), ("entry_order_type", str), ("entry_limit_offset_ticks", int),
    ("use_live_data", bool), ("dry_run", bool),
    # Tradovate (non-secret; password/cid/sec travel via connect()'s tdv_secrets)
    ("tdv_environment", str), ("tdv_username", str), ("tdv_app_id", str),
    ("tdv_price_source", str),
]


class Api:
    """One instance per window; pywebview invokes methods on worker threads."""

    def __init__(self) -> None:
        self.settings = Settings.load()
        self.engine = None
        self.controller = None        # browser-backend controller (gui parity)
        self.accounts: List[Account] = []
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._log: list[dict] = []
        self._seq = 0
        self.log = Logger(sink=self._sink)
        self._nq = None          # latest Quote dicts
        self._vix = None
        self._last_price = None  # engine dashboard poll
        self._range = None
        self._mp = None          # last morning-plan dict
        self._mp_busy = False
        self._tick_stop = threading.Event()
        self._poll_stop = threading.Event()
        self._update = None       # UpdateInfo when a newer build is published
        threading.Thread(target=self._ticker_worker, name="Ticker", daemon=True).start()
        threading.Thread(target=self._update_check, name="UpdateCheck", daemon=True).start()

    # ------------------------------------------------------------------ log
    def _sink(self, line: str, level: str) -> None:
        with self._log_lock:
            self._seq += 1
            self._log.append({"seq": self._seq, "ts": datetime.now().strftime("%H:%M:%S"),
                              "level": level, "msg": line})
            if len(self._log) > 800:
                del self._log[: len(self._log) - 800]

    # -------------------------------------------------------- quote threads
    def _ticker_worker(self) -> None:
        from ..ticker import fetch_quote, DEFAULT_TICKER_SYMBOL, VIX_SYMBOL
        while not self._tick_stop.is_set():
            for sym, slot in ((DEFAULT_TICKER_SYMBOL, "_nq"), (VIX_SYMBOL, "_vix")):
                try:
                    q = fetch_quote(sym)
                    if q:
                        setattr(self, slot, {"symbol": q.symbol, "price": q.price,
                                             "chg": q.change, "pct": q.change_pct})
                except Exception:
                    pass
            self._tick_stop.wait(5.0)

    def _poll_worker(self) -> None:
        # Bound to ONE engine: exits when the engine is replaced, so re-connects
        # never stack pollers (stacked pollers rate-limited the TopStep feed
        # into HTTP 429 on 2026-07-19).
        eng = self.engine
        while not self._poll_stop.is_set() and self.engine is eng and eng is not None:
            try:
                self._last_price = eng.last_price()
                self._range = eng.overnight_range()
            except Exception:
                pass
            self._poll_stop.wait(5.0)

    # --------------------------------------------------------- settings I/O
    def _dollars_per_point(self) -> Optional[float]:
        """$/pt per contract: resolved contract math when connected, else the
        symbol-root fallback table (None for unknown roots)."""
        if self.engine is not None:
            c = getattr(self.engine, "contract", None)
            if c is not None and c.tick_size:
                return c.tick_value / c.tick_size
        from ..models import dollars_per_point_for
        return dollars_per_point_for(self.settings.contract_symbol)

    def get_settings(self) -> dict:
        s = self.settings
        out = {k: getattr(s, k) for k, _ in _SETTINGS_FIELDS}
        out["dollars_per_point"] = self._dollars_per_point()
        out["has_key"] = bool(s.username and load_api_key(s.username))
        out["has_tdv_credentials"] = bool(
            s.tdv_username and load_tradovate_credentials(s.tdv_username))
        out["account_name"] = s.account_name
        out["max_contracts"] = s.max_contracts
        return out

    def _apply_settings(self, payload: dict) -> Optional[str]:
        """gui._collect_settings equivalent; returns an error string or None."""
        try:
            for key, coerce in _SETTINGS_FIELDS:
                if key in payload:
                    setattr(self.settings, key, coerce(payload[key]))
        except (ValueError, TypeError) as exc:
            return f"Check the strategy numbers: {exc}"
        # $-entry mode: dollars (per position) -> tick-floored points. Points
        # stay the single source of truth the engine reads.
        s = self.settings
        if s.sl_tp_entry_mode == "dollars":
            from ..models import dollars_to_points
            dpp = self._dollars_per_point()
            if not dpp:
                return (f"Don't know the $/point for {s.contract_symbol!r} — "
                        "connect first or switch SL/TP entry back to points.")
            c = getattr(self.engine, "contract", None) if self.engine else None
            tick = c.tick_size if (c is not None and c.tick_size) else 0.25
            try:
                if s.stop_loss_dollars > 0:
                    s.stop_loss_points = dollars_to_points(
                        s.stop_loss_dollars, s.contracts, dpp, tick)
                if s.take_profit_dollars > 0:
                    s.take_profit_points = dollars_to_points(
                        s.take_profit_dollars, s.contracts, dpp, tick)
            except ValueError as exc:
                return f"Check the $ SL/TP values: {exc}"
        return None

    def save_settings(self, payload: dict) -> dict:
        with self._lock:
            err = self._apply_settings(payload)
            if err:
                return {"ok": False, "error": err}
            s = self.settings
            s.save()
            contract_txt = ""
            if self.engine:
                self.engine.settings = s
                self.engine.risk.settings = s
                try:
                    c = self.engine.refresh_contract()
                    contract_txt = f"{c.name} ({c.id})  tick={c.tick_size} ${c.tick_value}/tick"
                except Exception as exc:
                    self.log(f"contract refresh failed: {exc}", "warn")
            self.log(f"Saved: {s.contract_symbol} ±{s.entry_points}pt SL{s.stop_loss_points} "
                     f"TP{s.take_profit_points} x{s.contracts} mode={s.trade_mode} dry_run={s.dry_run}")
            return {"ok": True, "contract": contract_txt}

    # -------------------------------------------------------------- connect
    def _retire_engine(self) -> None:
        """Shut down a previous engine before a re-connect. Leaked clients kept
        their WebSocket reconnect loops alive with stale tokens (endless 401s
        that also burn Tradovate's ~5 auth attempts/hour budget)."""
        if self.engine is None:
            return
        self._poll_stop.set()
        try:
            if self.engine.armed:
                self.engine.disarm()
        except Exception:
            pass
        close = getattr(self.engine.client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        self.engine = None

    def connect(self, payload: dict, api_key: str, remember: bool) -> dict:
        with self._lock:
            # Secrets NEVER pass through _apply_settings (they'd land in settings.json).
            payload = dict(payload or {})
            tdv_secrets = payload.pop("tdv_secrets", None) or {}
            err = self._apply_settings(payload)
            if err:
                return {"ok": False, "error": err}
            self._retire_engine()
            s = self.settings
            s.save()
            if s.backend == "tradovate":
                return self._connect_tradovate(s, tdv_secrets, remember)
            if s.backend == "browser":
                # gui._launch_browser parity: a real Chrome opens; user logs in, then arms.
                if self.controller is not None:
                    return {"ok": False, "error": "Browser session is already running."}
                try:
                    from ..browser import BrowserController
                except Exception as exc:
                    return {"ok": False, "error": f"Browser mode needs Selenium + Chrome: {exc}"}
                self.controller = BrowserController(s, log=self.log)
                self.controller.launch()
                self.log(f"Browser backend launching for {s.browser_platform}. Log in, then Arm.")
                return {"ok": True, "browser": True, "accounts": [], "contract": ""}
            key = (api_key or "").strip() or (load_api_key(s.username) or "")
            if not key:
                return {"ok": False, "error": "Enter your TopstepX API key."}
            if remember and api_key.strip():
                backend = store_api_key(s.username, api_key.strip())
                self.log(f"API key stored via {backend}.")
            try:
                from ..engine import BotEngine
                engine = BotEngine(s, log=self.log)
                engine.connect(key)
                accounts = engine.list_accounts()
            except Exception as exc:
                self.log(f"Connect failed: {exc}", "error")
                return {"ok": False, "error": str(exc)}
            self.engine, self.accounts = engine, accounts
            self._poll_stop.clear()
            threading.Thread(target=self._poll_worker, daemon=True).start()
            c = engine.contract
            return {"ok": True,
                    "accounts": [{"id": a.id, "name": a.name, "can_trade": a.can_trade}
                                 for a in accounts],
                    "account_id": engine.account.id if engine.account else None,
                    "contract": (f"{c.name} ({c.id})  tick={c.tick_size} ${c.tick_value}/tick"
                                 if c else "")}

    def _connect_tradovate(self, s: Settings, secrets: dict, remember: bool) -> dict:
        """Tradovate branch of connect(). ``secrets`` = {password, cid, sec} from
        the form (all optional if previously remembered). Secrets go to the
        keyring when remember=True, otherwise ride the client's session-only
        override — never settings.json, never the log, never a response."""
        if not s.tdv_username:
            return {"ok": False, "error": "Enter your Tradovate username."}
        secrets = {k: str(v).strip() for k, v in (secrets or {}).items()
                   if k in ("password", "cid", "sec") and str(v or "").strip()}
        stored = load_tradovate_credentials(s.tdv_username) or {}
        if not (secrets.get("password") or stored.get("password")):
            return {"ok": False, "error": "Enter your Tradovate password."}
        if not (secrets.get("cid") or stored.get("cid")) or \
           not (secrets.get("sec") or stored.get("sec")):
            return {"ok": False, "error":
                    "Enter the API key cid + secret (Tradovate → API Access)."}
        if remember and secrets:
            blob = dict(stored)
            blob.update(secrets)
            blob["app_id"] = s.tdv_app_id or "Imbabot"
            backend = store_tradovate_credentials(s.tdv_username, blob)
            self.log(f"Tradovate credentials stored via {backend}.")
        try:
            from ..engine import BotEngine
            engine = BotEngine(s, log=self.log)
            if not remember and secrets:
                engine.client.session_secrets = secrets
            engine.connect(secrets.get("password", ""))
            accounts = engine.list_accounts()
        except Exception as exc:
            self.log(f"Tradovate connect failed: {exc}", "error")
            return {"ok": False, "error": str(exc)}
        self.engine, self.accounts = engine, accounts
        s.save()   # persists the auto-generated tdv_device_id
        self._poll_stop.clear()
        threading.Thread(target=self._poll_worker, daemon=True).start()
        c = engine.contract
        return {"ok": True,
                "env": s.tdv_environment,
                "accounts": [{"id": a.id, "name": a.name, "can_trade": a.can_trade}
                             for a in accounts],
                "account_id": engine.account.id if engine.account else None,
                "contract": (f"{c.name} ({c.id})  tick={c.tick_size} ${c.tick_value}/tick"
                             if c else "")}

    def pick_account(self, account_id: int) -> dict:
        with self._lock:
            if not self.engine:
                return {"ok": False, "error": "Connect first."}
            for a in self.accounts:
                if a.id == account_id:
                    self.engine.account = a
                    self.engine.settings.account_id = a.id
                    self.engine.settings.account_name = a.name
                    self.engine.settings.save()
                    self.log(f"Account set to {a.name} (id={a.id}).")
                    return {"ok": True}
            return {"ok": False, "error": "Unknown account."}

    # ---------------------------------------------------------- arm / fire
    def preview_test_time(self, hms: str) -> dict:
        """For the frontend's pre-arm confirm: does this test time roll to tomorrow?"""
        from ..scheduler import parse_hms, next_local_fire
        try:
            parse_hms(hms.strip())
            fire = next_local_fire(hms.strip())
        except ValueError as exc:
            return {"ok": False, "error": f"Use HH:MM or HH:MM:SS (24-hour): {exc}"}
        return {"ok": True, "fires_tomorrow": fire.date() != datetime.now().astimezone().date(),
                "first_fire": fire.strftime("%a %b %d at %H:%M:%S"),
                "now": datetime.now().astimezone().strftime("%H:%M:%S")}

    def arm(self, payload: dict) -> dict:
        """Mirror gui._on_arm (both backends). LIVE confirmation happens in the frontend."""
        with self._lock:
            if self.settings.backend == "browser":
                # gui._on_arm_browser parity
                if self.controller is None:
                    return {"ok": False, "error": "Click Connect (Launch Browser) and log in before arming."}
                if self.controller.state in ("armed", "monitoring"):
                    self.controller.disarm()
                    return {"ok": True, "armed": False}
                err = self._apply_settings(payload)
                if err:
                    return {"ok": False, "error": err}
                s = self.settings
                s.save()
                self.controller.settings = s
                self.controller.engine.settings = s
                try:
                    self.controller.arm()
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "armed": True}
            if not self.engine:
                return {"ok": False, "error": "Connect before arming."}
            if self.engine.armed:
                self.engine.disarm()
                return {"ok": True, "armed": False}
            err = self._apply_settings(payload)
            if err:
                return {"ok": False, "error": err}
            s = self.settings
            s.save()
            self.engine.settings = s
            self.engine.risk.settings = s
            try:
                self.engine.arm(on_tick=None)
            except Exception as exc:
                self.log(f"Arm refused: {exc}", "error")
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "armed": True}

    def schedule_daily(self, payload: dict, hms: str) -> dict:
        """Mirror gui._on_schedule_strategy (toggle: cancels when armed)."""
        from ..scheduler import parse_hms, next_weekday_local_fire
        with self._lock:
            if not self.engine:
                return {"ok": False, "error": "Connect before arming the daily schedule."}
            if self.engine.armed:
                self.engine.disarm()
                self.log("Daily schedule cancelled (disarmed).", "warn")
                return {"ok": True, "armed": False}
            try:
                parse_hms(hms.strip())
            except ValueError as exc:
                return {"ok": False, "error": f"Use HH:MM:SS (24-hour): {exc}"}
            payload = dict(payload); payload["test_mode"] = False
            payload["strategy_fire_time"] = hms.strip()
            err = self._apply_settings(payload)
            if err:
                return {"ok": False, "error": err}
            s = self.settings
            s.save()
            self.engine.settings = s
            self.engine.risk.settings = s
            fire = next_weekday_local_fire(hms.strip())
            try:
                self.engine.arm(on_tick=None)
            except Exception as exc:
                self.log(f"Daily schedule refused: {exc}", "error")
                return {"ok": False, "error": str(exc)}
            self.log(f"Daily auto-fire armed — first fire {fire.strftime('%a %b %d %H:%M:%S')}, "
                     f"then every weekday at {hms.strip()} (your computer's local clock).")
            return {"ok": True, "armed": True, "first_fire": fire.strftime("%a %b %d %H:%M:%S")}

    def schedule_test(self, payload: dict, hms: str) -> dict:
        """Mirror gui._on_schedule_autofire (toggle: cancels when armed)."""
        from ..scheduler import parse_hms, next_local_fire
        with self._lock:
            if not self.engine:
                return {"ok": False, "error": "Connect before scheduling auto-fire."}
            if self.engine.armed:
                self.engine.disarm()
                self.log("Auto-fire cancelled (disarmed).", "warn")
                return {"ok": True, "armed": False}
            try:
                parse_hms(hms.strip())
            except ValueError as exc:
                return {"ok": False, "error": f"Use HH:MM or HH:MM:SS (24-hour): {exc}"}
            payload = dict(payload); payload["test_mode"] = True
            payload["test_fire_time"] = hms.strip()
            err = self._apply_settings(payload)
            if err:
                return {"ok": False, "error": err}
            s = self.settings
            s.save()
            self.engine.settings = s
            self.engine.risk.settings = s
            fire = next_local_fire(hms.strip())
            try:
                self.engine.arm(on_tick=None)
            except Exception as exc:
                self.log(f"Auto-fire schedule refused: {exc}", "error")
                return {"ok": False, "error": str(exc)}
            self.log(f"Auto-fire scheduled for {fire.strftime('%a %b %d %H:%M:%S')} "
                     "(your computer's local clock) — it will fire automatically.")
            return {"ok": True, "armed": True,
                    "fires_tomorrow": fire.date() != datetime.now().astimezone().date(),
                    "first_fire": fire.strftime("%a %b %d %H:%M:%S")}

    def fire_test_now(self, payload: dict) -> dict:
        """Mirror gui._on_fire_now (API backend); JS confirms before calling."""
        with self._lock:
            err = self._apply_settings(payload)
            if err:
                return {"ok": False, "error": err}
            s = self.settings
            s.save()
            if s.backend == "browser":
                if self.controller is None:
                    return {"ok": False, "error": "Launch the browser and log in before firing."}
                self.controller.settings = s
                self.controller.engine.settings = s
                self.controller.fire_now()
                return {"ok": True}
            if not self.engine:
                return {"ok": False, "error": "Connect before firing."}
            self.engine.settings = s
            self.engine.risk.settings = s
            self.engine.fire_now()
            return {"ok": True}

    def flatten(self) -> dict:
        if self.settings.backend == "browser" and self.controller:
            threading.Thread(target=self.controller.flatten, daemon=True).start()
            return {"ok": True}
        if not self.engine:
            return {"ok": False, "error": "Connect first."}
        threading.Thread(target=self.engine.flatten_all, daemon=True).start()
        return {"ok": True}

    def emergency_stop(self) -> dict:
        if self.settings.backend == "browser" and self.controller:
            threading.Thread(target=self.controller.panic, daemon=True).start()
            return {"ok": True}
        if not self.engine:
            return {"ok": False, "error": "Connect first."}
        threading.Thread(target=self.engine.emergency_stop, daemon=True).start()
        return {"ok": True}

    # --------------------------------------------------------- Morning Plan
    def recalc_morning(self, target: float) -> dict:
        """Mirror gui._morning_recalc_worker (runs on the js_api worker thread)."""
        if self._mp_busy:
            return {"ok": False, "error": "already calculating"}
        self._mp_busy = True
        try:
            try:
                from ..analysis.market_history import refresh, VIX_SYMBOL, NQ_SYMBOL
                refresh(VIX_SYMBOL)
                refresh(NQ_SYMBOL)
            except Exception:
                pass
            from ..analysis.tick_runner import morning_plan
            dpp = 20.0
            if self.engine:
                c = getattr(self.engine, "contract", None)
                if c and c.tick_size:
                    dpp = c.tick_value / c.tick_size
            today = datetime.now().astimezone().date().isoformat()
            mp = morning_plan(today, target_dollars=float(target or 800.0),
                              dollars_per_point=dpp,
                              max_contracts=self.settings.max_contracts)
            d = asdict(mp)
            self._mp = d
            if not mp.calibrated:
                from ..analysis.spike_model import _path as _model_path
                mp_ = _model_path()
                self.log(f"Morning Plan {mp.session_date}: MODEL NOT LOADED — "
                         f"model={mp_} exists={mp_.exists()} (showing no advice)", "warn")
            else:
                self.log(f"Morning Plan {mp.session_date}: {mp.decision}/{mp.conviction} "
                         f"spike ~{mp.predicted_spike:.0f}pt -> "
                         f"{mp.plan.contracts if (mp.decision == 'TRADE' and mp.plan.feasible) else 0}ct")
            return {"ok": True, "plan": d}
        except Exception as exc:
            self.log(f"morning plan: {exc}", "error")
            return {"ok": False, "error": str(exc)}
        finally:
            self._mp_busy = False

    # ----------------------------------------------------------------- log
    def save_log(self) -> dict:
        try:
            import webview
            win = webview.windows[0]
            dest = win.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"imbabot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log")
            if dest:
                path = dest if isinstance(dest, str) else dest[0]
                self.log.save_copy(Path(path))
                self.log(f"Log saved to {path}")
                return {"ok": True, "path": str(path)}
            return {"ok": False, "error": "cancelled"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------------------------------------------------------- state
    def get_state(self, cursor: int = 0) -> dict:
        """One JSON poll — mirrors gui._tick_countdown + _drain_events data."""
        from ..scheduler import (seconds_until, format_countdown, next_fire_time,
                                 next_local_fire, next_weekday_local_fire)
        s = self.settings
        d_fire = None
        try:
            if s.test_mode and s.test_fire_time:
                d_fire = next_local_fire(s.test_fire_time)
            elif s.strategy_fire_time:
                d_fire = next_weekday_local_fire(s.strategy_fire_time)
            if d_fire is None:
                d_fire = next_fire_time(s.open_time(), s.capture_offset_seconds, s.market_tz)
        except Exception:
            d_fire = None
        with self._log_lock:
            fresh = [e for e in self._log if e["seq"] > int(cursor or 0)]
        browser_armed = bool(self.controller and self.controller.state in ("armed", "monitoring"))
        return {
            "countdown": format_countdown(seconds_until(d_fire)) if d_fire else "—",
            "next_fire": d_fire.strftime("%H:%M:%S") if d_fire else "—",
            "connected": self.engine is not None or self.controller is not None,
            "armed": bool(self.engine and self.engine.armed) or browser_armed,
            "dry_run": bool(s.dry_run),
            "backend": s.backend,
            "tdv_env": s.tdv_environment if s.backend == "tradovate" else None,
            "update": ({"version": self._update.version,
                        "notes": self._update.notes[:400]} if self._update else None),
            "account": s.account_name or "",
            "nq": self._nq, "vix": self._vix,
            "last_price": self._last_price,
            "range": self._range,
            "log": fresh, "seq": self._seq,
        }

    # -------------------------------------------------------------- updates
    def _update_check(self) -> None:
        try:
            from ..updater import check_for_update
            info = check_for_update()
            if info and info.code_update_available:
                self._update = info
                self.log(f"Update available: v{info.version} — click Update in the header.")
        except Exception:
            pass

    def apply_update(self) -> dict:
        """Download + verify + swap to the newer build (frozen app), then relaunch."""
        info = self._update
        if not info:
            return {"ok": False, "error": "No update available."}
        try:
            from ..updater import download_app, apply_app_update
            self.log(f"Downloading v{info.version}…")
            path = download_app(info, log=self.log)
            if not path:
                return {"ok": False, "error": "This release has no app download."}
            if apply_app_update(path, log=self.log):
                # The updater script retries the exe copy until WE exit and
                # Windows releases the file lock — so exit shortly after
                # returning this response to the frontend. (Live-found
                # 2026-07-22: without this exit the update never applied.)
                def _exit_for_update():
                    import time as _t
                    _t.sleep(1.5)              # let the response reach the UI
                    try:
                        self.shutdown()        # disarm + stop threads cleanly
                    except Exception:
                        pass
                    try:
                        import webview
                        for w in list(webview.windows):
                            w.destroy()
                    except Exception:
                        pass
                    import os as _os
                    _os._exit(0)               # hard exit -> exe lock released
                threading.Thread(target=_exit_for_update, daemon=True).start()
                return {"ok": True, "restarting": True}
            return {"ok": False, "error": "Update applies to the packaged app only "
                    "(this looks like a source run)."}
        except Exception as exc:
            self.log(f"Update failed: {exc}", "error")
            return {"ok": False, "error": str(exc)}

    def shutdown(self) -> None:
        """gui.on_close parity: stop threads, disarm, shut the browser session."""
        self._tick_stop.set()
        self._poll_stop.set()
        if self.engine and self.engine.armed:
            self.engine.disarm()
        if self.controller is not None:
            try:
                self.controller.shutdown()
            except Exception:
                pass
