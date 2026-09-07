"""M5 model diagnostics: resampled uncertainty, selection quality, calibration.

Pure, stdlib-only, deterministic — the conventions of ``evaluation.stats``
(``None`` for unevaluable quantities, exact validation, ``math.fsum``).
Resampling uses ``random.Random(seed)`` so every interval is reproducible
byte-for-byte from its declared seed.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BootstrapCI:
    """Moving-block bootstrap percentile interval for one statistic."""

    lower: float
    upper: float
    point: float
    valid_iterations: int
    block_size: int
    confidence: float


@dataclass(frozen=True)
class CalibrationBin:
    """One equal-width probability bin of a reliability curve."""

    lower: float
    upper: float
    count: int
    mean_probability: float | None
    mean_outcome: float | None


def _finite(values: Sequence[float], *, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"{name} values must be finite")
    return result


def _binary_relevance(labels: Sequence[float]) -> tuple[bool, ...]:
    """Relevance = strictly above the cross-sectional median label.

    A label exactly at the median (including the averaged middle pair of an
    even-length vector) is NOT relevant — selection quality is about finding
    the better half, and the median name is never in it.
    """
    median = statistics.median(labels)
    return tuple(label > median for label in labels)


def _order_by_score(scores: Sequence[float]) -> tuple[int, ...]:
    """Indices by score descending, ties broken by ascending index."""
    return tuple(sorted(range(len(scores)), key=lambda i: (-scores[i], i)))


def block_bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: Callable[[tuple[float, ...]], float | None],
    block_size: int,
    iterations: int,
    seed: int,
    confidence: float = 0.95,
) -> BootstrapCI | None:
    """Moving-block bootstrap percentile interval of ``statistic``.

    Conventions (declared, deterministic given ``seed``): resamples draw
    ``ceil(n / block_size)`` block start positions uniformly from
    ``0 .. n - block_size`` via ``random.Random(seed)``, concatenate the
    blocks, and truncate to ``n``; resamples whose statistic is ``None`` are
    skipped; bounds are nearest-rank percentiles of the surviving statistics.
    Returns ``None`` when the point statistic itself is unevaluable or no
    resample produced one.
    """
    sample = _finite(values, name="value")
    n = len(sample)
    if n < 2:
        return None
    if not 1 <= block_size <= n:
        raise ValueError("block_size must be between 1 and the sample size")
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")

    point = statistic(sample)
    if point is None or not math.isfinite(point):
        return None

    rng = random.Random(seed)
    draws = -(-n // block_size)  # ceil
    max_start = n - block_size
    stats: list[float] = []
    for _ in range(iterations):
        resample: list[float] = []
        for _ in range(draws):
            start = rng.randrange(max_start + 1)
            resample.extend(sample[start : start + block_size])
        value = statistic(tuple(resample[:n]))
        if value is not None and math.isfinite(value):
            stats.append(value)
    if not stats:
        return None

    ordered = sorted(stats)
    m = len(ordered)

    def nearest_rank(fraction: float) -> float:
        return ordered[max(0, min(m - 1, math.ceil(fraction * m) - 1))]

    tail = (1.0 - confidence) / 2.0
    return BootstrapCI(
        lower=nearest_rank(tail),
        upper=nearest_rank(1.0 - tail),
        point=point,
        valid_iterations=m,
        block_size=block_size,
        confidence=confidence,
    )


def ndcg_at_k(scores: Sequence[float], labels: Sequence[float], *, k: int) -> float | None:
    """NDCG@K with binary relevance (label strictly above the median).

    Ranking is by score descending with ties broken by ascending index;
    the discount of position ``i`` (1-based) is ``log2(i + 1)``.  ``k`` is
    capped at the cross-section size.  ``None`` when nothing is relevant
    (the ideal ranking is undefined, so normalized quality is too).
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    x = _finite(scores, name="score")
    y = _finite(labels, name="label")
    n = len(x)
    if k < 1:
        raise ValueError("k must be >= 1")
    if n < 2:
        return None
    depth = min(k, n)
    relevance = _binary_relevance(y)
    if not any(relevance):
        return None

    dcg = math.fsum(
        (1.0 if relevance[i] else 0.0) / math.log2(position + 2)
        for position, i in enumerate(_order_by_score(x)[:depth])
    )
    n_relevant = sum(relevance)
    idcg = math.fsum(1.0 / math.log2(position + 2) for position in range(min(depth, n_relevant)))
    return dcg / idcg


def selected_tail_precision(
    scores: Sequence[float], labels: Sequence[float], *, k: int
) -> float | None:
    """Fraction of the top-K-by-score names with above-median labels."""
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    x = _finite(scores, name="score")
    y = _finite(labels, name="label")
    if k < 1:
        raise ValueError("k must be >= 1")
    n = len(x)
    if n < 2:
        return None
    relevance = _binary_relevance(y)
    if not any(relevance):
        return None
    depth = min(k, n)
    selected = _order_by_score(x)[:depth]
    return sum(1 for i in selected if relevance[i]) / depth


def quantile_spread(
    scores: Sequence[float], labels: Sequence[float], *, quantiles: int
) -> float | None:
    """Mean label of the top score-quantile minus the bottom score-quantile.

    Assignment: names ranked by (score, index) ASCENDING; the name at
    0-based rank ``r`` lands in quantile ``r * quantiles // n``, so
    quantile 0 holds the lowest scores ("bottom") and quantile
    ``quantiles - 1`` the highest ("top").  Index order is the declared
    tiebreak, so equal scores can straddle a boundary — the same
    determinism every other selector here carries.
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    x = _finite(scores, name="score")
    y = _finite(labels, name="label")
    n = len(x)
    if quantiles < 2:
        raise ValueError("quantiles must be >= 2")
    if n < quantiles:
        return None
    order = tuple(sorted(range(n), key=lambda i: (x[i], i)))
    bottom: list[float] = []
    top: list[float] = []
    for rank, i in enumerate(order):
        bucket = rank * quantiles // n
        if bucket == 0:
            bottom.append(y[i])
        elif bucket == quantiles - 1:
            top.append(y[i])
    if not bottom or not top:
        return None
    return statistics.fmean(top) - statistics.fmean(bottom)


def brier_score(probabilities: Sequence[float], outcomes: Sequence[bool]) -> float:
    """Mean squared error of probability forecasts against binary outcomes."""
    p = _finite(probabilities, name="probability")
    if len(p) != len(outcomes):
        raise ValueError("probabilities and outcomes must have the same length")
    if not p:
        raise ValueError("at least one forecast is required")
    if any(not 0.0 <= value <= 1.0 for value in p):
        raise ValueError("probabilities must be between 0 and 1")
    if any(type(outcome) is not bool for outcome in outcomes):
        raise ValueError("outcomes must be bool")
    return math.fsum(
        (value - (1.0 if outcome else 0.0)) ** 2 for value, outcome in zip(p, outcomes, strict=True)
    ) / len(p)


def calibration_bins(
    probabilities: Sequence[float], outcomes: Sequence[bool], *, bins: int
) -> tuple[CalibrationBin, ...]:
    """Equal-width reliability bins over [0, 1]; empty bins carry ``None`` means."""
    p = _finite(probabilities, name="probability")
    if len(p) != len(outcomes):
        raise ValueError("probabilities and outcomes must have the same length")
    if bins < 1:
        raise ValueError("bins must be >= 1")
    if any(not 0.0 <= value <= 1.0 for value in p):
        raise ValueError("probabilities must be between 0 and 1")
    if any(type(outcome) is not bool for outcome in outcomes):
        raise ValueError("outcomes must be bool")
    counts = [0] * bins
    sums_p = [0.0] * bins
    sums_o = [0.0] * bins
    for value, outcome in zip(p, outcomes, strict=True):
        index = min(int(value * bins), bins - 1)
        counts[index] += 1
        sums_p[index] += value
        sums_o[index] += 1.0 if outcome else 0.0
    return tuple(
        CalibrationBin(
            lower=index / bins,
            upper=(index + 1) / bins,
            count=counts[index],
            mean_probability=(sums_p[index] / counts[index]) if counts[index] else None,
            mean_outcome=(sums_o[index] / counts[index]) if counts[index] else None,
        )
        for index in range(bins)
    )
