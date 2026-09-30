"""Portfolio-construction primitives, deliberately separate from strategy scores."""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from decimal import Decimal

from tree_options.strategy_lab.contracts import TargetWeight


class PortfolioError(ValueError):
    pass


def equal_weight(entity_ids: Sequence[str]) -> tuple[TargetWeight, ...]:
    ids = tuple(sorted(set(entity_ids)))
    if not ids:
        return ()
    weight = Decimal(1) / Decimal(len(ids))
    out = [TargetWeight(entity_id=item, weight=weight) for item in ids[:-1]]
    residual = Decimal(1) - weight * Decimal(len(ids) - 1)
    out.append(TargetWeight(entity_id=ids[-1], weight=residual))
    return tuple(out)


def normalize_nonnegative(weights: Mapping[str, Decimal]) -> tuple[TargetWeight, ...]:
    if not weights:
        return ()
    for entity_id, weight in weights.items():
        if not isinstance(weight, Decimal) or not weight.is_finite() or weight < 0:
            raise PortfolioError(f"{entity_id} weight must be finite and non-negative Decimal")
    total = sum(weights.values(), Decimal(0))
    if total <= 0:
        raise PortfolioError("positive total weight required")
    ids = sorted(weights)
    result = [TargetWeight(entity_id=entity_id, weight=weights[entity_id] / total) for entity_id in ids[:-1]]
    result.append(TargetWeight(ids[-1], Decimal("1") - sum((w.weight for w in result), Decimal("0"))))
    return tuple(result)


def capped_equal_weight(entity_ids: Sequence[str], *, max_weight: Decimal) -> tuple[TargetWeight, ...]:
    ids = tuple(sorted(set(entity_ids)))
    if not ids:
        return ()
    if max_weight <= 0 or max_weight > 1:
        raise PortfolioError("max_weight must be in (0,1]")
    if max_weight * Decimal(len(ids)) < Decimal(1):
        raise PortfolioError("cap makes a fully-invested portfolio impossible")
    # For equal weight the cap either permits 1/N or makes the portfolio impossible.
    return equal_weight(ids)


class PyPortfolioOptMaxSharpe:
    """Optional adapter; dependency is intentionally lazy and research-only."""

    def construct(self, *, expected_returns, covariance, max_weight: float = 0.10):
        try:
            EfficientFrontier = importlib.import_module("pypfopt.efficient_frontier").EfficientFrontier
        except ImportError as error:  # pragma: no cover - optional integration
            raise PortfolioError("PyPortfolioOpt is not installed") from error
        frontier = EfficientFrontier(expected_returns, covariance, weight_bounds=(0.0, max_weight))
        frontier.max_sharpe()
        return frontier.clean_weights()
