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
from .backtest import (BacktestResult, BracketSpec, Backtest2D, CostSpec, backtest,
                       backtest_2d, spread_grid, stop_grid)
from .csv_history import load_records
from .features import feature_row, opening_impulse, row_from_record
from .market_history import NQ_SYMBOL, VIX_SYMBOL, by_date, load_daily, prior_value, refresh
from .model import Recommendation, SpreadModel, load_model, save_model
from .morning import MorningModel, MorningPlan, load_morning_model, save_morning_model
from .sizing import size_for_target
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


# ------------------------------------------------ morning model (2-D: spread x stop)
def _is_fine_grained(records) -> bool:
    """1-second data has large second-offsets; 1-minute data has tiny minute-offsets."""
    return any(b.minute > 30 for r in records for b in r.open_bars)


@dataclass
class MorningCalibration:
    n_days: int
    best_cell: Optional[tuple]
    backtest: Backtest2D
    model: MorningModel
    summary: str


def calibrate_morning(
    symbol: str,
    *,
    tp_points: float = 13.3,
    slippage_points: float = 2.0,
    commission_points: float = 0.13,
    spike_window_seconds: int = 3,
    refresh_daily: bool = True,
) -> MorningCalibration:
    """Backtest cached history over spread x stop (NET of costs), fit + save the model.

    Also measures each day's opening spike (first ``spike_window_seconds`` of the open) so
    the Morning Plan can flag the expected 9:30 swing. Advisory only — never changes the bot.
    """
    records = load_records(symbol)
    if not records:
        raise RuntimeError(f"No cached history for {symbol}. Run ingest-history first.")
    vix = refresh(VIX_SYMBOL) if refresh_daily else load_daily(VIX_SYMBOL)
    nq = refresh(NQ_SYMBOL) if refresh_daily else load_daily(NQ_SYMBOL)
    vix_by_date = by_date(vix)

    fine = _is_fine_grained(records)
    costs = CostSpec(slippage_points=slippage_points, commission_points=commission_points)
    # Coarse step-2 grids keep the per-day-cell matrix the k-NN policy stores compact.
    bt = backtest_2d(records, target_points=tp_points, spreads=spread_grid(6, 28, 2),
                     stops=stop_grid(4, 20, 2), fine_grained=fine, costs=costs)
    dates = [r.date for r in records]
    feature_rows = [row_from_record(r, vix_by_date, nq) for r in records]
    impulses = [opening_impulse(r, spike_window_seconds) for r in records]
    model = MorningModel().fit(feature_rows, dates, bt, impulses=impulses)
    save_morning_model(model)

    bc = bt.best_cell()
    res = "1-second (exact whipsaw)" if fine else "1-minute (estimated whipsaw)"
    profitable = sum(1 for d in dates if bt.per_day_best[d].pnl_points > 0)
    summary = (f"Morning calibration: {len(records)} days, {res}, NET of "
               f"{slippage_points:.1f}pt slippage + {commission_points:.2f}pt commission. "
               f"Best static cell spread/stop = {bc}. Net-positive days at best cell: "
               f"{profitable}/{len(dates)} ({profitable*100//max(1,len(dates))}%). "
               f"Model n={model.n_samples}.")
    return MorningCalibration(len(records), bc, bt, model, summary)


@dataclass
class MorningResult:
    date: str
    plan: MorningPlan
    sizing: object  # SizingPlan | None
    report_text: str
    report_path: Path


def run_morning(
    symbol: str,
    *,
    overnight_range: Optional[float] = None,
    current_price: Optional[float] = None,
    current_spread: Optional[float] = None,
    target_dollars: Optional[float] = None,
    dollars_per_point: float = 20.0,
    max_contracts: int = 10,
    min_spread: float = 0.0,
    date: Optional[str] = None,
) -> MorningResult:
    """Produce today's Morning Plan (+ optional sizing) from pre-open inputs."""
    model = load_morning_model()
    if model is None:
        raise RuntimeError("No morning model. Run calibrate-morning first.")
    nq = load_daily(NQ_SYMBOL)
    vix_by_date = by_date(load_daily(VIX_SYMBOL))
    date = date or datetime.now().astimezone().date().isoformat()

    if current_price is not None:
        pv = prior_value(by_date(nq), date)
        gap = (current_price - pv.c) if pv else None
    else:
        gap = None
    row = feature_row(date, overnight_range, gap, vix_by_date, nq)
    plan = model.recommend(row, min_spread=min_spread)

    sizing = None
    if target_dollars:
        sizing = size_for_target(
            target_dollars, tp_points=model.target_points, stop_points=plan.stop_points,
            dollars_per_point=dollars_per_point, winrate=plan.predicted_winrate,
            max_contracts=max_contracts)

    text = _report.morning_report(date, plan, row, symbol=symbol, sizing=sizing,
                                  current_spread=current_spread)
    rd = reports_dir()
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / f"{date}-morning.txt"
    path.write_text(text, encoding="utf-8")
    return MorningResult(date, plan, sizing, text, path)


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
