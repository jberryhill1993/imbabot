"""Opening-spike metrics, day label, and volatility level — from tick data.

- **Spike geometry** (from the tick path after the open): the directional *thrust* (how far
  the dominant move reached from the reference) and the *counter-poke* (how far price went
  the OTHER way before that thrust) — the counter-poke is what whipsaws a tight straddle.
- **Day label** (what the straddle actually did): ``clean-winner`` (a directional move the
  straddle rode to TP), ``whipsaw`` (triggered then stopped), or ``no-trade``.
- **Volatility level** LOW/MED/HIGH from prior VIX + a scheduled-news bump (known pre-open).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .tick_data import TickDay
from .tick_sim import TickOutcome


@dataclass
class SpikeMetrics:
    ref: float
    thrust: float            # dominant directional excursion from ref (points)
    direction: str           # "up" | "down"
    counter_poke: float      # max opposite excursion BEFORE the thrust peak (points)
    time_to_thrust_s: float  # seconds from the open to the thrust extreme


def spike_metrics(td: TickDay, *, capture_offset: float = -3.0,
                  window_s: float = 60.0) -> Optional[SpikeMetrics]:
    """Measure the opening spike over the first ``window_s`` seconds after the open."""
    ref = td.price_at(capture_offset)
    post = [tk for tk in td.ticks if 0.0 <= tk.t <= window_s]
    if ref is None or not post:
        return None
    hi = max(post, key=lambda x: x.price)
    lo = min(post, key=lambda x: x.price)
    up, down = hi.price - ref, ref - lo.price
    if up >= down:
        direction, thrust, peak_t = "up", up, hi.t
        counter = max((ref - tk.price for tk in post if tk.t <= peak_t), default=0.0)
    else:
        direction, thrust, peak_t = "down", down, lo.t
        counter = max((tk.price - ref for tk in post if tk.t <= peak_t), default=0.0)
    return SpikeMetrics(ref, round(thrust, 2), direction, round(max(0.0, counter), 2), round(peak_t, 3))


def label_day(outcome: TickOutcome) -> str:
    """Classify the day by what the straddle did: clean-winner / whipsaw / no-trade."""
    if not outcome.triggered:
        return "no-trade"
    if outcome.resolved == "target":
        return "clean-winner"
    if outcome.resolved == "stop":
        return "whipsaw"
    return "no-trade"          # window-flatten: neither TP nor SL — treat as no clean spike


# VIX bands for the morning volatility level (calibrate on full history; sensible defaults).
VIX_LOW, VIX_HIGH = 15.0, 22.0


def volatility_level(prior_vix: Optional[float], news_score: int = 0) -> str:
    """LOW / MEDIUM / HIGH for the morning. Driven by prior VIX (known pre-open); a
    high-impact scheduled release bumps it up one band."""
    if prior_vix is None:
        return "UNKNOWN"
    base = "LOW" if prior_vix < VIX_LOW else ("MEDIUM" if prior_vix < VIX_HIGH else "HIGH")
    if news_score >= 2:        # CPI / NFP / FOMC-class catalyst
        base = {"LOW": "MEDIUM", "MEDIUM": "HIGH", "HIGH": "HIGH"}[base]
    return base
