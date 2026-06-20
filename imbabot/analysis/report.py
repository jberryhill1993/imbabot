"""Human-readable reports for the spread analyzer."""
from __future__ import annotations

from typing import Dict

from .model import Recommendation

DISCLAIMER = (
    "Informational only — NOT financial advice. This is a statistical estimate from "
    "historical data; it does not predict the market. You decide the spread; the bot "
    "never changes it automatically. Past performance does not guarantee future results."
)


def daily_report(date: str, rec: Recommendation, row: Dict[str, float],
                 *, symbol: str = "NQ", current_spread: float | None = None) -> str:
    lines = [
        f"IMBABOT — Recommended spread for {date}  ({symbol})",
        "=" * 60,
        f"  RECOMMENDED ENTRY SPREAD:  +/- {rec.spread:.0f} points",
        f"  Whipsaw risk at this spread: {rec.whipsaw_risk.upper()}",
        f"  Confidence: {rec.confidence.upper()}   (method: {rec.method})",
    ]
    if current_spread is not None and abs(current_spread - rec.spread) >= 1:
        lines.append(f"  Your current setting: +/- {current_spread:.0f}  "
                     f"-> consider {rec.spread:.0f}")
    lines += [
        "",
        "  Inputs:",
        f"    Prior VIX        {row.get('prior_vix', 0):.2f}  (change {row.get('vix_change', 0):+.2f})",
        f"    Overnight range  {row.get('overnight_range', 0):.0f} pts",
        f"    Est. open gap    {row.get('gap_abs', 0):.0f} pts",
        f"    NQ 14d ATR       {row.get('atr14', 0):.0f} pts",
        f"    Pre-open events  score {int(row.get('preopen_score', 0))}"
        f"{'  + FOMC day' if row.get('fomc') else ''}",
        "",
        "  Reasoning:",
        f"    {rec.rationale}",
        "",
        "  " + DISCLAIMER,
    ]
    return "\n".join(lines)


def calibration_summary(n_days: int, best_spread, per_spread: dict, model_n: int) -> str:
    lines = [
        f"Calibration complete: {n_days} trading days.",
        f"  Backtest best single spread (max mean P&L): +/- {best_spread} pts",
        f"  Model fit on n={model_n} samples"
        f"{'  (LOW confidence — needs ~250 days)' if model_n < 120 else ''}.",
        "",
        "  Spread sweep (points -> mean P&L / trigger% / whipsaw%):",
    ]
    for sp in sorted(per_spread):
        st = per_spread[sp]
        lines.append(f"    +/-{sp:>4}:  {st.mean_pnl:+7.2f}   "
                     f"trig {st.trigger_rate*100:4.0f}%   whip {st.whipsaw_rate*100:4.0f}%")
    return "\n".join(lines)
