"""Forecast contract — RL-3 calibrated outlook and study templates.

A forecast run EVALUATES distributional forecasts; it never claims a
validated product. Handoff §10: "Do not turn insufficient-data models
into a forecast product by hiding their status."

The forecast object is a QUANTILE FUNCTION of the series level at the
h-th observed session after the origin: a fixed tau grid
(:data:`QUANTILE_GRID`) evaluated per model per origin on a rolling-origin
grid (monthly first-session origins, ``desk.har.walk_forward``
discipline — monthly refit cadence, fit only on data available before
each origin).

Status vocabulary (kept apart on purpose):
    execution_status    the run lifecycle (queued/running/completed/…).
    evaluation_status   whether an evaluation receipt was publishable
                        (origin floor met, counts uncensored).
    calibration_status  always ``not_claimed`` in v1 — empirical
                        coverage with uncertainty is DISPLAYED;
                        calibration is never asserted (12 origins give a
                        Wilson 95% interval of [0.65, 0.99]; even 90/100
                        gives [0.83, 0.94]).

Wire number convention: statistics and index levels are native JSON
floats on this surface — never money, so the comparison lane's
Decimal-as-string rule does not apply (desk precedent, ``desk/har.py``:
"Floats: variance statistics, not money"). Non-finite values are
structurally impossible: ``desk.contracts.canonical`` (allow_nan=False)
hashes every published payload, and the harness demotes any non-finite
model output to a FAILED origin before the wire is assembled.

Run identity (v4): ``forecast_run_id`` binds spec + series bytes +
BOTH calendar shas (the pinned comparison calendar that declares scope
AND the closure-corrected session authority that shapes the grid) +
ENGINE sha. Any change to the data, the calendars, the request, or the
engine code produces a NEW run — an engine fix can never re-serve a
stale receipt, and a vendor revision can never publish revised bytes
under the submission's identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Final

from tree_options.desk.contracts import canonical

#: The declared quantile grid. Strictly increasing, every tau in (0, 1).
#: The aggregate score over this grid is a GRID score (2 x mean pinball),
#: never CRPS — see ``forecast/metrics.py`` for the distinction and the
#: U[1, 2] counterexample that keeps it honest.
QUANTILE_GRID: Final[tuple[float, ...]] = (0.05, 0.25, 0.5, 0.75, 0.95)

#: Minimum evaluated origins before an evaluation receipt may publish.
#: A machinery floor for EXPLORATORY validation only — it cannot confer
#: calibration (see module docstring) and the refusal retains the ledger.
ORIGIN_FLOOR: Final[int] = 12

#: Minimum eligible training history (sessions with completed h-step
#: targets) before the first evaluation origin.
MIN_HISTORY_SESSIONS: Final[int] = 260

#: Trailing window (eligible h-step changes) for the ``rw_window`` model.
EMPIRICAL_WINDOW_SESSIONS: Final[int] = 250

#: Diebold-Mariano bandwidth in ORIGIN units (monthly origins; adjacent
#: h=20 target windows overlap by one session in 9/104 pairs — see the
#: rl3 plan's verified counts — so lag=1 covers the mechanical overlap
#: and one step of shared-regime persistence). Sensitivity {0, 1, 2}.
DM_LAG_ORIGIN_UNITS: Final[int] = 1
DM_LAG_SENSITIVITY: Final[tuple[int, ...]] = (0, 1, 2)

#: Moving-block bootstrap for coverage: block length in origin units and
#: resample count. Degenerate cases (all hits / all misses) are marked
#: degenerate — CI null + reason — never [1, 1] presented as evidence.
BOOTSTRAP_BLOCK: Final[int] = 2
N_BOOTSTRAP: Final[int] = 2000

#: Minimum PAIRED origins before a skill score or DM test may publish.
#: Both individual floors can pass with a 0/1-origin intersection; that
#: must not emit a skill number (reason: paired_cohort_insufficient).
PAIRED_FLOOR: Final[int] = 8

#: Study-template schema version (the receipt's ``study`` block).
STUDY_SCHEMA: Final[str] = "research-forecast-study/1"


class ForecastSourceId(StrEnum):
    """Forecastable series sources (the registry lives in sources.py)."""

    SYNTHETIC = "synthetic-forecast-v1"
    INDEX_VIX = "index:VIX"


@dataclass(frozen=True)
class ForecastSpec:
    """An evaluation request: which source, which horizon, which window.

    The spec surface is deliberately minimal — models are ENGINE POLICY
    (all three always run; the naive baseline cannot be dropped, or the
    handoff §6 "comparison to a baseline is visible" exit row could be
    hidden by omission). Horizon gating is REGISTRY policy, enforced
    pre-write at HTTP and again in the engine.
    """

    source: ForecastSourceId
    horizon: int
    evaluation_start: date
    evaluation_end: date | None = None
    proposed_by: str = "operator"
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "horizon": int(self.horizon),
            "evaluation_start": self.evaluation_start.isoformat(),
            "evaluation_end": (
                self.evaluation_end.isoformat() if self.evaluation_end is not None else None
            ),
            "proposed_by": self.proposed_by,
            "notes": self.notes,
        }


def forecast_run_id(
    spec: ForecastSpec,
    *,
    series_sha256: str,
    calendar_sha256: str,
    session_authority_sha256: str,
    engine_sha256: str,
) -> str:
    """Execution-bound run id: sha256 over the canonical binding of
    spec + series bytes + BOTH calendar shas + engine sha.

    Any of the five changing produces a NEW run (a rerun under changed
    inputs or changed engine code is a new execution, never a silent
    re-serve of the old receipt). ``calendar_sha256`` is the pinned
    comparison calendar (declared scope); ``session_authority_sha256``
    is the closure-corrected session authority whose intersection with
    the observed dates IS the evaluation grid — a closure correction
    moves targets and scores, so it must move the run id (checkpoint B,
    P1-1). Domain-separated from the comparison and scenario hash
    schemes by the embedded schema string.
    """
    payload = canonical(
        {
            "schema": "forecast-run/2",
            "spec": spec.to_dict(),
            "series_sha256": series_sha256,
            "calendar_sha256": calendar_sha256,
            "session_authority_sha256": session_authority_sha256,
            "engine_sha256": engine_sha256,
        }
    )
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class OriginTally:
    """Horizon-level origin accounting — uncensored by construction.

    ``total == evaluated + excluded`` holds at the GRID level (before any
    model runs); per MODEL the enforced identity is
    ``total == evaluated + excluded + failed`` (a model failure is an
    outcome of running, not a property of the grid).
    """

    total: int
    evaluated: int
    excluded: int
    excluded_reasons: Mapping[str, int]
    floor: int
    floor_met: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "evaluated": self.evaluated,
            "excluded": self.excluded,
            "excluded_reasons": dict(self.excluded_reasons),
            "floor": self.floor,
            "floor_met": self.floor_met,
        }


@dataclass(frozen=True)
class LedgerRow:
    """One origin's full trace for one model — the auditable cohort.

    The ledger is what makes the receipt's comparisons honest: skill and
    DM are computed on the MATCHED evaluated origins across models, and
    anyone can re-derive every displayed number from these rows.
    """

    origin_date: date
    #: None only for excluded origins whose target lies beyond the data
    #: (the reason says so; an invented target date would be dishonest).
    target_date: date | None
    training_count: int
    status: str  # "evaluated" | "failed" | "excluded"
    reason: str | None  # failure/exclusion reason, None when evaluated
    actual: float | None
    quantiles: tuple[float, ...] = ()
    losses_by_tau: tuple[float, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_date": self.origin_date.isoformat(),
            "target_date": (self.target_date.isoformat() if self.target_date is not None else None),
            "training_count": self.training_count,
            "status": self.status,
            "reason": self.reason,
            "actual": self.actual,
            "quantiles": list(self.quantiles),
            "losses_by_tau": list(self.losses_by_tau),
        }


@dataclass(frozen=True)
class ModelReceipt:
    """One model's evaluation over the origin grid."""

    model: str
    is_baseline: bool
    n_evaluated: int
    n_failed: int
    failure_reasons: Mapping[str, int]
    metrics: Mapping[str, Any]
    ledger: tuple[LedgerRow, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "is_baseline": self.is_baseline,
            "n_evaluated": self.n_evaluated,
            "n_failed": self.n_failed,
            "failure_reasons": dict(self.failure_reasons),
            "metrics": dict(self.metrics),
            "ledger": [row.to_dict() for row in self.ledger],
        }


def tally_identity_ok(total: int, evaluated: int, excluded: int, failed: int) -> bool:
    """The enforced per-model accounting identity (§10 uncensoring):
    every origin is exactly one of evaluated / excluded / failed."""
    return total == evaluated + excluded + failed


__all__ = [
    "BOOTSTRAP_BLOCK",
    "DM_LAG_ORIGIN_UNITS",
    "DM_LAG_SENSITIVITY",
    "EMPIRICAL_WINDOW_SESSIONS",
    "MIN_HISTORY_SESSIONS",
    "N_BOOTSTRAP",
    "ORIGIN_FLOOR",
    "PAIRED_FLOOR",
    "QUANTILE_GRID",
    "STUDY_SCHEMA",
    "ForecastSourceId",
    "ForecastSpec",
    "LedgerRow",
    "ModelReceipt",
    "OriginTally",
    "forecast_run_id",
    "tally_identity_ok",
]
