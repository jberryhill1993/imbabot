"""Fire-time scheduling, anchored to the US cash-equity open (09:30 America/New_York).

The bot captures the reference price a few seconds *before* the open
(``capture_offset_seconds``, default 3 -> 09:29:57) and treats that as the fire
moment. Times are computed in the market's own timezone so DST is handled for you.
"""
from __future__ import annotations

import threading
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

MARKET_TZ = "America/New_York"


def _tz(name: str):
    if ZoneInfo is None:
        raise RuntimeError(
            "zoneinfo unavailable. On Windows, `pip install tzdata`."
        )
    return ZoneInfo(name)


def next_fire_time(
    open_time: dtime,
    capture_offset_seconds: int = 3,
    market_tz: str = MARKET_TZ,
    now: Optional[datetime] = None,
) -> datetime:
    """Return the next fire datetime (tz-aware, in ``market_tz``).

    Fire == market open minus ``capture_offset_seconds``. If today's fire time has
    already passed, rolls to tomorrow. Note: this does not skip weekends/holidays;
    the daily morning routine is a deliberate human-in-the-loop step.
    """
    tz = _tz(market_tz)
    now = now.astimezone(tz) if now else datetime.now(tz)

    open_today = now.replace(
        hour=open_time.hour,
        minute=open_time.minute,
        second=open_time.second,
        microsecond=0,
    )
    fire_today = open_today - timedelta(seconds=capture_offset_seconds)
    if fire_today <= now:
        fire_today = fire_today + timedelta(days=1)
    return fire_today


def parse_hms(text: str) -> dtime:
    """Parse 'HH:MM' or 'HH:MM:SS' into a time. Raises ValueError if malformed."""
    parts = [int(p) for p in text.strip().split(":")]
    if not (1 <= len(parts) <= 3):
        raise ValueError(f"bad time {text!r}; use HH:MM or HH:MM:SS")
    h = parts[0]
    m = parts[1] if len(parts) > 1 else 0
    s = parts[2] if len(parts) > 2 else 0
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError(f"time out of range: {text!r}")
    return dtime(h, m, s)


def next_local_fire(time_str: str, now: Optional[datetime] = None) -> datetime:
    """Next occurrence of a wall-clock time in the MACHINE's local timezone.

    Used by Test mode so you can set, say, 19:50 and have it fire at 7:50pm your time.
    """
    t = parse_hms(time_str)
    now = now or datetime.now().astimezone()  # local, tz-aware
    target = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def seconds_until(target: datetime, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(target.tzinfo)
    return (target - now).total_seconds()


def format_countdown(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class FireTimer:
    """Waits until a target time, then runs a callback — on a cancellable thread.

    ``on_tick`` (if given) is called roughly once a second with the remaining
    seconds so a UI can render a live countdown. ``disarm()`` cancels cleanly.
    """

    def __init__(
        self,
        target: datetime,
        on_fire: Callable[[], None],
        on_tick: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.target = target
        self._on_fire = on_fire
        self._on_tick = on_tick
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.fired = False

    def arm(self) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("timer already armed")
        self._stop.clear()
        self.fired = False
        self._thread = threading.Thread(target=self._run, name="FireTimer", daemon=True)
        self._thread.start()

    def disarm(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def armed(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            remaining = seconds_until(self.target)
            if self._on_tick:
                try:
                    self._on_tick(remaining)
                except Exception:
                    pass
            if remaining <= 0:
                if not self._stop.is_set():
                    self.fired = True
                    self._on_fire()
                return
            # Sleep in small slices so disarm() is responsive, and so we land
            # close to the exact second near the open.
            self._stop.wait(timeout=min(1.0, max(0.05, remaining)))
