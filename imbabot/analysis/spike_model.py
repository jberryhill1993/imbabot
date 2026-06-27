"""Opening-spike predictor — fitted on the full-year tick dataset.

For today's pre-open features (VIX, VIX change, news impact, FOMC, day-of-week, recent realized
vol) it finds the most-similar PAST mornings and reads off:
- **expected spike S** (mean realized first-~2s thrust of the neighbours),
- **P(clean-winner)** = conviction the straddle wins (not a whipsaw),
- **P(big)** = chance of a 30+ pt opening spike — the days the user wants to capitalize on.

Until fit, an honest VIX/news heuristic stands in (``calibrated=False``). Predictions are made by
comparison to history exactly as the user asked: today's VIX+news vs every past day's VIX+news.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..config import config_dir
from .tick_dataset import FEATURES, DayRow, to_matrix

K_NEIGHBORS = 25


@dataclass
class SpikePrediction:
    expected_spike: float     # points (first ~2s thrust)
    p_clean: float            # P(straddle clean-winner)
    p_big: float              # P(30+ pt spike)
    n_neighbors: int
    calibrated: bool


@dataclass
class SpikeModel:
    calibrated: bool = False
    means: List[float] = field(default_factory=list)
    stds: List[float] = field(default_factory=list)
    X: List[List[float]] = field(default_factory=list)   # standardized history
    thrust: List[float] = field(default_factory=list)
    clean: List[int] = field(default_factory=list)
    big: List[int] = field(default_factory=list)
    n_days: int = 0
    # uncalibrated fallback coefficients
    vix_coef: float = 0.75
    news_coef: float = 3.0
    floor: float = 8.0
    cap: float = 60.0

    # ---- fit ----
    def fit(self, rows: List[DayRow]) -> "SpikeModel":
        if not rows:
            return self
        Xraw, _dates = to_matrix(rows)
        cols = list(zip(*Xraw))
        self.means = [statistics.fmean(c) for c in cols]
        self.stds = [(statistics.pstdev(c) or 1.0) for c in cols]
        self.X = [[(v - self.means[j]) / self.stds[j] for j, v in enumerate(row)] for row in Xraw]
        self.thrust = [r.thrust for r in rows]
        self.clean = [1 if r.label == "clean-winner" else 0 for r in rows]
        self.big = [r.is_big for r in rows]
        self.n_days = len(rows)
        self.calibrated = True
        return self

    def _z(self, feats: dict) -> List[float]:
        return [(feats[f] - self.means[j]) / self.stds[j] for j, f in enumerate(FEATURES)]

    # ---- predict ----
    def predict(self, feats: dict, k: int = K_NEIGHBORS) -> SpikePrediction:
        if not self.calibrated or not self.X:
            vix = feats.get("prior_vix", 17.0)
            s = max(self.floor, min(self.cap, self.vix_coef * vix + self.news_coef * feats.get("news_score", 0)))
            return SpikePrediction(round(s, 1), 0.0, 0.0, 0, False)
        z = self._z(feats)
        order = sorted(range(len(self.X)), key=lambda i: math.dist(z, self.X[i]))[:min(k, len(self.X))]
        S = statistics.fmean(self.thrust[i] for i in order)
        pc = statistics.fmean(self.clean[i] for i in order)
        pb = statistics.fmean(self.big[i] for i in order)
        return SpikePrediction(round(S, 1), round(pc, 3), round(pb, 3), len(order), True)

    # ---- persistence ----
    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("calibrated", "means", "stds", "X", "thrust", "clean", "big", "n_days",
                 "vix_coef", "news_coef", "floor", "cap")}

    @classmethod
    def from_dict(cls, d: dict) -> "SpikeModel":
        m = cls()
        for k, v in d.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m


def _path() -> Path:
    return config_dir() / "analysis" / "spike_model.json"


def save_spike_model(m: SpikeModel) -> None:
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m.to_dict()), encoding="utf-8")


def load_spike_model() -> SpikeModel:
    p = _path()
    if p.exists():
        try:
            return SpikeModel.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return SpikeModel()
