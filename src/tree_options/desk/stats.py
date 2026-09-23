"""Small statistics kit for the desk's pre-registered scorings.

FORECAST-001 (``docs/desk/FORECAST-001.md``) and IVHIST-001 score with:

* QLIKE (``rv/f - ln(rv/f) - 1``, Patton's robust loss) and squared error;
* the Diebold-Mariano test on a loss-differential series with a Newey-West
  (Bartlett) long-run variance, one-sided against the normal;
* pooled OLS and Driscoll-Kraay (date-summed scores, Bartlett) covariances
  for the encompassing regression;
* Pearson correlation and the median for the IV-history benchmark.

Floats throughout: these are statistics, not money.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np


def qlike(rv: float, f: float) -> float:
    ratio = rv / f
    return ratio - math.log(ratio) - 1.0


def mse(rv: float, f: float) -> float:
    return (rv - f) ** 2


def norm_sf(x: float) -> float:
    """1 - Phi(x)."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def newey_west_lrv(x: Sequence[float], lag: int) -> float:
    """Long-run variance of ``x`` (population autocovariances, Bartlett
    weights ``1 - j/(lag+1)``)."""
    n = len(x)
    if n == 0:
        raise ValueError("empty series")
    if lag < 0:
        raise ValueError(f"lag must be >= 0, got {lag}")
    mean = sum(x) / n
    dev = [v - mean for v in x]
    out = sum(d * d for d in dev) / n
    for j in range(1, min(lag, n - 1) + 1):
        gamma = sum(dev[t] * dev[t - j] for t in range(j, n)) / n
        out += 2.0 * (1.0 - j / (lag + 1)) * gamma
    return out


@dataclass(frozen=True)
class DMResult:
    n: int
    mean: float
    stat: float
    p_one_sided: float  # H1: mean > 0 (the benchmark's loss is higher)


def dm_test(d: Sequence[float], *, lag: int) -> DMResult | None:
    """Diebold-Mariano on the loss differential ``d`` (benchmark - model);
    None when there is nothing to test or no variance."""
    n = len(d)
    if n == 0:
        return None
    lrv = newey_west_lrv(d, lag)
    if not lrv > 0.0:
        return None
    mean = sum(d) / n
    stat = mean / math.sqrt(lrv / n)
    return DMResult(n=n, mean=mean, stat=stat, p_one_sided=norm_sf(stat))


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) != len(y):
        raise ValueError("length mismatch")
    n = len(x)
    if n < 2:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return sxy / math.sqrt(sxx * syy)


def median(x: Sequence[float]) -> float:
    return float(statistics.median(x))


def ols(design: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(beta, residuals) by least squares (``lstsq``, full rank expected)."""
    beta, _res, rank, _sv = np.linalg.lstsq(design, y, rcond=None)
    if rank < design.shape[1]:
        raise np.linalg.LinAlgError(f"rank-deficient design ({rank} < {design.shape[1]})")
    return beta, y - design @ beta


def driscoll_kraay(
    design: np.ndarray, resid: np.ndarray, dates: Sequence[Hashable], *, lag: int
) -> np.ndarray:
    """Driscoll-Kraay covariance of OLS coefficients: per-date summed scores
    ``s_t = sum_i x_it e_it`` with a Bartlett long-run variance of lag
    ``lag``, sandwiched by ``(X'X)^-1``."""
    order: list[Hashable] = []
    index: dict[Hashable, int] = {}
    for d in dates:
        if d not in index:
            index[d] = len(order)
            order.append(d)
    k = design.shape[1]
    scores = np.zeros((len(order), k))
    for row, d in enumerate(dates):
        scores[index[d]] += design[row] * resid[row]
    meat = scores.T @ scores
    for j in range(1, min(lag, len(order) - 1) + 1):
        gamma = scores[j:].T @ scores[:-j]
        meat += (1.0 - j / (lag + 1)) * (gamma + gamma.T)
    bread = np.linalg.inv(design.T @ design)
    cov: np.ndarray = bread @ meat @ bread
    return cov
