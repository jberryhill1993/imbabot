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


def morning_report(date, plan, row, *, symbol="NQ", sizing=None,
                   current_spread=None) -> str:
    """Format the full Morning Plan (spread + stop + conviction + trade/skip [+ sizing])."""
    head = "TRADE" if plan.action == "TRADE" else ">> SKIP TODAY <<"
    lines = [
        f"IMBABOT — Morning Plan for {date}  ({symbol})",
        "=" * 60,
        f"  ACTION:  {head}",
        f"  Recommended entry spread:  +/- {plan.spread:.0f} pts",
        f"  Recommended stop:          {plan.stop_points:.0f} pts",
        f"  Conviction: {plan.conviction.upper()}   Whipsaw risk: {plan.whipsaw_risk.upper()}"
        f"   Confidence: {plan.confidence.upper()} ({plan.method})",
        f"  Predicted win-rate ~{plan.predicted_winrate*100:.0f}%, "
        f"avg best ~{plan.expected_pnl_points:+.1f} pts",
    ]
    if current_spread is not None and abs(current_spread - plan.spread) >= 1:
        lines.append(f"  (Your current spread +/- {current_spread:.0f} -> consider {plan.spread:.0f})")
    if sizing is not None:
        lines += [
            "",
            "  Profit-target sizing:",
            f"    Target ${sizing.target_dollars:,.0f}  ->  {sizing.contracts} contract(s)"
            + ("  (capped)" if sizing.capped else ""),
            f"    Set TopStep TP ${sizing.tp_bracket_dollars:,.0f} / SL ${sizing.sl_bracket_dollars:,.0f}",
            f"    Winning morning ~+${sizing.gross_win_dollars:,.0f}; "
            f"stopped morning ~-${sizing.downside_dollars:,.0f}; EV ~${sizing.expected_value_dollars:,.0f}",
            f"    {sizing.note}",
        ]
    lines += [
        "",
        f"  Reasoning: {plan.rationale}",
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
