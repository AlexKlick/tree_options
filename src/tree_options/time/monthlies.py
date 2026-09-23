"""Monthly option-expiry convention: the third Friday of the month.

Weekday and calendar-day arithmetic is ONLY sanctioned inside `time/`
(enforced by the AST lint in test_calendar.py), so the monthly-expiry
predicate lives here and callers outside `time/` import it rather than
reopening `weekday()` arithmetic at their own call sites.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta

from tree_options.time.expiries import is_friday


def is_monthly_expiry(d: date) -> bool:
    """True iff `d` is the third Friday of its month (standard monthly expiry).

    The third Friday of any month is the unique Friday between the 15th and
    the 21st inclusive: the first Friday falls in days 1-7 and the second in
    days 8-14, so a Friday at or past the 15th can only be the third.

    NAMED APPROXIMATION: when the exchange closes on a third Friday (Good
    Friday, rare) the traded expiry moves, typically to the Thursday before.
    This predicate answers only the calendar question "is this the third
    Friday" -- callers that need the actually-traded expiry must additionally
    filter by session/calendar membership, exactly as `expiries` does for
    holiday Fridays.
    """
    return is_friday(d) and 15 <= d.day <= 21


def monthly_expiries(dates: Iterable[date]) -> list[date]:
    """The monthly expiries among `dates`, sorted ascending and deduplicated."""
    return sorted({d for d in dates if is_monthly_expiry(d)})


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def is_traded_monthly_expiry(d: date, is_session: Callable[[date], bool]) -> bool:
    """True iff `d` is the monthly expiry that actually TRADED in its month.

    The session-aware companion to `is_monthly_expiry`: the third Friday when
    the exchange is open on it, otherwise the last session before it. The
    move is real and recurring -- Good Friday 2025-04-18, Juneteenth
    2026-06-19 and Juneteenth-observed 2027-06-18 all closed the exchange on
    a third Friday, and the vendor lists those months' monthlies on the
    Thursday before. A calendar-only filter silently drops those months.

    `is_session` is the caller's exchange calendar (e.g. the trex calendar's
    `StaticSessionCalendar.is_session`); a date it does not know is treated
    as closed, so a calendar that ends early fails toward "no monthly".
    """
    third = _third_friday(d.year, d.month)
    if is_session(third):
        return d == third
    for back in range(1, 7):
        candidate = third - timedelta(days=back)
        if is_session(candidate):
            return d == candidate
    return False
