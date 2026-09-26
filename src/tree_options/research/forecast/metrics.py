"""Forecast scoring metrics (RL-3) — numpy for the vector losses, pure
Python for the interval arithmetic.

THE GRID SCORE IS NOT CRPS. For a quantile-function forecast the exact
identity is CRPS(F, y) = 2*integral ₀¹ rho_τ(y - Q(τ)) dτ — an INTEGRAL over all
τ. A finite, unequally spaced grid (ours: 0.05/0.25/0.5/0.75/0.95)
approximates that integral at best, and leaves the interpolation and
tail convention unspecified, so this module computes and names a GRID
SCORE — 2 x mean pinball over the declared grid — and never labels it
CRPS. The counterexample that keeps this honest (pinned by
``test_forecast_metrics.py``): for a Uniform[1, 2] forecast at y = 1.5,
the exact CRPS is 1/12 ~ 0.08333 while this grid score is 0.068 — 18%
low. A degenerate-forecast oracle alone cannot catch that distinction.

Floats throughout: these are statistics, not money (desk precedent,
``desk/har.py``). Every value that reaches a hashed payload passes
through ``desk.contracts.canonical`` (allow_nan=False); the harness
demotes non-finite model outputs to FAILED origins before metrics are
computed, so a non-finite metric is unreachable by construction.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final

import numpy as np

from tree_options.desk.stats import DMResult, dm_test
from tree_options.evaluation.diagnostics import (
    BootstrapCI,
    block_bootstrap_ci,
)
from tree_options.research.forecast.refusal_codes import (
    REASON_BENCH_LOSS_NONPOSITIVE,
    REASON_PAIRED_COHORT_INSUFFICIENT,
)

#: z for a two-sided 95% interval.
Z_95: Final[float] = 1.959963984540054


def pinball_losses(
    y_true: np.ndarray | Sequence[float],
    q_fore: np.ndarray | Sequence[float],
    taus: Sequence[float],
) -> dict[float, float]:
    """Mean standard pinball loss per tau over the origins.

    rho_tau(y, q) = tau*(y - q) when y >= q, else (1 - tau)*(q - y).
    NO factor of 2 — the grid score applies it once, so the identity
    ``grid score == 2 x mean-over-grid`` stays exact and checkable.

    ``y_true`` is (n,), ``q_fore`` is (n, K) with one column per tau in
    the same order as ``taus``. Returns {tau: mean loss}.
    """
    y = np.asarray(y_true, dtype=float)
    q = np.asarray(q_fore, dtype=float)
    t = np.asarray(taus, dtype=float)
    if q.ndim != 2 or q.shape[0] != y.shape[0] or q.shape[1] != t.shape[0]:
        raise ValueError(
            f"shape mismatch: y {y.shape}, q {q.shape}, taus {t.shape}")
    err = y[:, None] - q                       # (n, K)
    losses = np.maximum(t * err, (t - 1.0) * err)
    means = losses.mean(axis=0)
    return {float(tau): float(m) for tau, m in zip(taus, means, strict=True)}


def per_origin_grid_losses(
    y_true: np.ndarray | Sequence[float],
    q_fore: np.ndarray | Sequence[float],
    taus: Sequence[float],
) -> np.ndarray:
    """Per-origin aggregate loss: 2 x mean-over-grid pinball for each
    origin. This is the per-origin series skill scores and the DM test
    consume (matched origins only, at the engine layer)."""
    y = np.asarray(y_true, dtype=float)
    q = np.asarray(q_fore, dtype=float)
    t = np.asarray(taus, dtype=float)
    err = y[:, None] - q
    losses = np.maximum(t * err, (t - 1.0) * err)
    return 2.0 * losses.mean(axis=1)


def grid_quantile_score(mean_pinball: Sequence[float]) -> float:
    """2 x mean pinball over the declared grid — the aggregate GRID
    score (see module docstring: this is not CRPS)."""
    vals = list(mean_pinball)
    if not vals:
        raise ValueError("empty pinball sequence")
    return 2.0 * (sum(vals) / len(vals))


def empirical_coverage(
    y_true: np.ndarray | Sequence[float],
    lo: np.ndarray | Sequence[float],
    hi: np.ndarray | Sequence[float],
) -> tuple[int, int]:
    """(hits, n) for the central interval [lo, hi]. Bounds are
    INCLUSIVE by declaration: y == lo and y == hi are hits (the wire
    documents this; the tests pin it)."""
    y = np.asarray(y_true, dtype=float)
    lo_a = np.asarray(lo, dtype=float)
    hi_a = np.asarray(hi, dtype=float)
    if not (y.shape == lo_a.shape == hi_a.shape):
        raise ValueError("coverage inputs must share a shape")
    inside = (y >= lo_a) & (y <= hi_a)
    return int(inside.sum()), int(y.shape[0])


def wilson_interval(hits: int, n: int,
                    z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion — a BINOMIAL
    approximation: origins are time-ordered and their coverage
    indicators may be dependent, which this interval does not capture
    (the receipt labels it; the block-bootstrap sensitivity is the
    companion)."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= hits <= n:
        raise ValueError(f"hits {hits} outside [0, n={n}]")
    p = hits / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = (z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))) / denom
    return center - half, center + half


def coverage_bootstrap_ci(
    hit_indicators: Sequence[float],
    *,
    block_size: int,
    iterations: int,
    seed: int,
) -> BootstrapCI | None:
    """Moving-block bootstrap CI on the coverage proportion, reusing
    ``tree_options.evaluation.diagnostics.block_bootstrap_ci`` verbatim
    (declared deterministic conventions: ``random.Random(seed)``,
    moving blocks, nearest-rank percentiles).

    DEGENERATE inputs (all hits / all misses) return ``None`` by
    declaration — every resample reproduces [1, 1] or [0, 0], which is
    not uncertainty evidence; the caller records
    ``research.forecast.`` reason ``degenerate coverage`` and keeps the
    Wilson interval.
    """
    finite = [float(v) for v in hit_indicators]
    if len(finite) < 2:
        return None
    if all(v == finite[0] for v in finite):
        return None
    return block_bootstrap_ci(
        finite,
        statistic=lambda sample: (
            sum(sample) / len(sample) if sample else None),
        block_size=block_size,
        iterations=iterations,
        seed=seed,
        confidence=0.95,
    )


def mean_interval_width(
    lo: np.ndarray | Sequence[float],
    hi: np.ndarray | Sequence[float],
) -> float:
    lo_a = np.asarray(lo, dtype=float)
    hi_a = np.asarray(hi, dtype=float)
    if lo_a.shape != hi_a.shape or lo_a.size == 0:
        raise ValueError("width inputs must share a non-empty shape")
    return float((hi_a - lo_a).mean())


def skill_matched(
    model_paired: Sequence[float],
    bench_paired: Sequence[float],
    *,
    paired_floor: int,
) -> dict[str, object]:
    """Skill vs baseline on the MATCHED origin set only.

    Both sequences are per-origin grid losses over the SAME origins
    (the engine intersects the evaluated sets). Guard rails, each with
    a named reason on the wire:
      * ``paired_cohort_insufficient`` — fewer than ``paired_floor``
        matched origins (two models can each clear the origin floor
        with a 0/1-origin intersection; that must not emit a number);
      * ``baseline loss not positive`` — bench mean <= 0 (refuse to
        invent a skill ratio).
    """
    if len(model_paired) != len(bench_paired):
        raise ValueError("paired sequences must share a length")
    n = len(model_paired)
    if n < paired_floor:
        return {"paired_n": n, "loss_paired": None, "bench_paired": None,
                "pinball_skill": None,
                "reason": REASON_PAIRED_COHORT_INSUFFICIENT}
    loss = sum(model_paired) / n
    bench = sum(bench_paired) / n
    if bench <= 0.0:
        return {"paired_n": n, "loss_paired": loss, "bench_paired": bench,
                "pinball_skill": None,
                "reason": REASON_BENCH_LOSS_NONPOSITIVE}
    return {"paired_n": n, "loss_paired": loss, "bench_paired": bench,
            "pinball_skill": 1.0 - loss / bench, "reason": None}


def dm_on_differentials(
    d: Sequence[float], *, lag: int,
) -> DMResult | None:
    """Thin pass-through to ``desk.stats.dm_test``. ``d`` is the
    per-origin loss differential bench - model over the matched origins;
    ``lag`` is in ORIGIN units (the differential series is indexed by
    monthly origins, NOT sessions — see contracts.DM_LAG_ORIGIN_UNITS).
    Returns None when there is no loss-differential variance; the wire
    then records the reason instead of an invented p-value."""
    return dm_test(list(d), lag=lag)


__all__ = [
    "Z_95",
    "coverage_bootstrap_ci",
    "dm_on_differentials",
    "empirical_coverage",
    "grid_quantile_score",
    "mean_interval_width",
    "per_origin_grid_losses",
    "pinball_losses",
    "skill_matched",
    "wilson_interval",
]
