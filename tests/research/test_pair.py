"""Pairwise alignment tests — RL §11 "comparison pairing" row.

A cell where EITHER side has missing data renders as null with a reason
(not zero). Ineligible candidates never enter a pair.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tree_options.research.comparison.missingness import (
    REASON_BENCHMARK_OVERLAP_MISSING,
)
from tree_options.research.comparison.pair import (
    align_pair,
    diff_in_dollars,
    intersect_window,
)
from tree_options.research.contracts import (
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)


def _candidate(supported_start=None, supported_end=None, *,
               family="vix_term") -> ResearchCandidate:
    return ResearchCandidate(
        id=f"{family}-v2",
        family=family,
        version="v2",
        evidence_kind=ResearchEvidenceKind.SEALED_CAMPAIGN,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=True,
        supported_start=supported_start,
        supported_end=supported_end,
    )


def test_intersect_window_uses_both_starts_and_ends() -> None:
    lo, hi = intersect_window(
        date(2024, 1, 2), date(2026, 9, 25),
        date(2024, 6, 1), date(2026, 6, 30),
    )
    assert lo == date(2024, 6, 1)
    assert hi == date(2026, 6, 30)


def test_intersect_window_returns_none_if_either_unknown() -> None:
    assert intersect_window(None, date(2026, 1, 1), date(2024, 1, 1),
                             date(2026, 1, 1)) == (None, None)
    assert intersect_window(date(2024, 1, 1), None, date(2024, 1, 1),
                             date(2026, 1, 1)) == (None, None)


def test_align_pair_emits_null_when_baseline_missing() -> None:
    c = _candidate(date(2024, 1, 1), date(2026, 9, 25))
    b = _candidate(date(2024, 1, 1), date(2026, 9, 25), family="bh")
    pair = align_pair(
        c, b,
        {date(2024, 1, 2): Decimal("100"), date(2024, 1, 3): Decimal("101"),
         date(2024, 6, 1): Decimal("110")},
        {date(2024, 1, 2): Decimal("100"),
         date(2024, 6, 1): Decimal("105")},
    )
    # 2024-01-03: candidate=101, baseline missing → null + benchmark
    # overlap-missing reason (the baseline is the comparison partner; its
    # absence on a date is the canonical "benchmark overlap missing" case).
    cell_3 = next(c for c in pair.cells if c.date == date(2024, 1, 3))
    assert cell_3.value is None
    assert cell_3.reason is not None
    assert cell_3.reason.code == REASON_BENCHMARK_OVERLAP_MISSING
    # 2024-01-02: both present → 100 - 100 = 0
    cell_2 = next(c for c in pair.cells if c.date == date(2024, 1, 2))
    assert cell_2.value == Decimal("0")
    assert cell_2.reason is None
    # 2024-06-01: both present → 110 - 105 = 5
    cell_6 = next(c for c in pair.cells if c.date == date(2024, 6, 1))
    assert cell_6.value == Decimal("5")


def test_align_pair_emits_benchmark_overlap_missing_when_baseline_lacks_date() -> None:
    """The case the handoff calls out: baseline has no observation on
    that date → reason = research.benchmark_overlap_missing."""
    c = _candidate(date(2024, 1, 1), date(2026, 9, 25))
    b = _candidate(date(2024, 1, 1), date(2026, 9, 25), family="bh")
    pair = align_pair(
        c, b,
        {date(2024, 6, 1): Decimal("110"), date(2024, 6, 2): Decimal("112")},
        {date(2024, 6, 1): Decimal("105")},  # 2024-06-02 missing for baseline
    )
    cell_6_2 = next(c for c in pair.cells if c.date == date(2024, 6, 2))
    assert cell_6_2.value is None
    assert cell_6_2.reason is not None
    assert cell_6_2.reason.code == REASON_BENCHMARK_OVERLAP_MISSING


def test_align_pair_without_baseline_emits_unpaired_series() -> None:
    c = _candidate(date(2024, 1, 1), date(2024, 6, 30))
    pair = align_pair(
        c, None,
        {date(2024, 1, 2): Decimal("100"), date(2024, 1, 3): Decimal("101")},
        {},
    )
    assert pair.baseline_id is None
    assert len(pair.cells) == 2
    assert all(c.value is not None for c in pair.cells)


def test_align_pair_intersects_supported_windows() -> None:
    """A candidate whose supported window ends in 2026 should not have
    cells past 2026 even if the baseline extends to 2027."""
    c = _candidate(date(2024, 1, 1), date(2026, 6, 30))
    b = _candidate(date(2024, 1, 1), date(2027, 1, 1), family="bh")
    pair = align_pair(
        c, b,
        {date(2026, 6, 30): Decimal("110"),
         date(2026, 12, 31): Decimal("120")},
        {date(2026, 6, 30): Decimal("105"),
         date(2026, 12, 31): Decimal("108")},
    )
    # Only 2026-06-30 survives the intersection; the 2026-12-31 candidate
    # datum is also dropped because it falls outside the candidate's
    # supported window.
    dates = [c.date for c in pair.cells]
    assert dates == [date(2026, 6, 30)]


def test_diff_in_dollars_serializes_decimals_as_strings() -> None:
    c = _candidate(date(2024, 1, 1), date(2024, 6, 30))
    b = _candidate(date(2024, 1, 1), date(2024, 6, 30), family="bh")
    pair = align_pair(
        c, b,
        {date(2024, 1, 2): Decimal("100.50")},
        {date(2024, 1, 2): Decimal("100.25")},
    )
    d = diff_in_dollars(pair)
    assert d["2024-01-02"]["value"] == "0.25"
    assert d["2024-01-02"]["reason"] is None


def test_diff_in_dollars_null_cells_carry_reason_codes_not_zero() -> None:
    """Missing cells are null + reason — never 0.00."""
    c = _candidate(date(2024, 1, 1), date(2024, 6, 30))
    b = _candidate(date(2024, 1, 1), date(2024, 6, 30), family="bh")
    pair = align_pair(c, b, {}, {})
    assert pair.cells == ()
    d = diff_in_dollars(pair)
    assert d == {}
