"""Monthly-expiry predicate: the third-Friday rule, its edges, and the named
holiday approximation.

`is_monthly_expiry` is the sanctioned home of the rule (`weekday()` arithmetic
outside `time/` is banned by the AST lint in test_calendar.py), so what needs
proving here is the rule itself: the day-of-month window, non-Fridays, a full
year of exactly one expiry per month, and that the docstring keeps naming the
approximation a future reader will need to know about.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import pytest

from tests.conftest import REPO_ROOT
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.time.monthlies import (
    is_monthly_expiry,
    is_traded_monthly_expiry,
    monthly_expiries,
)

# Third Fridays verified against the calendar (each is a Friday in days 15-21).
KNOWN_MONTHLIES = (date(2026, 3, 20), date(2024, 11, 15), date(2025, 6, 20))

# Fridays in March 2026: 6th (1st), 13th (2nd), 20th (3rd), 27th (4th).
MARCH_2026_FRIDAYS = (date(2026, 3, 6), date(2026, 3, 13), date(2026, 3, 20), date(2026, 3, 27))

# 2026 third Fridays, one per month (checked below over every day of the year).
THIRD_FRIDAYS_2026 = (
    date(2026, 1, 16),
    date(2026, 2, 20),
    date(2026, 3, 20),
    date(2026, 4, 17),
    date(2026, 5, 15),
    date(2026, 6, 19),
    date(2026, 7, 17),
    date(2026, 8, 21),
    date(2026, 9, 18),
    date(2026, 10, 16),
    date(2026, 11, 20),
    date(2026, 12, 18),
)


def test_known_third_fridays_are_monthly_expiries() -> None:
    assert all(is_monthly_expiry(d) for d in KNOWN_MONTHLIES)


def test_other_fridays_of_the_same_month_are_not() -> None:
    third = date(2026, 3, 20)
    assert [d for d in MARCH_2026_FRIDAYS if is_monthly_expiry(d)] == [third]


def test_non_fridays_inside_the_day_window_are_not() -> None:
    """The day window alone must not carry the predicate -- weekday does."""
    window = [date(2026, 3, day) for day in range(15, 22)]
    assert [d for d in window if is_monthly_expiry(d)] == [date(2026, 3, 20)]


def test_month_boundaries_of_the_day_window() -> None:
    # August 2026 Fridays are the 7th, 14th, and 21st: the 14th is the SECOND
    # Friday (day 14, one below the window floor) and the 21st is the third.
    assert is_monthly_expiry(date(2026, 8, 14)) is False
    assert is_monthly_expiry(date(2026, 8, 21)) is True
    # August 2025 Fridays are 1, 8, 15, 22, 29: the 22nd is the FOURTH Friday
    # (one above the window ceiling).
    assert is_monthly_expiry(date(2025, 8, 22)) is False
    # A FIFTH Friday (January 2026: 2, 9, 16, 23, 30) is far outside.
    assert is_monthly_expiry(date(2026, 1, 30)) is False


def test_a_full_year_has_exactly_one_monthly_expiry_per_month() -> None:
    """Every day of 2026, checked against the hand-listed third Fridays."""
    day = date(2026, 1, 1)
    hits: list[date] = []
    while day.year == 2026:
        if is_monthly_expiry(day):
            hits.append(day)
        day += timedelta(days=1)
    assert hits == list(THIRD_FRIDAYS_2026)
    assert len({d.month for d in hits}) == 12, "exactly one expiry per month"


@pytest.mark.parametrize("d", THIRD_FRIDAYS_2026)
def test_every_monthly_expiry_is_a_friday_in_the_window(d: date) -> None:
    assert d.weekday() == 4
    assert 15 <= d.day <= 21


def test_monthly_expiries_filters_sorts_and_dedupes() -> None:
    messy = [
        date(2026, 3, 20),
        date(2026, 1, 16),
        date(2026, 3, 13),  # second Friday: dropped
        date(2026, 3, 20),  # duplicate: dropped
        date(2026, 1, 17),  # Saturday: dropped
    ]
    assert monthly_expiries(messy) == [date(2026, 1, 16), date(2026, 3, 20)]
    assert monthly_expiries([]) == []
    assert monthly_expiries([date(2026, 3, 13)]) == []


def test_the_docstring_names_the_holiday_approximation() -> None:
    """A reader must be told the rule is calendar-only before relying on it."""
    doc = is_monthly_expiry.__doc__ or ""
    assert "third Friday" in doc
    assert "APPROXIMATION" in doc


# ---- the TRADED monthly: the third Friday, or the session before a closed one --

# Exchange-closed third Fridays, hand-listed from the NYSE holiday schedule:
# Good Friday 2025, Juneteenth 2026, and Juneteenth-observed 2027 (June 19,
# 2027 is a Saturday, so the exchange closes Friday the 18th). The vendor
# lists each month's monthlies on the Thursday before (verified in the
# coverage-era masters: 2025-04-17, 2026-06-18, 2027-06-17).
CLOSED_THIRD_FRIDAYS = {
    date(2025, 4, 18): date(2025, 4, 17),
    date(2026, 6, 19): date(2026, 6, 18),
    date(2027, 6, 18): date(2027, 6, 17),
}

# Every traded monthly expiry of 2025-2027, hand-listed (third Fridays with
# the three moves above substituted). The oracle is this list, never the
# implementation.
TRADED_MONTHLIES_2025_2027 = (
    date(2025, 1, 17), date(2025, 2, 21), date(2025, 3, 21), date(2025, 4, 17),
    date(2025, 5, 16), date(2025, 6, 20), date(2025, 7, 18), date(2025, 8, 15),
    date(2025, 9, 19), date(2025, 10, 17), date(2025, 11, 21), date(2025, 12, 19),
    date(2026, 1, 16), date(2026, 2, 20), date(2026, 3, 20), date(2026, 4, 17),
    date(2026, 5, 15), date(2026, 6, 18), date(2026, 7, 17), date(2026, 8, 21),
    date(2026, 9, 18), date(2026, 10, 16), date(2026, 11, 20), date(2026, 12, 18),
    date(2027, 1, 15), date(2027, 2, 19), date(2027, 3, 19), date(2027, 4, 16),
    date(2027, 5, 21), date(2027, 6, 17), date(2027, 7, 16), date(2027, 8, 20),
    date(2027, 9, 17), date(2027, 10, 15), date(2027, 11, 19), date(2027, 12, 17),
)  # fmt: skip


def _closed_on(*closed: date) -> Callable[[date], bool]:
    """A session predicate: every weekday except the named closures."""
    shut = set(closed)
    return lambda d: d.weekday() < 5 and d not in shut


def test_a_session_third_friday_is_the_traded_monthly() -> None:
    open_all = _closed_on()
    assert is_traded_monthly_expiry(date(2026, 3, 20), open_all) is True
    assert is_traded_monthly_expiry(date(2026, 3, 19), open_all) is False, "Thursday before"
    assert is_traded_monthly_expiry(date(2026, 3, 13), open_all) is False, "second Friday"


def test_a_closed_third_friday_moves_to_the_session_before_it() -> None:
    for friday, thursday in CLOSED_THIRD_FRIDAYS.items():
        sessions = _closed_on(friday)
        assert is_traded_monthly_expiry(thursday, sessions) is True, thursday
        assert is_traded_monthly_expiry(friday, sessions) is False, friday
        wednesday = date(thursday.year, thursday.month, thursday.day - 1)
        assert is_traded_monthly_expiry(wednesday, sessions) is False, wednesday


def test_a_move_skips_a_closed_thursday_too() -> None:
    """Never seen on the NYSE, but the rule is 'the last session before', not
    'Thursday': with Thursday also closed the expiry lands on Wednesday."""
    friday, thursday = date(2026, 6, 19), date(2026, 6, 18)
    sessions = _closed_on(friday, thursday)
    assert is_traded_monthly_expiry(date(2026, 6, 17), sessions) is True
    assert is_traded_monthly_expiry(thursday, sessions) is False


def test_the_checked_in_trex_calendar_yields_one_traded_monthly_per_month() -> None:
    """Every day of 2025-2027 against the real NYSE calendar the capture uses."""
    base = REPO_ROOT / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
    calendar = StaticSessionCalendar(base, base.with_suffix(".sha256"))
    day = date(2025, 1, 1)
    hits: list[date] = []
    while day.year <= 2027:
        if is_traded_monthly_expiry(day, calendar.is_session):
            hits.append(day)
        day += timedelta(days=1)
    assert hits == list(TRADED_MONTHLIES_2025_2027)
    for friday in CLOSED_THIRD_FRIDAYS:
        assert not calendar.is_session(friday), f"{friday} must be an exchange holiday"
