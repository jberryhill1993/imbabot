"""Live-trade journal — REAL morning trades recorded against Morning-Plan predictions.

The spike model is validated on SIMULATED tick outcomes; this is the first place
actual fills are stored, so the live win-rate can be tracked against the model's
~56-58% TRADE-day expectation. It is pure record-keeping: nothing here places or
influences orders.

Persisted as a JSON list at ``config_dir()/analysis/live_trades.json`` (the same
per-build config dir as the tick cache and spike model).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..config import config_dir

# $ per index point per contract, by root symbol.
_DPP = {"NQ": 20.0, "MNQ": 2.0, "ES": 50.0, "MES": 5.0}
DEFAULT_COMMISSION_RT = 2.6      # ~round-turn commission/contract (NQ, TopStep)
_SCRATCH_DOLLARS = 5.0           # |net| below this = scratch, not a win/loss


def dollars_per_point(symbol: str) -> float:
    root = "".join(c for c in (symbol or "").upper() if c.isalpha())
    # strip a trailing month code if a full contract name was given (NQU6 -> NQ)
    for cand in (root, root[:-1] if len(root) > 2 else root):
        if cand in _DPP:
            return _DPP[cand]
    return _DPP["NQ"]


@dataclass
class LiveTrade:
    date: str                        # session date, ISO
    backend: str = "topstep"         # "topstep" | "tradovate" | "demo"
    decision: str = ""               # the Morning Plan call: TRADE / NO-TRADE
    conviction: str = ""             # LOW / MODERATE / STRONG
    predicted_spike: float = 0.0     # pts, from the Morning Plan
    reference: Optional[float] = None
    side: str = "none"               # which entry filled: long | short | none
    entry_fill: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""            # tp | sl | manual | no-fill
    contracts: int = 0
    dollars_per_point: float = 20.0
    commission_per_rt: float = DEFAULT_COMMISSION_RT
    overnight_gap: Optional[float] = None
    outcome: str = ""                # win | loss | scratch | no-fill (derived)
    notes: str = ""

    # ---- P&L ----
    def gross_pnl(self) -> Optional[float]:
        if self.entry_fill is None or self.exit_price is None or not self.contracts:
            return None
        move = self.exit_price - self.entry_fill
        if self.side == "short":
            move = -move
        return move * self.contracts * self.dollars_per_point

    def net_pnl(self) -> Optional[float]:
        g = self.gross_pnl()
        if g is None:
            return None
        return g - self.contracts * self.commission_per_rt

    def derive_outcome(self) -> str:
        if self.side == "none" or self.entry_fill is None:
            return "no-fill"
        net = self.net_pnl()
        if net is None:
            return "no-fill"
        if abs(net) < _SCRATCH_DOLLARS:
            return "scratch"
        return "win" if net > 0 else "loss"

    def finalize(self) -> "LiveTrade":
        self.outcome = self.derive_outcome()
        return self


# ------------------------------------------------------------- persistence
def journal_path() -> Path:
    return config_dir() / "analysis" / "live_trades.json"


def load_journal() -> List[LiveTrade]:
    p = journal_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    known = {f for f in LiveTrade.__dataclass_fields__}  # type: ignore[attr-defined]
    out = []
    for d in raw:
        if isinstance(d, dict):
            out.append(LiveTrade(**{k: v for k, v in d.items() if k in known}))
    return out


def save_journal(trades: List[LiveTrade]) -> Path:
    p = journal_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([asdict(t) for t in trades], indent=2), encoding="utf-8")
    return p


def append_trade(trade: LiveTrade) -> Path:
    """Append (or replace an exact duplicate on date+backend+entry_fill)."""
    trade.finalize()
    trades = load_journal()
    key = (trade.date, trade.backend, trade.entry_fill)
    trades = [t for t in trades if (t.date, t.backend, t.entry_fill) != key]
    trades.append(trade)
    trades.sort(key=lambda t: (t.date, t.backend))
    return save_journal(trades)


def actual_dict() -> Dict[str, float]:
    """{date: net_pnl} — feeds the dormant analysis_report `actual` hook so a day
    present in BOTH the tick dataset and the journal shows its real $."""
    out: Dict[str, float] = {}
    for t in load_journal():
        net = t.net_pnl()
        if net is not None:
            out[t.date] = round(out.get(t.date, 0.0) + net, 2)
    return out


# --------------------------------------------------------------- scorecard
@dataclass
class Scorecard:
    n: int = 0
    fired: int = 0                   # trades that actually took a position
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    total_net: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    trade_calls: int = 0             # Morning Plan said TRADE
    trade_call_wins: int = 0
    text: str = ""

    @property
    def win_rate(self) -> Optional[float]:
        decided = self.wins + self.losses
        return (100.0 * self.wins / decided) if decided else None


def scorecard(trades: Optional[List[LiveTrade]] = None) -> Scorecard:
    trades = load_journal() if trades is None else trades
    sc = Scorecard(n=len(trades))
    win_amts: List[float] = []
    loss_amts: List[float] = []
    for t in trades:
        net = t.net_pnl()
        if net is not None:
            sc.total_net += net
        if t.outcome == "win":
            sc.wins += 1
            sc.fired += 1
            if net is not None:
                win_amts.append(net)
        elif t.outcome == "loss":
            sc.losses += 1
            sc.fired += 1
            if net is not None:
                loss_amts.append(net)
        elif t.outcome == "scratch":
            sc.scratches += 1
            sc.fired += 1
        if (t.decision or "").upper().startswith("TRADE"):
            sc.trade_calls += 1
            if t.outcome == "win":
                sc.trade_call_wins += 1
    sc.avg_win = sum(win_amts) / len(win_amts) if win_amts else 0.0
    sc.avg_loss = sum(loss_amts) / len(loss_amts) if loss_amts else 0.0
    sc.text = _scorecard_text(sc)
    return sc


def _scorecard_text(sc: Scorecard) -> str:
    wr = f"{sc.win_rate:.0f}%" if sc.win_rate is not None else "—"
    lines = [
        "LIVE SCORECARD",
        "-" * 52,
        f"trades logged : {sc.n}   (fired {sc.fired})",
        f"record        : {sc.wins}W - {sc.losses}L"
        + (f" - {sc.scratches} scratch" if sc.scratches else ""),
        f"win rate      : {wr}   (model expects ~56-58% on TRADE days)",
        f"net P&L       : ${sc.total_net:,.2f}",
        f"avg win/loss  : +${sc.avg_win:,.0f} / ${sc.avg_loss:,.0f}",
    ]
    if sc.trade_calls:
        lines.append(f"TRADE-call hit: {sc.trade_call_wins}/{sc.trade_calls} won")
    return "\n".join(lines)


def format_trade(t: LiveTrade) -> str:
    net = t.net_pnl()
    net_s = f"${net:,.0f}" if net is not None else "—"
    rr = ""
    if t.entry_fill and t.sl_price and t.tp_price:
        risk = abs(t.entry_fill - t.sl_price)
        reward = abs(t.tp_price - t.entry_fill)
        if risk:
            rr = f"  R:R 1:{reward / risk:.1f}"
    path = (f"{t.entry_fill:g}->{t.exit_price:g}"
            if t.entry_fill and t.exit_price is not None else "no fill")
    tag = {"win": "WIN ", "loss": "LOSS", "scratch": "SCR ", "no-fill": "----"}.get(
        t.outcome, "    ")
    return (f"{t.date}  {t.backend:8s}  {(t.decision or '-'):8s}/{t.conviction[:4]:4s} "
            f"spike~{t.predicted_spike:>3.0f}  {t.side:5s} {path:>17s} x{t.contracts}"
            f"{rr}  {tag} {net_s}"
            + (f"   [{t.notes}]" if t.notes else ""))
