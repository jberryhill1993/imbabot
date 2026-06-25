"""Morning plan model — a feature-conditioned k-NN policy over the 2-D backtest.

For today's pre-open features it finds the most-similar past mornings and picks the
(spread, stop) cell with the best **average** P&L across them — then judges conviction
and TRADE/SKIP from how that *same* cell actually performed on those mornings. This is
deliberately NOT a regression to each day's argmax cell (that target is pure noise and
predicts near-constant extremes); the policy answers "on days that looked like today,
which fixed setup did best, and how reliably?"

Advisory only — the bot never applies these. Persisted to
`config_dir()/analysis/morning_model.json`.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..config import config_dir
from .backtest import Backtest2D
from .features import FEATURE_NAMES, to_vector
from .model import _standardize

MORNING_K = 30          # neighbors averaged per cell (stable with ~250 days)
SPIKE_SLIP_MARGIN = 5.0  # headroom (pts) the expected spike must clear entry+TP by for a "likely"
                         # in-candle TP — a violent open fills several pts past the stop (observed ~15pt)


@dataclass
class MorningPlan:
    action: str              # "TRADE" | "SKIP"
    spread: float            # recommended entry spread, points
    stop_points: float       # recommended stop distance, points
    conviction: str          # "low" | "medium" | "high"
    confidence: str          # "low" | "medium" | "high" (sample size)
    whipsaw_risk: str        # "low" | "medium" | "high"
    predicted_winrate: float
    expected_pnl_points: float
    method: str
    rationale: str
    expected_spike_points: float = 0.0   # predicted 9:30 opening swing (advisory)
    spike_label: str = "unknown"         # "calm" | "normal" | "violent"
    spike_needed_points: float = 0.0     # entry(±X) + TP distance to complete in the candle
    spike_verdict: str = ""              # "likely" | "marginal" | "unlikely" | ""
    max_entry_for_spike: float = 0.0     # widest ±X whose entry+TP still fits the expected spike


@dataclass
class MorningModel:
    feature_names: List[str] = field(default_factory=lambda: list(FEATURE_NAMES))
    means: List[float] = field(default_factory=list)
    stds: List[float] = field(default_factory=list)
    cells: List[list] = field(default_factory=list)        # [[spread,stop], ...] (JSON-safe)
    hist_rows: List[List[float]] = field(default_factory=list)   # standardized features/day
    hist_pnl: List[List[float]] = field(default_factory=list)    # day x cell P&L (points)
    hist_whip: List[List[int]] = field(default_factory=list)     # day x cell whipsaw flag
    hist_impulse: List[float] = field(default_factory=list)      # day -> opening-spike size (pts)
    n_samples: int = 0
    target_points: float = 13.0

    # ---- fit ----
    def fit(self, feature_rows: List[Dict[str, float]], dates: List[str],
            bt: Backtest2D, impulses: Optional[List[float]] = None) -> "MorningModel":
        self.n_samples = len(feature_rows)
        self.target_points = bt.target_points
        self.cells = [list(c) for c in bt.cells_order]
        if not feature_rows:
            return self
        X = [to_vector(r) for r in feature_rows]
        self.means, self.stds = _standardize(X)
        self.hist_rows = [[(r[j] - self.means[j]) / self.stds[j] for j in range(len(r))] for r in X]
        self.hist_pnl = [bt.per_day_cell_pnl[d] for d in dates]
        self.hist_whip = [bt.per_day_cell_whip[d] for d in dates]
        # Opening-spike size per day (advisory). Missing/None -> 0 so indexing stays aligned.
        self.hist_impulse = [float(v) if v is not None else 0.0
                             for v in (impulses or [0.0] * len(dates))]
        return self

    # ---- predict ----
    def _neighbors(self, z: List[float], k: int) -> List[int]:
        order = sorted(range(len(self.hist_rows)),
                       key=lambda i: math.dist(z, self.hist_rows[i]))
        return order[:min(k, len(order))]

    def recommend(self, row: Dict[str, float], *, min_spread: float = 0.0,
                  user_entry: Optional[float] = None) -> MorningPlan:
        if not self.means or not self.cells:
            return MorningPlan("SKIP", 0, 0, "low", "low", "high", 0.0, 0.0,
                               "knn-policy", "No model fitted.")
        v = to_vector(row)
        z = [(v[j] - self.means[j]) / self.stds[j] for j in range(len(v))]
        nbrs = self._neighbors(z, MORNING_K)
        ncells = len(self.cells)

        # For each cell, average P&L / win-rate / whipsaw across the similar mornings.
        # Respect the entry floor: never recommend a spread tighter than ``min_spread``
        # (slippage + pre-open fills make ultra-tight entries unrealistic).
        best_i, best_mean = None, None
        mean_pnl = [0.0] * ncells
        for ci in range(ncells):
            vals = [self.hist_pnl[i][ci] for i in nbrs]
            m = statistics.fmean(vals) if vals else 0.0
            mean_pnl[ci] = m
            if self.cells[ci][0] < min_spread:
                continue
            if best_mean is None or m > best_mean:
                best_mean, best_i = m, ci
        if best_i is None:                       # floor excluded all cells -> widest one
            best_i = max(range(ncells), key=lambda c: self.cells[c][0])
            best_mean = mean_pnl[best_i]

        spread, stop = self.cells[best_i]
        nb_pnl = [self.hist_pnl[i][best_i] for i in nbrs]
        nb_whip = [self.hist_whip[i][best_i] for i in nbrs]
        winrate = statistics.fmean(1.0 if p > 0 else 0.0 for p in nb_pnl) if nb_pnl else 0.0
        whip_rate = statistics.fmean(nb_whip) if nb_whip else 0.0
        exp_pnl = best_mean or 0.0

        # Expected opening spike: average the opening-swing of the same similar mornings.
        # Advisory only — flags whipsaw risk; NOT used to set the spread (it doesn't predict
        # the best spread, corr ~0). Bucketed for a plain calm/normal/violent label.
        spike_vals = [self.hist_impulse[i] for i in nbrs] if self.hist_impulse else []
        exp_spike = statistics.fmean(spike_vals) if spike_vals else 0.0
        spike_label = ("calm" if exp_spike < 8 else "normal" if exp_spike < 16 else "violent")

        # Go/no-go: will the opening spike trigger the entry AND reach the $ TP in the candle?
        # The trade completes in the spike when one-directional excursion >= entry(±X) + TP.
        # Real fills slip several points PAST the stop on a violent open (observed ~15pt), so a
        # thin margin is unreliable -> require SPIKE_SLIP_MARGIN of headroom for a "likely".
        tp = self.target_points
        entry_x = float(user_entry) if user_entry else float(spread)
        spike_needed = entry_x + tp
        max_entry = max(0.0, exp_spike - tp)         # widest ±X whose entry+TP still fits
        if exp_spike >= spike_needed + SPIKE_SLIP_MARGIN:
            spike_verdict = "likely"
        elif exp_spike >= spike_needed:
            spike_verdict = "marginal"
        else:
            spike_verdict = "unlikely"

        action = "SKIP" if exp_pnl <= 0 else "TRADE"
        conviction = ("high" if (winrate >= 0.6 and whip_rate <= 0.35 and exp_pnl >= 2) else
                      "medium" if (winrate >= 0.5 and exp_pnl > 0) else "low")
        whipsaw_risk = "low" if whip_rate < 0.35 else ("medium" if whip_rate < 0.55 else "high")
        confidence = ("high" if self.n_samples >= 120 else
                      "medium" if self.n_samples >= 40 else "low")
        rationale = (
            f"On the {len(nbrs)} most-similar mornings (of {self.n_samples}), entry ±{spread:.0f}"
            f"/stop {stop:.0f} (TP {self.target_points:.0f}) averaged {exp_pnl:+.1f} pts, "
            f"win-rate {winrate*100:.0f}%, whipsaw {whip_rate*100:.0f}%. "
            f"Expected 9:30 opening spike ~{exp_spike:.0f} pts ({spike_label}); to hit TP in the "
            f"candle needs ±{entry_x:.0f}+{tp:.0f}={spike_needed:.0f} pts -> {spike_verdict.upper()} "
            f"(slippage eats several pts past the stop). Widest ±entry that still fits: ±{max_entry:.0f}. "
            f"VIX {row.get('prior_vix', 0):.1f}, ATR14 {row.get('atr14', 0):.0f}, "
            f"events preopen={int(row.get('preopen_score', 0))} fomc={int(row.get('fomc', 0))}."
        )
        return MorningPlan(action, float(spread), float(stop), conviction, confidence,
                           whipsaw_risk, winrate, exp_pnl, "knn-policy", rationale,
                           expected_spike_points=float(exp_spike), spike_label=spike_label,
                           spike_needed_points=float(spike_needed), spike_verdict=spike_verdict,
                           max_entry_for_spike=float(max_entry))

    # ---- persistence ----
    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "feature_names", "means", "stds", "cells", "hist_rows", "hist_pnl",
            "hist_whip", "hist_impulse", "n_samples", "target_points")}

    @classmethod
    def from_dict(cls, d: dict) -> "MorningModel":
        m = cls()
        for k, v in d.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m


def morning_model_path() -> Path:
    return config_dir() / "analysis" / "morning_model.json"


def save_morning_model(model: MorningModel) -> None:
    p = morning_model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(model.to_dict()), encoding="utf-8")


def load_morning_model() -> Optional[MorningModel]:
    p = morning_model_path()
    if not p.exists():
        return None
    return MorningModel.from_dict(json.loads(p.read_text(encoding="utf-8")))
