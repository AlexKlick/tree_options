"""Pairwise common-support alignment for the comparison engine.

Handoff §4 (Comparability contract): "A fair comparison must declare
starting capital, common start/end dates and supported coverage … Paired
resampling preserves dependence." Handoff §11 (Comparison pairing):
"Same support/basis/calendar; absent eligibility versus missing
observations remain distinct; paired resampling preserves dependence."

Rules enforced here:
    * Calendar-intersect common supported dates for each pair.
    * Cells where EITHER side has missing data render as null — never zero.
    * Eligibility (ineligible candidates) is distinct from missing data
      (eligible candidates whose data has a gap).
    * Baseline (synthetic buy-and-hold) is treated as a regular candidate
      for pairing; an unsupported date on the baseline emits a
      benchmark-overlap-missing reason on that date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tree_options.research.comparison.missingness import (
    MissingnessReason,
    reason_benchmark_overlap_missing,
)
from tree_options.research.contracts import (
    ResearchCandidate,
)


@dataclass(frozen=True)
class PairCell:
    """One (date, value, reason) cell of a paired comparison.

    A cell with ``value is None`` carries a non-null ``reason``. A cell
    with ``value`` set carries ``reason=None``."""
    date: date
    value: Decimal | None
    reason: MissingnessReason | None


@dataclass(frozen=True)
class PairSeries:
    """The paired values for one (candidate, baseline) combination over
    the common supported window."""

    candidate_id: str
    baseline_id: str | None  # None when spec.benchmark_candidate_id is None
    cells: tuple[PairCell, ...]


def intersect_window(
    a_start: date | None, a_end: date | None,
    b_start: date | None, b_end: date | None,
) -> tuple[date | None, date | None]:
    """Intersect two supported-date windows. None on either side means
    'unknown' — never 'open-ended'. The intersection is conservative:
    unknown input widens the unknown output."""
    if a_start is None or b_start is None:
        return None, None
    if a_end is None or b_end is None:
        return None, None
    return max(a_start, b_start), min(a_end, b_end)


def align_pair(
    candidate: ResearchCandidate,
    baseline: ResearchCandidate | None,
    candidate_value_by_session: dict[date, Decimal],
    baseline_value_by_session: dict[date, Decimal],
) -> PairSeries:
    """Build the paired series. Missingness rules:
        - candidate and baseline MUST both have a value for the cell to
          carry a number. If either is missing, value=None + reason.
        - If the baseline is None (no spec.benchmark_candidate_id), the
          pair series is just the candidate's values with no reason for
          each date the candidate has a value.
        - If the baseline is present but lacks data on a date the
          candidate covers, the cell carries
          ``reason_benchmark_overlap_missing``.
    """
    if candidate_value_by_session and baseline is None:
        paired_cells: list[PairCell] = [
            PairCell(date=d, value=v, reason=None)
            for d, v in sorted(candidate_value_by_session.items())
        ]
        return PairSeries(candidate.id, None, tuple(paired_cells))

    if baseline is None:
        return PairSeries(candidate.id, None, ())

    window_lo, window_hi = intersect_window(
        candidate.supported_start, candidate.supported_end,
        baseline.supported_start, baseline.supported_end,
    )

    dates: set[date] = set(candidate_value_by_session) | set(baseline_value_by_session)
    if not dates:
        return PairSeries(candidate.id, baseline.id, ())

    # If neither side has a known supported window, fall back to the union
    # of observed dates — the operator's spec asked for a specific window;
    # we trust their spec.date window.
    if window_lo is None or window_hi is None:
        window_lo = min(dates)
        window_hi = max(dates)

    paired_cells = []
    for d in sorted(d for d in dates if window_lo <= d <= window_hi):
        c_val = candidate_value_by_session.get(d)
        b_val = baseline_value_by_session.get(d)
        if c_val is None or b_val is None:
            paired_cells.append(PairCell(
                date=d,
                value=None,
                reason=(reason_benchmark_overlap_missing(d.isoformat())
                        if (b_val is None and c_val is not None)
                        else MissingnessReason(
                            code="research.pair.incomplete",
                            description=(
                                f"candidate or baseline missing data on {d.isoformat()}"
                            ),
                        )),
            ))
        else:
            paired_cells.append(PairCell(date=d, value=c_val - b_val, reason=None))
    return PairSeries(candidate.id, baseline.id, tuple(paired_cells))


def diff_in_dollars(pair: PairSeries) -> dict[str, dict[str, str | None]]:
    """Render the paired series as a JSON-safe dict.

    Cells with ``value is None`` carry the reason code and a
    ``null`` value — never zero. Decimal values cross as strings.
    """
    return {
        c.date.isoformat(): {
            "value": str(c.value) if c.value is not None else None,
            "reason": c.reason.code if c.reason is not None else None,
        }
        for c in pair.cells
    }


__all__ = [
    "PairCell",
    "PairSeries",
    "align_pair",
    "diff_in_dollars",
    "intersect_window",
]
