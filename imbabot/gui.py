"""Modern dashboard — packaged into the .exe/.app.

A professional dark UI built on ttk's ``clam`` theme with a hand-tuned palette
(works on Tk 8.5 and 8.6, and overrides macOS aqua so colors render everywhere):
a header with live status pills, a hero dashboard (countdown / price / range),
Arm + Emergency-Stop controls, tabbed settings, and a themed activity log.

Threading model (unchanged): only the main thread touches widgets. Background work
runs on worker threads that push events onto a queue drained via ``root.after``.
"""
from __future__ import annotations

import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

try:
    import tkinter as tk
    from tkinter import messagebox, filedialog, ttk
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Tkinter is required. On macOS use python.org Python or `brew install python-tk`. "
        "Error: %s" % exc
    )

from . import __version__
from .config import Settings, load_api_key, store_api_key, log_path
from .logbus import Logger
from .models import Account

# ---- palette (arc-reactor / Jarvis HUD: cyan glow on near-black) ----
BG = "#03070d"
SURFACE = "#06121b"
CARD = "#081a26"
ELEV = "#0c2735"
BORDER = "#0f3c4d"
FG = "#bfeffb"
MUTED = "#3f8197"
ACCENT = "#00e5ff"
ACCENT_H = "#62f1ff"
GREEN = "#00e6a0"
GREEN_H = "#43ffc4"
RED = "#ff3b54"
RED_H = "#ff6f82"
AMBER = "#ffb02e"
AMBER_H = "#ffc861"
# extra HUD tones
GLOW = "#0a6f86"     # dim cyan for glow underlays
GRID = "#0a2330"     # faint grid / backdrop lines

FONT = "Segoe UI" if sys.platform.startswith("win") else ("Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")
MONO = "Consolas" if sys.platform.startswith("win") else ("Menlo" if sys.platform == "darwin" else "DejaVu Sans Mono")

DISCLAIMER = (
    "Imbabot places real futures orders through your TopstepX / ProjectX account.\n\n"
    "• This is NOT trading or financial advice. You are solely responsible for every "
    "order it places and any losses.\n"
    "• Automated trading is allowed in Topstep Combine/Funded (evaluation) accounts but "
    "PROHIBITED in the Live Funded account, and must run locally (no VPS/cloud). Confirm "
    "your firm's current rules before trading live.\n"
    "• The bot starts in DRY-RUN mode (no orders sent). You must deliberately disable it "
    "to trade.\n\n"
    "Click OK to accept, or Cancel to exit."
)


class _HudField:
    """Adapter so existing ``lbl.configure(text=…)`` calls drive a HUD canvas
    readout instead of a ttk Label — keeps the rest of the GUI code unchanged."""

    def __init__(self, hud: "HudHero", key: str) -> None:
        self._hud = hud
        self._key = key

    def configure(self, text: Optional[str] = None, **_kw) -> None:
        if text is not None:
            self._hud.set_text(self._key, text)

    config = configure

    def cget(self, opt: str):
        return self._hud.values.get(self._key, "") if opt == "text" else ""


class HudHero(tk.Canvas):
    """Animated arc-reactor HUD: a rotating countdown gauge in the centre with
    bracketed readout cells (next fire / last price / overnight range) to the
    right. Pure Tk Canvas — no images, no external assets."""

    def __init__(self, parent, height: int = 280) -> None:
        super().__init__(parent, height=height, background=BG,
                         highlightthickness=0, bd=0)
        self.values = {"count": "00:00:00", "fire": "—", "price": "—", "range": "—"}
        self._items: dict = {}
        self._angle = 0.0
        self._h = height
        self.bind("<Configure>", lambda _e: self._draw_static())
        self.after(16, self._draw_static)

    # ---- public API ----
    def field(self, key: str) -> _HudField:
        return _HudField(self, key)

    def set_text(self, key: str, text: str) -> None:
        self.values[key] = text
        item = self._items.get(key)
        if item is not None:
            try:
                self.itemconfigure(item, text=text)
            except tk.TclError:
                pass

    def animate(self, angle: float) -> None:
        self._angle = angle
        self._draw_rot(angle)

    # ---- geometry ----
    def _geo(self):
        w = self.winfo_width()
        if w <= 1:
            w = 960
        h = self._h
        cx = 168
        cy = h // 2
        R = min(116, cy - 16)
        return w, h, cx, cy, R

    # ---- drawing ----
    def _brackets(self, x0, y0, x1, y1, L, col, tag, width=1):
        c = self.create_line
        for (ax, ay, bx, by) in (
            (x0, y0, x0 + L, y0), (x0, y0, x0, y0 + L),
            (x1, y0, x1 - L, y0), (x1, y0, x1, y0 + L),
            (x0, y1, x0 + L, y1), (x0, y1, x0, y1 - L),
            (x1, y1, x1 - L, y1), (x1, y1, x1, y1 - L),
        ):
            c(ax, ay, bx, by, fill=col, tags=tag, width=width)

    def _draw_static(self) -> None:
        import math
        self.delete("static")
        w, h, cx, cy, R = self._geo()

        # outer frame corner brackets + faint vertical grid backdrop
        self._brackets(5, 5, w - 5, h - 5, 24, BORDER, "static")
        for gx in range(60, w - 30, 90):
            self.create_line(gx, 10, gx, h - 10, fill=GRID, tags="static")

        # gauge concentric rings
        for rr, col, wd in ((R, GLOW, 6), (R, ACCENT, 2), (R - 12, BORDER, 1),
                            (R - 50, BORDER, 1)):
            self.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=col,
                             width=wd, tags="static")
        # tick ring
        for i in range(60):
            a = math.radians(i * 6)
            r1 = R - 4
            r2 = R - (14 if i % 5 == 0 else 8)
            self.create_line(cx + r1 * math.cos(a), cy + r1 * math.sin(a),
                             cx + r2 * math.cos(a), cy + r2 * math.sin(a),
                             fill=(ACCENT if i % 5 == 0 else BORDER), tags="static")

        # centre readout (countdown)
        self.create_text(cx, cy - 48, text="T — MINUS", fill=MUTED,
                         font=(MONO, 9, "bold"), tags="static")
        self._items["count"] = self.create_text(
            cx, cy - 6, text=self.values["count"], fill=ACCENT_H,
            font=(MONO, 31, "bold"), tags="static")
        self.create_text(cx, cy + 32, text="TO MARKET OPEN", fill=MUTED,
                         font=(MONO, 8), tags="static")

        # right-hand readout cells
        x0 = cx + R + 46
        x1 = w - 18
        pad = 10
        gap = 8
        cellh = (h - 2 * pad - 2 * gap) / 3
        for idx, (key, lab) in enumerate(
                (("fire", "NEXT FIRE"), ("price", "LAST PRICE"),
                 ("range", "OVERNIGHT RANGE"))):
            ty = pad + idx * (cellh + gap)
            self._brackets(x0, ty, x1, ty + cellh, 16, BORDER, "static")
            self.create_line(x0, ty + 1, x0 + 4, ty + 1, fill=ACCENT, width=3, tags="static")
            self.create_text(x0 + 18, ty + 18, text=lab, anchor="w", fill=MUTED,
                             font=(MONO, 9, "bold"), tags="static")
            self._items[key] = self.create_text(
                x0 + 18, ty + cellh - 17, text=self.values[key], anchor="w",
                fill=FG, font=(MONO, 18, "bold"), tags="static")

        self._draw_rot(self._angle)

    def _draw_rot(self, angle: float) -> None:
        import math
        self.delete("rot")
        w, h, cx, cy, R = self._geo()

        # outer rotating dashed arc (glow underlay + bright)
        rr = R + 9
        for k in range(6):
            start = angle + k * 60
            self.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=start,
                            extent=34, style="arc", outline=GLOW, width=6, tags="rot")
            self.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=start,
                            extent=34, style="arc", outline=ACCENT, width=2, tags="rot")
        # counter-rotating inner arcs
        rr2 = R - 26
        for k in range(3):
            start = -angle * 1.6 + k * 120
            self.create_arc(cx - rr2, cy - rr2, cx + rr2, cy + rr2, start=start,
                            extent=50, style="arc", outline=GREEN_H, width=2, tags="rot")
        # sweeping reticle
        a = math.radians(angle * 2)
        rs = R - 30
        self.create_line(cx, cy, cx + rs * math.cos(a), cy + rs * math.sin(a),
                         fill=ACCENT, width=1, tags="rot")
        # orbiting node on the outer ring
        ao = math.radians(-angle * 2.4)
        ro = R + 9
        nx, ny = cx + ro * math.cos(ao), cy + ro * math.sin(ao)
        self.create_oval(nx - 3, ny - 3, nx + 3, ny + 3, fill=ACCENT_H,
                         outline="", tags="rot")


class ImbabotGUI:
    def __init__(self, root: tk.Misc, shot_path: Optional[str] = None) -> None:
        self.root = root
        self.settings = Settings.load()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.log = Logger(sink=self._enqueue_log)
        self.engine = None       # API backend, created on connect
        self.controller = None   # browser backend controller, created on launch
        self.accounts: List[Account] = []
        self._poll_stop = threading.Event()
        self._tick_stop = threading.Event()

        root.title(f"Imbabot {__version__}")
        root.configure(bg=BG)
        root.minsize(920, 740)
        root.update_idletasks()
        w, h = 1000, 800
        x = max(0, (root.winfo_screenwidth() - w) // 2)
        y = max(0, (root.winfo_screenheight() - h) // 3)
        root.geometry(f"{w}x{h}+{x}+{y}")

        if shot_path is None and not self._show_disclaimer():
            root.destroy()
            return

        self._build_styles()
        self._build_widgets()
        self._load_into_widgets()
        self.root.after(150, self._drain_events)
        self.root.after(1000, self._tick_countdown)
        self.log(f"Imbabot {__version__} ready. Config: {log_path().parent}")
        if shot_path is None:
            self._start_ticker()

        if shot_path:  # self-portrait mode (skips disclaimer, captures, quits)
            self._demo_fill()
            self.root.after(1600, lambda: self._take_shot(shot_path))

    def _demo_fill(self) -> None:
        self.lbl_price.configure(text="30181.25")
        self.lbl_range.configure(text="29478–30201")
        self.lbl_count.configure(text="17:42:09")
        self.lbl_fire.configure(text="09:29:57")
        for line, lvl in [("Imbabot ready. Backend: API (TopstepX).", "info"),
                          ("Connected · $150K PRACTICE | PRAC-V2", "info"),
                          ("ARMED. Fire at 09:29:57. Mode=semi_auto dry_run=True", "warn")]:
            self.log(line, lvl)

    def _take_shot(self, path: str) -> None:
        import subprocess
        try:
            self.root.update_idletasks()
            self.root.update()
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            subprocess.run(["screencapture", "-x", f"-R{x},{y},{w},{h}", path], timeout=10)
        except Exception:
            pass
        self.root.destroy()

    # ---------------------------------------------------------- disclaimer
    def _show_disclaimer(self) -> bool:
        return messagebox.askokcancel("Risk disclaimer", DISCLAIMER, icon="warning", default="cancel")

    # ------------------------------------------------------------- styling
    def _build_styles(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        self.root.option_add("*TCombobox*Listbox.background", SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        st.configure(".", background=BG, foreground=FG, fieldbackground=SURFACE,
                     bordercolor=BORDER, focuscolor=ACCENT, font=(FONT, 11))
        for name, bg in (("TFrame", BG), ("Surface.TFrame", SURFACE), ("Card.TFrame", CARD),
                         ("Header.TFrame", BG)):
            st.configure(name, background=bg)
        st.configure("TLabel", background=BG, foreground=FG, font=(FONT, 11))
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(FONT, 10))
        st.configure("Brand.TLabel", background=BG, foreground=ACCENT, font=(FONT, 21, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 10))
        st.configure("H.TLabel", background=BG, foreground=FG, font=(FONT, 11, "bold"))
        st.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9, "bold"))
        st.configure("CardVal.TLabel", background=CARD, foreground=FG, font=(FONT, 18, "bold"))
        st.configure("CardBig.TLabel", background=CARD, foreground=ACCENT, font=(FONT, 34, "bold"))
        st.configure("Banner.TLabel", background=BG, foreground=MUTED, font=(FONT, 11, "bold"))
        # header live ticker (NQ)
        st.configure("TickSym.TLabel", background=ELEV, foreground=FG, font=(FONT, 10, "bold"), padding=(9, 4))
        st.configure("TickPrice.TLabel", background=BG, foreground=FG, font=(MONO, 15, "bold"))
        st.configure("TickUp.TLabel", background=BG, foreground=GREEN_H, font=(FONT, 10, "bold"))
        st.configure("TickDown.TLabel", background=BG, foreground=RED_H, font=(FONT, 10, "bold"))
        st.configure("TickFlat.TLabel", background=BG, foreground=MUTED, font=(FONT, 10, "bold"))
        # pills
        for nm, bg, fg in (("Pill.Sec.TLabel", ELEV, MUTED), ("Pill.Ok.TLabel", GREEN, "#ffffff"),
                           ("Pill.Bad.TLabel", RED, "#ffffff"), ("Pill.Warn.TLabel", AMBER, "#0d1117")):
            st.configure(nm, background=bg, foreground=fg, font=(FONT, 9, "bold"), padding=(11, 5))
        # inputs
        st.configure("TEntry", fieldbackground="#06141d", foreground=FG, insertcolor=FG,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=6)
        st.map("TEntry", bordercolor=[("focus", ACCENT)])
        st.configure("TCombobox", fieldbackground="#06141d", background=SURFACE, foreground=FG,
                     arrowcolor=MUTED, bordercolor=BORDER, padding=5)
        st.map("TCombobox", fieldbackground=[("readonly", "#06141d")], foreground=[("readonly", FG)])
        st.configure("TCheckbutton", background=BG, foreground=FG, font=(FONT, 10), focuscolor=BG)
        st.map("TCheckbutton", background=[("active", BG)], indicatorcolor=[("selected", ACCENT)])
        st.configure("TRadiobutton", background=BG, foreground=FG, font=(FONT, 10), focuscolor=BG)
        st.map("TRadiobutton", background=[("active", BG)], indicatorcolor=[("selected", ACCENT)])
        st.configure("Card.TCheckbutton", background=BG)
        st.configure("TSeparator", background=BORDER)
        st.configure("Vertical.TScrollbar", background=ELEV, troughcolor=BG, bordercolor=BG,
                     arrowcolor=MUTED)
        # notebook
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(2, 6, 2, 0))
        st.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(18, 9),
                     font=(FONT, 10, "bold"), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", SURFACE)], foreground=[("selected", FG)],
               expand=[("selected", (0, 0, 0, 0))])
        # buttons
        self._btn_style(st, "Accent.TButton", ACCENT, ACCENT_H)
        self._btn_style(st, "Success.TButton", GREEN, GREEN_H)
        self._btn_style(st, "Danger.TButton", RED, RED_H)
        self._btn_style(st, "Warning.TButton", AMBER, AMBER_H, fg="#0d1117")
        self._btn_style(st, "Ghost.TButton", ELEV, BORDER, fg=FG, small=True)
        self.st = st

    def _btn_style(self, st, name, bg, hover, fg="#ffffff", small=False):
        st.configure(name, background=bg, foreground=fg, font=(FONT, 10 if small else 11, "bold"),
                     borderwidth=0, relief="flat", focuscolor=bg,
                     padding=(12, 7) if small else (20, 11))
        st.map(name, background=[("active", hover), ("pressed", hover), ("disabled", ELEV)],
               foreground=[("disabled", MUTED)])

    # ----------------------------------------------------------- helpers
    def _pill(self, parent, text, kind="Sec"):
        lbl = ttk.Label(parent, text=text, style=f"Pill.{kind}.TLabel")
        return lbl

    def _set_pill(self, lbl, text, kind):
        lbl.configure(text=text, style=f"Pill.{kind}.TLabel")

    def _field(self, parent, label, var, r, c=0, width=18, show=None):
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=r, column=c, sticky="w", padx=(0, 8), pady=5)
        ent = ttk.Entry(parent, textvariable=var, width=width, font=(FONT, 11))
        if show:
            ent.configure(show=show)
        ent.grid(row=r, column=c + 1, sticky="w", pady=5)
        return ent

    def _stat(self, parent, title, col, big=False):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 13))
        card.grid(row=0, column=col, padx=6, sticky="nsew")
        parent.columnconfigure(col, weight=1)
        ttk.Label(card, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w")
        val = ttk.Label(card, text="—", style="CardBig.TLabel" if big else "CardVal.TLabel")
        val.pack(anchor="w", pady=(6, 0))
        return val

    # ------------------------------------------------------------- widgets
    def _build_widgets(self) -> None:
        root = self.root

        # ===== header =====
        header = ttk.Frame(root, style="Header.TFrame", padding=(20, 16))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side="left")
        ttk.Label(left, text="◆ IMBABOT", style="Brand.TLabel").pack(side="left")
        ttk.Label(left, text=f"   v{__version__}  ·  TopstepX opening-range bot",
                  style="Sub.TLabel").pack(side="left", pady=(8, 0))
        # live NQ ticker — fed by a public quote feed, independent of the API login
        tick = ttk.Frame(header, style="Header.TFrame")
        tick.pack(side="left", padx=(32, 0))
        self.lbl_tick_sym = ttk.Label(tick, text="NQ", style="TickSym.TLabel")
        self.lbl_tick_sym.pack(side="left")
        self.lbl_tick_price = ttk.Label(tick, text="—", style="TickPrice.TLabel")
        self.lbl_tick_price.pack(side="left", padx=(9, 7))
        self.lbl_tick_chg = ttk.Label(tick, text="connecting…", style="TickFlat.TLabel")
        self.lbl_tick_chg.pack(side="left", pady=(3, 0))
        # live VIX chip — the guide's daily routine reviews the VIX
        vix = ttk.Frame(header, style="Header.TFrame")
        vix.pack(side="left", padx=(22, 0))
        self.lbl_vix_sym = ttk.Label(vix, text="VIX", style="TickSym.TLabel")
        self.lbl_vix_sym.pack(side="left")
        self.lbl_vix_price = ttk.Label(vix, text="—", style="TickPrice.TLabel")
        self.lbl_vix_price.pack(side="left", padx=(9, 7))
        self.lbl_vix_chg = ttk.Label(vix, text="", style="TickFlat.TLabel")
        self.lbl_vix_chg.pack(side="left", pady=(3, 0))
        badges = ttk.Frame(header, style="Header.TFrame")
        badges.pack(side="right")
        self.badge_conn = self._pill(badges, "Offline", "Sec")
        self.badge_conn.pack(side="left", padx=4)
        self.badge_mode = self._pill(badges, "● DRY-RUN", "Ok")
        self.badge_mode.pack(side="left", padx=4)
        self.badge_armed = self._pill(badges, "DISARMED", "Sec")
        self.badge_armed.pack(side="left", padx=4)
        ttk.Separator(root).pack(fill="x", padx=20)

        # ===== hero HUD (animated arc-reactor gauge) =====
        self.hud = HudHero(root, height=280)
        self.hud.pack(fill="x", padx=20, pady=(12, 4))
        self.lbl_count = self.hud.field("count")
        self.lbl_fire = self.hud.field("fire")
        self.lbl_price = self.hud.field("price")
        self.lbl_range = self.hud.field("range")
        self._hud_angle = 0.0
        self.root.after(60, self._hud_animate)

        # ===== control bar =====
        ctrl = ttk.Frame(root, style="TFrame", padding=(20, 2))
        ctrl.pack(fill="x")
        self.btn_arm = ttk.Button(ctrl, text="ARM", command=self._on_arm, style="Success.TButton", width=11)
        self.btn_arm.pack(side="left")
        self.lbl_mode_banner = ttk.Label(ctrl, text="", style="Banner.TLabel")
        self.lbl_mode_banner.pack(side="left", padx=18)
        self.btn_panic = ttk.Button(ctrl, text="■  EMERGENCY STOP", command=self._on_panic, style="Danger.TButton")
        self.btn_panic.pack(side="right")
        self.btn_flatten = ttk.Button(ctrl, text="▽  FLATTEN", command=self._on_flatten, style="Warning.TButton")
        self.btn_flatten.pack(side="right", padx=(0, 10))

        # ===== settings notebook =====
        nb = ttk.Notebook(root)
        nb.pack(fill="x", padx=20, pady=(16, 8))
        self._build_tab_connect(nb)
        self._build_tab_strategy(nb)
        self._build_tab_test(nb)

        # ===== log =====
        logwrap = ttk.Frame(root, style="Surface.TFrame", padding=10)
        logwrap.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        topbar = ttk.Frame(logwrap, style="Surface.TFrame")
        topbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(topbar, text="ACTIVITY LOG", background=SURFACE, foreground=MUTED,
                  font=(FONT, 9, "bold")).pack(side="left")
        ttk.Button(topbar, text="Save log…", command=self._on_save_log, style="Ghost.TButton").pack(side="right")
        self.txt = tk.Text(logwrap, height=9, wrap="word", relief="flat", borderwidth=0,
                           background="#06141d", foreground=FG, insertbackground=FG,
                           font=(MONO, 10), padx=12, pady=10, highlightthickness=0)
        self.txt.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logwrap, command=self.txt.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=sb.set, state="disabled")
        logwrap.columnconfigure(0, weight=1)
        logwrap.rowconfigure(1, weight=1)

        self._update_mode_banner()
        self._on_backend_change()

    def _build_tab_connect(self, nb) -> None:
        tab = ttk.Frame(nb, style="Surface.TFrame", padding=18)
        nb.add(tab, text="Connect")
        self._surface(tab)  # register surface-bg style variants used inside tabs

        self.var_backend = tk.StringVar(value=self.settings.backend)
        ttk.Label(tab, text="Backend", style="Hs.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(tab, text="API (TopstepX — recommended)", variable=self.var_backend,
                        value="api", command=self._on_backend_change, style="S.TRadiobutton").grid(
            row=0, column=1, sticky="w", padx=10)
        ttk.Radiobutton(tab, text="Browser automation", variable=self.var_backend,
                        value="browser", command=self._on_backend_change, style="S.TRadiobutton").grid(
            row=0, column=2, sticky="w", padx=10)

        row2 = ttk.Frame(tab, style="Surface.TFrame")
        row2.grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(row2, text="Platform", style="Sm.TLabel").pack(side="left")
        self.var_platform = tk.StringVar(value=self.settings.browser_platform)
        self.cmb_platform = ttk.Combobox(row2, state="readonly", width=12, values=["projectx", "tradesea"],
                                         textvariable=self.var_platform, font=(FONT, 11))
        self.cmb_platform.pack(side="left", padx=(8, 18))
        self.var_use_chrome = tk.BooleanVar(value=self.settings.chrome_channel == "chrome")
        ttk.Checkbutton(row2, text="Use my installed Google Chrome", variable=self.var_use_chrome,
                        style="S.TCheckbutton").pack(side="left")

        self.lbl_backend_hint = ttk.Label(tab, text="", style="Hint.TLabel", wraplength=840)
        self.lbl_backend_hint.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 12))
        ttk.Separator(tab).grid(row=3, column=0, columnspan=4, sticky="ew", pady=4)

        self.var_base = tk.StringVar(value=self.settings.base_url)
        self.var_user = tk.StringVar(value=self.settings.username)
        self.var_key = tk.StringVar(value="")
        self.var_remember = tk.BooleanVar(value=True)
        self._sfield(tab, "Base URL", self.var_base, 4, width=34)
        self._sfield(tab, "Username", self.var_user, 5, width=26)
        self._sfield(tab, "API key", self.var_key, 6, width=34, show="•")
        ttk.Checkbutton(tab, text="Remember on this device", variable=self.var_remember,
                        style="S.TCheckbutton").grid(row=7, column=1, sticky="w", pady=(2, 10))
        self.btn_connect = ttk.Button(tab, text="Connect", command=self._on_connect, style="Accent.TButton")
        self.btn_connect.grid(row=8, column=1, sticky="w")
        self.lbl_conn = ttk.Label(tab, text="not connected", style="Warn.TLabel")
        self.lbl_conn.grid(row=8, column=2, columnspan=2, sticky="w", padx=10)

    def _build_tab_strategy(self, nb) -> None:
        tab = ttk.Frame(nb, style="Surface.TFrame", padding=18)
        nb.add(tab, text="Strategy")

        ttk.Label(tab, text="Account", style="Sm.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        self.cmb_account = ttk.Combobox(tab, state="readonly", width=30, values=[], font=(FONT, 11))
        self.cmb_account.grid(row=0, column=1, sticky="w", padx=8)
        self.cmb_account.bind("<<ComboboxSelected>>", self._on_account_pick)
        ttk.Label(tab, text="Symbol", style="Sm.TLabel").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.var_symbol = tk.StringVar(value=self.settings.contract_symbol)
        ttk.Entry(tab, textvariable=self.var_symbol, width=10, font=(FONT, 11)).grid(row=0, column=3, sticky="w", padx=8)
        ttk.Button(tab, text="Resolve", command=self._on_resolve, style="Ghost.TButton").grid(row=0, column=4, padx=4)
        self.lbl_contract = ttk.Label(tab, text="—", style="Sm.TLabel")
        self.lbl_contract.grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 10))
        ttk.Separator(tab).grid(row=2, column=0, columnspan=5, sticky="ew", pady=4)

        self.var_points = tk.StringVar(value=str(self.settings.entry_points))
        self.var_sl = tk.StringVar(value=str(self.settings.stop_loss_points))
        self.var_tp = tk.StringVar(value=str(self.settings.take_profit_points))
        self.var_contracts = tk.StringVar(value=str(self.settings.contracts))
        self._sfield(tab, "Entry points (±)", self.var_points, 3, width=8)
        self._sfield(tab, "Stop-loss points", self.var_sl, 4, width=8)
        self._sfield(tab, "Take-profit points (0 = none)", self.var_tp, 5, width=8)
        self._sfield(tab, "Contracts", self.var_contracts, 6, width=8)

        self.var_mode = tk.StringVar(value=self.settings.trade_mode)
        ttk.Label(tab, text="Mode", style="Hs.TLabel").grid(row=3, column=2, sticky="w", padx=(28, 0))
        ttk.Radiobutton(tab, text="Semi-Auto (you manage)", variable=self.var_mode, value="semi_auto",
                        style="S.TRadiobutton").grid(row=4, column=2, columnspan=2, sticky="w", padx=(28, 0))
        ttk.Radiobutton(tab, text="One-Trade (auto OCO)", variable=self.var_mode, value="one_trade",
                        style="S.TRadiobutton").grid(row=5, column=2, columnspan=2, sticky="w", padx=(28, 0))
        ttk.Radiobutton(tab, text="Two-Trade (leave both in)", variable=self.var_mode, value="two_trade",
                        style="S.TRadiobutton").grid(row=6, column=2, columnspan=2, sticky="w", padx=(28, 0))
        self.var_live_data = tk.BooleanVar(value=self.settings.use_live_data)
        self.var_dry = tk.BooleanVar(value=self.settings.dry_run)
        ttk.Checkbutton(tab, text="Use live data feed", variable=self.var_live_data,
                        style="S.TCheckbutton").grid(row=7, column=2, columnspan=2, sticky="w", padx=(28, 0))
        ttk.Checkbutton(tab, text="DRY-RUN (no real orders)", variable=self.var_dry, command=self._on_dry_toggle,
                        style="S.TCheckbutton").grid(row=8, column=2, columnspan=2, sticky="w", padx=(28, 0))
        ttk.Button(tab, text="Save settings", command=self._on_save, style="Accent.TButton").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _build_tab_test(self, nb) -> None:
        tab = ttk.Frame(nb, style="Surface.TFrame", padding=18)
        nb.add(tab, text="Test")
        ttk.Label(tab, text="Verify it actually places orders", style="Hs.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w")
        self.var_test_mode = tk.BooleanVar(value=self.settings.test_mode)
        ttk.Checkbutton(tab, text="Test mode: fire at a custom time (instead of 9:30)",
                        variable=self.var_test_mode, command=self._on_test_toggle,
                        style="S.TCheckbutton").grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 6))
        ttk.Label(tab, text="Fire at (HH:MM:SS, your local time)", style="Sm.TLabel").grid(
            row=2, column=0, sticky="w", pady=5)
        self.var_test_time = tk.StringVar(value=self.settings.test_fire_time)
        ttk.Entry(tab, textvariable=self.var_test_time, width=12, font=(FONT, 11)).grid(row=2, column=1, sticky="w")
        ttk.Button(tab, text="⚡  Fire TEST now", command=self._on_fire_now, style="Warning.TButton").grid(
            row=2, column=2, padx=16)
        ttk.Label(tab, text="Use a SIM / practice account. With test mode on: Save → Arm and it fires at "
                            "that time; or click ‘Fire TEST now’. Honors dry-run.",
                  style="Hint.TLabel", wraplength=840).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _surface(self, tab):
        """Register surface-bg variants of styles used inside notebook tabs (once)."""
        st = self.st
        st.configure("Sm.TLabel", background=SURFACE, foreground=MUTED, font=(FONT, 10))
        st.configure("Hs.TLabel", background=SURFACE, foreground=FG, font=(FONT, 10, "bold"))
        st.configure("Hint.TLabel", background=SURFACE, foreground=MUTED, font=(FONT, 9))
        st.configure("Warn.TLabel", background=SURFACE, foreground=AMBER_H, font=(FONT, 10))
        st.configure("S.TCheckbutton", background=SURFACE, foreground=FG, font=(FONT, 10))
        st.map("S.TCheckbutton", background=[("active", SURFACE)], indicatorcolor=[("selected", ACCENT)])
        st.configure("S.TRadiobutton", background=SURFACE, foreground=FG, font=(FONT, 10))
        st.map("S.TRadiobutton", background=[("active", SURFACE)], indicatorcolor=[("selected", ACCENT)])

    def _sfield(self, parent, label, var, r, c=0, width=18, show=None):
        ttk.Label(parent, text=label, style="Sm.TLabel").grid(row=r, column=c, sticky="w", padx=(0, 8), pady=5)
        ent = ttk.Entry(parent, textvariable=var, width=width, font=(FONT, 11))
        if show:
            ent.configure(show=show)
        ent.grid(row=r, column=c + 1, sticky="w", pady=5)
        return ent

    # ---------------------------------------------------------- settings io
    def _load_into_widgets(self) -> None:
        key = load_api_key(self.settings.username) if self.settings.username else None
        if key:
            self.var_key.set(key)
            self.lbl_conn.configure(text="API key found — click Connect")

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
            self.lbl_mode_banner.configure(text="● LIVE — REAL ORDERS", foreground=RED_H)
        else:
            self.lbl_mode_banner.configure(text="● DRY-RUN — no orders sent", foreground=GREEN_H)

    # ------------------------------------------------------------- actions
    def _on_backend_change(self) -> None:
        browser = self.var_backend.get() == "browser"
        self.btn_connect.configure(text="Launch Browser" if browser else "Connect")
        if browser:
            self.lbl_backend_hint.configure(
                text="Browser mode: a real Chrome window opens; you log in, then Arm. TopstepX (projectx) "
                     "ships pre-calibrated — use Semi-Auto.")
        else:
            self.lbl_backend_hint.configure(
                text="API mode: official TopstepX API (recommended). Needs your API key.")

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
        self.lbl_conn.configure(text="connecting…")
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
            messagebox.showerror("Browser backend unavailable",
                                 f"Browser mode needs Selenium + Chrome.\n\n{exc}")
            return
        self.controller = BrowserController(s, log=self.log)
        self.controller.launch()
        self.lbl_conn.configure(text=f"browser launching · {s.browser_platform}")
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
            self.btn_arm.configure(text="ARM", style="Success.TButton")
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
            self.btn_arm.configure(text="DISARM", style="Warning.TButton")
        except Exception as exc:
            messagebox.showerror("Arm refused", str(exc))
            self.log(f"Arm refused: {exc}", "error")

    def _on_arm_browser(self) -> None:
        if self.controller is None:
            messagebox.showinfo("Launch first", "Click Launch Browser and log in before arming.")
            return
        if self.controller.state in ("armed", "monitoring"):
            self.controller.disarm()
            self.btn_arm.configure(text="ARM", style="Success.TButton")
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
        self.btn_arm.configure(text="DISARM", style="Warning.TButton")

    def _on_panic(self) -> None:
        if self.var_backend.get() == "browser":
            if self.controller is None:
                return
            if not messagebox.askyesno("Emergency stop",
                                       "Cancel ALL orders and flatten ALL positions now?",
                                       icon="warning"):
                return
            self.btn_arm.configure(text="ARM", style="Success.TButton")
            self.controller.panic()
            return
        if not self.engine:
            return
        if not messagebox.askyesno("Emergency stop",
                                   "Cancel ALL orders and flatten ALL positions now?",
                                   icon="warning"):
            return
        self.btn_arm.configure(text="ARM", style="Success.TButton")
        threading.Thread(target=self.engine.emergency_stop, daemon=True).start()

    def _on_flatten(self) -> None:
        if not messagebox.askyesno(
            "Flatten all positions",
            "Close ALL open positions with market orders now?\n\n"
            "Working orders are left alone (use Emergency Stop to also cancel those).",
            icon="warning", default="no",
        ):
            return
        if self.var_backend.get() == "browser":
            if self.controller is None:
                messagebox.showinfo("Launch first", "Launch the browser before flattening.")
                return
            self.controller.flatten()
            return
        if not self.engine:
            messagebox.showinfo("Connect first", "Connect before flattening.")
            return
        threading.Thread(target=self.engine.flatten_all, daemon=True).start()

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
            self._append_log(evt[1], evt[2] if len(evt) > 2 else "info")
        elif kind == "connected":
            self.engine, self.accounts = evt[1], evt[2]
            self.btn_connect.configure(state="normal")
            self.lbl_conn.configure(text=f"connected · {self.engine.account.name}")
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
            self.lbl_conn.configure(text=f"connect failed: {evt[1]}")
            messagebox.showerror("Connection failed", evt[1])
        elif kind == "contract":
            self.lbl_contract.configure(text=evt[1])
        elif kind == "dashboard":
            price, rng = evt[1], evt[2]
            self.lbl_price.configure(text=f"{price:,.2f}" if price is not None else "—")
            self.lbl_range.configure(text=f"{rng['low']:,.1f}–{rng['high']:,.1f}" if rng else "—")
        elif kind == "ticker":
            self._update_ticker(evt[1])
        elif kind == "ticker_vix":
            self._update_vix(evt[1])
        elif kind == "error":
            self.log(evt[1], "error")

    def _append_log(self, line: str, level: str = "info") -> None:
        self.txt.configure(state="normal")
        if "error" not in self.txt.tag_names():
            self.txt.tag_configure("error", foreground=RED_H)
            self.txt.tag_configure("warn", foreground=AMBER_H)
            self.txt.tag_configure("info", foreground=FG)
        tag = level if level in ("warn", "error") else "info"
        self.txt.insert("end", line + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _refresh_badges(self) -> None:
        if self.controller is not None:
            ok = getattr(self.controller, "logged_in", False)
            self._set_pill(self.badge_conn, "Browser ready" if ok else "Browser…", "Ok" if ok else "Warn")
        elif self.engine is not None:
            self._set_pill(self.badge_conn, "Connected", "Ok")
        else:
            self._set_pill(self.badge_conn, "Offline", "Sec")
        live = not self.var_dry.get()
        self._set_pill(self.badge_mode, "● LIVE" if live else "● DRY-RUN", "Bad" if live else "Ok")
        armed = bool((self.engine and self.engine.armed) or
                     (self.controller and getattr(self.controller, "state", "") in ("armed", "monitoring")))
        self._set_pill(self.badge_armed, "ARMED" if armed else "DISARMED", "Warn" if armed else "Sec")

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
                        d_fire = None
            if d_fire is None:
                d_fire = next_fire_time(s.open_time(), s.capture_offset_seconds, s.market_tz)
            self.lbl_fire.configure(text=d_fire.strftime("%H:%M:%S"))
            self.lbl_count.configure(text=format_countdown(seconds_until(d_fire)))
            self._refresh_badges()
        except Exception:
            pass
        self.root.after(1000, self._tick_countdown)

    def _hud_animate(self) -> None:
        if not self.root.winfo_exists():
            return
        self._hud_angle = (self._hud_angle + 2.2) % 360
        try:
            self.hud.animate(self._hud_angle)
        except Exception:
            pass
        self.root.after(60, self._hud_animate)

    def _start_ticker(self) -> None:
        self._tick_stop.clear()
        threading.Thread(target=self._ticker_worker, name="Ticker", daemon=True).start()

    def _ticker_worker(self) -> None:
        from .ticker import fetch_quote, DEFAULT_TICKER_SYMBOL, VIX_SYMBOL

        while not self._tick_stop.is_set():
            self.events.put(("ticker", fetch_quote(DEFAULT_TICKER_SYMBOL)))
            self.events.put(("ticker_vix", fetch_quote(VIX_SYMBOL)))
            self._tick_stop.wait(5.0)

    def _update_ticker(self, q) -> None:
        if q is None:
            if self.lbl_tick_price.cget("text") == "—":
                self.lbl_tick_chg.configure(text="no feed", style="TickFlat.TLabel")
            return
        self.lbl_tick_sym.configure(text=q.symbol)
        self.lbl_tick_price.configure(text=f"{q.price:,.2f}")
        chg, pct = q.change, q.change_pct
        arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "■")
        style = "TickUp.TLabel" if chg > 0 else ("TickDown.TLabel" if chg < 0 else "TickFlat.TLabel")
        self.lbl_tick_chg.configure(text=f"{arrow} {chg:+,.2f} ({pct:+.2f}%)", style=style)

    def _update_vix(self, q) -> None:
        if q is None:
            if self.lbl_vix_price.cget("text") == "—":
                self.lbl_vix_chg.configure(text="no feed", style="TickFlat.TLabel")
            return
        self.lbl_vix_price.configure(text=f"{q.price:,.2f}")
        chg, pct = q.change, q.change_pct
        # VIX up = fear up = risk-off, so colour it the opposite of a price tape:
        # a rising VIX is "bad" (red), a falling VIX is "good" (green).
        arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "■")
        style = "TickDown.TLabel" if chg > 0 else ("TickUp.TLabel" if chg < 0 else "TickFlat.TLabel")
        self.lbl_vix_chg.configure(text=f"{arrow} {chg:+,.2f} ({pct:+.2f}%)", style=style)

    def _start_poller(self) -> None:
        self._poll_stop.clear()
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def _poll_worker(self) -> None:
        while not self._poll_stop.is_set() and (self.engine or self.controller):
            try:
                if self.settings.backend == "browser" and self.controller:
                    self.events.put(("dashboard", self.controller.last_price, None))
                elif self.engine:
                    self.events.put(("dashboard", self.engine.last_price(), self.engine.overnight_range()))
            except Exception:
                pass
            self._poll_stop.wait(5.0)

    def on_close(self) -> None:
        self._poll_stop.set()
        self._tick_stop.set()
        if self.engine and self.engine.armed:
            self.engine.disarm()
        if self.controller is not None:
            try:
                self.controller.shutdown()
            except Exception:
                pass
        self.root.destroy()


def main() -> int:
    shot_path = None
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        shot_path = sys.argv[i + 1] if i + 1 < len(sys.argv) else "/tmp/imba_gui.png"
    root = tk.Tk()
    app = ImbabotGUI(root, shot_path=shot_path)
    # If the disclaimer was declined, __init__ already called root.destroy();
    # winfo_exists() then raises TclError ("application has been destroyed")
    # rather than returning False, so guard it and exit cleanly.
    try:
        alive = bool(root.winfo_exists())
    except tk.TclError:
        alive = False
    if not alive:
        return 0
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
