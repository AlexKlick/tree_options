"""M5 portfolio diagnostics: risk/return metrics, cost bridge, comparators.

Pure, stdlib-only, deterministic — the conventions of ``evaluation.stats``
and ``evaluation.controls``: ``None`` for unevaluable quantities, exact
validation, ``math.fsum``, declared estimators wherever the literature
varies.

Conventions pinned here: drawdown and CAGR run over the COMPOUNDED equity
curve (geometric, never additive); Sharpe annualizes the per-session ratio
by ``sqrt(periods_per_year)``; Sortino's downside deviation is the full-sample
root-mean-square of shortfalls below the target (dividing by ALL sessions,
not only the downside sessions); profit factor is ``None`` when there are no
losses (infinite is not a number this module will fabricate).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DrawdownAssessment:
    """Peak-to-trough drawdown of the compounded curve, with its location."""

    depth: float  # strictly <= 0, as a fraction of the running peak
    peak_session: date
    trough_session: date
    recovered_session: date | None  # first session closing at/above the peak


@dataclass(frozen=True)
class CostBridge:
    """Gross-to-net waterfall over one return series and its turnover."""

    gross_cagr: float | None
    fee_drag_cagr: float | None
    slippage_drag_cagr: float | None
    net_cagr: float | None
    round_trip_cost_fraction: float  # per unit of turnover


@dataclass(frozen=True)
class GroupStat:
    """One group's slice of a stability table."""

    label: str
    n: int
    mean_return: float
    hit_rate: float
    compounded_return: float


def _finite(values: Sequence[float], *, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"{name} values must be finite")
    return result


def _validate_periods(periods_per_year: float) -> None:
    if not math.isfinite(periods_per_year) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be finite and > 0")


def _subtract_or_none(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


def annualized_sharpe(
    session_returns: Sequence[float], *, periods_per_year: float = 252.0
) -> float | None:
    """Per-session Sharpe (mean/std, ddof=1) annualized by sqrt(periods)."""
    sample = _finite(session_returns, name="session return")
    _validate_periods(periods_per_year)
    if len(sample) < 2:
        return None
    mean = math.fsum(sample) / len(sample)
    variance = math.fsum((value - mean) ** 2 for value in sample) / (len(sample) - 1)
    if variance == 0.0:
        return None
    return mean / math.sqrt(variance) * math.sqrt(periods_per_year)


def sortino_ratio(
    session_returns: Sequence[float],
    *,
    target_return: float = 0.0,
    periods_per_year: float = 252.0,
) -> float | None:
    """Annualized Sortino over the FULL-SAMPLE downside deviation.

    ``DD = sqrt(mean(max(target - r, 0)^2))`` across ALL sessions — the
    convention that keeps the ratio comparable to Sharpe's denominator
    (dividing only over downside sessions understates risk by construction).
    """
    sample = _finite(session_returns, name="session return")
    if not math.isfinite(target_return):
        raise ValueError("target_return must be finite")
    _validate_periods(periods_per_year)
    if len(sample) < 2 or not any(value < target_return for value in sample):
        return None
    mean = math.fsum(sample) / len(sample)
    downside = math.sqrt(
        math.fsum(max(target_return - value, 0.0) ** 2 for value in sample) / len(sample)
    )
    if downside == 0.0:
        return None
    return (mean - target_return) / downside * math.sqrt(periods_per_year)


def compounded_return(session_returns: Sequence[float]) -> float:
    """Total return of the compounded equity curve."""
    sample = _finite(session_returns, name="session return")
    if any(value <= -1.0 for value in sample):
        raise ValueError("session return cannot be less than or equal to -1")
    return math.prod(1.0 + value for value in sample) - 1.0


def cagr(session_returns: Sequence[float], *, periods_per_year: float = 252.0) -> float | None:
    """Geometric annualization of the compounded curve."""
    sample = _finite(session_returns, name="session return")
    _validate_periods(periods_per_year)
    if not sample:
        return None
    total = compounded_return(sample)
    if total <= -1.0:
        return None  # total ruin has no finite annualization
    years = len(sample) / periods_per_year
    return (1.0 + total) ** (1.0 / years) - 1.0


def max_drawdown(
    session_returns: Sequence[float], *, sessions: Sequence[date] | None = None
) -> DrawdownAssessment | None:
    """Deepest peak-to-trough loss of the COMPOUNDED curve.

    The peak is the running maximum of the curve; the reported drawdown is
    the deepest point below the peak that PRECEDED it.  ``recovered_session``
    is the first session whose equity closes at or above that peak (``None``
    if the sample ends underwater).
    """
    sample = _finite(session_returns, name="session return")
    if not sample:
        return None
    if sessions is not None and len(sessions) != len(sample):
        raise ValueError("sessions must align with the returns")
    dates = list(sessions) if sessions is not None else [date(2000, 1, 1)] * len(sample)
    if any(value <= -1.0 for value in sample):
        raise ValueError("session return cannot be less than or equal to -1")

    curve = [1.0]
    for value in sample:
        curve.append(curve[-1] * (1.0 + value))
    running_peak = curve[0]
    peak_index = 0
    depth = 0.0
    worst = (0, 0)  # (peak index, trough index) over the CURVE (0 == origin)
    for index, equity in enumerate(curve):
        if equity >= running_peak:
            running_peak = equity
            peak_index = index
        else:
            drawdown = equity / running_peak - 1.0
            if drawdown < depth:
                depth = drawdown
                worst = (peak_index, index)
    if depth == 0.0:
        # a monotonically rising (or flat) curve never drew down; there is
        # no recovery event because there was no drawdown
        return DrawdownAssessment(0.0, dates[0], dates[-1], None)
    peak_equity = curve[worst[0]]
    recovered = next(
        (index - 1 for index in range(worst[1] + 1, len(curve)) if curve[index] >= peak_equity),
        None,
    )
    return DrawdownAssessment(
        depth=depth,
        peak_session=dates[worst[0] - 1] if worst[0] > 0 else dates[0],
        trough_session=dates[worst[1] - 1],
        recovered_session=dates[recovered] if recovered is not None else None,
    )


def calmar_ratio(
    session_returns: Sequence[float], *, periods_per_year: float = 252.0
) -> float | None:
    """CAGR divided by the absolute drawdown depth (the classic definition)."""
    annual = cagr(session_returns, periods_per_year=periods_per_year)
    drawdown = max_drawdown(session_returns)
    if annual is None or drawdown is None or drawdown.depth == 0.0:
        return None
    return annual / abs(drawdown.depth)


def profit_factor(session_returns: Sequence[float]) -> float | None:
    """Gross gains over gross losses; ``None`` when nothing was lost."""
    sample = _finite(session_returns, name="session return")
    if not sample:
        return None
    gains = math.fsum(value for value in sample if value > 0.0)
    losses = math.fsum(-value for value in sample if value < 0.0)
    if losses == 0.0:
        return None
    return gains / losses


def hit_rate(session_returns: Sequence[float]) -> float | None:
    """Fraction of strictly positive sessions."""
    sample = _finite(session_returns, name="session return")
    if not sample:
        return None
    return sum(1 for value in sample if value > 0.0) / len(sample)


def cost_bridge(
    session_returns: Sequence[float],
    *,
    turnovers: Sequence[float],
    fee_bps_per_side: float,
    slippage_bps_per_side: float,
    periods_per_year: float = 252.0,
) -> CostBridge:
    """Gross-to-net waterfall: per-session cost = turnover x round-trip bps.

    Each unit of turnover pays ``fee_bps_per_side + slippage_bps_per_side``
    (in basis points, converted to a fraction) once per side traded — the
    drag is charged against the session's gross return and every figure is
    re-annualized on its own compounded curve, so the drags compound
    honestly rather than subtracting linearly.
    """
    gross = _finite(session_returns, name="session return")
    churn = _finite(turnovers, name="turnover")
    if len(gross) != len(churn):
        raise ValueError("session_returns and turnovers must have the same length")
    if any(value < 0.0 for value in churn):
        raise ValueError("turnover cannot be negative")
    for name, bps in (("fee", fee_bps_per_side), ("slippage", slippage_bps_per_side)):
        if not math.isfinite(bps) or bps < 0.0:
            raise ValueError(f"{name}_bps_per_side must be finite and >= 0")
    _validate_periods(periods_per_year)

    per_side = (fee_bps_per_side + slippage_bps_per_side) / 10_000.0
    fee_only = [g - c * (fee_bps_per_side / 10_000.0) for g, c in zip(gross, churn, strict=True)]
    with_slippage = [g - c * per_side for g, c in zip(gross, churn, strict=True)]
    gross_cagr_value = cagr(gross, periods_per_year=periods_per_year)
    fee_only_cagr_value = cagr(fee_only, periods_per_year=periods_per_year)
    net_cagr_value = cagr(with_slippage, periods_per_year=periods_per_year)
    return CostBridge(
        gross_cagr=gross_cagr_value,
        fee_drag_cagr=_subtract_or_none(gross_cagr_value, fee_only_cagr_value),
        slippage_drag_cagr=_subtract_or_none(fee_only_cagr_value, net_cagr_value),
        net_cagr=net_cagr_value,
        round_trip_cost_fraction=per_side,
    )


def matched_risk_scale(target_volatility: float, realized_volatility: float) -> float:
    """Leverage/scale factor that matches a comparator's risk: k = target/realized.

    Uncapped by declaration — the MATCHED-RISK convention is equality of
    realized volatility, and capping here would silently break it; policy
    about leverage limits lives with the caller.
    """
    if not math.isfinite(target_volatility) or target_volatility <= 0.0:
        raise ValueError("target_volatility must be finite and > 0")
    if not math.isfinite(realized_volatility) or realized_volatility <= 0.0:
        raise ValueError("realized_volatility must be finite and > 0")
    return target_volatility / realized_volatility


def excess_over_baseline(
    session_returns: Sequence[float], baseline_returns: Sequence[float]
) -> tuple[float, ...]:
    """Per-session arithmetic excess (strategy minus baseline), aligned."""
    strategy = _finite(session_returns, name="session return")
    baseline = _finite(baseline_returns, name="baseline return")
    if len(strategy) != len(baseline):
        raise ValueError("session_returns and baseline_returns must have the same length")
    return tuple(s - b for s, b in zip(strategy, baseline, strict=True))


def slice_stability(
    session_returns: Sequence[float],
    labels: Sequence[str],
) -> tuple[GroupStat, ...]:
    """Per-group stability table over labelled sessions.

    Groups appear in first-seen order; the compounded return is over the
    group's own sessions in sequence (a within-group sub-curve, not the
    whole-curve segment between its endpoints).
    """
    returns = _finite(session_returns, name="session return")
    if len(returns) != len(labels):
        raise ValueError("session_returns and labels must have the same length")
    order: list[str] = []
    buckets: dict[str, list[float]] = {}
    for value, label in zip(returns, labels, strict=True):
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(value)
    return tuple(
        GroupStat(
            label=label,
            n=len(rows),
            mean_return=math.fsum(rows) / len(rows),
            hit_rate=sum(1 for value in rows if value > 0.0) / len(rows),
            compounded_return=compounded_return(rows),
        )
        for label in order
        for rows in (buckets[label],)
    )
