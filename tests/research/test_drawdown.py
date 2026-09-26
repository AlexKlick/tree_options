"""Drawdown + recovery tests — RL §3 "drawdown and recovery on the
same calendar", RL §11 selection integrity (no recovery claim on a
back-filled window).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tree_options.research.comparison.drawdown import compute_drawdown
from tree_options.research.contracts import ResearchRegistration


def test_drawdown_no_data_returns_empty_series() -> None:
    s = compute_drawdown("cand-v2", ResearchRegistration.BEFORE_ENTRY_WINDOW_END, {})
    assert s.cells == ()


def test_drawdown_monotone_increase_no_drawdown() -> None:
    s = compute_drawdown(
        "cand-v2",
        ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        {
            date(2024, 1, 2): Decimal("100"),
            date(2024, 1, 3): Decimal("105"),
            date(2024, 1, 4): Decimal("110"),
        },
    )
    assert s.cells == ()
    assert s.peak_value == Decimal("110")
    assert s.peak_date == date(2024, 1, 4)


def test_drawdown_then_recovery_marks_recovery_end_date() -> None:
    s = compute_drawdown(
        "cand-v2",
        ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        {
            date(2024, 1, 2): Decimal("100"),
            date(2024, 1, 3): Decimal("90"),   # drawdown
            date(2024, 1, 4): Decimal("85"),   # deeper drawdown
            date(2024, 1, 5): Decimal("95"),   # recovery partial
            date(2024, 1, 8): Decimal("100"),  # recovered to peak
        },
    )
    assert len(s.cells) == 3  # only the under-peak cells
    # First under-peak cell resolves to 2024-01-08 (recovery at peak)
    assert s.cells[0].recovery_end_date == date(2024, 1, 8)
    assert s.cells[1].recovery_end_date == date(2024, 1, 8)
    assert s.cells[2].recovery_end_date == date(2024, 1, 8)
    assert s.cells[0].drawdown_dollar == Decimal("-10")
    assert s.cells[0].drawdown_pct == Decimal("-0.10")


def test_drawdown_open_at_last_observation_keeps_recovery_null() -> None:
    """An unresolved drawdown at the last observation must NOT claim a
    recovery end date — it's visibly open-ended."""
    s = compute_drawdown(
        "cand-v2",
        ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        {
            date(2024, 1, 2): Decimal("100"),
            date(2024, 1, 3): Decimal("80"),
            date(2024, 1, 4): Decimal("70"),  # last observation
        },
    )
    assert all(c.recovery_end_date is None for c in s.cells)


def test_retrospective_registration_never_claims_recovery() -> None:
    """For ``retrospective_backfill``, every under-peak cell gets
    ``recovery_end_date=None`` regardless of whether the series later
    returned to peak — the audit window isn't complete in the original
    selection's reference frame."""
    s = compute_drawdown(
        "cand-v2",
        ResearchRegistration.RETROSPECTIVE_BACKFILL,
        {
            date(2024, 1, 2): Decimal("100"),
            date(2024, 1, 3): Decimal("80"),
            date(2024, 1, 4): Decimal("90"),
            date(2024, 1, 5): Decimal("100"),  # recovered — but doesn't matter
        },
    )
    assert len(s.cells) == 2
    assert all(c.recovery_end_date is None for c in s.cells)


def test_drawdown_percentage_handles_zero_peak() -> None:
    """Zero- or negative-peak edge case: percentage renders as 0 (we
    never invent a negative-percent drawdown from a zero peak — that
    would be a fabricated magnitude)."""
    s = compute_drawdown(
        "cand-v2",
        ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        {date(2024, 1, 2): Decimal("0"),
         date(2024, 1, 3): Decimal("-10")},
    )
    assert all(c.drawdown_pct == Decimal("0") for c in s.cells)
    assert all(c.drawdown_dollar <= Decimal("0") for c in s.cells)
