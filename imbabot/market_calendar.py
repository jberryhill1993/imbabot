"""US equity/futures market calendar — weekends + bank holidays.

The opening-range strategy trades the 09:30 ET cash session, which is closed on NYSE/CME
holidays. This is pure date math (no project deps) so both the scheduler (live auto-fire) and
the analysis layer (Morning Plan) can ask "is the market open?" and "what's the next session?".

Holiday rules (full closures; half-days like the day after Thanksgiving stay OPEN):
New Year's Day, MLK Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth, Independence
Day, Labor Day, Thanksgiving, Christmas — each on its weekend-observed date.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Set


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Mon=0) of ``month`` (1-based n)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` (Mon=0) of ``month``."""
    d = date(year, month, 28) + timedelta(days=4)      # always in the next month
    d = d.replace(day=1) - timedelta(days=1)           # last day of the target month
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Federal observation: Saturday holiday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:       # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:       # Sunday
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    """Anonymous Gregorian (computus) algorithm -> Easter Sunday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=32)
def holidays(year: int) -> Set[date]:
    """The set of full-closure US market holiday dates for ``year`` (observed)."""
    h = {
        _observed(date(year, 1, 1)),                 # New Year's Day
        _nth_weekday(year, 1, 0, 3),                  # MLK (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),                  # Presidents' Day (3rd Mon Feb)
        _easter(year) - timedelta(days=2),            # Good Friday
        _last_weekday(year, 5, 0),                    # Memorial Day (last Mon May)
        _observed(date(year, 6, 19)),                 # Juneteenth
        _observed(date(year, 7, 4)),                  # Independence Day
        _nth_weekday(year, 9, 0, 1),                  # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),                 # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),                # Christmas
    }
    return h


def is_holiday(d: date) -> bool:
    return d in holidays(d.year)


def is_trading_day(d: date) -> bool:
    """A weekday (Mon-Fri) that is not a market holiday."""
    return d.weekday() < 5 and not is_holiday(d)


def next_trading_session(d: date) -> date:
    """``d`` itself if it's a trading day, else the next trading day after it."""
    cur = d
    while not is_trading_day(cur):
        cur += timedelta(days=1)
    return cur


def next_trading_session_after(d: date) -> date:
    """The next trading day strictly after ``d``."""
    return next_trading_session(d + timedelta(days=1))
