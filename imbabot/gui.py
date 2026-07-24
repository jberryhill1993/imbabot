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
from .config import (Settings, load_api_key, store_api_key, log_path,
                     load_tradovate_credentials, store_tradovate_credentials)
from .logbus import Logger
from .models import Account

# ---- palette (modern fintech dashboard: soft navy cards, mint/coral semantics) ----
# (previous arc-reactor palette, for rollback: BG #03070d SURFACE #06121b CARD #081a26
#  ELEV #0c2735 BORDER #0f3c4d FG #bfeffb MUTED #3f8197 ACCENT #00e5ff/#62f1ff
#  GREEN #00e6a0/#43ffc4 RED #ff3b54/#ff6f82 AMBER #ffb02e/#ffc861 GLOW #0a6f86 GRID #0a2330)
BG = "#0B1120"        # page: very dark desaturated navy
SURFACE = "#131B2E"   # panel
CARD = "#16203A"      # card
ELEV = "#1B2742"      # elevated chip / hover
BORDER = "#26314D"    # 1px low-contrast blue-gray borders
FG = "#E8EDF5"        # soft white text
MUTED = "#7D8CA6"     # secondary / labels
ACCENT = "#38BDF8"    # interactive / info (cyan-blue)
ACCENT_H = "#6FD0FF"
GREEN = "#3DDC97"     # bullish / positive / active
GREEN_H = "#6BEBB4"
RED = "#F45B69"       # bearish / negative / danger
RED_H = "#FF7D89"
AMBER = "#F5B759"     # warning / highlight
AMBER_H = "#FFD08A"
# semantic tinted fills (stat cells: green value on faint green field, red on faint red)
GREEN_TINT = "#12281F"
GREEN_TINT_BR = "#1F4D38"
RED_TINT = "#2A1721"
RED_TINT_BR = "#572733"
INPUT_BG = "#0E1526"  # input field / log fill (darker than cards)
# legacy HUD tones (HudHero is dormant; kept so the class still compiles)
GLOW = "#1B2742"
GRID = "#141D33"

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
        root.resizable(True, True)
        root.minsize(980, 560)
        root.update_idletasks()
        # Initial size; _fit_window() (after widgets build) snaps it to the content
        # so everything shows without maximizing.
        w, h = 1060, 860
        x = max(0, (root.winfo_screenwidth() - w) // 2)
        y = max(0, (root.winfo_screenheight() - h) // 3)
        root.geometry(f"{w}x{h}+{x}+{y}")

        if shot_path is None and not self._show_disclaimer():
            root.destroy()
            return

        self._build_styles()
        self._build_widgets()
        self._load_into_widgets()
        if shot_path is None:
            self._fit_window()
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
        # cards read as panels: subtle 1px border on card + surface frames
        st.configure("Card.TFrame", bordercolor=BORDER, relief="solid", borderwidth=1)
        st.configure("Surface.TFrame", bordercolor=BORDER, relief="solid", borderwidth=1)
        st.configure("TLabel", background=BG, foreground=FG, font=(FONT, 11))
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(FONT, 10))
        st.configure("Brand.TLabel", background=BG, foreground=FG, font=(FONT, 19, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 10))
        st.configure("H.TLabel", background=BG, foreground=FG, font=(FONT, 11, "bold"))
        st.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9, "bold"))
        st.configure("CardVal.TLabel", background=CARD, foreground=FG, font=(MONO, 18, "bold"))
        st.configure("CardBig.TLabel", background=CARD, foreground=FG, font=(MONO, 30, "bold"))
        st.configure("Banner.TLabel", background=SURFACE, foreground=MUTED, font=(FONT, 11, "bold"))
        # semantic tinted stat cells (Morning Plan TP/SL): colored value on faint tinted field
        st.configure("TintGreen.TFrame", background=GREEN_TINT, bordercolor=GREEN_TINT_BR,
                     relief="solid", borderwidth=1)
        st.configure("TintRed.TFrame", background=RED_TINT, bordercolor=RED_TINT_BR,
                     relief="solid", borderwidth=1)
        st.configure("TintTitleG.TLabel", background=GREEN_TINT, foreground=MUTED, font=(FONT, 9, "bold"))
        st.configure("TintValG.TLabel", background=GREEN_TINT, foreground=GREEN_H, font=(MONO, 20, "bold"))
        st.configure("TintTitleR.TLabel", background=RED_TINT, foreground=MUTED, font=(FONT, 9, "bold"))
        st.configure("TintValR.TLabel", background=RED_TINT, foreground=RED_H, font=(MONO, 20, "bold"))
        st.configure("CellTitle.TLabel", background=ELEV, foreground=MUTED, font=(FONT, 9, "bold"))
        st.configure("CellVal.TLabel", background=ELEV, foreground=FG, font=(MONO, 20, "bold"))
        st.configure("Cell.TFrame", background=ELEV, bordercolor=BORDER, relief="solid", borderwidth=1)
        # header live ticker (NQ)
        st.configure("TickSym.TLabel", background=ELEV, foreground=FG, font=(FONT, 10, "bold"), padding=(9, 4))
        st.configure("TickPrice.TLabel", background=BG, foreground=FG, font=(MONO, 15, "bold"))
        st.configure("TickUp.TLabel", background=BG, foreground=GREEN_H, font=(FONT, 10, "bold"))
        st.configure("TickDown.TLabel", background=BG, foreground=RED_H, font=(FONT, 10, "bold"))
        st.configure("TickFlat.TLabel", background=BG, foreground=MUTED, font=(FONT, 10, "bold"))
        # pills — softer "status chip" look: tinted fills with colored text (not solid saturated)
        for nm, bg, fg in (("Pill.Sec.TLabel", ELEV, MUTED), ("Pill.Ok.TLabel", GREEN_TINT, GREEN_H),
                           ("Pill.Bad.TLabel", RED_TINT, RED_H), ("Pill.Warn.TLabel", AMBER, "#0B1120")):
            st.configure(nm, background=bg, foreground=fg, font=(FONT, 9, "bold"), padding=(12, 6))
        # inputs
        st.configure("TEntry", fieldbackground=INPUT_BG, foreground=FG, insertcolor=FG,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=7)
        st.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
               darkcolor=[("focus", ACCENT)],
               foreground=[("disabled", MUTED)], fieldbackground=[("disabled", BG)])
        st.configure("TCombobox", fieldbackground=INPUT_BG, background=SURFACE, foreground=FG,
                     arrowcolor=MUTED, bordercolor=BORDER, padding=6)
        st.map("TCombobox", fieldbackground=[("readonly", INPUT_BG)], foreground=[("readonly", FG)])
        st.configure("TCheckbutton", background=BG, foreground=FG, font=(FONT, 10), focuscolor=BG)
        st.map("TCheckbutton", background=[("active", BG)], indicatorcolor=[("selected", ACCENT)])
        st.configure("TRadiobutton", background=BG, foreground=FG, font=(FONT, 10), focuscolor=BG)
        st.map("TRadiobutton", background=[("active", BG)], indicatorcolor=[("selected", ACCENT)])
        st.configure("Card.TCheckbutton", background=BG)
        st.configure("TSeparator", background=BORDER)
        st.configure("Vertical.TScrollbar", background=ELEV, troughcolor=BG, bordercolor=BG,
                     arrowcolor=MUTED)
        # notebook — underline-style tabs: flat, muted; selected = white text on a subtly
        # elevated fill with an accent bottom edge (approximated via a colored underline border)
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(2, 8, 2, 0))
        st.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(20, 10),
                     font=(FONT, 10, "bold"), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", SURFACE)], foreground=[("selected", ACCENT)],
               expand=[("selected", (0, 0, 0, 2))])
        # buttons — semantic fills: ARM=green, FLATTEN=amber, STOP=red, Save/primary=cyan
        self._btn_style(st, "Accent.TButton", ACCENT, ACCENT_H, fg="#0B1120")
        self._btn_style(st, "Success.TButton", GREEN, GREEN_H, fg="#0B1120")
        self._btn_style(st, "Danger.TButton", RED, RED_H)
        self._btn_style(st, "Warning.TButton", AMBER, AMBER_H, fg="#0B1120")
        self._btn_style(st, "Ghost.TButton", ELEV, BORDER, fg=FG, small=True)
        self.st = st

    def _btn_style(self, st, name, bg, hover, fg="#ffffff", small=False):
        st.configure(name, background=bg, foreground=fg, font=(FONT, 10 if small else 11, "bold"),
                     borderwidth=0, relief="flat", focuscolor=bg,
                     padding=(14, 8) if small else (22, 12))
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

        # ===== hero stat cards (replaces the HudHero canvas; same 4 readouts, same
        # .configure(text=…) update path — HudHero/_HudField stay in the file, dormant) =====
        self.hud = None
        hero = ttk.Frame(root, style="TFrame")
        hero.pack(fill="x", padx=20, pady=(14, 6))
        self.lbl_count = self._stat(hero, "T-minus to market open", 0, big=True)
        self.lbl_fire = self._stat(hero, "Next fire", 1)
        self.lbl_price = self._stat(hero, "Last price", 2)
        self.lbl_range = self._stat(hero, "Overnight range", 3)

        # ===== action card: ARM · mode banner · FLATTEN · EMERGENCY STOP =====
        # Safety-critical controls stay full-size and top-level (never buried in a tab).
        ctrl = ttk.Frame(root, style="Surface.TFrame", padding=(16, 12))
        ctrl.pack(fill="x", padx=20, pady=(6, 4))
        self.btn_arm = ttk.Button(ctrl, text="ARM", command=self._on_arm, style="Success.TButton", width=11)
        self.btn_arm.pack(side="left")
        self.lbl_mode_banner = ttk.Label(ctrl, text="", style="Banner.TLabel")
        self.lbl_mode_banner.pack(side="left", padx=18)
        self.btn_panic = ttk.Button(ctrl, text="■  EMERGENCY STOP", command=self._on_panic, style="Danger.TButton")
        self.btn_panic.pack(side="right")
        self.btn_flatten = ttk.Button(ctrl, text="▽  FLATTEN", command=self._on_flatten, style="Warning.TButton")
        self.btn_flatten.pack(side="right", padx=(0, 10))

        # ===== settings notebook =====
        self._scroll_canvases = []      # (canvas, inner) for the scrollable tabs
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=20, pady=(16, 8))
        self._build_tab_connect(nb)
        self._build_tab_strategy(nb)
        self._build_tab_test(nb)

        # ===== log (collapsible) =====
        self._logwrap = ttk.Frame(root, style="Surface.TFrame", padding=10)
        self._logwrap.pack(fill="x", expand=False, padx=20, pady=(0, 16))
        topbar = ttk.Frame(self._logwrap, style="Surface.TFrame")
        topbar.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.btn_log_toggle = ttk.Button(topbar, text="▸  Activity log",
                                         command=self._toggle_log, style="Ghost.TButton")
        self.btn_log_toggle.pack(side="left")
        ttk.Button(topbar, text="Save log…", command=self._on_save_log, style="Ghost.TButton").pack(side="right")
        self.txt = tk.Text(self._logwrap, height=9, wrap="word", relief="flat", borderwidth=0,
                           background=INPUT_BG, foreground=FG, insertbackground=FG,
                           font=(MONO, 10), padx=12, pady=10, highlightthickness=0)
        self.txt.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self._log_sb = ttk.Scrollbar(self._logwrap, command=self.txt.yview)
        self._log_sb.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.txt.configure(yscrollcommand=self._log_sb.set, state="disabled")
        self._logwrap.columnconfigure(0, weight=1)
        self._logwrap.rowconfigure(1, weight=1)
        # collapsed by default — only the toggle bar shows
        self._log_expanded = False
        self.txt.grid_remove()
        self._log_sb.grid_remove()

        self._update_mode_banner()
        self._on_backend_change()

    def _toggle_log(self) -> None:
        """Expand/collapse the activity log; resize the window to match."""
        self._log_expanded = not self._log_expanded
        if self._log_expanded:
            self.txt.grid()
            self._log_sb.grid()
            self._logwrap.pack_configure(fill="both", expand=True)
            self.btn_log_toggle.configure(text="▾  Activity log")
        else:
            self.txt.grid_remove()
            self._log_sb.grid_remove()
            self._logwrap.pack_configure(fill="x", expand=False)
            self.btn_log_toggle.configure(text="▸  Activity log")
        self._fit_window()

    def _fit_window(self) -> None:
        """Size the window to its natural content (clamped to the screen) so
        everything shows without maximizing. Re-run when the log expands/collapses."""
        r = self.root
        r.update_idletasks()
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w = min(max(r.winfo_reqwidth(), 1000), sw - 80)
        h = min(r.winfo_reqheight() + 6, sh - 80)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        r.geometry(f"{w}x{h}+{x}+{y}")

    def _grow_to_content(self) -> None:
        """Grow the window (without moving it) so dynamic content — e.g. the Morning Plan
        panel after Recalculate — is fully visible without manual resizing. Clamped to the
        screen; never shrinks below the current size, so it doesn't 'jump'."""
        try:
            r = self.root
            r.update_idletasks()
            sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
            w = min(max(r.winfo_reqwidth(), r.winfo_width()), sw - 40)
            h = min(max(r.winfo_reqheight() + 6, r.winfo_height()), sh - 60)
            r.geometry(f"{w}x{h}")
        except Exception:
            pass

    def _scrollable_tab(self, nb, title: str, padding: int = 18):
        """Add a notebook tab whose body scrolls vertically, and return the inner frame to
        build into. Lets tall content (e.g. the Morning Plan) stay reachable on small screens
        where the window is clamped to the display — caller code grids into the returned frame
        exactly as before."""
        outer = ttk.Frame(nb, style="Surface.TFrame")
        nb.add(outer, text=title)
        canvas = tk.Canvas(outer, bg=SURFACE, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                            style="Vertical.TScrollbar")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        inner = ttk.Frame(canvas, style="Surface.TFrame", padding=padding)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # Request just enough height to show the content on a big screen; the window's
            # screen-clamp then forces the canvas to scroll on a small one.
            sh = canvas.winfo_screenheight()
            canvas.configure(height=min(inner.winfo_reqheight(), sh - 220))

        def _on_canvas(e):
            canvas.itemconfigure(win, width=e.width)  # inner matches width -> no h-scroll, labels wrap

        inner.bind("<Configure>", _on_inner)
        canvas.bind("<Configure>", _on_canvas)
        # Mouse-wheel scrolls only while the pointer is over this tab.
        canvas.bind("<Enter>", lambda _e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        self._scroll_canvases.append((canvas, inner))
        return inner

    def _build_tab_connect(self, nb) -> None:
        tab = ttk.Frame(nb, style="Surface.TFrame", padding=18)
        nb.add(tab, text="Connect")
        self._surface(tab)  # register surface-bg style variants used inside tabs

        self.var_backend = tk.StringVar(value=self.settings.backend)
        ttk.Label(tab, text="Backend", style="Hs.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(tab, text="API (TopstepX — recommended)", variable=self.var_backend,
                        value="api", command=self._on_backend_change, style="S.TRadiobutton").grid(
            row=0, column=1, sticky="w", padx=10)
        ttk.Radiobutton(tab, text="Tradovate", variable=self.var_backend,
                        value="tradovate", command=self._on_backend_change, style="S.TRadiobutton").grid(
            row=0, column=2, sticky="w", padx=10)
        ttk.Radiobutton(tab, text="Browser automation", variable=self.var_backend,
                        value="browser", command=self._on_backend_change, style="S.TRadiobutton").grid(
            row=0, column=3, sticky="w", padx=10)

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

        # --- Tradovate credentials (visible only for the tradovate backend).
        # Secrets go to the keyring (remember) or a session-only override —
        # never to settings.json or the log.
        self.var_tdv_env = tk.StringVar(value=self.settings.tdv_environment)
        self.var_tdv_user = tk.StringVar(value=self.settings.tdv_username)
        self.var_tdv_pass = tk.StringVar(value="")
        self.var_tdv_cid = tk.StringVar(value="")
        self.var_tdv_sec = tk.StringVar(value="")
        self.frm_tdv = ttk.Frame(tab, style="Surface.TFrame")
        self.frm_tdv.grid(row=9, column=0, columnspan=4, sticky="w", pady=(12, 0))

        def _tdv_field(label: str, var, r: int, show=None, width=30):
            ttk.Label(self.frm_tdv, text=label, style="Sm.TLabel").grid(
                row=r, column=0, sticky="w", pady=4)
            ent = ttk.Entry(self.frm_tdv, textvariable=var, width=width, font=(FONT, 11))
            if show:
                ent.configure(show=show)
            ent.grid(row=r, column=1, sticky="w", padx=8)

        ttk.Label(self.frm_tdv, text="Environment", style="Sm.TLabel").grid(
            row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(self.frm_tdv, state="readonly", width=8, values=["demo", "live"],
                     textvariable=self.var_tdv_env, font=(FONT, 11)).grid(
            row=0, column=1, sticky="w", padx=8)
        _tdv_field("Tradovate username", self.var_tdv_user, 1)
        _tdv_field("Password", self.var_tdv_pass, 2, show="•", width=34)
        _tdv_field("API key cid", self.var_tdv_cid, 3, width=14)
        _tdv_field("API key secret", self.var_tdv_sec, 4, show="•", width=38)
        ttk.Label(self.frm_tdv, text="Price source", style="Sm.TLabel").grid(
            row=5, column=0, sticky="w", pady=4)
        self.var_tdv_price = tk.StringVar(value=self.settings.tdv_price_source)
        ttk.Combobox(self.frm_tdv, state="readonly", width=12,
                     values=["topstep", "tradovate", "public"],
                     textvariable=self.var_tdv_price, font=(FONT, 11)).grid(
            row=5, column=1, sticky="w", padx=8)
        ttk.Label(self.frm_tdv, style="Hint.TLabel", wraplength=560,
                  text="Needs the Tradovate API Access add-on (cid + secret). Order routing "
                       "needs NO CME data license — only the 'tradovate' price source does "
                       "(~$290/mo CME sub-vendor); 'topstep' (default) reuses your TopStep "
                       "feed. LIVE stays locked until safety.py LIVE_TRADING is enabled.").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.frm_tdv.grid_remove()

    def _build_tab_strategy(self, nb) -> None:
        tab = self._scrollable_tab(nb, "Strategy")

        ttk.Label(tab, text="Account", style="Sm.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        self.cmb_account = ttk.Combobox(tab, state="readonly", width=30, values=[], font=(FONT, 11))
        self.cmb_account.grid(row=0, column=1, sticky="w", padx=8)
        self.cmb_account.bind("<<ComboboxSelected>>", self._on_account_pick)
        ttk.Label(tab, text="Symbol", style="Sm.TLabel").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.var_symbol = tk.StringVar(value=self.settings.contract_symbol)
        ttk.Entry(tab, textvariable=self.var_symbol, width=10, font=(FONT, 11)).grid(row=0, column=3, sticky="w", padx=8)
        self.lbl_contract = ttk.Label(tab, text="—", style="Sm.TLabel")
        self.lbl_contract.grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 10))
        ttk.Separator(tab).grid(row=2, column=0, columnspan=5, sticky="ew", pady=4)

        self.var_points = tk.StringVar(value=str(self.settings.entry_points))
        self.var_sl = tk.StringVar(value=str(self.settings.stop_loss_points))
        self.var_tp = tk.StringVar(value=str(self.settings.take_profit_points))
        self.var_contracts = tk.StringVar(value=str(self.settings.contracts))
        # Required fields are highlighted (green/bold ✓).
        self._sfield(tab, "✓ Entry points (±)", self.var_points, 3, width=8, style="Req.TLabel")
        self._sfield(tab, "✓ Contracts", self.var_contracts, 6, width=8, style="Req.TLabel")
        # Stop-loss / Take-profit are platform-managed by default: the label is an
        # ENABLE checkbox; ticking it un-grays the field and the bot attaches that
        # bracket instead of TopStep.
        self.var_bot_sl = tk.BooleanVar(value=self.settings.bot_stop_loss)
        self.var_bot_tp = tk.BooleanVar(value=self.settings.bot_take_profit)
        self.chk_bot_sl = ttk.Checkbutton(tab, text="Stop-loss points (bot-managed)",
                                          variable=self.var_bot_sl,
                                          command=self._on_bracket_toggle, style="S.TCheckbutton")
        self.chk_bot_sl.grid(row=4, column=0, sticky="w", padx=(0, 8), pady=5)
        self.ent_sl = ttk.Entry(tab, textvariable=self.var_sl, width=8, font=(FONT, 11))
        self.ent_sl.grid(row=4, column=1, sticky="w", pady=5)
        self.chk_bot_tp = ttk.Checkbutton(tab, text="Take-profit points (bot-managed)",
                                          variable=self.var_bot_tp,
                                          command=self._on_bracket_toggle, style="S.TCheckbutton")
        self.chk_bot_tp.grid(row=5, column=0, sticky="w", padx=(0, 8), pady=5)
        self.ent_tp = ttk.Entry(tab, textvariable=self.var_tp, width=8, font=(FONT, 11))
        self.ent_tp.grid(row=5, column=1, sticky="w", pady=5)
        self._on_bracket_toggle()    # set initial grayed/enabled state from settings
        # $-entry mode: type SL/TP as dollars per POSITION (TopStep Position-
        # Brackets UX); converted to tick-floored points at collect time.
        self.var_sltp_dollars = tk.BooleanVar(
            value=self.settings.sl_tp_entry_mode == "dollars")
        ttk.Checkbutton(tab, text="Enter SL/TP in $ (per position)",
                        variable=self.var_sltp_dollars, command=self._on_sltp_mode,
                        style="S.TCheckbutton").grid(row=6, column=2, columnspan=2,
                                                     sticky="w", padx=(28, 0))
        if self.var_sltp_dollars.get():
            if self.settings.stop_loss_dollars > 0:
                self.var_sl.set(str(self.settings.stop_loss_dollars))
            if self.settings.take_profit_dollars > 0:
                self.var_tp.set(str(self.settings.take_profit_dollars))
            self._on_sltp_mode()

        # Only One-Trade is exposed. Semi-Auto / Two-Trade remain in the engine
        # (TradeMode values) but are no longer offered in the UI.
        self.var_mode = tk.StringVar(value="one_trade")
        ttk.Label(tab, text="Mode", style="Hs.TLabel").grid(row=3, column=2, sticky="w", padx=(28, 0))
        ttk.Radiobutton(tab, text="One-Trade (auto OCO)  ✓", variable=self.var_mode,
                        value="one_trade", style="Req.TRadiobutton").grid(
                        row=4, column=2, columnspan=2, sticky="w", padx=(28, 0))
        # use_live_data is hidden — the engine auto-detects the live/sim feed. The var
        # is retained (default off) so settings + the advanced override still work.
        self.var_live_data = tk.BooleanVar(value=self.settings.use_live_data)
        self.var_dry = tk.BooleanVar(value=self.settings.dry_run)
        ttk.Checkbutton(tab, text="DRY-RUN (no real orders)", variable=self.var_dry, command=self._on_dry_toggle,
                        style="S.TCheckbutton").grid(row=5, column=2, columnspan=2, sticky="w", padx=(28, 0))
        # Stop-limit entries: cap slippage past the trigger (forward-test on PRAC).
        self.var_stop_limit = tk.BooleanVar(value=self.settings.entry_order_type == "stop_limit")
        self.var_limit_off = tk.StringVar(value=str(self.settings.entry_limit_offset_ticks))
        ttk.Checkbutton(tab, text="Stop-limit entries (cap slippage)", variable=self.var_stop_limit,
                        style="S.TCheckbutton").grid(row=6, column=2, sticky="w", padx=(28, 0))
        ttk.Entry(tab, textvariable=self.var_limit_off, width=4, font=(FONT, 11)).grid(
            row=6, column=3, sticky="w")
        ttk.Label(tab, text="Stop-loss / Take-profit are handled by TopStep (Position Brackets) by "
                            "default. Tick a box to let the BOT manage that bracket instead — only if "
                            "your TopStep account is in Auto OCO Brackets mode (not Position Brackets).",
                  style="Hint.TLabel", wraplength=520).grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Button(tab, text="Save settings", command=self._on_save, style="Accent.TButton").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # --- Production daily auto-fire (recurring, weekday-only) ---
        ttk.Separator(tab).grid(row=9, column=0, columnspan=5, sticky="ew", pady=(14, 6))
        ttk.Label(tab, text="Fire daily at (HH:MM:SS, 24-hour) — Mon–Fri, your computer's clock",
                  style="Hs.TLabel").grid(row=10, column=0, columnspan=5, sticky="w")
        self.var_strategy_time = tk.StringVar(value=self.settings.strategy_fire_time)
        ttk.Entry(tab, textvariable=self.var_strategy_time, width=12, font=(FONT, 11)).grid(
            row=11, column=0, sticky="w", pady=6)
        self.btn_strategy_sched = ttk.Button(tab, text="💾  Save & arm daily (Mon–Fri)",
                                             command=self._on_schedule_strategy, style="Success.TButton")
        self.btn_strategy_sched.grid(row=11, column=1, columnspan=2, sticky="w", padx=8)
        ttk.Label(tab, text="Type a time and click Save & arm daily: the bot stays armed and fires at "
                            "that time every weekday, re-arming itself (8:31 AM = 08:31:00). Fires real "
                            "orders when DRY-RUN is off. DISARM or this button cancels. Holidays are NOT "
                            "skipped — disarm on holidays.",
                  style="Hint.TLabel", wraplength=860).grid(row=12, column=0, columnspan=5, sticky="w", pady=(8, 0))

        # --- Morning Plan (tick-data: volatility + TP-driven sizing/spread; advisory) ---
        ttk.Separator(tab).grid(row=13, column=0, columnspan=5, sticky="ew", pady=(16, 6))
        ttk.Label(tab, text="Morning Plan — tick-data (advisory; the bot never changes your settings)",
                  style="Hs.TLabel").grid(row=14, column=0, columnspan=5, sticky="w")
        self.lbl_mp_action = ttk.Label(tab, text="— not yet calculated —", style="Hs.TLabel")
        self.lbl_mp_action.grid(row=15, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.btn_mp_recalc = ttk.Button(tab, text="↻ Recalculate now",
                                        command=self._on_morning_recalc, style="Accent.TButton")
        self.btn_mp_recalc.grid(row=15, column=2, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(tab, text="Profit target $", style="Sm.TLabel").grid(row=16, column=0, sticky="w", pady=(8, 0))
        self.var_mp_target = tk.StringVar(value="800")
        ttk.Entry(tab, textvariable=self.var_mp_target, width=10, font=(FONT, 11)).grid(
            row=16, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(tab, text="(drives the TopStep inputs below on Recalculate; capped at 5 contracts)",
                  style="Hint.TLabel").grid(row=16, column=2, columnspan=3, sticky="w", pady=(8, 0))
        # The headline that tells the user exactly what to type into TopStep, followed by the
        # four stat cells (TP tinted green / SL tinted red) that carry the actual numbers.
        self.lbl_mp_inputs = ttk.Label(tab, text="", style="Hs.TLabel", foreground=GREEN_H)
        self.lbl_mp_inputs.grid(row=17, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self.mp_cells = ttk.Frame(tab, style="Surface.TFrame")
        self.mp_cells.grid(row=18, column=0, columnspan=5, sticky="w", pady=(6, 2))
        self.cell_mp_ct = self._mp_cell(self.mp_cells, "CONTRACTS", 0, "plain")
        self.cell_mp_entry = self._mp_cell(self.mp_cells, "ENTRY (PTS)", 1, "plain")
        self.cell_mp_tp = self._mp_cell(self.mp_cells, "TAKE-PROFIT", 2, "green")
        self.cell_mp_sl = self._mp_cell(self.mp_cells, "STOP-LOSS", 3, "red")
        self.mp_cells.grid_remove()
        # Amber cap alert — shown only when the entered $TP would need > the contract cap (5).
        self.lbl_mp_alert = ttk.Label(tab, text="", style="Hs.TLabel", foreground=AMBER,
                                      wraplength=860)
        self.lbl_mp_alert.grid(row=19, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self.lbl_mp_alert.grid_remove()
        self.lbl_mp_sizing = ttk.Label(tab, text="", style="Sm.TLabel", wraplength=860)
        self.lbl_mp_sizing.grid(row=20, column=0, columnspan=5, sticky="w", pady=(2, 0))
        self.lbl_mp_detail = ttk.Label(
            tab, text="Enter a profit-target $, then Recalculate for the volatility, predicted spike, "
                      "TRADE/NO-TRADE, and the contracts + entry spread to use.",
            style="Sm.TLabel", wraplength=860)
        self.lbl_mp_detail.grid(row=21, column=0, columnspan=5, sticky="w", pady=(6, 0))

    def _mp_cell(self, parent, title: str, col: int, kind: str = "plain"):
        """One Morning-Plan stat cell: tiny uppercase title over a large mono value.
        kind: 'plain' (elevated) | 'green' (tinted, TP) | 'red' (tinted, SL)."""
        fr_style, ti_style, va_style = {
            "plain": ("Cell.TFrame", "CellTitle.TLabel", "CellVal.TLabel"),
            "green": ("TintGreen.TFrame", "TintTitleG.TLabel", "TintValG.TLabel"),
            "red": ("TintRed.TFrame", "TintTitleR.TLabel", "TintValR.TLabel"),
        }[kind]
        cell = ttk.Frame(parent, style=fr_style, padding=(18, 10))
        cell.grid(row=0, column=col, padx=(0 if col == 0 else 10, 0), sticky="nsew")
        ttk.Label(cell, text=title, style=ti_style).pack(anchor="w")
        val = ttk.Label(cell, text="—", style=va_style)
        val.pack(anchor="w", pady=(3, 0))
        return val

    def _build_tab_test(self, nb) -> None:
        tab = self._scrollable_tab(nb, "Test")
        ttk.Label(tab, text="Verify it actually places orders", style="Hs.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w")
        self.var_test_mode = tk.BooleanVar(value=self.settings.test_mode)
        ttk.Checkbutton(tab, text="Test mode: fire at a custom time (instead of 9:30)",
                        variable=self.var_test_mode, command=self._on_test_toggle,
                        style="S.TCheckbutton").grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 6))
        ttk.Label(tab, text="Fire at (HH:MM:SS, 24-hour) — your computer's clock", style="Sm.TLabel").grid(
            row=2, column=0, sticky="w", pady=5)
        self.var_test_time = tk.StringVar(value=self.settings.test_fire_time)
        ttk.Entry(tab, textvariable=self.var_test_time, width=12, font=(FONT, 11)).grid(row=2, column=1, sticky="w")
        ttk.Button(tab, text="⚡  Fire TEST now", command=self._on_fire_now, style="Warning.TButton").grid(
            row=2, column=2, padx=16)
        # Temporary testing aid: save the time and schedule the bot to auto-fire at it.
        self.btn_autofire = ttk.Button(tab, text="💾  Save & schedule auto-fire",
                                       command=self._on_schedule_autofire, style="Success.TButton")
        self.btn_autofire.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(tab, text="Use a SIM / practice account. The bot fires off your computer's clock "
                            "(the Windows taskbar time — NOT the chart's UTC-5), in 24-hour format "
                            "(7:50 PM = 19:50). Enter a time, click Save & schedule, and it auto-fires "
                            "then; or click ‘Fire TEST now’. DISARM or this button cancels it. Honors dry-run.",
                  style="Hint.TLabel", wraplength=840).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _on_bracket_toggle(self) -> None:
        """Gray/un-gray the SL/TP entries to match their 'bot-managed' checkboxes."""
        self.ent_sl.configure(state="normal" if self.var_bot_sl.get() else "disabled")
        self.ent_tp.configure(state="normal" if self.var_bot_tp.get() else "disabled")

    def _on_sltp_mode(self) -> None:
        """Relabel the SL/TP checkboxes for $-per-position vs points entry."""
        dollars = self.var_sltp_dollars.get()
        self.chk_bot_sl.configure(text="Stop-loss $ (bot-managed)" if dollars
                                  else "Stop-loss points (bot-managed)")
        self.chk_bot_tp.configure(text="Take-profit $ (bot-managed)" if dollars
                                  else "Take-profit points (bot-managed)")

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
        # Highlight styles for the REQUIRED settings (entry / contracts / one-trade).
        st.configure("Req.TLabel", background=SURFACE, foreground=GREEN_H, font=(FONT, 10, "bold"))
        st.configure("Req.TRadiobutton", background=SURFACE, foreground=GREEN_H, font=(FONT, 10, "bold"))
        st.map("Req.TRadiobutton", background=[("active", SURFACE)], indicatorcolor=[("selected", GREEN_H)])

    def _sfield(self, parent, label, var, r, c=0, width=18, show=None, style="Sm.TLabel"):
        ttk.Label(parent, text=label, style=style).grid(row=r, column=c, sticky="w", padx=(0, 8), pady=5)
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
            s.strategy_fire_time = self.var_strategy_time.get().strip()
            s.base_url = self.var_base.get().strip()
            s.username = self.var_user.get().strip()
            s.contract_symbol = self.var_symbol.get().strip().upper()
            s.entry_points = float(self.var_points.get())
            s.stop_loss_points = float(self.var_sl.get())
            s.take_profit_points = float(self.var_tp.get())
            s.contracts = int(self.var_contracts.get())
            s.sl_tp_entry_mode = "dollars" if self.var_sltp_dollars.get() else "points"
            if s.sl_tp_entry_mode == "dollars":
                # var_sl/var_tp hold dollars per position; convert to points.
                from .models import dollars_per_point_for, dollars_to_points
                dpp = None
                c = getattr(self.engine, "contract", None) if self.engine else None
                if c is not None and c.tick_size:
                    dpp = c.tick_value / c.tick_size
                dpp = dpp or dollars_per_point_for(s.contract_symbol)
                if not dpp:
                    messagebox.showerror(
                        "Unknown $/point",
                        f"Don't know the $/point for {s.contract_symbol!r} — connect "
                        "first or untick 'Enter SL/TP in $'.")
                    return None
                tick = c.tick_size if (c is not None and c.tick_size) else 0.25
                s.stop_loss_dollars = float(self.var_sl.get())
                s.take_profit_dollars = float(self.var_tp.get())
                s.stop_loss_points = dollars_to_points(
                    s.stop_loss_dollars, s.contracts, dpp, tick)
                s.take_profit_points = dollars_to_points(
                    s.take_profit_dollars, s.contracts, dpp, tick)
            s.bot_stop_loss = bool(self.var_bot_sl.get())
            s.bot_take_profit = bool(self.var_bot_tp.get())
            s.trade_mode = self.var_mode.get()
            s.entry_order_type = "stop_limit" if self.var_stop_limit.get() else "stop"
            s.entry_limit_offset_ticks = int(self.var_limit_off.get())
            s.use_live_data = bool(self.var_live_data.get())
            s.dry_run = bool(self.var_dry.get())
            s.tdv_environment = self.var_tdv_env.get().strip() or "demo"
            s.tdv_username = self.var_tdv_user.get().strip()
            s.tdv_price_source = self.var_tdv_price.get().strip() or "topstep"
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
                c = self.engine.refresh_contract()
                self.lbl_contract.configure(
                    text=f"{c.name} ({c.id})  tick={c.tick_size} ${c.tick_value}/tick")
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
            text, color = "● LIVE — REAL ORDERS", RED_H
        else:
            text, color = "● DRY-RUN — no orders sent", GREEN_H
        if self.var_backend.get() == "tradovate":
            env = (self.var_tdv_env.get() or "demo").upper()
            text += f"   ·   TDV {env}"
            if env == "LIVE":
                color = RED_H
        self.lbl_mode_banner.configure(text=text, foreground=color)

    # ------------------------------------------------------------- actions
    def _on_backend_change(self) -> None:
        choice = self.var_backend.get()
        browser = choice == "browser"
        tradovate = choice == "tradovate"
        self.btn_connect.configure(text="Launch Browser" if browser else "Connect")
        if tradovate:
            self.frm_tdv.grid()
        else:
            self.frm_tdv.grid_remove()
        if browser:
            self.lbl_backend_hint.configure(
                text="Browser mode: a real Chrome window opens; you log in, then Arm. TopstepX (projectx) "
                     "ships pre-calibrated.")
        elif tradovate:
            self.lbl_backend_hint.configure(
                text="Tradovate API mode: direct REST + WebSocket connection. DEMO endpoint by default; "
                     "brackets ride server-side OSO orders.")
        else:
            self.lbl_backend_hint.configure(
                text="API mode: official TopstepX API (recommended). Needs your API key.")
        self._update_mode_banner()

    def _on_connect(self) -> None:
        s = self._collect_settings()
        if not s:
            return
        s.save()
        if s.backend == "browser":
            self._launch_browser(s)
            return
        if s.backend == "tradovate":
            self._connect_tradovate(s)
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

    def _connect_tradovate(self, s: Settings) -> None:
        """Tradovate branch of Connect. Secrets: keyring when remembered, else a
        session-only override on the client — never settings.json or the log."""
        if not s.tdv_username:
            messagebox.showerror("Missing username", "Enter your Tradovate username.")
            return
        secrets = {k: v for k, v in {
            "password": self.var_tdv_pass.get().strip(),
            "cid": self.var_tdv_cid.get().strip(),
            "sec": self.var_tdv_sec.get().strip(),
        }.items() if v}
        stored = load_tradovate_credentials(s.tdv_username) or {}
        if not (secrets.get("password") or stored.get("password")):
            messagebox.showerror("Missing password", "Enter your Tradovate password.")
            return
        if not ((secrets.get("cid") or stored.get("cid"))
                and (secrets.get("sec") or stored.get("sec"))):
            messagebox.showerror(
                "Missing API key",
                "Enter the API key cid + secret (Tradovate → API Access).")
            return
        session_secrets = None
        if self.var_remember.get() and secrets:
            blob = dict(stored)
            blob.update(secrets)
            blob["app_id"] = s.tdv_app_id or "Imbabot"
            backend = store_tradovate_credentials(s.tdv_username, blob)
            self.log(f"Tradovate credentials stored via {backend}.")
        elif secrets:
            session_secrets = secrets
        self.lbl_conn.configure(text="connecting…")
        self.btn_connect.configure(state="disabled")
        threading.Thread(target=self._connect_worker,
                         args=(s, secrets.get("password", ""), session_secrets),
                         daemon=True).start()

    def _connect_worker(self, s: Settings, key: str, session_secrets=None) -> None:
        try:
            from .engine import BotEngine

            engine = BotEngine(s, log=self.log)
            if session_secrets:
                engine.client.session_secrets = session_secrets
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

    def _on_schedule_autofire(self) -> None:
        """Temporary test aid: lock in the Test-tab time and arm the bot so it
        auto-fires at that local clock time. Reuses the normal arm/FireTimer
        path, so at the target second it runs the same fire sequence as
        'Fire TEST now'. A second click cancels (disarms)."""
        from .scheduler import parse_hms, next_local_fire

        if self.engine is None:
            messagebox.showinfo("Connect first", "Connect before scheduling auto-fire.")
            return
        # Toggle off: if already armed, this button cancels the schedule.
        if self.engine.armed:
            self.engine.disarm()
            self.btn_arm.configure(text="ARM", style="Success.TButton")
            self.btn_autofire.configure(text="💾  Save & schedule auto-fire")
            self.log("Auto-fire cancelled (disarmed).", "warn")
            return
        # Validate the time before touching anything.
        raw = self.var_test_time.get().strip()
        try:
            parse_hms(raw)
        except ValueError as exc:
            messagebox.showerror("Invalid time", f"Use HH:MM or HH:MM:SS (24-hour, your computer's clock).\n\n{exc}")
            return
        # If the chosen time already passed today, next_local_fire rolls it to
        # tomorrow. Make that explicit so "nothing happened" can't surprise you.
        now = datetime.now().astimezone()
        fire = next_local_fire(raw)
        if fire.date() != now.date():
            if not messagebox.askyesno(
                "Fires TOMORROW",
                f"{raw} has already passed on your computer's clock "
                f"({now.strftime('%H:%M:%S')}).\n\nThis will fire TOMORROW — "
                f"{fire.strftime('%a %b %d at %H:%M:%S')}.\n\n"
                "Schedule it for tomorrow? (Times are 24-hour: use 19:50 for 7:50 PM.)",
                icon="warning", default="no",
            ):
                return
        # Lock it in: test mode on + save, then push settings to the engine.
        self.var_test_mode.set(True)
        s = self._collect_settings()
        if not s:
            return
        s.save()
        self.engine.settings = s
        self.engine.risk.settings = s
        if not s.dry_run:
            if not messagebox.askyesno(
                "Schedule LIVE auto-fire?",
                f"Auto-fire in LIVE mode at {raw} (your computer's clock)?\n\n"
                f"{s.contract_symbol}  ±{s.entry_points}pt  x{s.contracts}  mode={s.trade_mode}\n\n"
                "Real orders will be sent at that time.",
                icon="warning", default="no",
            ):
                return
        try:
            self.engine.arm(on_tick=None)
        except Exception as exc:
            messagebox.showerror("Schedule refused", str(exc))
            self.log(f"Auto-fire schedule refused: {exc}", "error")
            return
        self.btn_arm.configure(text="DISARM", style="Warning.TButton")
        self.btn_autofire.configure(text="✖  Cancel auto-fire")
        self.log(f"Auto-fire scheduled for {fire.strftime('%a %b %d %H:%M:%S')} "
                 "(your computer's local clock) — it will fire automatically.")

    def _on_schedule_strategy(self) -> None:
        """Production daily schedule: arm the bot to fire at the Strategy-tab time
        every weekday (Mon–Fri), re-arming itself after each fire. Uses the local
        computer clock. A second click cancels (disarms)."""
        from .scheduler import parse_hms, next_weekday_local_fire

        if self.engine is None:
            messagebox.showinfo("Connect first", "Connect before arming the daily schedule.")
            return
        # Toggle off: if already armed, this button cancels the schedule.
        if self.engine.armed:
            self.engine.disarm()
            self.btn_arm.configure(text="ARM", style="Success.TButton")
            self.btn_strategy_sched.configure(text="💾  Save & arm daily (Mon–Fri)")
            self.log("Daily schedule cancelled (disarmed).", "warn")
            return
        # Validate the time.
        raw = self.var_strategy_time.get().strip()
        try:
            parse_hms(raw)
        except ValueError as exc:
            messagebox.showerror("Invalid time", f"Use HH:MM:SS (24-hour, your computer's clock).\n\n{exc}")
            return
        # Lock it in: test mode OFF (use the weekday schedule), save, push to engine.
        self.var_test_mode.set(False)
        s = self._collect_settings()
        if not s:
            return
        s.save()
        self.engine.settings = s
        self.engine.risk.settings = s
        fire = next_weekday_local_fire(raw)
        now = datetime.now().astimezone()
        if fire.date() != now.date():
            messagebox.showinfo(
                "First fire",
                f"{raw} won't run again today — the first fire will be "
                f"{fire.strftime('%a %b %d at %H:%M:%S')}, then every weekday at {raw}.",
            )
        if not s.dry_run:
            if not messagebox.askyesno(
                "Arm LIVE daily schedule?",
                f"Fire LIVE every weekday at {raw} (your computer's clock)?\n\n"
                f"{s.contract_symbol}  ±{s.entry_points}pt  x{s.contracts}  mode={s.trade_mode}\n\n"
                "Real orders will be sent automatically each weekday until you DISARM.",
                icon="warning", default="no",
            ):
                return
        try:
            self.engine.arm(on_tick=None)
        except Exception as exc:
            messagebox.showerror("Schedule refused", str(exc))
            self.log(f"Daily schedule refused: {exc}", "error")
            return
        self.btn_arm.configure(text="DISARM", style="Warning.TButton")
        self.btn_strategy_sched.configure(text="✖  Cancel daily schedule")
        self.log(f"Daily auto-fire armed — first fire {fire.strftime('%a %b %d %H:%M:%S')}, "
                 f"then every weekday at {raw} (your computer's local clock).")

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
        elif kind == "dashboard":
            price, rng = evt[1], evt[2]
            self.lbl_price.configure(text=f"{price:,.2f}" if price is not None else "—")
            self.lbl_range.configure(text=f"{rng['low']:,.1f}–{rng['high']:,.1f}" if rng else "—")
        elif kind == "ticker":
            self._update_ticker(evt[1])
        elif kind == "ticker_vix":
            self._update_vix(evt[1])
        elif kind == "morning_plan":
            self._show_morning_plan(evt[1])
        elif kind == "morning_error":
            self.btn_mp_recalc.configure(state="normal")
            self.lbl_mp_action.configure(text="—")
            self.lbl_mp_detail.configure(text=f"Morning Plan error: {evt[1]}")
            self.log(f"morning plan: {evt[1]}", "error")
        elif kind == "error":
            self.log(evt[1], "error")

    # ---------------------------------------------------- Morning Plan (advisory)
    def _on_morning_recalc(self) -> None:
        self.btn_mp_recalc.configure(state="disabled")
        self.lbl_mp_action.configure(text="… calculating …")
        threading.Thread(target=self._morning_recalc_worker, daemon=True).start()

    def _morning_recalc_worker(self) -> None:
        try:
            from datetime import datetime
            from .analysis.tick_runner import morning_plan
            # Pull fresh daily VIX + NQ so the prior-close features (VIX level, overnight gap)
            # aren't stale. Best-effort: refresh() falls back to the cache on any failure.
            try:
                from .analysis.market_history import refresh, VIX_SYMBOL, NQ_SYMBOL
                refresh(VIX_SYMBOL)
                refresh(NQ_SYMBOL)
            except Exception:
                pass
            target = 800.0
            t = self.var_mp_target.get().strip()
            if t:
                try:
                    target = float(t.replace("$", "").replace(",", ""))
                except ValueError:
                    pass
            dpp = 20.0
            if self.engine:
                c = getattr(self.engine, "contract", None)
                if c and c.tick_size:
                    dpp = c.tick_value / c.tick_size
            # "as of today" -> morning_plan rolls weekends/holidays to the next real session.
            today = datetime.now().astimezone().date().isoformat()
            mp = morning_plan(today, target_dollars=target, dollars_per_point=dpp,
                              max_contracts=self.settings.max_contracts)
            self.events.put(("morning_plan", mp))
        except Exception as exc:
            self.events.put(("morning_error", str(exc)))

    def _vix_is_stale(self, session_date: str) -> bool:
        """True if the cached VIX daily close is older than the session's prior trading day
        (i.e. the refresh failed and we're showing a stale prior-close feature)."""
        try:
            from datetime import date as _d, timedelta
            from .analysis.market_history import load_daily, VIX_SYMBOL, by_date, prior_value
            from .market_calendar import is_trading_day
            pv = prior_value(by_date(load_daily(VIX_SYMBOL)), session_date)
            if not pv:
                return True
            d = _d.fromisoformat(session_date) - timedelta(days=1)
            while not is_trading_day(d):
                d -= timedelta(days=1)
            return pv.date < d.isoformat()
        except Exception:
            return False

    def _show_morning_plan(self, mp) -> None:
        from datetime import date as _date
        self.btn_mp_recalc.configure(state="normal")
        # Never present the crude fallback (0.75*VIX) as a real call: when the
        # trained model isn't loaded, say so loudly and show nothing actionable.
        if not mp.calibrated:
            self.lbl_mp_action.configure(
                text="⛔ MODEL NOT LOADED — no advice", foreground=RED_H)
            self.lbl_mp_inputs.configure(
                text="Morning-Plan data missing — relaunch the bot (it self-installs the model). "
                     "If it persists, reinstall from the latest download.", foreground=RED_H)
            self.mp_cells.grid_remove()
            self.lbl_mp_sizing.configure(text="")
            self.lbl_mp_alert.configure(text="")
            self.lbl_mp_detail.configure(
                text="The spike predictor could not load its trained model, so it cannot advise "
                     "today. (This was NOT a real NO-TRADE call.)")
            return
        tag = "✅ TRADE" if mp.decision == "TRADE" else "⛔ NO-TRADE"
        try:
            sess = _date.fromisoformat(mp.session_date).strftime("%a %b %d")
        except Exception:
            sess = mp.session_date
        banner = "  ·  ⚠ MARKET CLOSED TODAY — plan is for the NEXT session" if mp.market_closed_today else ""
        cal = "" if mp.calibrated else "  ·  UNCALIBRATED"
        self.lbl_mp_action.configure(
            text=f"{tag}  ·  {mp.conviction}  ·  Session: {sess}  ·  Vol {mp.volatility}  ·  "
                 f"spike ~{mp.predicted_spike:.0f}pt  ·  P(30+)={mp.p_big*100:.0f}%{banner}{cal}")
        p = mp.plan
        # Headline + 4 stat cells = the FIXED-BRACKET recommendation (2026-07-22 sweep-validated:
        # symmetric ~8pt TP/SL, entry ±X by the VIX rule), sized to the USER'S profit-target box.
        # Shown for TRADE and NO-TRADE alike — the verdict line stays the advice.
        rec_cap = mp.rec_tp_dollars < p.target_dollars - 1  # target needed >max contracts
        self.cell_mp_ct.configure(text=f"{mp.rec_contracts}")
        self.cell_mp_entry.configure(text=f"±{mp.rec_entry_spread:.0f}")
        self.cell_mp_tp.configure(text=f"${mp.rec_tp_dollars:,.0f}")
        self.cell_mp_sl.configure(text=f"${mp.rec_sl_dollars:,.0f}")
        self.mp_cells.grid()
        if mp.decision == "TRADE" and p.feasible:
            self.lbl_mp_inputs.configure(text="➡  ENTER IN TOPSTEP", foreground=GREEN_H)
        else:
            self.lbl_mp_inputs.configure(
                text="➡  NO-TRADE — sit out (sizing below only if you trade anyway)",
                foreground=RED_H)
        sized = (f"Your ${p.target_dollars:,.0f} target exceeds the {p.max_contracts}-contract max — "
                 f"sized to the max instead" if rec_cap else
                 f"Sized to your ${p.target_dollars:,.0f} target")
        self.lbl_mp_sizing.configure(
            text=(f"Entry ±{mp.rec_entry_spread:.0f} — {mp.rec_entry_reason} (±14 only when prior "
                  f"VIX ≥ 18; widening below that hurts). Enter the spread manually — advisory."
                  f"\n{sized} at the validated ~8pt symmetric bracket "
                  f"({mp.rec_contracts} × $160/contract)."))
        if rec_cap:
            self.lbl_mp_alert.configure(
                text=f"⚠  ${p.target_dollars:,.0f} needs more than {p.max_contracts} contracts — "
                     f"capped. Today's max: TP ${mp.rec_tp_dollars:,.0f} / "
                     f"SL ${mp.rec_sl_dollars:,.0f} — use that.")
            self.lbl_mp_alert.grid()
        else:
            self.lbl_mp_alert.grid_remove()
        # VIX shown is the PRIOR SESSION CLOSE (the model feature) — flag if the daily cache is stale.
        if mp.prior_vix:
            stale = "  ⚠ stale" if self._vix_is_stale(mp.session_date) else ""
            vix = f"VIX {mp.prior_vix:.1f} (prior close){stale}"
        else:
            vix = "VIX n/a"
        if getattr(mp, "overnight_gap", None) is not None:
            early = "" if getattr(mp, "gap_fresh", True) else " (early ⚠)"
            gap = f"Gap {mp.overnight_gap:.0f}pt{early}"
        else:
            gap = "Gap n/a"
        self.lbl_mp_detail.configure(
            text=f"{vix}  ·  {gap}  ·  News: {mp.news_label}\n{mp.rationale}")
        self.log(f"Morning Plan {mp.session_date}: {mp.decision}/{mp.conviction} spike ~{mp.predicted_spike:.0f}pt "
                 f"-> {mp.rec_contracts}ct ±{mp.rec_entry_spread:.0f} ({mp.rec_entry_reason})")
        self._grow_to_content()
        # Make sure the plan is in view even when the window is clamped on a small screen:
        # scroll the Strategy tab (first scrollable tab) to reveal the bottom panel.
        try:
            canvas = self._scroll_canvases[0][0]
            canvas.update_idletasks()
            canvas.yview_moveto(1.0)
        except Exception:
            pass

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
            from .scheduler import (seconds_until, format_countdown, next_fire_time,
                                    next_local_fire, next_weekday_local_fire)

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
                st = getattr(self, "var_strategy_time", None)
                st = st.get().strip() if st else ""
                if st:
                    try:
                        d_fire = next_weekday_local_fire(st)
                    except Exception:
                        d_fire = None
            if d_fire is None:
                d_fire = next_fire_time(s.open_time(), s.capture_offset_seconds, s.market_tz)
            self.lbl_fire.configure(text=d_fire.strftime("%H:%M:%S"))
            self.lbl_count.configure(text=format_countdown(seconds_until(d_fire)))
            self._refresh_badges()
            # Keep the auto-fire button labels in sync with armed state, so
            # disarming from the main control bar updates them too.
            armed = self.engine is not None and self.engine.armed
            btn = getattr(self, "btn_autofire", None)
            if btn is not None and self.engine is not None:
                btn.configure(text="✖  Cancel auto-fire" if armed
                              else "💾  Save & schedule auto-fire")
            btn2 = getattr(self, "btn_strategy_sched", None)
            if btn2 is not None and self.engine is not None:
                btn2.configure(text="✖  Cancel daily schedule" if armed
                               else "💾  Save & arm daily (Mon–Fri)")
        except Exception:
            pass
        self.root.after(1000, self._tick_countdown)

    def _hud_animate(self) -> None:
        """Dormant since the fintech-dashboard restyle (no HudHero instance; not scheduled)."""
        if self.hud is None or not self.root.winfo_exists():
            return
        self._hud_angle = (getattr(self, "_hud_angle", 0.0) + 2.2) % 360
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
