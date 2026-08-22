"""Monthly-expiry predicate: the third-Friday rule, its edges, and the named
holiday approximation.

`is_monthly_expiry` is the sanctioned home of the rule (`weekday()` arithmetic
outside `time/` is banned by the AST lint in test_calendar.py), so what needs
proving here is the rule itself: the day-of-month window, non-Fridays, a full
year of exactly one expiry per month, and that the docstring keeps naming the
approximation a future reader will need to know about.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tree_options.time.monthlies import is_monthly_expiry, monthly_expiries

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
