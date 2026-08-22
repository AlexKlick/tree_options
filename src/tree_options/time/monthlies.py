"""Monthly option-expiry convention: the third Friday of the month.

Weekday and calendar-day arithmetic is ONLY sanctioned inside `time/`
(enforced by the AST lint in test_calendar.py), so the monthly-expiry
predicate lives here and callers outside `time/` import it rather than
reopening `weekday()` arithmetic at their own call sites.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

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
