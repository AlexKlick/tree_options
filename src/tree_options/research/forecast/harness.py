"""Rolling-origin evaluation harness (RL-3).

Semantics mirrored from the desk's pre-registered lanes
(``desk/har.py walk_forward``): monthly refit cadence — origins are the
FIRST session of each month — and fitting strictly on data available
before the origin. Training eligibility is stated in COMPLETED
targets: an h-step training pair starting at u is eligible at origin t
only when u + h <= t (an incomplete target is future information).

Uncensoring is structural (handoff §10): every month-start origin in
the declared window lands in the ledger as exactly one of
evaluated / excluded (reason recorded) / failed (reason recorded), and
the per-model identity ``total == evaluated + excluded + failed`` is
enforced — never merely displayed.

The forecast object is a LOG-SCALE quantile vector per (model, origin);
the harness exponentiates to levels, checks finiteness AND ordering
(a violation is a FAILED origin — never repaired, never dropped), and
records the full per-origin trace in the ledger.

Quantile interpolation is DECLARED: ``numpy.quantile(..., method=
"linear")`` — the receipt's study block carries the convention so any
number in it is re-derivable. Models are pure functions: each is
``f(closes, *, h, taus) -> log-quantiles | None`` and is bound into a
``closes -> ...`` callable per evaluation — no module-global state.
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final, Literal

import numpy as np

from tree_options.desk.stats import ols
from tree_options.research.forecast.contracts import (
    EMPIRICAL_WINDOW_SESSIONS,
    LedgerRow,
    OriginTally,
)
from tree_options.research.forecast.refusal_codes import (
    EXCLUDE_INSUFFICIENT_HISTORY,
    EXCLUDE_TARGET_BEYOND_DATA,
    FAIL_FIT,
    FAIL_NON_FINITE,
    FAIL_QUANTILE_ORDERING,
)

#: Declared quantile interpolation convention (recorded in the study
#: block; every empirical quantile in a receipt uses it).
QUANTILE_METHOD: Final[Literal["linear"]] = "linear"

#: A bound model: closes-through-origin -> log-quantile vector (one per
#: tau, same order) | None (the origin FAILED for this model).
BoundModel = Callable[[tuple[float, ...]], tuple[float, ...] | None]
#: A model factory: (horizon, taus) -> BoundModel.
ModelFactory = Callable[[int, tuple[float, ...]], BoundModel]


@dataclass(frozen=True)
class OriginGrid:
    """Month-start origins in the window, classified up front."""

    origins: tuple[int, ...]
    excluded: tuple[tuple[int, str], ...]

    def reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, reason in self.excluded:
            out[reason] = out.get(reason, 0) + 1
        return out

    @property
    def total(self) -> int:
        return len(self.origins) + len(self.excluded)


def month_origin_grid(
    sessions: Sequence[date],
    *,
    first_eval: date,
    last_eval: date | None,
    horizon: int,
    min_history: int,
) -> OriginGrid:
    """Month-start origins within the declared window, each classified.

    ``total`` counts EVERY month start in the window — an origin that
    cannot run is excluded WITH a reason, never omitted from the count.
    ``min_history`` counts ELIGIBLE h-step training pairs (u + h <= t),
    not raw sessions.
    """
    last_index = len(sessions) - 1
    origins: list[int] = []
    excluded: list[tuple[int, str]] = []
    for i, d in enumerate(sessions):
        if i > 0 and (d.year, d.month) == (
                sessions[i - 1].year, sessions[i - 1].month):
            continue  # not a month start
        if d < first_eval:
            continue
        if last_eval is not None and d > last_eval:
            continue
        if i + horizon > last_index:
            excluded.append((i, EXCLUDE_TARGET_BEYOND_DATA))
            continue
        if (i - horizon + 1) < min_history:
            excluded.append((i, EXCLUDE_INSUFFICIENT_HISTORY))
            continue
        origins.append(i)
    return OriginGrid(origins=tuple(origins), excluded=tuple(excluded))


@dataclass(frozen=True)
class ModelRun:
    """One model's raw evaluation output (metrics assemble later)."""

    model: str
    ledger: tuple[LedgerRow, ...] = field(default=())
    #: aligned arrays over EVALUATED origins only:
    actuals: tuple[float, ...] = ()
    level_quantiles: tuple[tuple[float, ...], ...] = ()
    per_tau_losses: tuple[tuple[float, ...], ...] = ()
    inside_90: tuple[bool, ...] = ()
    failure_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def n_evaluated(self) -> int:
        return len(self.actuals)

    @property
    def n_failed(self) -> int:
        return sum(self.failure_reasons.values())

    def tally(self, *, total: int, floor: int) -> OriginTally:
        """Per-model tally over the grid; ``total`` is the grid's origin
        count so the enforced identity
        ``total == evaluated + excluded + failed`` is checked HERE."""
        excluded_n = sum(
            1 for row in self.ledger if row.status == "excluded")
        assert total == self.n_evaluated + excluded_n + self.n_failed, (
            f"tally identity violated for {self.model}: "
            f"{total} != {self.n_evaluated} + {excluded_n} "
            f"+ {self.n_failed}")
        reasons: dict[str, int] = {}
        for row in self.ledger:
            if row.status == "excluded" and row.reason:
                reasons[row.reason] = reasons.get(row.reason, 0) + 1
        return OriginTally(
            total=total,
            evaluated=self.n_evaluated,
            excluded=excluded_n,
            excluded_reasons=reasons,
            floor=floor,
            floor_met=self.n_evaluated >= floor,
        )


def evaluate_model(
    sessions: Sequence[date],
    closes: Sequence[float],
    *,
    grid: OriginGrid,
    horizon: int,
    taus: Sequence[float],
    model_name: str,
    model: BoundModel,
) -> ModelRun:
    """Run one bound model over the grid, building the uncensored
    ledger.

    ``model`` receives ``closes[: t + 1]`` — the closes THROUGH the
    origin and nothing after (the future-data boundary is the slice
    itself). A None return is a FAILED origin (``fit_failed``); a
    non-finite or unordered log-quantile vector fails with its specific
    reason. Failures never remove the origin from the tally, and a
    failed origin still records the actual it would have scored.
    """
    taus_t = tuple(float(t) for t in taus)
    rows: list[tuple[int, LedgerRow]] = []
    actuals: list[float] = []
    level_qs: list[tuple[float, ...]] = []
    per_tau: list[tuple[float, ...]] = []
    inside: list[bool] = []
    failures: dict[str, int] = {}

    for idx, reason in grid.excluded:
        target = (sessions[idx + horizon]
                  if idx + horizon < len(sessions) else None)
        rows.append((idx, LedgerRow(
            origin_date=sessions[idx],
            target_date=target,
            training_count=max(0, idx - horizon + 1),
            status="excluded",
            reason=reason,
            actual=None,
        )))

    for t in grid.origins:
        log_q = model(tuple(closes[: t + 1]))
        target = sessions[t + horizon]
        actual = closes[t + horizon]
        training_count = t - horizon + 1
        failed: str | None = None
        vals: tuple[float, ...] = ()
        if log_q is None:
            failed = FAIL_FIT
        else:
            vals = tuple(float(v) for v in log_q)
            if any(not math.isfinite(v) for v in vals):
                failed = FAIL_NON_FINITE
            elif any(b < a for a, b in itertools.pairwise(vals)):
                failed = FAIL_QUANTILE_ORDERING
        if failed is not None:
            failures[failed] = failures.get(failed, 0) + 1
            rows.append((t, LedgerRow(
                origin_date=sessions[t], target_date=target,
                training_count=training_count, status="failed",
                reason=failed, actual=actual)))
            continue
        levels = tuple(math.exp(v) for v in vals)
        losses = tuple(
            tau * (actual - q) if actual >= q else (1.0 - tau) * (q - actual)
            for tau, q in zip(taus_t, levels, strict=True)
        )
        actuals.append(actual)
        level_qs.append(levels)
        per_tau.append(losses)
        inside.append(levels[0] <= actual <= levels[-1])
        rows.append((t, LedgerRow(
            origin_date=sessions[t], target_date=target,
            training_count=training_count, status="evaluated",
            reason=None, actual=actual, quantiles=levels,
            losses_by_tau=losses)))

    rows.sort(key=lambda pair: pair[0])
    return ModelRun(
        model=model_name,
        ledger=tuple(row for _, row in rows),
        actuals=tuple(actuals),
        level_quantiles=tuple(level_qs),
        per_tau_losses=tuple(per_tau),
        inside_90=tuple(inside),
        failure_reasons=failures,
    )


def forward_fan(
    closes: Sequence[float],
    model: BoundModel,
) -> tuple[float, ...] | None:
    """Latest-origin forward quantiles: fit on ALL data, forecast
    beyond the data edge. Levels (exponentiated), or None when the
    model fails / is non-finite / unordered on the full sample.
    ``beyond_data`` labeling and receipt linkage are the engine's job."""
    log_q = model(tuple(closes))
    if log_q is None:
        return None
    vals = [float(v) for v in log_q]
    if any(not math.isfinite(v) for v in vals):
        return None
    if any(b < a for a, b in itertools.pairwise(vals)):
        return None
    return tuple(math.exp(v) for v in vals)


# ---------------------------------------------------------------- models
# Pure functions of (closes, h, taus); the engine binds them per run.


def _eligible_h_step_changes(
    closes: Sequence[float], h: int, window: int | None,
) -> np.ndarray[tuple[int], np.dtype[np.float64]]:
    """log(c[u+h]) - log(c[u]) over eligible u (u + h <= last index) —
    COMPLETED targets only — optionally truncated to the trailing
    ``window`` changes (rw_window's declared estimation window)."""
    logs = np.log(np.asarray(closes, dtype=np.float64))
    n = len(closes)
    changes = logs[h:] - logs[: n - h]
    if window is not None and changes.shape[0] > window:
        changes = changes[-window:]
    return changes


def _log_quantiles_from_changes(
    last_close: float,
    changes: np.ndarray[tuple[int], np.dtype[np.float64]],
    taus: tuple[float, ...],
) -> tuple[float, ...]:
    qs = [float(np.quantile(changes, float(t), method=QUANTILE_METHOD))
          for t in taus]
    return tuple(math.log(last_close) + q for q in qs)


def rw_full(closes: tuple[float, ...], *, h: int,
            taus: tuple[float, ...]) -> tuple[float, ...] | None:
    """Empirical-change benchmark, EXPANDING window: quantiles of ALL
    historical h-step log changes placed around the last close. The
    median is the EMPIRICAL median — NOT recentered to the last close
    (it is an empirical-change benchmark, named as such; a persistence
    point forecast is a different, ambiguous object)."""
    changes = _eligible_h_step_changes(closes, h, window=None)
    if changes.shape[0] < 2:
        return None
    return _log_quantiles_from_changes(closes[-1], changes, taus)


def rw_window(closes: tuple[float, ...], *, h: int,
              taus: tuple[float, ...]) -> tuple[float, ...] | None:
    """Empirical-change benchmark, trailing EMPIRICAL_WINDOW_SESSIONS
    changes (the declared rolling estimation window)."""
    changes = _eligible_h_step_changes(
        closes, h, window=EMPIRICAL_WINDOW_SESSIONS)
    if changes.shape[0] < 2:
        return None
    return _log_quantiles_from_changes(closes[-1], changes, taus)


def ar1_direct(closes: tuple[float, ...], *, h: int,
               taus: tuple[float, ...]) -> tuple[float, ...] | None:
    """AR(1) on the log level with DIRECT h-step error bands.

    Point: x_{t+h} = beta0 * (1 + phi + ... + phi^(h-1)) + phi^h * x_t.
    Bands: quantiles of the in-sample DIRECT h-step prediction errors
    e_u = x_{u+h} - point_h(u) over eligible u (u + h <= t) — NOT
    one-step residuals scaled (h-step innovations have sd
    sigma * sqrt(sum phi^(2j)), 2.99x sigma at phi = 0.95, h = 20; the
    scaled-one-step construction is wrong and this model does not use
    it). |phi| >= 1 refuses (explosive); parameter uncertainty is NOT
    modeled (declared in the study block).
    """
    logs = np.log(np.asarray(closes, dtype=np.float64))
    if logs.shape[0] < h + 2:
        return None
    x = logs[:-1]
    y = logs[1:]
    design = np.column_stack([np.ones_like(x), x])
    try:
        beta, _resid = ols(design, y)
    except np.linalg.LinAlgError:
        return None
    beta0, phi = float(beta[0]), float(beta[1])
    if abs(phi) >= 1.0:
        return None
    geom = (1.0 - phi ** h) / (1.0 - phi)  # 1 + phi + ... + phi^(h-1)

    def point_h(idx: int) -> float:
        return beta0 * geom + (phi ** h) * float(logs[idx])

    last = logs.shape[0] - 1
    errors = np.asarray(
        [float(logs[u + h]) - point_h(u)
         for u in range(last - h + 1)], dtype=np.float64)
    if errors.shape[0] < 2:
        return None
    qs = [float(np.quantile(errors, float(t), method=QUANTILE_METHOD))
          for t in taus]
    return tuple(point_h(last) + q for q in qs)


def bind(model: Callable[..., tuple[float, ...] | None], *, h: int,
         taus: tuple[float, ...]) -> BoundModel:
    """Bind (h, taus) into a closes-only callable (per-run closure, no
    module-global state)."""
    def bound(closes: tuple[float, ...]) -> tuple[float, ...] | None:
        return model(closes, h=h, taus=taus)
    return bound


__all__ = [
    "QUANTILE_METHOD",
    "BoundModel",
    "ModelFactory",
    "ModelRun",
    "OriginGrid",
    "ar1_direct",
    "bind",
    "evaluate_model",
    "forward_fan",
    "month_origin_grid",
    "rw_full",
    "rw_window",
]
