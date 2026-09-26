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
    *,
    sessions: tuple[date, ...] | None = None,
) -> PairSeries:
    """Build the paired series over the DECLARED session calendar.

    ``sessions`` is the resolved plan's calendar — the common basis the
    operator asked for. Cells are emitted for every declared session:
    both sides observed -> the dollar difference; either side missing ->
    value=None with the specific reason. There is NO fallback to the
    union of observed dates: a date the calendar declares but the data
    does not cover is a visible gap with a reason, not a date the
    comparison quietly drops (RL1-02: the spec window binds, the union
    of trade dates never did).

    Without ``sessions`` (legacy/test callers) the observed candidate
    dates are used, sorted — still no union fallback against a baseline.

    Missingness rules:
        - candidate and baseline MUST both have a value for the cell to
          carry a number. If either is missing, value=None + reason.
        - If the baseline is None (no spec.benchmark_candidate_id), the
          pair series is just the candidate's values with no reason for
          each date the candidate has a value.
        - If the baseline is present but lacks data on a date the
          candidate covers, the cell carries
          ``reason_benchmark_overlap_missing``.
    """
    if baseline is None:
        if sessions is not None:
            paired_cells = [
                PairCell(date=d, value=candidate_value_by_session.get(d),
                         reason=(None if d in candidate_value_by_session
                                 else MissingnessReason(
                                     code="research.pair.candidate_missing",
                                     description=(
                                         f"candidate has no observation on {d.isoformat()}"
                                     ),
                                 )))
                for d in sessions
            ]
            return PairSeries(candidate.id, None, tuple(paired_cells))
        if candidate_value_by_session:
            paired_cells_legacy: list[PairCell] = [
                PairCell(date=d, value=v, reason=None)
                for d, v in sorted(candidate_value_by_session.items())
            ]
            return PairSeries(candidate.id, None, tuple(paired_cells_legacy))
        return PairSeries(candidate.id, None, ())

    declared = sessions if sessions is not None else tuple(
        sorted(set(candidate_value_by_session) | set(baseline_value_by_session)))

    paired_cells = []
    for d in declared:
        c_val = candidate_value_by_session.get(d)
        b_val = baseline_value_by_session.get(d)
        if c_val is None and b_val is None:
            paired_cells.append(PairCell(
                date=d, value=None,
                reason=MissingnessReason(
                    code="research.pair.no_observation",
                    description=(f"neither candidate nor baseline observed "
                                 f"{d.isoformat()}"),
                ),
            ))
        elif c_val is None:
            paired_cells.append(PairCell(
                date=d, value=None,
                reason=MissingnessReason(
                    code="research.pair.candidate_missing",
                    description=f"candidate has no observation on {d.isoformat()}",
                ),
            ))
        elif b_val is None:
            paired_cells.append(PairCell(
                date=d, value=None,
                reason=reason_benchmark_overlap_missing(d.isoformat()),
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
