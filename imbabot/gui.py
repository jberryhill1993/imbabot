"""Tkinter dashboard — this is what gets packaged into the .exe.

Layout mirrors the setup guide: a risk disclaimer on launch, connection +
strategy settings, a live dashboard (price / overnight range / countdown), Arm
and Emergency-Stop controls, and a log panel you can save.

Threading model: only the main thread touches widgets. Background work (connect,
data polling, the fire timer, the OCO monitor) runs on worker threads that push
events onto a queue; the main thread drains it via ``root.after``.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

try:
    import tkinter as tk
    from tkinter import messagebox, filedialog, ttk
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Tkinter is required for the GUI. On macOS use python.org Python or "
        "`brew install python-tk`. Error: %s" % exc
    )

from . import __version__
from .config import Settings, load_api_key, store_api_key, log_path
from .logbus import Logger
from .models import Account

DISCLAIMER = (
    "Imbabot places real futures orders through your TopstepX / ProjectX account.\n\n"
    "• This is NOT trading or financial advice. You are solely responsible for every "
    "order it places and any losses.\n"
    "• Automated trading is allowed in Topstep Combine/Funded (evaluation) accounts but "
    "PROHIBITED in the Live Funded account, and must run locally (no VPS/cloud). Confirm "
    "your firm's current rules before trading live.\n"
    "• The bot starts in DRY-RUN mode (no orders sent). You must deliberately disable it "
    "to trade.\n\n"
    "Click Accept to continue, or Exit to close."
)

RED = "#c0392b"
GREEN = "#1e8449"
AMBER = "#b9770e"
BG = "#1b1f24"
FG = "#e6e6e6"
PANEL = "#252b32"


class ImbabotGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = Settings.load()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.log = Logger(sink=self._enqueue_log)
        self.engine = None       # API backend, created on connect
        self.controller = None   # browser backend controller, created on launch
        self.accounts: List[Account] = []
        self._poll_stop = threading.Event()

        root.title(f"Imbabot {__version__} — TopstepX opening-range bot")
        root.configure(bg=BG)
        root.geometry("860x720")
        root.minsize(760, 640)

        if not self._show_disclaimer():
            root.destroy()
            return

        self._build_styles()
        self._build_widgets()
        self._load_into_widgets()
        self.root.after(150, self._drain_events)
        self.root.after(1000, self._tick_countdown)
        self.log(f"Imbabot {__version__} ready. Config: {log_path().parent}")

    # ---------------------------------------------------------- disclaimer
    def _show_disclaimer(self) -> bool:
        return messagebox.askokcancel(
            "Risk disclaimer", DISCLAIMER, icon="warning",
            default="cancel",
        )

    # ------------------------------------------------------------- styling
    def _build_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("Head.TLabel", background=BG, foreground=FG, font=("Helvetica", 11, "bold"))
        style.configure("Big.TLabel", background=PANEL, foreground=FG, font=("Helvetica", 22, "bold"))
        style.configure("TButton", padding=6)
        style.configure("TCheckbutton", background=PANEL, foreground=FG)
        style.configure("TRadiobutton", background=PANEL, foreground=FG)
        style.configure("TEntry", fieldbackground="#0f1316", foreground=FG)

    def _panel(self, parent, title: str) -> ttk.Frame:
        wrap = ttk.Frame(parent, style="TFrame")
        ttk.Label(wrap, text=title, style="Head.TLabel").pack(anchor="w", pady=(8, 2))
        inner = ttk.Frame(wrap, style="Panel.TFrame", padding=10)
        inner.pack(fill="x")
        wrap.pack(fill="x", padx=12)
        return inner

    # ------------------------------------------------------------- widgets
    def _build_widgets(self) -> None:
        # ---- backend ----
        b = self._panel(self.root, "0 · Backend")
        self.var_backend = tk.StringVar(value=self.settings.backend)
        ttk.Radiobutton(b, text="API (TopstepX — recommended)", variable=self.var_backend,
                        value="api", command=self._on_backend_change).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(b, text="Browser automation (fallback)", variable=self.var_backend,
                        value="browser", command=self._on_backend_change).grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Label(b, text="Platform", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(16, 4))
        self.var_platform = tk.StringVar(value=self.settings.browser_platform)
        self.cmb_platform = ttk.Combobox(b, state="readonly", width=12,
                                         values=["projectx", "tradesea"], textvariable=self.var_platform)
        self.cmb_platform.grid(row=0, column=3, sticky="w")
        self.var_use_chrome = tk.BooleanVar(value=self.settings.chrome_channel == "chrome")
        ttk.Checkbutton(b, text="Use my installed Google Chrome", variable=self.var_use_chrome).grid(
            row=0, column=4, sticky="w", padx=(16, 0))
        self.lbl_backend_hint = ttk.Label(b, text="", style="Panel.TLabel", foreground=AMBER)
        self.lbl_backend_hint.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        # ---- connection ----
        c = self._panel(self.root, "1 · Connection")
        self.var_base = tk.StringVar(value=self.settings.base_url)
        self.var_user = tk.StringVar(value=self.settings.username)
        self.var_key = tk.StringVar(value="")
        self.var_remember = tk.BooleanVar(value=True)
        self._row(c, "Base URL", self.var_base, 0, width=34)
        self._row(c, "Username", self.var_user, 1, width=24)
        ttk.Label(c, text="API key", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(c, textvariable=self.var_key, show="•", width=36).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(c, text="remember on this device", variable=self.var_remember).grid(
            row=2, column=2, padx=8, sticky="w")
        self.btn_connect = ttk.Button(c, text="Connect", command=self._on_connect)
        self.btn_connect.grid(row=0, column=2, rowspan=2, padx=8)
        self.lbl_conn = ttk.Label(c, text="not connected", style="Panel.TLabel", foreground=AMBER)
        self.lbl_conn.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---- account / contract ----
        a = self._panel(self.root, "2 · Account & Contract")
        ttk.Label(a, text="Account", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.cmb_account = ttk.Combobox(a, state="readonly", width=30, values=[])
        self.cmb_account.grid(row=0, column=1, sticky="w", padx=6)
        self.cmb_account.bind("<<ComboboxSelected>>", self._on_account_pick)
        ttk.Label(a, text="Symbol", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.var_symbol = tk.StringVar(value=self.settings.contract_symbol)
        ttk.Entry(a, textvariable=self.var_symbol, width=10).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Button(a, text="Resolve", command=self._on_resolve).grid(row=0, column=4, padx=4)
        self.lbl_contract = ttk.Label(a, text="—", style="Panel.TLabel")
        self.lbl_contract.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        # ---- strategy ----
        s = self._panel(self.root, "3 · Strategy")
        self.var_points = tk.StringVar(value=str(self.settings.entry_points))
        self.var_sl = tk.StringVar(value=str(self.settings.stop_loss_points))
        self.var_tp = tk.StringVar(value=str(self.settings.take_profit_points))
        self.var_contracts = tk.StringVar(value=str(self.settings.contracts))
        self._row(s, "Entry points (±)", self.var_points, 0, width=8)
        self._row(s, "Stop-loss points", self.var_sl, 1, width=8)
        self._row(s, "Take-profit points", self.var_tp, 2, width=8)
        self._row(s, "Contracts", self.var_contracts, 3, width=8)
        self.var_mode = tk.StringVar(value=self.settings.trade_mode)
        ttk.Label(s, text="Mode", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(20, 0))
        ttk.Radiobutton(s, text="Semi-Auto (you manage)", variable=self.var_mode,
                        value="semi_auto").grid(row=1, column=2, sticky="w", padx=(20, 0))
        ttk.Radiobutton(s, text="One-Trade (auto OCO)", variable=self.var_mode,
                        value="one_trade").grid(row=2, column=2, sticky="w", padx=(20, 0))
        self.var_live_data = tk.BooleanVar(value=self.settings.use_live_data)
        self.var_dry = tk.BooleanVar(value=self.settings.dry_run)
        ttk.Checkbutton(s, text="use live data feed", variable=self.var_live_data).grid(
            row=3, column=2, sticky="w", padx=(20, 0))
        ttk.Checkbutton(s, text="DRY-RUN (no real orders)", variable=self.var_dry,
                        command=self._on_dry_toggle).grid(row=4, column=2, sticky="w", padx=(20, 0))
        ttk.Button(s, text="Save settings", command=self._on_save).grid(row=4, column=0, sticky="w", pady=(8, 0))

        # ---- test ----
        t = self._panel(self.root, "3b · Test — verify it actually places orders")
        self.var_test_mode = tk.BooleanVar(value=self.settings.test_mode)
        ttk.Checkbutton(t, text="Test mode: fire at a custom time (not 9:30)",
                        variable=self.var_test_mode, command=self._on_test_toggle).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(t, text="Fire at (HH:MM:SS, your local time)", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", pady=3)
        self.var_test_time = tk.StringVar(value=self.settings.test_fire_time)
        ttk.Entry(t, textvariable=self.var_test_time, width=12).grid(row=1, column=1, sticky="w")
        tk.Button(t, text="Fire TEST now", command=self._on_fire_now, bg=AMBER, fg="white",
                  font=("Helvetica", 10, "bold"), relief="flat").grid(row=1, column=2, padx=12)
        ttk.Label(t, text="Use a SIM/practice account. With test mode on, Save then Arm and it fires at that time; "
                          "or click ‘Fire TEST now’. Honors dry-run.",
                  style="Panel.TLabel", foreground=AMBER, wraplength=560).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---- dashboard ----
        d = self._panel(self.root, "4 · Dashboard")
        self.lbl_price = self._metric(d, "Last price", "—", 0)
        self.lbl_range = self._metric(d, "Overnight range", "—", 1)
        self.lbl_fire = self._metric(d, "Next fire (ET)", "—", 2)
        ttk.Label(d, text="Countdown", style="Panel.TLabel").grid(row=0, column=3, padx=(24, 6))
        self.lbl_count = ttk.Label(d, text="—:—:—", style="Big.TLabel")
        self.lbl_count.grid(row=1, column=3, rowspan=2, padx=(24, 6))

        # ---- controls ----
        ctrl = ttk.Frame(self.root, style="TFrame")
        ctrl.pack(fill="x", padx=12, pady=10)
        self.btn_arm = tk.Button(ctrl, text="ARM", command=self._on_arm, bg=GREEN, fg="white",
                                 font=("Helvetica", 12, "bold"), width=14, relief="flat")
        self.btn_arm.pack(side="left")
        self.btn_panic = tk.Button(ctrl, text="EMERGENCY STOP", command=self._on_panic, bg=RED,
                                   fg="white", font=("Helvetica", 12, "bold"), width=20, relief="flat")
        self.btn_panic.pack(side="right")
        self.lbl_mode_banner = tk.Label(ctrl, text="", bg=BG, fg=AMBER, font=("Helvetica", 11, "bold"))
        self.lbl_mode_banner.pack(side="left", padx=16)

        # ---- log ----
        lf = self._panel(self.root, "5 · Log")
        self.txt = tk.Text(lf, height=10, bg="#0f1316", fg=FG, insertbackground=FG,
                           wrap="word", relief="flat")
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, command=self.txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=sb.set, state="disabled")
        lf.columnconfigure(0, weight=1)
        ttk.Button(lf, text="Save log…", command=self._on_save_log).grid(row=1, column=0, sticky="w", pady=(6, 0))

        self._update_mode_banner()
        self._on_backend_change()

    def _row(self, parent, label, var, r, width=20):
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var, width=width).grid(row=r, column=1, sticky="w")

    def _metric(self, parent, label, value, col):
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=0, column=col, padx=6, sticky="w")
        lbl = ttk.Label(parent, text=value, style="Panel.TLabel", font=("Helvetica", 13, "bold"))
        lbl.grid(row=1, column=col, padx=6, sticky="w")
        return lbl

    # ---------------------------------------------------------- settings io
    def _load_into_widgets(self) -> None:
        key = load_api_key(self.settings.username) if self.settings.username else None
        if key:
            self.var_key.set(key)
            self.lbl_conn.configure(text="API key found — click Connect", foreground=AMBER)

    def _collect_settings(self) -> Optional[Settings]:
        s = self.settings
        try:
            s.backend = self.var_backend.get()
            s.browser_platform = self.var_platform.get()
            s.chrome_channel = "chrome" if self.var_use_chrome.get() else "chromium"
            s.test_mode = bool(self.var_test_mode.get())
            s.test_fire_time = self.var_test_time.get().strip()
            s.base_url = self.var_base.get().strip()
            s.username = self.var_user.get().strip()
            s.contract_symbol = self.var_symbol.get().strip().upper()
            s.entry_points = float(self.var_points.get())
            s.stop_loss_points = float(self.var_sl.get())
            s.take_profit_points = float(self.var_tp.get())
            s.contracts = int(self.var_contracts.get())
            s.trade_mode = self.var_mode.get()
            s.use_live_data = bool(self.var_live_data.get())
            s.dry_run = bool(self.var_dry.get())
        except ValueError as exc:
            messagebox.showerror("Invalid input", f"Check the strategy numbers: {exc}")
            return None
        return s

    def _on_save(self) -> None:
        s = self._collect_settings()
        if not s:
            return
        s.save()
        if self.engine:
            self.engine.settings = s
            self.engine.risk.settings = s
            try:
                self.engine.refresh_contract()
            except Exception as exc:
                self.log(f"contract refresh failed: {exc}", "warn")
        self._update_mode_banner()
        self.log(
            f"Saved: {s.contract_symbol} ±{s.entry_points}pt SL{s.stop_loss_points} "
            f"TP{s.take_profit_points} x{s.contracts} mode={s.trade_mode} dry_run={s.dry_run}"
        )

    def _on_dry_toggle(self) -> None:
        if not self.var_dry.get():
            ok = messagebox.askyesno(
                "Disable dry-run?",
                "This allows the bot to place REAL orders on your account.\n\nProceed?",
                icon="warning", default="no",
            )
            if not ok:
                self.var_dry.set(True)
        self._update_mode_banner()

    def _update_mode_banner(self) -> None:
        if not self.var_dry.get():
            self.lbl_mode_banner.configure(text="● LIVE — REAL ORDERS", fg=RED)
        else:
            self.lbl_mode_banner.configure(text="● DRY-RUN — no orders sent", fg=GREEN)

    # ------------------------------------------------------------- actions
    def _on_backend_change(self) -> None:
        browser = self.var_backend.get() == "browser"
        self.btn_connect.configure(text="Launch Browser" if browser else "Connect")
        if browser:
            self.lbl_backend_hint.configure(
                text="Browser mode: a real browser opens; you log in, then Arm. "
                     "Selectors for live sites need calibration (see README).")
        else:
            self.lbl_backend_hint.configure(
                text="API mode: official TopstepX API. Needs your API key (recommended).")

    def _on_connect(self) -> None:
        s = self._collect_settings()
        if not s:
            return
        s.save()
        if s.backend == "browser":
            self._launch_browser(s)
            return
        key = self.var_key.get().strip()
        if not key:
            messagebox.showerror("Missing API key", "Enter your TopstepX API key.")
            return
        if self.var_remember.get():
            backend = store_api_key(s.username, key)
            self.log(f"API key stored via {backend}.")
        self.lbl_conn.configure(text="connecting…", foreground=AMBER)
        self.btn_connect.configure(state="disabled")
        threading.Thread(target=self._connect_worker, args=(s, key), daemon=True).start()

    def _connect_worker(self, s: Settings, key: str) -> None:
        try:
            from .engine import BotEngine

            engine = BotEngine(s, log=self.log)
            engine.connect(key)
            accounts = engine.list_accounts()
            self.events.put(("connected", engine, accounts))
        except Exception as exc:
            self.events.put(("connect_failed", str(exc)))

    def _launch_browser(self, s: Settings) -> None:
        if self.controller is not None:
            messagebox.showinfo("Already launched", "Browser session is already running.")
            return
        try:
            from .browser import BrowserController
        except Exception as exc:
            messagebox.showerror("Playwright missing",
                                 f"Browser mode needs Playwright:\n\n"
                                 f"pip install playwright\nplaywright install chromium\n\n{exc}")
            return
        self.controller = BrowserController(s, log=self.log)
        self.controller.launch()
        self.lbl_conn.configure(text=f"browser launching · {s.browser_platform}", foreground=AMBER)
        self.log(f"Browser backend launching for {s.browser_platform}. Log in, then Arm.")
        self._start_poller()

    def _on_account_pick(self, _evt=None) -> None:
        if not self.engine or not self.accounts:
            return
        idx = self.cmb_account.current()
        if idx < 0:
            return
        acct = self.accounts[idx]
        self.engine.account = acct
        self.engine.settings.account_id = acct.id
        self.engine.settings.account_name = acct.name
        self.engine.settings.save()
        self.log(f"Account set to {acct.name} (id={acct.id}).")

    def _on_resolve(self) -> None:
        if not self.engine:
            messagebox.showinfo("Connect first", "Connect before resolving a contract.")
            return
        self.engine.settings.contract_symbol = self.var_symbol.get().strip().upper()
        threading.Thread(target=self._resolve_worker, daemon=True).start()

    def _resolve_worker(self) -> None:
        try:
            c = self.engine.refresh_contract()
            self.events.put(("contract", f"{c.name} ({c.id})  tick={c.tick_size} ${c.tick_value}/tick"))
        except Exception as exc:
            self.events.put(("error", f"resolve failed: {exc}"))

    def _on_arm(self) -> None:
        if self.var_backend.get() == "browser":
            self._on_arm_browser()
            return
        if not self.engine:
            messagebox.showinfo("Connect first", "Connect before arming.")
            return
        if self.engine.armed:
            self.engine.disarm()
            self.btn_arm.configure(text="ARM", bg=GREEN)
            return
        s = self._collect_settings()
        if not s:
            return
        s.save()
        self.engine.settings = s
        self.engine.risk.settings = s
        if not s.dry_run:
            if not messagebox.askyesno(
                "Arm LIVE?",
                f"Arm in LIVE mode?\n\n{s.contract_symbol}  ±{s.entry_points}pt  "
                f"x{s.contracts}  mode={s.trade_mode}\n\nReal orders will be sent at the open.",
                icon="warning", default="no",
            ):
                return
        try:
            self.engine.arm(on_tick=None)
            self.btn_arm.configure(text="DISARM", bg=AMBER)
        except Exception as exc:
            messagebox.showerror("Arm refused", str(exc))
            self.log(f"Arm refused: {exc}", "error")

    def _on_arm_browser(self) -> None:
        if self.controller is None:
            messagebox.showinfo("Launch first", "Click Launch Browser and log in before arming.")
            return
        if self.controller.state in ("armed", "monitoring"):
            self.controller.disarm()
            self.btn_arm.configure(text="ARM", bg=GREEN)
            return
        s = self._collect_settings()
        if not s:
            return
        s.save()
        self.controller.settings = s
        self.controller.engine.settings = s
        if not s.dry_run:
            if not messagebox.askyesno(
                "Arm LIVE (browser)?",
                f"Arm in LIVE mode on {s.browser_platform}?\n\n{s.contract_symbol}  "
                f"±{s.entry_points}pt  x{s.contracts}  mode={s.trade_mode}\n\n"
                f"Real orders will be placed in the browser at the open.",
                icon="warning", default="no",
            ):
                return
        self.controller.arm()
        self.btn_arm.configure(text="DISARM", bg=AMBER)

    def _on_panic(self) -> None:
        if self.var_backend.get() == "browser":
            if self.controller is None:
                return
            if not messagebox.askyesno("Emergency stop",
                                       "Cancel ALL orders and flatten ALL positions now?",
                                       icon="warning"):
                return
            self.btn_arm.configure(text="ARM", bg=GREEN)
            self.controller.panic()
            return
        if not self.engine:
            return
        if not messagebox.askyesno("Emergency stop",
                                   "Cancel ALL orders and flatten ALL positions now?",
                                   icon="warning"):
            return
        self.btn_arm.configure(text="ARM", bg=GREEN)
        threading.Thread(target=self.engine.emergency_stop, daemon=True).start()

    def _on_test_toggle(self) -> None:
        if self.var_test_mode.get():
            self.log("Test mode ON — fire time = the custom time. Save, then Arm (or 'Fire TEST now').", "warn")
        else:
            self.log("Test mode OFF — back to the 9:30 open.")
        self._update_mode_banner()

    def _on_fire_now(self) -> None:
        if not messagebox.askyesno(
            "Fire test now?",
            "Run the fire sequence RIGHT NOW (places the straddle)?\n\n"
            "Use a SIM/practice account. If DRY-RUN is on it only logs the plan; "
            "otherwise it places real orders. Cancel/flatten with Emergency Stop after.",
            icon="warning", default="no",
        ):
            return
        s = self._collect_settings()
        if not s:
            return
        s.save()
        if self.var_backend.get() == "browser":
            if self.controller is None:
                messagebox.showinfo("Launch first", "Launch the browser and log in before firing.")
                return
            self.controller.settings = s
            self.controller.engine.settings = s
            self.controller.fire_now()
        else:
            if self.engine is None:
                messagebox.showinfo("Connect first", "Connect before firing.")
                return
            self.engine.settings = s
            self.engine.risk.settings = s
            self.engine.fire_now()

    def _on_save_log(self) -> None:
        dest = filedialog.asksaveasfilename(
            defaultextension=".log",
            initialfile=f"imbabot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log",
        )
        if dest:
            self.log.save_copy(Path(dest))
            self.log(f"Log saved to {dest}")

    # --------------------------------------------------------- event loop
    def _enqueue_log(self, line: str, level: str) -> None:
        self.events.put(("log", line, level))

    def _drain_events(self) -> None:
        try:
            while True:
                evt = self.events.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        self.root.after(150, self._drain_events)

    def _handle_event(self, evt: tuple) -> None:
        kind = evt[0]
        if kind == "log":
            self._append_log(evt[1])
        elif kind == "connected":
            self.engine, self.accounts = evt[1], evt[2]
            self.btn_connect.configure(state="normal")
            self.lbl_conn.configure(text=f"connected · {self.engine.account.name}", foreground=GREEN)
            names = [f"{a.name} (id={a.id}){'' if a.can_trade else ' [locked]'}" for a in self.accounts]
            self.cmb_account.configure(values=names)
            for i, a in enumerate(self.accounts):
                if self.engine.account and a.id == self.engine.account.id:
                    self.cmb_account.current(i)
            if self.engine.contract:
                self.lbl_contract.configure(
                    text=f"{self.engine.contract.name} ({self.engine.contract.id})  "
                         f"tick={self.engine.contract.tick_size} ${self.engine.contract.tick_value}/tick")
            self._start_poller()
        elif kind == "connect_failed":
            self.btn_connect.configure(state="normal")
            self.lbl_conn.configure(text=f"connect failed: {evt[1]}", foreground=RED)
            messagebox.showerror("Connection failed", evt[1])
        elif kind == "contract":
            self.lbl_contract.configure(text=evt[1])
        elif kind == "dashboard":
            price, rng = evt[1], evt[2]
            self.lbl_price.configure(text=f"{price:g}" if price is not None else "—")
            self.lbl_range.configure(
                text=f"{rng['low']:g} – {rng['high']:g}" if rng else "—")
        elif kind == "error":
            self.log(evt[1], "error")

    def _append_log(self, line: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _tick_countdown(self) -> None:
        try:
            from .scheduler import seconds_until, format_countdown, next_fire_time, next_local_fire

            s = self.settings
            d_fire = None
            if getattr(self, "var_test_mode", None) and self.var_test_mode.get():
                tt = self.var_test_time.get().strip()
                if tt:
                    try:
                        d_fire = next_local_fire(tt)
                    except Exception:
                        d_fire = None  # malformed time -> fall back to the market open
            if d_fire is None:
                d_fire = next_fire_time(s.open_time(), s.capture_offset_seconds, s.market_tz)
            self.lbl_fire.configure(text=d_fire.strftime("%H:%M:%S %Z"))
            self.lbl_count.configure(text=format_countdown(seconds_until(d_fire)))
        except Exception:
            pass
        self.root.after(1000, self._tick_countdown)

    def _start_poller(self) -> None:
        self._poll_stop.clear()
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def _poll_worker(self) -> None:
        while not self._poll_stop.is_set() and (self.engine or self.controller):
            try:
                if self.settings.backend == "browser" and self.controller:
                    # The controller reads price on its own (Playwright) thread; we
                    # only read the cached value here — never touch Playwright cross-thread.
                    self.events.put(("dashboard", self.controller.last_price, None))
                elif self.engine:
                    self.events.put(("dashboard", self.engine.last_price(), self.engine.overnight_range()))
            except Exception:
                pass
            self._poll_stop.wait(5.0)

    def on_close(self) -> None:
        self._poll_stop.set()
        if self.engine and self.engine.armed:
            self.engine.disarm()
        if self.controller is not None:
            try:
                self.controller.shutdown()
            except Exception:
                pass
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    app = ImbabotGUI(root)
    if not root.winfo_exists():
        return 0
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
