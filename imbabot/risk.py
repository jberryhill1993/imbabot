"""Client-side risk guardrails.

These are a *backup*, not the primary safety net. The README explains the
platform-side guards (daily loss limit + liquidate, trade limit) you should also
set in TopstepX — those are enforced by the broker even if this software crashes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from .config import Settings, config_dir


class RiskError(RuntimeError):
    """Raised when a guardrail blocks an action."""


@dataclass
class RiskGuard:
    settings: Settings

    def _counter_path(self) -> Path:
        return config_dir() / "trade_count.json"

    def _today_count(self) -> int:
        path = self._counter_path()
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        if data.get("date") != date.today().isoformat():
            return 0
        return int(data.get("count", 0))

    def record_trade(self) -> None:
        path = self._counter_path()
        path.write_text(
            json.dumps({"date": date.today().isoformat(), "count": self._today_count() + 1}),
            encoding="utf-8",
        )

    def check_can_arm(self, account_can_trade: bool) -> None:
        """Validate static config + account state before arming. Raises RiskError."""
        s = self.settings
        if not account_can_trade:
            raise RiskError("Selected account has canTrade=false (locked or restricted).")
        if s.contracts < 1:
            raise RiskError("Contracts must be >= 1.")
        if s.contracts > s.max_contracts:
            raise RiskError(
                f"Contracts ({s.contracts}) exceeds the safety cap "
                f"max_contracts={s.max_contracts}. Raise the cap deliberately if intended."
            )
        if s.entry_points <= 0 or s.stop_loss_points <= 0 or s.take_profit_points <= 0:
            raise RiskError("Points (entry/stop/target) must all be > 0.")
        count = self._today_count()
        if count >= s.max_trades_per_day:
            raise RiskError(
                f"Daily trade limit reached ({count}/{s.max_trades_per_day}). "
                "Reset is automatic at the next calendar day."
            )

    def check_can_send_orders(self) -> None:
        """Final gate right before live order placement."""
        s = self.settings
        if s.dry_run:
            raise RiskError("dry_run is enabled — order sending is blocked by design.")
        if s.contracts > s.max_contracts:
            raise RiskError("Contract size exceeds safety cap; refusing to send.")
