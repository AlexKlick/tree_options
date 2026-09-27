"""Forecast evaluation engine (RL-3) — orchestration and receipt.

``evaluate_forecast`` never trusts the HTTP layer: the registry and
horizon policy are RE-CHECKED here (a spec written directly into the
store bypassing the routes gets the same enforcement). Refusal order:

    unknown source -> horizon not enabled -> invalid grid ->
    insufficient history -> (evaluate) -> insufficient origins
    (ANY model below the floor, with the full tally AND the ledgers
    retained in the refusal — the refusal is the receipt of the
    attempt, never a bare "no").

The receipt separates status vocabulary on purpose:
``execution_status`` (run lifecycle), ``evaluation_status`` (floor and
counts), ``calibration_status`` — always ``not_claimed`` in v1. Every
forecast run is EXPLORATORY in v1 (the spec surface deliberately
carries no access-mode field; the study block records the constant).

Skill and DM are computed on the MATCHED evaluated origins only, with
PAIRED_FLOOR; the wire publishes paired counts and both paired losses,
and the DM block records direction, lag units, and the sensitivity
band. Anyone can re-derive every displayed number from the ledger —
that is the point of the ledger.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

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
    ModelReceipt,
    OriginTally,
)
from tree_options.research.forecast.harness import (
    QUANTILE_METHOD,
    ar1_direct,
    bind,
    evaluate_model,
    forward_fan,
    month_origin_grid,
    rw_full,
    rw_window,
)
from tree_options.research.forecast.metrics import (
    coverage_bootstrap_ci,
    dm_on_differentials,
    empirical_coverage,
    grid_quantile_score,
    mean_interval_width,
    skill_matched,
    wilson_interval,
)
from tree_options.research.forecast.refusal_codes import (
    FORECAST_HORIZON_NOT_ENABLED,
    FORECAST_INSUFFICIENT_HISTORY,
    FORECAST_INSUFFICIENT_ORIGINS,
    FORECAST_INVALID_QUANTILE_GRID,
    FORECAST_UNKNOWN_SOURCE,
    REASON_BOOTSTRAP_DEGENERATE,
    REASON_DM_NO_VARIANCE,
    ForecastRefusal,
)
from tree_options.research.forecast.sources import (
    SOURCE_REGISTRY,
    ForecastSeries,
)

#: v1 access mode — every forecast run is exploratory (recorded in the
#: study block; the spec surface carries no access-mode field).
ACCESS_MODE: Final[str] = "exploratory"

#: (name, raw model, is_baseline) — the baseline CANNOT be dropped:
#: the handoff §10 "comparison to a baseline is visible" row must not
#: be hideable by omission. Raw models are ``f(closes, *, h, taus)``;
#: the ENGINE binds them per run via ``harness.bind``.
RawModel = Callable[..., tuple[float, ...] | None]
ModelSpec = tuple[str, RawModel, bool]
DEFAULT_MODELS: Final[tuple[ModelSpec, ...]] = (
    ("rw_full", rw_full, True),
    ("rw_window", rw_window, False),
    ("ar1_direct", ar1_direct, False),
)


@dataclass(frozen=True)
class ForecastOutcome:
    """A complete evaluation receipt (to_wire is the published shape)."""

    spec: ForecastSpec
    series: ForecastSeries
    origins: OriginTally
    models: tuple[ModelReceipt, ...]
    forward: dict[str, Any]
    calendar_sha256: str
    engine_sha256: str
    #: per-model failed-origin counts, published beside the GRID-level
    #: headline so the wire reconciles without opening the ledgers
    failed_by_model: dict[str, int]

    def to_wire(self) -> dict[str, Any]:
        descriptor = SOURCE_REGISTRY[self.spec.source]
        origins = dict(self.origins.to_dict())
        origins["failed_by_model"] = dict(self.failed_by_model)
        return {
            "schema": "research-forecast-result/1",
            "source": self.spec.source.value,
            "source_basis": descriptor.basis,
            "grid_basis": descriptor.grid_basis,
            "horizon": self.spec.horizon,
            "quantile_grid": list(QUANTILE_GRID),
            "quantile_interpolation": QUANTILE_METHOD,
            "series": self.series.summary(),
            "evaluation_window": {
                "start": self.spec.evaluation_start.isoformat(),
                "end": (self.spec.evaluation_end.isoformat()
                        if self.spec.evaluation_end else None),
            },
            "study": self._study_block(),
            "execution_status": "completed",
            "evaluation_status": (
                "receipt_published" if self.origins.floor_met
                else "below_floor"),
            "calibration_status": "not_claimed",
            "origins": origins,
            "models": [m.to_dict() for m in self.models],
            "forward": self.forward,
            "refusal": None,
        }

    def _study_block(self) -> dict[str, Any]:
        return {
            "schema": STUDY_SCHEMA,
            "estimand": "distributional forecast quality of the "
                        "h-session-ahead series level",
            "target": "close at the h-th grid session after the origin",
            "data_vintage": {
                "series_sha256": self.series.series_sha256,
                "basis": self.series.basis,
                "grid_basis": self.series.grid_basis,
                "provenance": dict(self.series.provenance),
            },
            "windows": {
                "evaluation_start":
                    self.spec.evaluation_start.isoformat(),
                "evaluation_end": (
                    self.spec.evaluation_end.isoformat()
                    if self.spec.evaluation_end else None),
            },
            "models": [m.model for m in self.models],
            "benchmark": next(
                (m.model for m in self.models if m.is_baseline), None),
            "primary_score": "grid_quantile_score",
            "inference": {
                "dm_direction": "baseline loss - model loss",
                "dm_lag": DM_LAG_ORIGIN_UNITS,
                "dm_lag_units": "origin_index",
                "dm_sensitivity": list(DM_LAG_SENSITIVITY),
                "origin_sequence": "evaluated origins are COMPRESSED to "
                                   "a consecutive sequence: failed or "
                                   "excluded origins are dropped, and "
                                   "lag / block units are positions in "
                                   "the evaluated sequence, not "
                                   "calendar months",
                "wilson": "binomial approximation; time-ordered "
                          "origins, dependence not captured",
                "bootstrap_block": BOOTSTRAP_BLOCK,
            },
            "model_notes": {
                "rw_full": "expanding window over ALL eligible h-step "
                           "log changes; median is the empirical median, "
                           "not recentered",
                "rw_window": f"trailing {EMPIRICAL_WINDOW_SESSIONS} "
                             "eligible h-step log changes",
                "ar1_direct": "OLS AR(1) on the log level; bands are "
                              "quantiles of IN-SAMPLE direct h-step "
                              "prediction errors; parameter uncertainty "
                              "NOT modeled; |phi| >= 1 refuses",
            },
            "access_mode": ACCESS_MODE,
        }


def _bootstrap_seed(source: ForecastSourceId, horizon: int, model: str,
                    series_sha256: str) -> int:
    material = (f"rl3-bootstrap/1|{source.value}|{horizon}|{model}"
                f"|{series_sha256}").encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _loss_by_date(receipt_ledger: Sequence[Any]) -> dict[date, float]:
    """Per-origin aggregate loss (2 x mean-over-tau pinball) keyed by
    origin date over EVALUATED rows — re-derived from the ledger."""
    out: dict[date, float] = {}
    for row in receipt_ledger:
        if row.status == "evaluated" and row.losses_by_tau:
            out[row.origin_date] = 2.0 * (
                sum(row.losses_by_tau) / len(row.losses_by_tau))
    return out


def evaluate_forecast(
    spec: ForecastSpec,
    *,
    series: ForecastSeries,
    calendar_sha256: str,
    engine_sha256: str,
    models: Sequence[ModelSpec] | None = None,
    min_history: int = MIN_HISTORY_SESSIONS,
    origin_floor: int = ORIGIN_FLOOR,
) -> ForecastOutcome | ForecastRefusal:
    """Evaluate every model over the rolling-origin grid and assemble
    the receipt — or refuse, with the evidence retained."""
    descriptor = SOURCE_REGISTRY.get(spec.source)
    if descriptor is None:
        return ForecastRefusal(
            code=FORECAST_UNKNOWN_SOURCE,
            message=f"source {spec.source.value!r} is not in the registry",
        )
    if spec.horizon not in descriptor.enabled_horizons:
        return ForecastRefusal(
            code=FORECAST_HORIZON_NOT_ENABLED,
            message=(f"horizon {spec.horizon} is not enabled for "
                     f"{spec.source.value} (enabled "
                     f"{list(descriptor.enabled_horizons)}, listed "
                     f"{list(descriptor.listed_horizons)}; listed-but-"
                     f"disabled horizons are illustrative only)"),
            payload={"enabled": list(descriptor.enabled_horizons),
                     "listed": list(descriptor.listed_horizons)},
        )
    if not QUANTILE_GRID or any(not 0.0 < t < 1.0 for t in QUANTILE_GRID) \
            or list(QUANTILE_GRID) != sorted(QUANTILE_GRID):
        return ForecastRefusal(
            code=FORECAST_INVALID_QUANTILE_GRID,
            message="the declared quantile grid is not a valid grid",
        )
    needed = min_history + spec.horizon
    if len(series.sessions) < needed:
        return ForecastRefusal(
            code=FORECAST_INSUFFICIENT_HISTORY,
            message=(f"series has {len(series.sessions)} sessions; "
                     f"needs >= {needed} (min_history {min_history} + "
                     f"horizon {spec.horizon})"),
        )

    model_specs = list(models) if models is not None else \
        list(DEFAULT_MODELS)
    baselines = [name for name, _f, is_b in model_specs if is_b]
    if len(baselines) != 1:
        raise ValueError(
            f"exactly one baseline model required, got {baselines}")

    grid = month_origin_grid(
        series.sessions,
        first_eval=spec.evaluation_start,
        last_eval=spec.evaluation_end,
        horizon=spec.horizon,
        min_history=min_history,
    )

    runs = [
        evaluate_model(
            series.sessions, series.closes, grid=grid,
            horizon=spec.horizon, taus=QUANTILE_GRID, model_name=name,
            model=bind(factory, h=spec.horizon, taus=QUANTILE_GRID),
        )
        for name, factory, _is_b in model_specs
    ]

    below = [r for r in runs if r.n_evaluated < origin_floor]
    if grid.total == 0 or below:
        # Headline tally is the GRID's (per-model tallies travel in the
        # payload with their ledgers — the refusal is the receipt of
        # the attempt).
        grid_tally = OriginTally(
            total=grid.total,
            evaluated=len(grid.origins),
            excluded=len(grid.excluded),
            excluded_reasons=grid.reasons(),
            floor=origin_floor,
            floor_met=len(grid.origins) >= origin_floor,
        )
        return ForecastRefusal(
            code=FORECAST_INSUFFICIENT_ORIGINS,
            message=(f"horizon {spec.horizon} on {spec.source.value}: "
                     + "; ".join(
                         f"{r.model} evaluated {r.n_evaluated} origins "
                         f"(< floor {origin_floor})" for r in below)
                     + (f"; grid has {grid.total} month-start origins "
                        f"{dict(grid.reasons())}" if grid.total else
                        " no month-start origins in the window")),
            payload={
                "origins": grid_tally.to_dict(),
                "grid_reasons": grid.reasons(),
                "models": [
                    {"model": r.model,
                     "tally": r.tally(total=grid.total,
                                      floor=origin_floor).to_dict(),
                     "ledger": [row.to_dict() for row in r.ledger]}
                    for r in runs
                ],
            },
        )

    baseline_name = baselines[0]
    baseline_run = next(r for r in runs if r.model == baseline_name)
    baseline_loss_by_date = _loss_by_date(baseline_run.ledger)

    receipts: list[ModelReceipt] = []
    for (name, _factory, is_baseline), run in zip(
            model_specs, runs, strict=True):
        receipts.append(ModelReceipt(
            model=name,
            is_baseline=is_baseline,
            n_evaluated=run.n_evaluated,
            n_failed=run.n_failed,
            failure_reasons=dict(run.failure_reasons),
            metrics=_metrics_for(
                spec, run, series_sha256=series.series_sha256,
                baseline_name=baseline_name,
                baseline_loss_by_date=baseline_loss_by_date),
            ledger=run.ledger,
        ))

    forward: dict[str, Any] = {
        "origin_session": series.sessions[-1].isoformat(),
        "last_close": series.closes[-1],
        "horizon_sessions": spec.horizon,
        "beyond_data": True,
        "target_session": None,
        "fan": [],
    }
    for (name, factory, _is_b) in model_specs:
        levels = forward_fan(
            series.closes,
            bind(factory, h=spec.horizon, taus=QUANTILE_GRID))
        if levels is None:
            # A latest-fit failure (e.g. AR(1) explosive on the full
            # sample) must not VANISH from the receipt: the model gets
            # an explicit unavailable entry (checkpoint B, P2-5).
            forward["fan"].append(
                {"model": name, "status": "unavailable"})
            continue
        forward["fan"].append({
            "model": name,
            "status": "ok",
            "quantiles": dict(
                (f"{t:.2f}", q)
                for t, q in zip(QUANTILE_GRID, levels, strict=True)),
        })

    # Headline tally is the GRID's (checkpoint B-prime: a baseline-only
    # tally cannot reconcile on the wire when the baseline fails
    # origins — its shape has no `failed` count). Grid-level
    # ``total == evaluated + excluded`` holds by construction; each
    # model's own evaluated/excluded/failed identity is asserted inside
    # its tally() and its failures are counted here by name.
    tally = OriginTally(
        total=grid.total,
        evaluated=len(grid.origins),
        excluded=len(grid.excluded),
        excluded_reasons=grid.reasons(),
        floor=origin_floor,
        floor_met=len(grid.origins) >= origin_floor,
    )
    failed_by_model = {r.model: r.n_failed for r in runs}
    return ForecastOutcome(
        spec=spec,
        series=series,
        origins=tally,
        models=tuple(receipts),
        forward=forward,
        calendar_sha256=calendar_sha256,
        engine_sha256=engine_sha256,
        failed_by_model=failed_by_model,
    )


def _contains_non_finite(obj: object) -> bool:
    """Recursive scan: any non-finite float anywhere in a metrics tree."""
    if isinstance(obj, float):
        return not math.isfinite(obj)
    if isinstance(obj, dict):
        return any(_contains_non_finite(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_contains_non_finite(v) for v in obj)
    return False


def _degraded_metrics(n: int) -> dict[str, Any]:
    """The honest fallback when finite per-origin losses overflow an
    aggregate (checkpoint B-prime): the metrics are WITHHELD with a
    reason instead of published as non-finite (which the wire cannot
    even hash) or crashing the run and losing the ledger."""
    return {
        "aggregate_status": "non_finite",
        "reason": "finite per-origin losses produced a non-finite "
                  "aggregate (overflow); metrics withheld rather than "
                  "published as non-finite; the ledger is intact and "
                  "re-derivable",
        "n_evaluated": n,
    }


def _metrics_for(
    spec: ForecastSpec,
    run: Any,
    *,
    series_sha256: str,
    baseline_name: str,
    baseline_loss_by_date: dict[date, float],
) -> dict[str, Any]:
    """Assemble one model's metrics from its run (everything
    re-derivable from the ledger)."""
    taus = QUANTILE_GRID
    losses = run.per_tau_losses
    n = len(losses)
    per_tau_mean = {
        f"{t:.2f}": (sum(row[k] for row in losses) / n)
        for k, t in enumerate(taus)
    }
    score = grid_quantile_score(list(per_tau_mean.values()))

    lo = [q[0] for q in run.level_quantiles]
    hi = [q[-1] for q in run.level_quantiles]
    hits, n_cov = empirical_coverage(run.actuals, lo, hi)
    wilson_low, wilson_high = wilson_interval(hits, n_cov)
    seed = _bootstrap_seed(spec.source, spec.horizon, run.model,
                           series_sha256)
    indicators = [1.0 if flag else 0.0 for flag in run.inside_90]
    boot = coverage_bootstrap_ci(
        indicators, block_size=BOOTSTRAP_BLOCK,
        iterations=N_BOOTSTRAP, seed=seed)

    metrics: dict[str, Any] = {
        "pinball_by_tau": per_tau_mean,
        "grid_quantile_score": score,
        "coverage_90": {
            "hits": hits,
            "n": n_cov,
            "point": hits / n_cov if n_cov else None,
            "wilson_low": wilson_low,
            "wilson_high": wilson_high,
            "wilson_note": "binomial approximation; time-ordered "
                           "origins, dependence not captured",
            "bootstrap_low": boot.lower if boot else None,
            "bootstrap_high": boot.upper if boot else None,
            "bootstrap_block": BOOTSTRAP_BLOCK,
            "bootstrap_seed": seed,
            **({} if boot else
               {"bootstrap_reason": REASON_BOOTSTRAP_DEGENERATE}),
        },
        "mean_width_90": mean_interval_width(lo, hi),
    }

    if run.model == baseline_name:
        metrics["skill_vs_baseline"] = None
        if _contains_non_finite(metrics):
            return _degraded_metrics(n)
        return metrics

    model_loss_by_date = _loss_by_date(run.ledger)
    matched = sorted(
        set(model_loss_by_date) & set(baseline_loss_by_date))
    skill = skill_matched(
        [model_loss_by_date[d] for d in matched],
        [baseline_loss_by_date[d] for d in matched],
        paired_floor=PAIRED_FLOOR,
    )
    dm_block: dict[str, Any] | None = None
    dm_reason: str | None = None
    if skill["pinball_skill"] is None and skill["reason"]:
        dm_reason = str(skill["reason"])
    else:
        d = [baseline_loss_by_date[m] - model_loss_by_date[m]
             for m in matched]
        dm = dm_on_differentials(d, lag=DM_LAG_ORIGIN_UNITS)
        if dm is None:
            dm_block = None
            dm_reason = REASON_DM_NO_VARIANCE
        else:
            dm_block = {
                "n": dm.n,
                "mean": dm.mean,
                "stat": dm.stat,
                "p_one_sided": dm.p_one_sided,
                "lag": DM_LAG_ORIGIN_UNITS,
                "lag_units": "origin_index",
                "direction": "baseline loss - model loss",
                "series_note": "differentials are the matched EVALUATED "
                               "origins in origin order; gaps from "
                               "failed or excluded origins are dropped, "
                               "not modeled",
                "sensitivity": {},
            }
            for lag in DM_LAG_SENSITIVITY:
                alt = dm_on_differentials(d, lag=lag)
                dm_block["sensitivity"][str(lag)] = (
                    {"stat": alt.stat, "p_one_sided": alt.p_one_sided}
                    if alt is not None else None)
    metrics["skill_vs_baseline"] = {
        "baseline": baseline_name,
        **{k: v for k, v in skill.items()},
        "dm": dm_block,
        **({"dm_unavailable_reason": dm_reason} if dm_reason else {}),
    }
    if _contains_non_finite(metrics):
        return _degraded_metrics(n)
    return metrics


__all__ = [
    "ACCESS_MODE",
    "DEFAULT_MODELS",
    "ForecastOutcome",
    "ModelSpec",
    "evaluate_forecast",
]
