"""Research-specific missing-metric reasons.

Mirrors ``CandidateResult.reason: str`` from ``tree_options.desk.pricing``
(line 558). The desk reason codes describe why a valuation refused; this
module defines a parallel set for the comparison engine — why a chart
cell, a candidate pair, or a horizon slice cannot be plotted.

Convention:
    ``research.<surface>_<reason>``

Surfaces:
    candidate  — the candidate itself is unplotable (e.g. broker paper)
    cost       — entry cost is missing for a package
    benchmark  — the baseline lacks data on a date
    horizon    — the requested window exceeds supported coverage
    data       — the underlying dataset is gated or sealed-untouched
"""

from __future__ import annotations

from dataclasses import dataclass

#: Reason codes used in ``ComparisonResult.cells[*].reason`` and
#: ``Envelope.evidence_kind == 'broker_paper'`` rejection paths.
REASON_COST_UNKNOWN = "research.cost_unknown"
REASON_BENCHMARK_OVERLAP_MISSING = "research.benchmark_overlap_missing"
REASON_UNSUPPORTED_HORIZON = "research.unsupported_horizon"
REASON_DATA_GATED = "research.data_gated"
REASON_BROKER_PAPER = "research.broker_paper"
REASON_RETROSPECTIVE_ONLY = "research.retrospective_only"  # no wallet curve


@dataclass(frozen=True)
class MissingnessReason:
    """One reason a chart cell, candidate pair, or horizon slice is
    missing. Carries both the wire code and a one-sentence human
    description for the side drawer."""

    code: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "description": self.description}


def reason_cost_unknown(package: str) -> MissingnessReason:
    return MissingnessReason(
        code=REASON_COST_UNKNOWN,
        description=(
            f"entry cost missing for {package}; the priced-subset average "
            "never prices the whole position"
        ),
    )


def reason_benchmark_overlap_missing(date_iso: str) -> MissingnessReason:
    return MissingnessReason(
        code=REASON_BENCHMARK_OVERLAP_MISSING,
        description=(
            f"baseline has no supported observation on {date_iso}; "
            "the diff cell renders as null"
        ),
    )


def reason_unsupported_horizon(requested: str, supported: str) -> MissingnessReason:
    return MissingnessReason(
        code=REASON_UNSUPPORTED_HORIZON,
        description=(
            f"requested {requested} extends past supported coverage {supported}"
        ),
    )


def reason_data_gated(scope: str) -> MissingnessReason:
    return MissingnessReason(
        code=REASON_DATA_GATED,
        description=(
            f"scope {scope} is DATA-GATED-NOT-RUN — no chartable evidence yet"
        ),
    )


def reason_broker_paper() -> MissingnessReason:
    return MissingnessReason(
        code=REASON_BROKER_PAPER,
        description=(
            "E5 broker-paper is out of scope for RL-1; "
            "use the desk's shadow lane for proxy evidence"
        ),
    )


def reason_retrospective_only(scope: str) -> MissingnessReason:
    return MissingnessReason(
        code=REASON_RETROSPECTIVE_ONLY,
        description=(
            f"scope {scope} is registration=retrospective_backfill; "
            "no wallet curve is plotted (per-episode dispatches only)"
        ),
    )


def reason_to_dict(reason: MissingnessReason | None) -> dict[str, str] | None:
    """Wire helper — emits null for the success case."""
    return reason.to_dict() if reason is not None else None
