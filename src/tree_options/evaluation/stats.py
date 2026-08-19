"""Deterministic evaluation statistics (M2-proper workstream E).

All functions are pure and stdlib-only.  Cross-sectional information
coefficients use average ranks for ties.  Unevaluable cross-sections and
unidentifiable t-statistics are represented by ``None`` rather than a
fabricated zero.  The false-positive gate uses the exact binomial upper
tail; no normal approximation is hidden in the release criterion.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ScoredLabel:
    security_id: str
    session: date
    score: float
    label: float

    def __post_init__(self) -> None:
        if not self.security_id:
            raise ValueError("security_id must not be empty")
        if not math.isfinite(self.score) or not math.isfinite(self.label):
            raise ValueError("score and label must be finite")


@dataclass(frozen=True)
class SessionRankIC:
    session: date
    ic: float
    n: int


@dataclass(frozen=True)
class FalsePositiveAssessment:
    total: int
    rejections: int
    observed_rate: float
    max_allowed_rejections: int
    exact_upper_tail_probability: float
    passes: bool


@dataclass(frozen=True)
class BacktestSummary:
    session_returns: tuple[float, ...]
    total_return: float
    mean_turnover: float | None
    hit_rate: float | None


def _finite(values: Iterable[float], *, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"{name} values must be finite")
    return result


def _average_ranks(values: Sequence[float]) -> tuple[float, ...]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        stop = start + 1
        while stop < len(ordered) and ordered[stop][1] == ordered[start][1]:
            stop += 1
        average = (start + 1 + stop) / 2.0
        for position in range(start, stop):
            ranks[ordered[position][0]] = average
        start = stop
    return tuple(ranks)


def spearman_rank_ic(scores: Sequence[float], labels: Sequence[float]) -> float | None:
    """Spearman rank correlation with deterministic average-rank ties."""
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    x = _finite(scores, name="score")
    y = _finite(labels, name="label")
    if len(x) < 2:
        return None

    x_rank = _average_ranks(x)
    y_rank = _average_ranks(y)
    x_mean = math.fsum(x_rank) / len(x_rank)
    y_mean = math.fsum(y_rank) / len(y_rank)
    x_delta = tuple(value - x_mean for value in x_rank)
    y_delta = tuple(value - y_mean for value in y_rank)
    denominator = math.sqrt(
        math.fsum(value * value for value in x_delta)
        * math.fsum(value * value for value in y_delta)
    )
    if denominator == 0.0:
        return None
    correlation = math.fsum(a * b for a, b in zip(x_delta, y_delta, strict=True)) / denominator
    return max(-1.0, min(1.0, correlation))


def per_session_rank_ics(observations: Iterable[ScoredLabel]) -> tuple[SessionRankIC, ...]:
    """Group observations by session and return only evaluable ICs."""
    grouped: dict[date, list[ScoredLabel]] = defaultdict(list)
    for observation in observations:
        grouped[observation.session].append(observation)

    result: list[SessionRankIC] = []
    for session in sorted(grouped):
        rows = sorted(grouped[session], key=lambda row: row.security_id)
        security_ids = [row.security_id for row in rows]
        if len(security_ids) != len(set(security_ids)):
            raise ValueError(f"duplicate security_id in session {session}")
        ic = spearman_rank_ic([row.score for row in rows], [row.label for row in rows])
        if ic is not None:
            result.append(SessionRankIC(session=session, ic=ic, n=len(rows)))
    return tuple(result)


def one_sample_t_statistic(values: Sequence[float]) -> float | None:
    """One-sample t against zero using sample variance (ddof=1)."""
    sample = _finite(values, name="sample")
    if len(sample) < 2:
        return None
    mean = math.fsum(sample) / len(sample)
    variance = math.fsum((value - mean) ** 2 for value in sample) / (len(sample) - 1)
    if variance == 0.0:
        return None
    return mean / math.sqrt(variance / len(sample))


def exact_binomial_upper_tail(successes: int, trials: int, probability: float) -> float:
    """P[X >= successes] for X ~ Binomial(trials, probability)."""
    if trials < 0:
        raise ValueError("trials must be >= 0")
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be finite and between 0 and 1")
    return math.fsum(
        math.comb(trials, observed)
        * probability**observed
        * (1.0 - probability) ** (trials - observed)
        for observed in range(successes, trials + 1)
    )


def max_allowed_rejections(
    trials: int,
    *,
    nominal_rate: float = 0.05,
    alpha: float = 0.05,
) -> int:
    """Largest rejection count not significant above the nominal rate."""
    if trials < 0:
        raise ValueError("trials must be >= 0")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1")
    if not math.isfinite(nominal_rate) or not 0.0 <= nominal_rate <= 1.0:
        raise ValueError("nominal_rate must be finite and between 0 and 1")
    if trials == 0:
        return 0
    for allowed in range(trials):
        first_failing = allowed + 1
        if exact_binomial_upper_tail(first_failing, trials, nominal_rate) <= alpha:
            return allowed
    return trials


def assess_false_positives(
    fold_t_statistics: Sequence[float],
    *,
    critical_abs_t: float = 1.96,
    nominal_rate: float = 0.05,
    alpha: float = 0.05,
) -> FalsePositiveAssessment:
    """Apply the exact-binomial false-positive acceptance boundary."""
    values = _finite(fold_t_statistics, name="fold t-statistic")
    if not values:
        raise ValueError("at least one fold t-statistic is required")
    if not math.isfinite(critical_abs_t) or critical_abs_t <= 0.0:
        raise ValueError("critical_abs_t must be finite and > 0")
    allowed = max_allowed_rejections(len(values), nominal_rate=nominal_rate, alpha=alpha)
    rejections = sum(abs(value) >= critical_abs_t for value in values)
    return FalsePositiveAssessment(
        total=len(values),
        rejections=rejections,
        observed_rate=rejections / len(values),
        max_allowed_rejections=allowed,
        exact_upper_tail_probability=exact_binomial_upper_tail(
            rejections, len(values), nominal_rate
        ),
        passes=rejections <= allowed,
    )


def backtest_summary(
    session_returns: Sequence[float],
    turnovers: Sequence[float],
    label_hits: Sequence[bool],
) -> BacktestSummary:
    """Summarize the minimal M2 equity-backtest output."""
    returns = _finite(session_returns, name="session return")
    turnover_values = _finite(turnovers, name="turnover")
    if len(returns) != len(turnover_values):
        raise ValueError("session_returns and turnovers must have the same length")
    if any(value < -1.0 for value in returns):
        raise ValueError("session return cannot be less than -1")
    if any(value < 0.0 for value in turnover_values):
        raise ValueError("turnover cannot be negative")
    if any(type(value) is not bool for value in label_hits):
        raise ValueError("label_hits values must be bool")
    total_return = math.prod(1.0 + value for value in returns) - 1.0
    mean_turnover = math.fsum(turnover_values) / len(turnover_values) if turnover_values else None
    hit_rate = sum(label_hits) / len(label_hits) if label_hits else None
    return BacktestSummary(
        session_returns=returns,
        total_return=total_return,
        mean_turnover=mean_turnover,
        hit_rate=hit_rate,
    )
