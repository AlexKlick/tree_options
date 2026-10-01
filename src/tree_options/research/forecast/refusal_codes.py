"""Forecast refusal reason codes (wire-visible; the SPA renders these as
the honest blocker). Mirrors the ``research.scenario.*`` /
``research.plan.*`` namespace style: every refusal is machine-readable.

A refusal is a VALUE, not an error: the worker publishes it as a
content-bound result record (never a 404, never a silently dropped run).
``payload`` carries structured evidence — the origin tally and ledger for
``insufficient_origins``, BOTH shas for the drift/changed refusals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final


@dataclass(frozen=True)
class ForecastRefusal:
    code: str
    message: str
    payload: Mapping[str, Any] = field(default_factory=dict)


FORECAST_UNKNOWN_SOURCE: Final = "research.forecast.unknown_source"
FORECAST_HORIZON_NOT_ENABLED: Final = "research.forecast.horizon_not_enabled"
FORECAST_INSUFFICIENT_HISTORY: Final = "research.forecast.insufficient_history"
FORECAST_INSUFFICIENT_ORIGINS: Final = "research.forecast.insufficient_origins"
FORECAST_INVALID_QUANTILE_GRID: Final = "research.forecast.invalid_quantile_grid"
FORECAST_SOURCE_DRIFT: Final = "research.forecast.source_drift"
FORECAST_SOURCE_INVALID: Final = "research.forecast.source_invalid"
FORECAST_ENGINE_CHANGED: Final = "research.forecast.engine_changed"
#: Either calendar bound into the run identity moved between submission
#: and compute: the comparison calendar (scope) or the closure-corrected
#: session authority (the grid itself).
FORECAST_CALENDAR_CHANGED: Final = "research.forecast.calendar_changed"
#: The submission bindings do not REPRODUCE the run id they were stored
#: beside (checkpoint B-prime, N1): the run record's five identity
#: values, hashed together, must equal the run id under which the
#: record was spooled.
FORECAST_IDENTITY_MISMATCH: Final = "research.forecast.identity_mismatch"

FORECAST_REFUSAL_KINDS: Final[tuple[str, ...]] = (
    FORECAST_UNKNOWN_SOURCE,
    FORECAST_HORIZON_NOT_ENABLED,
    FORECAST_INSUFFICIENT_HISTORY,
    FORECAST_INSUFFICIENT_ORIGINS,
    FORECAST_INVALID_QUANTILE_GRID,
    FORECAST_SOURCE_DRIFT,
    FORECAST_SOURCE_INVALID,
    FORECAST_ENGINE_CHANGED,
    FORECAST_CALENDAR_CHANGED,
    FORECAST_IDENTITY_MISMATCH,
)

#: Per-ORIGIN failure reasons (counted per model, never hidden — §10
#: "uncensored failures and excluded origins are counted"). These are
#: ledger statuses, not run refusals.
FAIL_QUANTILE_ORDERING: Final = "quantile_ordering"
FAIL_FIT: Final = "fit_failed"
FAIL_NON_FINITE: Final = "non_finite"

#: Grid-level exclusion reasons (the origin never ran for ANY model).
EXCLUDE_TARGET_BEYOND_DATA: Final = "target_beyond_data"
EXCLUDE_INSUFFICIENT_HISTORY: Final = "insufficient_history"

#: Skill/DM unavailability reason (metrics-level, not a refusal).
REASON_PAIRED_COHORT_INSUFFICIENT: Final = "paired_cohort_insufficient"
REASON_DM_NO_VARIANCE: Final = "no loss-differential variance"
REASON_BENCH_LOSS_NONPOSITIVE: Final = "baseline loss not positive"
REASON_BOOTSTRAP_DEGENERATE: Final = "degenerate coverage (all hits or none)"


__all__ = [
    "EXCLUDE_INSUFFICIENT_HISTORY",
    "EXCLUDE_TARGET_BEYOND_DATA",
    "FAIL_FIT",
    "FAIL_NON_FINITE",
    "FAIL_QUANTILE_ORDERING",
    "FORECAST_CALENDAR_CHANGED",
    "FORECAST_ENGINE_CHANGED",
    "FORECAST_HORIZON_NOT_ENABLED",
    "FORECAST_IDENTITY_MISMATCH",
    "FORECAST_INSUFFICIENT_HISTORY",
    "FORECAST_INSUFFICIENT_ORIGINS",
    "FORECAST_INVALID_QUANTILE_GRID",
    "FORECAST_REFUSAL_KINDS",
    "FORECAST_SOURCE_DRIFT",
    "FORECAST_SOURCE_INVALID",
    "FORECAST_UNKNOWN_SOURCE",
    "REASON_BENCH_LOSS_NONPOSITIVE",
    "REASON_BOOTSTRAP_DEGENERATE",
    "REASON_DM_NO_VARIANCE",
    "REASON_PAIRED_COHORT_INSUFFICIENT",
    "ForecastRefusal",
]
