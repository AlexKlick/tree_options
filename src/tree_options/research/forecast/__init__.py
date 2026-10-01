"""RL-3: Calibrated outlook and study templates (handoff §10 / §6).

Module map (the scenarios-package shape; engine-side imports are lazy
so the import-time graph stays acyclic):

    contracts      ForecastSpec, ForecastSourceId, records, run-id hash
    refusal_codes  FORECAST_* refusal constants + per-origin reasons
    spec_io        forecast_from_dict(payload)
    metrics        pinball / grid score / coverage / Wilson / skill / DM
    sources        source registry + ForecastSeries loaders
    harness        rolling-origin grid, models, ledger evaluation
    engine         evaluate_forecast orchestration + receipt assembly

The aggregate score is a GRID score (2 x mean pinball over the declared
tau grid) — never CRPS; ``forecast/metrics.py`` carries the distinction
and the counterexample. Calibration is never claimed in v1
(``calibration_status`` is always ``not_claimed``); empirical coverage
with its Wilson interval and a block-bootstrap sensitivity is displayed
instead.
"""

from __future__ import annotations

from tree_options.research.forecast.contracts import (
    BOOTSTRAP_BLOCK,
    DM_LAG_ORIGIN_UNITS,
    DM_LAG_SENSITIVITY,
    EMPIRICAL_WINDOW_SESSIONS,
    MIN_HISTORY_SESSIONS,
    N_BOOTSTRAP,
    ORIGIN_FLOOR,
    PAIRED_FLOOR,
    QUANTILE_GRID,
    STUDY_SCHEMA,
    ForecastSourceId,
    ForecastSpec,
    LedgerRow,
    ModelReceipt,
    OriginTally,
    forecast_run_id,
    tally_identity_ok,
)
from tree_options.research.forecast.refusal_codes import (
    EXCLUDE_INSUFFICIENT_HISTORY,
    EXCLUDE_TARGET_BEYOND_DATA,
    FAIL_FIT,
    FAIL_NON_FINITE,
    FAIL_QUANTILE_ORDERING,
    FORECAST_ENGINE_CHANGED,
    FORECAST_HORIZON_NOT_ENABLED,
    FORECAST_INSUFFICIENT_HISTORY,
    FORECAST_INSUFFICIENT_ORIGINS,
    FORECAST_INVALID_QUANTILE_GRID,
    FORECAST_REFUSAL_KINDS,
    FORECAST_SOURCE_DRIFT,
    FORECAST_SOURCE_INVALID,
    FORECAST_UNKNOWN_SOURCE,
    REASON_BENCH_LOSS_NONPOSITIVE,
    REASON_BOOTSTRAP_DEGENERATE,
    REASON_DM_NO_VARIANCE,
    REASON_PAIRED_COHORT_INSUFFICIENT,
    ForecastRefusal,
)
from tree_options.research.forecast.spec_io import (
    FORECAST_SPEC_FIELDS,
    forecast_from_dict,
)

__all__ = [
    "BOOTSTRAP_BLOCK",
    "DM_LAG_ORIGIN_UNITS",
    "DM_LAG_SENSITIVITY",
    "EMPIRICAL_WINDOW_SESSIONS",
    "EXCLUDE_INSUFFICIENT_HISTORY",
    "EXCLUDE_TARGET_BEYOND_DATA",
    "FAIL_FIT",
    "FAIL_NON_FINITE",
    "FAIL_QUANTILE_ORDERING",
    "FORECAST_ENGINE_CHANGED",
    "FORECAST_HORIZON_NOT_ENABLED",
    "FORECAST_INSUFFICIENT_HISTORY",
    "FORECAST_INSUFFICIENT_ORIGINS",
    "FORECAST_INVALID_QUANTILE_GRID",
    "FORECAST_REFUSAL_KINDS",
    "FORECAST_SOURCE_DRIFT",
    "FORECAST_SOURCE_INVALID",
    "FORECAST_SPEC_FIELDS",
    "FORECAST_UNKNOWN_SOURCE",
    "MIN_HISTORY_SESSIONS",
    "N_BOOTSTRAP",
    "ORIGIN_FLOOR",
    "PAIRED_FLOOR",
    "QUANTILE_GRID",
    "REASON_BENCH_LOSS_NONPOSITIVE",
    "REASON_BOOTSTRAP_DEGENERATE",
    "REASON_DM_NO_VARIANCE",
    "REASON_PAIRED_COHORT_INSUFFICIENT",
    "STUDY_SCHEMA",
    "ForecastRefusal",
    "ForecastSourceId",
    "ForecastSpec",
    "LedgerRow",
    "ModelReceipt",
    "OriginTally",
    "forecast_from_dict",
    "forecast_run_id",
    "session_authority_sha256",
    "tally_identity_ok",
]


def __getattr__(name: str):
    """Lazy access for the engine-side surface (metrics / sources /
    harness / engine) — keeps the import graph acyclic exactly like the
    scenarios package: importing the contracts or spec_io must never pull
    the engine in."""
    if name in {
        "evaluate_forecast",
        "ForecastOutcome",
        "SOURCE_REGISTRY",
        "SourceDescriptor",
        "ForecastSeries",
        "load_synthetic",
        "load_index",
        "INTERVAL_SEMANTICS",
        "DEFAULT_MODELS",
        "BASELINE_MODEL",
        "session_authority_sha256",
    }:
        from tree_options.research.forecast import engine as _engine_mod
        from tree_options.research.forecast import sources as _sources_mod

        g = {**_engine_mod.__dict__, **_sources_mod.__dict__}
        value = g.get(name)
        if value is not None:
            return value
    raise AttributeError(f"module 'forecast' has no attribute {name!r}")
