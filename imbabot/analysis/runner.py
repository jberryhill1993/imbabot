"""Orchestration: calibrate the model from history, and run the daily recommendation.

``calibrate`` is the heavy, occasional job — backtest the cached 12 months and fit the
model. ``run_daily`` is the light pre-open job — assemble today's features from the
live overnight range + pre-open price + cached daily history, then recommend a spread
and write the report. Neither changes ``entry_points``; the number is advisory only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..config import config_dir
from .backtest import BacktestResult, BracketSpec, backtest, spread_grid
from .csv_history import load_records
from .features import feature_row, row_from_record
from .market_history import NQ_SYMBOL, VIX_SYMBOL, by_date, load_daily, prior_value, refresh
from .model import Recommendation, SpreadModel, load_model, save_model
from . import report as _report


@dataclass
class CalibrationResult:
    n_days: int
    best_spread: Optional[float]
    backtest: BacktestResult
    model: SpreadModel
    summary: str


def calibrate(
    symbol: str,
    *,
    bracket: BracketSpec,
    grid: Optional[List[float]] = None,
    refresh_daily: bool = True,
) -> CalibrationResult:
    """Backtest cached history + fit/save the spread model. Returns a summary."""
    records = load_records(symbol)
    if not records:
        raise RuntimeError(f"No cached history for {symbol}. Run ingest-history first.")
    if refresh_daily:
        vix = refresh(VIX_SYMBOL)
        nq = refresh(NQ_SYMBOL)
    else:
        vix, nq = load_daily(VIX_SYMBOL), load_daily(NQ_SYMBOL)
    vix_by_date = by_date(vix)

    grid = grid or spread_grid()
    bt = backtest(records, bracket=bracket, grid=grid)
    feature_rows = [row_from_record(r, vix_by_date, nq) for r in records]
    optimal = [bt.per_day_optimal[r.date] for r in records]
    model = SpreadModel().fit(feature_rows, optimal, bt)
    save_model(model)

    summary = _report.calibration_summary(len(records), bt.best_spread(),
                                           bt.per_spread, model.n_samples)
    return CalibrationResult(len(records), bt.best_spread(), bt, model, summary)


@dataclass
class DailyResult:
    date: str
    recommendation: Recommendation
    report_text: str
    report_path: Path


def reports_dir() -> Path:
    return config_dir() / "analysis" / "reports"


def run_daily(
    symbol: str,
    *,
    overnight_range: Optional[float],
    current_price: Optional[float],
    prior_close: Optional[float] = None,
    current_spread: Optional[float] = None,
    date: Optional[str] = None,
) -> DailyResult:
    """Produce today's recommendation from pre-open inputs; write the report."""
    model = load_model()
    if model is None:
        raise RuntimeError("No model. Run the 12-month calibration first.")
    nq = load_daily(NQ_SYMBOL)
    vix_by_date = by_date(load_daily(VIX_SYMBOL))
    date = date or datetime.now().astimezone().date().isoformat()

    # Estimate the open gap from the pre-open price vs the prior daily close.
    if prior_close is None:
        pv = prior_value(by_date(nq), date)
        prior_close = pv.c if pv else None
    gap = (current_price - prior_close) if (current_price is not None and prior_close) else None

    row = feature_row(date, overnight_range, gap, vix_by_date, nq)
    rec = model.recommend(row)
    text = _report.daily_report(date, rec, row, symbol=symbol, current_spread=current_spread)

    rd = reports_dir()
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / f"{date}.txt"
    path.write_text(text, encoding="utf-8")
    return DailyResult(date, rec, text, path)
