"""M5 statistical controls: overfitting, concentration, negative controls.

Pure, stdlib-only, deterministic.  These are the adversarial teeth of the
M5 gate: the deflated Sharpe ratio, combinatorially symmetric
cross-validation, concentration measures, and the deterministic
negative-control generators (block shuffle, random scores).

Conventions are DECLARED in each docstring because the literature varies;
where a paper wavers, the exact estimator used here is pinned so a result
computed once means the same thing forever.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

_EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True)
class DeflatedSharpe:
    """Deflated Sharpe ratio with its full declared estimator trail."""

    sharpe: float
    expected_max_sharpe: float
    n_sessions: int
    n_trials: int
    trial_variance: float
    skewness: float
    kurtosis: float  # non-excess (normal == 3)
    deflated: float


@dataclass(frozen=True)
class PboAssessment:
    """Probability-of-backtest-overfitting over CSCV combinations."""

    n_strategies: int
    n_sessions: int
    splits: int
    combinations: int
    pbo: float
    n_below_median: int
    mean_logit: float | None


def _finite(values: Sequence[float], *, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"{name} values must be finite")
    return result


def expected_max_sharpe(n_trials: int, trial_variance: float) -> float:
    """Expected maximum Sharpe among ``n_trials`` independent trials.

    Bailey & Lopez de Prado's ``SR0 = sqrt(V) * ((1 - gamma) * Z[1 - 1/N]
    + gamma * Z[1 - 1/(N e)])`` with the Euler-Mascheroni constant and the
    stdlib normal quantile — deterministic and exact to ``NormalDist``.
    """
    if n_trials < 2:
        raise ValueError("n_trials must be >= 2")
    if not math.isfinite(trial_variance) or trial_variance <= 0.0:
        raise ValueError("trial_variance must be finite and > 0")
    normal = statistics.NormalDist()
    left = normal.inv_cdf(1.0 - 1.0 / n_trials)
    right = normal.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(trial_variance) * (
        (1.0 - _EULER_MASCHERONI) * left + _EULER_MASCHERONI * right
    )


def deflated_sharpe_ratio(
    session_returns: Sequence[float],
    *,
    n_trials: int,
    trial_variance: float,
) -> DeflatedSharpe | None:
    """Deflated Sharpe ratio (per-session, NOT annualized).

    ``SR = mean / stdev(ddof=1)``; skewness ``g3 = m3 / m2**1.5`` and
    NON-EXCESS kurtosis ``g4 = m4 / m2**2`` over central moments
    ``m_k = sum((x - mean)^k) / T`` (population-style, per the DSR paper);
    ``DSR = Phi(((SR - SR0) * sqrt(T - 1)) / sqrt(1 - g3*SR + (g4 - 1)/4
    * SR**2))`` with ``SR0 = expected_max_sharpe(n_trials, trial_variance)``.
    ``None`` when the sample is too small, degenerate, or the denominator
    is non-positive.
    """
    sample = _finite(session_returns, name="session return")
    t = len(sample)
    if t < 2:
        return None
    mean = math.fsum(sample) / t
    m2 = math.fsum((value - mean) ** 2 for value in sample) / t
    if m2 == 0.0:
        return None
    variance = math.fsum((value - mean) ** 2 for value in sample) / (t - 1)
    sharpe = mean / math.sqrt(variance)
    m3 = math.fsum((value - mean) ** 3 for value in sample) / t
    m4 = math.fsum((value - mean) ** 4 for value in sample) / t
    skewness = m3 / m2**1.5
    kurtosis = m4 / m2**2
    denominator = math.sqrt(max(0.0, 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe**2))
    if denominator == 0.0:
        return None
    sr0 = expected_max_sharpe(n_trials, trial_variance)
    numerator = (sharpe - sr0) * math.sqrt(t - 1)
    return DeflatedSharpe(
        sharpe=sharpe,
        expected_max_sharpe=sr0,
        n_sessions=t,
        n_trials=n_trials,
        trial_variance=trial_variance,
        skewness=skewness,
        kurtosis=kurtosis,
        deflated=statistics.NormalDist().cdf(numerator / denominator),
    )


def cscv_pbo(
    returns_by_strategy: Sequence[Sequence[float]],
    *,
    splits: int = 16,
) -> PboAssessment | None:
    """Probability of backtest overfitting via combinatorially symmetric CV.

    DECLARED semantics (the plain-language definition of Bailey, Borwein,
    Lopez de Prado & Zhu): PBO is the fraction of train/test splittings in
    which the strategy that ranks best on the train blocks ranks BELOW the
    median on the test blocks.  Sessions are split into ``splits`` equal
    contiguous blocks; every choice of ``splits // 2`` blocks is one train
    set and its complement the test set; ranking statistic is the mean
    per-session return on the ranked side; ties break to the lower strategy
    index.  The logit column uses ``lambda = ln(w / (1 - w))`` with the
    0-based test rank mapped to ``w = (N - rank) / (N + 1)`` (high = good)
    so ``lambda`` is finite for every rank and positive exactly when the
    train-best beats the median.  ``None`` when the matrix is too small
    for the requested split count.
    """
    matrix = [_finite(row, name="strategy return") for row in returns_by_strategy]
    n = len(matrix)
    if n < 2:
        return None
    lengths = {len(row) for row in matrix}
    if len(lengths) != 1:
        raise ValueError("every strategy must cover the same sessions")
    sessions = lengths.pop()
    if splits < 2 or splits % 2 != 0:
        raise ValueError("splits must be even and >= 2")
    if sessions < splits or sessions % splits != 0:
        return None
    block = sessions // splits

    def mean_over(strategy: int, mask: tuple[bool, ...]) -> float:
        selected = [
            matrix[strategy][start * block : (start + 1) * block]
            for start in range(splits)
            if mask[start]
        ]
        flat = [value for part in selected for value in part]
        return math.fsum(flat) / len(flat)

    combinations = 0
    below = 0
    logits: list[float] = []
    for train_blocks in itertools.combinations(range(splits), splits // 2):
        train_mask = tuple(start in train_blocks for start in range(splits))
        test_mask = tuple(not flag for flag in train_mask)
        train_means = [mean_over(strategy, train_mask) for strategy in range(n)]
        best = max(range(n), key=lambda s: (train_means[s], -s))
        test_means = [mean_over(strategy, test_mask) for strategy in range(n)]
        # 0-based test rank of the train-best (0 == best); ties to lower index
        rank = sum(1 for value in test_means if value > test_means[best])
        w = (n - rank) / (n + 1)
        logits.append(math.log(w / (1.0 - w)))
        combinations += 1
        if rank >= n / 2:
            below += 1
    return PboAssessment(
        n_strategies=n,
        n_sessions=sessions,
        splits=splits,
        combinations=combinations,
        pbo=below / combinations if combinations else 0.0,
        n_below_median=below,
        mean_logit=statistics.fmean(logits) if logits else None,
    )


def concentration_hhi(weights: Sequence[float]) -> float:
    """Herfindahl-Hirschman index of non-negative weights (normalized)."""
    values = _finite(weights, name="weight")
    if not values:
        raise ValueError("at least one weight is required")
    if any(value < 0.0 for value in values):
        raise ValueError("weights must be non-negative")
    total = math.fsum(values)
    if total <= 0.0:
        raise ValueError("weights must not all be zero")
    return math.fsum((value / total) ** 2 for value in values)


def block_shuffle(values: Sequence[float], *, block_size: int, seed: int) -> tuple[float, ...]:
    """Deterministic negative control: permute WHOLE blocks, keep the tail.

    Splits into ``floor(n / block_size)`` blocks, shuffles their ORDER via
    ``random.Random(seed)``, concatenates, and appends the trailing partial
    block unchanged.  With ``block_size == n`` the result is the input.
    """
    sample = _finite(values, name="value")
    n = len(sample)
    if not 1 <= block_size <= max(n, 1):
        raise ValueError("block_size must be between 1 and the sample size")
    n_blocks = n // block_size
    order = list(range(n_blocks))
    random.Random(seed).shuffle(order)
    out: list[float] = []
    for position in order:
        out.extend(sample[position * block_size : (position + 1) * block_size])
    out.extend(sample[n_blocks * block_size :])
    return tuple(out)


def random_scores(count: int, *, seed: int) -> tuple[float, ...]:
    """Deterministic negative control: ``count`` U(0, 1) scores from ``seed``."""
    if count < 0:
        raise ValueError("count must be >= 0")
    rng = random.Random(seed)
    return tuple(rng.uniform(0.0, 1.0) for _ in range(count))
