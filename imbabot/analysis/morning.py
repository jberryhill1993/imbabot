"""Morning plan model — predicts spread, stop, conviction, and trade/skip.

Trains on the 2-D backtest (`backtest_2d`): the per-day optimal (spread, stop) and the
day's outcome at that cell. Predicts, from today's pre-open features, the recommended
**spread** and **stop** (pure-Python ridge, falling back to k-NN when samples are few),
plus a **conviction** and **TRADE/SKIP** call derived from how the most-similar past
mornings actually resolved. Advisory only — the bot never applies these.

Persisted to `config_dir()/analysis/morning_model.json`.
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
from .model import _solve, _standardize, MIN_FIT_SAMPLES, RIDGE_LAMBDA, K_NEIGHBORS


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
    method: str              # "regression" | "knn"
    rationale: str


def _ridge_fit(Xs: List[List[float]], y: List[float], k: int) -> tuple:
    """Ridge on standardized features -> (weights, y_mean)."""
    y_mean = statistics.fmean(y) if y else 0.0
    if not Xs:
        return [0.0] * k, y_mean
    yc = [v - y_mean for v in y]
    A = [[sum(Xs[r][i] * Xs[r][j] for r in range(len(Xs))) + (RIDGE_LAMBDA if i == j else 0.0)
          for j in range(k)] for i in range(k)]
    b = [sum(Xs[r][i] * yc[r] for r in range(len(Xs))) for i in range(k)]
    return _solve(A, b), y_mean


@dataclass
class MorningModel:
    feature_names: List[str] = field(default_factory=lambda: list(FEATURE_NAMES))
    means: List[float] = field(default_factory=list)
    stds: List[float] = field(default_factory=list)
    w_spread: List[float] = field(default_factory=list)
    w_stop: List[float] = field(default_factory=list)
    spread_mean: float = 0.0
    stop_mean: float = 0.0
    n_samples: int = 0
    spread_lo: float = 6.0
    spread_hi: float = 30.0
    stop_lo: float = 4.0
    stop_hi: float = 20.0
    target_points: float = 13.0
    # k-NN store (standardized rows + per-day optimal + that day's outcome)
    hist_rows: List[List[float]] = field(default_factory=list)
    hist_spread: List[float] = field(default_factory=list)
    hist_stop: List[float] = field(default_factory=list)
    hist_pnl: List[float] = field(default_factory=list)
    hist_whip: List[int] = field(default_factory=list)

    # ---- fit ----
    def fit(self, feature_rows: List[Dict[str, float]], dates: List[str],
            bt: Backtest2D) -> "MorningModel":
        self.n_samples = len(feature_rows)
        self.target_points = bt.target_points
        if bt.spreads:
            self.spread_lo, self.spread_hi = min(bt.spreads), max(bt.spreads)
        if bt.stops:
            self.stop_lo, self.stop_hi = min(bt.stops), max(bt.stops)
        if not feature_rows:
            return self
        X = [to_vector(r) for r in feature_rows]
        self.means, self.stds = _standardize(X)
        Xs = [[(r[j] - self.means[j]) / self.stds[j] for j in range(len(r))] for r in X]
        k = len(FEATURE_NAMES)
        sp = [bt.per_day_optimal[d][0] for d in dates]
        st = [bt.per_day_optimal[d][1] for d in dates]
        self.w_spread, self.spread_mean = _ridge_fit(Xs, sp, k)
        self.w_stop, self.stop_mean = _ridge_fit(Xs, st, k)
        self.hist_rows = Xs
        self.hist_spread, self.hist_stop = sp, st
        self.hist_pnl = [bt.per_day_best[d].pnl_points for d in dates]
        self.hist_whip = [int(bt.per_day_best[d].whipsaw) for d in dates]
        return self

    # ---- predict ----
    def _z(self, row: Dict[str, float]) -> List[float]:
        v = to_vector(row)
        return [(v[j] - self.means[j]) / self.stds[j] for j in range(len(v))]

    def _reg(self, z, w, mean):
        return mean + sum(wi * zi for wi, zi in zip(w, z))

    def _neighbors(self, z, k=K_NEIGHBORS):
        d = sorted(range(len(self.hist_rows)),
                   key=lambda i: math.dist(z, self.hist_rows[i]))
        return d[:min(k, len(d))]

    def recommend(self, row: Dict[str, float]) -> MorningPlan:
        if not self.means:
            return MorningPlan("SKIP", self.spread_lo, self.stop_lo, "low", "low", "high",
                               0.0, 0.0, "knn", "No model fitted yet.")
        z = self._z(row)
        nbrs = self._neighbors(z)
        knn_sp = statistics.fmean(self.hist_spread[i] for i in nbrs) if nbrs else self.spread_mean
        knn_st = statistics.fmean(self.hist_stop[i] for i in nbrs) if nbrs else self.stop_mean
        use_reg = self.n_samples >= MIN_FIT_SAMPLES
        spread = self._reg(z, self.w_spread, self.spread_mean) if use_reg else knn_sp
        stop = self._reg(z, self.w_stop, self.stop_mean) if use_reg else knn_st
        spread = float(round(max(self.spread_lo, min(self.spread_hi, spread))))
        stop = float(round(max(self.stop_lo, min(self.stop_hi, stop))))

        # Outcome expectations from the most-similar past mornings.
        pnl = [self.hist_pnl[i] for i in nbrs]
        whip = [self.hist_whip[i] for i in nbrs]
        winrate = statistics.fmean(1.0 if p > 0 else 0.0 for p in pnl) if pnl else 0.0
        exp_pnl = statistics.fmean(pnl) if pnl else 0.0
        whip_rate = statistics.fmean(whip) if whip else 0.0

        action = "SKIP" if (exp_pnl <= 0 or whip_rate >= 0.6) else "TRADE"
        conviction = ("high" if (winrate >= 0.65 and whip_rate <= 0.25) else
                      "medium" if (winrate >= 0.5 and whip_rate <= 0.45) else "low")
        whipsaw_risk = "low" if whip_rate < 0.25 else ("medium" if whip_rate < 0.45 else "high")
        confidence = ("high" if self.n_samples >= 120 else
                      "medium" if self.n_samples >= MIN_FIT_SAMPLES else "low")
        method = "regression" if use_reg else "knn"
        rationale = (
            f"Similar mornings (n={len(nbrs)} of {self.n_samples}): win-rate {winrate*100:.0f}%, "
            f"whipsaw {whip_rate*100:.0f}%, avg best ~{exp_pnl:+.1f} pts. "
            f"Entry ±{spread:.0f}, stop {stop:.0f} (TP {self.target_points:.0f}). "
            f"VIX {row.get('prior_vix', 0):.1f}, ATR14 {row.get('atr14', 0):.0f}, "
            f"events preopen={int(row.get('preopen_score', 0))} fomc={int(row.get('fomc', 0))}."
        )
        return MorningPlan(action, spread, stop, conviction, confidence, whipsaw_risk,
                           winrate, exp_pnl, method, rationale)

    # ---- persistence ----
    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "feature_names", "means", "stds", "w_spread", "w_stop", "spread_mean",
            "stop_mean", "n_samples", "spread_lo", "spread_hi", "stop_lo", "stop_hi",
            "target_points", "hist_rows", "hist_spread", "hist_stop", "hist_pnl", "hist_whip")}

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
