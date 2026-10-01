from decimal import Decimal

import pytest

from tree_options.strategy_lab.portfolio import PortfolioError, equal_weight, normalize_nonnegative


def test_normalized_weights_exact_nonnegative_and_empty_no_signal():
    for weights in (
        {"A": Decimal("1"), "B": Decimal("1"), "C": Decimal("1")},
        {"A": Decimal("0"), "B": Decimal("2")},
    ):
        result = normalize_nonnegative(weights)
        assert sum(w.weight for w in result) == 1
        assert all(w.weight >= 0 for w in result)
    assert equal_weight([]) == normalize_nonnegative({}) == ()
    with pytest.raises(PortfolioError):
        normalize_nonnegative({"A": Decimal("-1")})
