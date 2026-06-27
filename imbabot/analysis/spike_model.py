"""Predicted opening-spike magnitude S (points) — the input the sizing/spread plan needs.

The honest design: on the FULL tick history this is *fit* (VIX + scheduled-news + recent
tick features → expected clean directional thrust) and walk-forward validated. Until that
data exists it is a transparent, **uncalibrated** VIX/news-scaled estimate — clearly flagged
so no false confidence leaks into the Morning Plan.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import config_dir


@dataclass
class SpikeModel:
    calibrated: bool = False
    vix_coef: float = 0.75       # placeholder: ~0.75 pt of opening thrust per VIX point
    news_coef: float = 3.0       # + per scheduled-catalyst score
    floor: float = 8.0
    cap: float = 40.0
    n_days: int = 0

    def predict(self, prior_vix: Optional[float], news_score: int = 0) -> float:
        """Expected clean directional opening spike, in points."""
        if prior_vix is None:
            return self.floor
        s = self.vix_coef * prior_vix + self.news_coef * max(0, news_score)
        return round(max(self.floor, min(self.cap, s)), 1)

    # ---- persistence (so a future fit() survives) ----
    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("calibrated", "vix_coef", "news_coef", "floor", "cap", "n_days")}

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
    """Load the fitted model, or the uncalibrated placeholder if none exists."""
    p = _path()
    if p.exists():
        try:
            return SpikeModel.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return SpikeModel()   # uncalibrated default
