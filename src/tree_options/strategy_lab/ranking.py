"""Deterministic cross-sectional ranking baselines."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext
from statistics import mean

from tree_options.strategy_lab.contracts import StrategyScore, TargetWeight


class RankingError(ValueError):
    pass


def _finite(value: Decimal, *, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RankingError(f"{name} must be a finite Decimal")
    return value


def percentile_scores(
    values: Mapping[str, Decimal], *, higher_is_better: bool = True
) -> dict[str, Decimal]:
    """Tie-stable cross-sectional percentiles in (0, 1).

    A value gets the midpoint of the empirical mass tied at that value. The
    calculation is independent of input iteration order.
    """

    if not values:
        return {}
    checked = {key: _finite(value, name=key) for key, value in values.items()}
    n = Decimal(len(checked))
    out: dict[str, Decimal] = {}
    for key, value in checked.items():
        if higher_is_better:
            strictly_worse = sum(1 for other in checked.values() if other < value)
        else:
            strictly_worse = sum(1 for other in checked.values() if other > value)
        tied = sum(1 for other in checked.values() if other == value)
        with localcontext() as ctx:
            ctx.prec = 40
            out[key] = (Decimal(strictly_worse) + Decimal(tied) / Decimal(2)) / n
    return out


def twelve_minus_one_return(*, price_t_minus_12: Decimal, price_t_minus_1: Decimal) -> Decimal:
    """Return used by a 12-minus-1 momentum score.

    The caller names the two formation prices explicitly so the omitted recent
    month cannot be hidden by an indexing convention.
    """

    start = _finite(price_t_minus_12, name="price_t_minus_12")
    end = _finite(price_t_minus_1, name="price_t_minus_1")
    if start <= 0 or end <= 0:
        raise RankingError("formation prices must be positive")
    with localcontext() as ctx:
        ctx.prec = 40
        return end / start - Decimal(1)


def momentum_12_1_scores(
    formation_prices: Mapping[str, tuple[Decimal, Decimal]],
) -> tuple[StrategyScore, ...]:
    raw = {
        ticker: twelve_minus_one_return(
            price_t_minus_12=prices[0], price_t_minus_1=prices[1]
        )
        for ticker, prices in formation_prices.items()
    }
    pct = percentile_scores(raw, higher_is_better=True)
    return tuple(
        StrategyScore(entity_id=ticker, score=pct[ticker], components={"return_12_1": raw[ticker]})
        for ticker in sorted(raw)
    )


def hqm_scores(
    returns: Mapping[str, Mapping[str, Decimal]],
    *, horizons: Sequence[str] = ("1m", "3m", "6m", "12m"),
) -> tuple[StrategyScore, ...]:
    """Composite high-quality-momentum percentile score."""

    eligible: dict[str, dict[str, Decimal]] = {}
    for ticker, metrics in returns.items():
        if not all(horizon in metrics for horizon in horizons):
            continue
        eligible[ticker] = {
            horizon: _finite(metrics[horizon], name=f"{ticker}:{horizon}")
            for horizon in horizons
        }
    if not eligible:
        return ()
    by_horizon = {
        horizon: percentile_scores(
            {ticker: metrics[horizon] for ticker, metrics in eligible.items()},
            higher_is_better=True,
        )
        for horizon in horizons
    }
    out: list[StrategyScore] = []
    for ticker in sorted(eligible):
        components = {h: by_horizon[h][ticker] for h in horizons}
        score = Decimal(str(mean(components.values())))
        out.append(StrategyScore(ticker, score, components))
    return tuple(out)


_VALUE_METRICS = ("pe", "pb", "ps", "ev_ebitda", "ev_gross_profit")


def robust_value_scores(
    ratios: Mapping[str, Mapping[str, Decimal]],
) -> tuple[StrategyScore, ...]:
    """Five-metric composite value score; higher output means more value-like.

    This baseline intentionally requires all five ratios to be finite and
    strictly positive. It does not silently mean-impute missing/negative ratios.
    A later strategy version may choose another treatment explicitly.
    """

    eligible: dict[str, dict[str, Decimal]] = {}
    for ticker, metrics in ratios.items():
        if not all(metric in metrics for metric in _VALUE_METRICS):
            continue
        checked: dict[str, Decimal] = {}
        valid = True
        for metric in _VALUE_METRICS:
            value = _finite(metrics[metric], name=f"{ticker}:{metric}")
            if value <= 0:
                valid = False
                break
            checked[metric] = value
        if valid:
            eligible[ticker] = checked
    if not eligible:
        return ()
    by_metric = {
        metric: percentile_scores(
            {ticker: values[metric] for ticker, values in eligible.items()},
            higher_is_better=False,
        )
        for metric in _VALUE_METRICS
    }
    out: list[StrategyScore] = []
    for ticker in sorted(eligible):
        components = {metric: by_metric[metric][ticker] for metric in _VALUE_METRICS}
        score = Decimal(str(mean(components.values())))
        out.append(StrategyScore(ticker, score, components))
    return tuple(out)


def top_k(scores: Sequence[StrategyScore], k: int) -> tuple[StrategyScore, ...]:
    if k < 0:
        raise RankingError("k must be >= 0")
    return tuple(sorted(scores, key=lambda row: (-row.score, row.entity_id))[:k])


def equal_weight_targets(entity_ids: Sequence[str]) -> tuple[TargetWeight, ...]:
    ids = tuple(sorted(set(entity_ids)))
    if not ids:
        return ()
    weight = Decimal(1) / Decimal(len(ids))
    # Decimal 1/N can repeat (e.g. N=3). Carry the exact residual in the
    # final deterministic slot so accounting receives weights summing to 1.
    out = [TargetWeight(entity_id=item, weight=weight) for item in ids[:-1]]
    residual = Decimal(1) - weight * Decimal(len(ids) - 1)
    out.append(TargetWeight(entity_id=ids[-1], weight=residual))
    return tuple(out)
