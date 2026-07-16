"""Spread recommendation model.

Fits ``optimal_spread = f(features)`` from the backtest, using pure-Python ridge
regression (no numpy). With few samples the regression is unreliable, so a k-NN
"similar days" estimate is the robust primary until enough history accumulates;
regression takes over once the sample is large. Either way the output is clamped
to the spread grid and paired with a historical **whipsaw risk** for that spread.

Persisted to ``config_dir()/analysis/model.json`` so the daily runner loads it
without recomputing the 12-month backtest.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..config import config_dir
from .backtest import BacktestResult
from .features import FEATURE_NAMES, to_vector

MIN_FIT_SAMPLES = 30      # below this, k-NN is the primary estimate
RIDGE_LAMBDA = 1.0        # ridge penalty on standardized features
K_NEIGHBORS = 10


# --------------------------------------------------------------- linear algebra
def _solve(A: List[List[float]], b: List[float]) -> List[float]:
    """Solve A x = b for small systems via Gaussian elimination w/ partial pivot."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            continue
        M[col], M[piv] = M[piv], M[col]
        pivval = M[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col] / pivval
            for c in range(col, n + 1):
                M[r][c] -= factor * M[col][c]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def _standardize(rows: List[List[float]]):
    """Return (means, stds) per column; std=1 where a column is constant."""
    k = len(rows[0])
    means = [statistics.fmean(r[j] for r in rows) for j in range(k)]
    stds = []
    for j in range(k):
        sd = statistics.pstdev(r[j] for r in rows) if len(rows) > 1 else 0.0
        stds.append(sd if sd > 1e-9 else 1.0)
    return means, stds


# --------------------------------------------------------------------- model
@dataclass
class Recommendation:
    spread: float
    whipsaw_risk: str        # "low" | "medium" | "high"
    confidence: str          # "low" | "medium" | "high"
    method: str              # "regression" | "knn"
    regression_spread: Optional[float]
    knn_spread: Optional[float]
    rationale: str


@dataclass
class SpreadModel:
    feature_names: List[str] = field(default_factory=lambda: list(FEATURE_NAMES))
    means: List[float] = field(default_factory=list)
    stds: List[float] = field(default_factory=list)
    weights: List[float] = field(default_factory=list)   # on standardized features
    y_mean: float = 0.0
    n_samples: int = 0
    spread_min: float = 6.0
    spread_max: float = 30.0
    whipsaw_by_spread: Dict[str, float] = field(default_factory=dict)  # str(spread)->rate
    grid: List[float] = field(default_factory=list)
    # k-NN store: standardized history rows + their optimal spreads
    hist_rows: List[List[float]] = field(default_factory=list)
    hist_optimal: List[float] = field(default_factory=list)

    # ---- fit ----
    def fit(self, feature_rows: List[Dict[str, float]], optimal: List[float],
            backtest: BacktestResult) -> "SpreadModel":
        X = [to_vector(r) for r in feature_rows]
        self.n_samples = len(X)
        self.grid = list(backtest.grid)
        self.spread_min, self.spread_max = min(self.grid), max(self.grid)
        self.whipsaw_by_spread = {str(s): backtest.per_spread[s].whipsaw_rate
                                  for s in backtest.grid}
        self.y_mean = statistics.fmean(optimal) if optimal else 0.0
        if not X:
            return self
        self.means, self.stds = _standardize(X)
        Xs = [[(r[j] - self.means[j]) / self.stds[j] for j in range(len(r))] for r in X]
        self.hist_rows = Xs
        self.hist_optimal = list(optimal)
        # Ridge normal equations: (Xs^T Xs + λI) w = Xs^T (y - y_mean)
        k = len(FEATURE_NAMES)
        yc = [o - self.y_mean for o in optimal]
        A = [[sum(Xs[r][i] * Xs[r][j] for r in range(len(Xs))) + (RIDGE_LAMBDA if i == j else 0.0)
              for j in range(k)] for i in range(k)]
        b = [sum(Xs[r][i] * yc[r] for r in range(len(Xs))) for i in range(k)]
        self.weights = _solve(A, b)
        return self

    # ---- predict ----
    def _standardize_one(self, row: Dict[str, float]) -> List[float]:
        v = to_vector(row)
        return [(v[j] - self.means[j]) / self.stds[j] for j in range(len(v))]

    def _regression(self, zrow: List[float]) -> float:
        return self.y_mean + sum(w * z for w, z in zip(self.weights, zrow))

    def _knn(self, zrow: List[float]) -> Optional[float]:
        if not self.hist_rows:
            return None
        dists = []
        for i, hr in enumerate(self.hist_rows):
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(zrow, hr)))
            dists.append((d, self.hist_optimal[i]))
        dists.sort(key=lambda x: x[0])
        near = [o for _, o in dists[:min(K_NEIGHBORS, len(dists))]]
        return statistics.fmean(near)

    def _clamp_round(self, x: float) -> float:
        x = max(self.spread_min, min(self.spread_max, x))
        return float(round(x))

    def _whipsaw_at(self, spread: float) -> float:
        if not self.grid:
            return 0.0
        nearest = min(self.grid, key=lambda g: abs(g - spread))
        return self.whipsaw_by_spread.get(str(nearest), 0.0)

    def recommend(self, row: Dict[str, float]) -> Recommendation:
        zrow = self._standardize_one(row) if self.means else None
        reg = self._clamp_round(self._regression(zrow)) if (zrow and self.weights) else None
        knn = self._knn(zrow) if zrow else None
        knn_r = self._clamp_round(knn) if knn is not None else None

        if self.n_samples >= MIN_FIT_SAMPLES and reg is not None:
            spread, method = reg, "regression"
        elif knn_r is not None:
            spread, method = knn_r, "knn"
        else:
            spread, method = self._clamp_round(self.y_mean), "knn"

        wr = self._whipsaw_at(spread)
        whipsaw_risk = "low" if wr < 0.25 else ("medium" if wr < 0.45 else "high")
        confidence = ("high" if self.n_samples >= 120 else
                      "medium" if self.n_samples >= MIN_FIT_SAMPLES else "low")
        rationale = (
            f"VIX {row.get('prior_vix', 0):.1f} (Δ{row.get('vix_change', 0):+.1f}), "
            f"overnight range {row.get('overnight_range', 0):.0f}, "
            f"ATR14 {row.get('atr14', 0):.0f}, "
            f"events: preopen={int(row.get('preopen_score', 0))} fomc={int(row.get('fomc', 0))}. "
            f"Backtest whipsaw at ±{spread:.0f}: {wr*100:.0f}%. "
            f"(regression={reg}, similar-days={knn_r}, n={self.n_samples})"
        )
        return Recommendation(spread, whipsaw_risk, confidence, method, reg, knn_r, rationale)

    # ---- persistence ----
    def to_dict(self) -> dict:
        return {
            "feature_names": self.feature_names, "means": self.means, "stds": self.stds,
            "weights": self.weights, "y_mean": self.y_mean, "n_samples": self.n_samples,
            "spread_min": self.spread_min, "spread_max": self.spread_max,
            "whipsaw_by_spread": self.whipsaw_by_spread, "grid": self.grid,
            "hist_rows": self.hist_rows, "hist_optimal": self.hist_optimal,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpreadModel":
        m = cls()
        for k, v in d.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m


def model_path() -> Path:
    return config_dir() / "analysis" / "model.json"


def save_model(model: SpreadModel) -> None:
    path = model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.to_dict()), encoding="utf-8")


def load_model() -> Optional[SpreadModel]:
    path = model_path()
    if not path.exists():
        return None
    return SpreadModel.from_dict(json.loads(path.read_text(encoding="utf-8")))
