"""Option-expiry calendar conventions (M3 plan §3.A).

Weekday and calendar-day arithmetic is ONLY sanctioned inside `time/`
(enforced by the AST lint in test_calendar.py): option expiries are
calendar-date facts (Fridays; third-Friday-of-quarter months), while
session arithmetic stays on the SessionCalendar. A holiday Friday simply
is not a session — the caller filters by calendar membership.
"""

from __future__ import annotations

from datetime import date, timedelta

_QUARTER_MONTHS = frozenset({3, 6, 9, 12})


def is_friday(d: date) -> bool:
    return d.weekday() == 4


def is_third_friday_of_quarter_month(d: date) -> bool:
    """Third Friday of March/June/September/December (quarterly expiry)."""
    return d.month in _QUARTER_MONTHS and d.weekday() == 4 and 15 <= d.day <= 21


def minus_calendar_days(d: date, days: int) -> date:
    """d minus `days` calendar days — the single sanctioned timedelta use."""
    return d - timedelta(days=days)
