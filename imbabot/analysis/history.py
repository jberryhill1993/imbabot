"""Historical 1-minute bar access for the spread analyzer.

Phase 0 lives here: ``probe_depth`` answers the gating question — *how far back
does TopStep/ProjectX actually serve 1-minute bars?* The whole backtest depends on
~12 months of intraday history, and TopStep's retention is undocumented, so we
measure it before building on it.

The probe is strictly read-only (it only calls ``retrieve_bars``) and works against
any client exposing the ``retrieve_bars`` surface — the real ``ProjectXClient`` or
the offline ``FakeClient`` — so it is unit-testable without a network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

# Lookback checkpoints (days back from "now") the probe tests, shallow -> deep.
# ~18 months of checkpoints so we can confirm a full 12-month window is reachable.
_CHECKPOINTS_DAYS = [7, 30, 60, 90, 120, 150, 180, 240, 270, 300, 365, 455, 545]


@dataclass
class ProbeResult:
    """Outcome of a history-depth probe."""

    deepest_days: Optional[int]          # furthest-back checkpoint that returned bars
    earliest_bar_t: Optional[str]        # ISO timestamp of the oldest bar seen
    enough_for_backtest: bool            # True if >= ~12 months reachable
    checkpoints: List[dict] = field(default_factory=list)

    def summary(self) -> str:
        if self.deepest_days is None:
            return "No 1-minute history returned at any checkpoint (probe found nothing)."
        months = self.deepest_days / 30.0
        verdict = "ENOUGH for the 12-month backtest" if self.enough_for_backtest else (
            "SHALLOW — not enough for 12 months; consider the CSV-ingest fallback")
        lines = [
            f"Deepest 1-min data reached: ~{self.deepest_days} days back (~{months:.1f} months).",
            f"Oldest bar observed: {self.earliest_bar_t or 'n/a'}.",
            f"Verdict: {verdict}.",
            "",
            "Checkpoints (days back -> bars returned):",
        ]
        for c in self.checkpoints:
            mark = "ok " if c.get("ok") else "—  "
            extra = c.get("error")
            note = f"  bars={c.get('count', 0)}" + (f"  ERROR: {extra}" if extra else "")
            lines.append(f"  {mark}{c['days_back']:>4}d{note}")
        return "\n".join(lines)


def probe_depth(
    client: Any,
    contract_id: str,
    *,
    live: bool = False,
    enough_days: int = 365,
    now: Optional[datetime] = None,
) -> ProbeResult:
    """Measure how far back ``client`` serves 1-minute bars for ``contract_id``.

    Requests a small 1-minute window at each checkpoint (a liquid mid-US-morning
    weekday time in UTC, where index futures always trade) and records whether bars
    came back. The furthest checkpoint that still returns data approximates the
    retention depth.

    ``enough_days`` (default 365) is the threshold for ``enough_for_backtest``.
    Read-only; never places or cancels anything.
    """
    now = now or datetime.now(timezone.utc)
    checkpoints: List[dict] = []
    deepest: Optional[int] = None
    earliest: Optional[str] = None

    for days in _CHECKPOINTS_DAYS:
        anchor = now - timedelta(days=days)
        # Nudge onto a weekday so the window lands on a trading session.
        while anchor.weekday() >= 5:  # Sat=5, Sun=6
            anchor -= timedelta(days=1)
        # 15:00–16:00 UTC = mid US morning in both EST and EDT; futures are liquid.
        start = anchor.replace(hour=15, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        entry: dict = {"days_back": days, "window_start": start.isoformat()}
        try:
            bars = client.retrieve_bars(
                contract_id, unit=2, unit_number=1, limit=5000,
                start_time=start, end_time=end, include_partial_bar=False, live=live,
            )
            count = len(bars)
            entry["ok"] = count > 0
            entry["count"] = count
            if count > 0:
                deepest = days
                oldest_t = min(b.t for b in bars)
                if earliest is None or oldest_t < earliest:
                    earliest = oldest_t
        except Exception as exc:  # network/auth/permission — record, keep probing
            entry["ok"] = False
            entry["count"] = 0
            entry["error"] = str(exc)
        checkpoints.append(entry)

    return ProbeResult(
        deepest_days=deepest,
        earliest_bar_t=earliest,
        enough_for_backtest=bool(deepest is not None and deepest >= enough_days),
        checkpoints=checkpoints,
    )
