from decimal import Decimal

from tree_options.strategy_lab.ranking import (
    equal_weight_targets,
    hqm_scores,
    momentum_12_1_scores,
    percentile_scores,
    robust_value_scores,
    twelve_minus_one_return,
)


def test_percentiles_are_order_independent_and_tie_stable():
    a = percentile_scores({"B": Decimal("2"), "A": Decimal("1"), "C": Decimal("2")})
    b = percentile_scores({"C": Decimal("2"), "B": Decimal("2"), "A": Decimal("1")})
    assert a == b
    assert a["B"] == a["C"] > a["A"]


def test_twelve_minus_one_hand_oracle():
    assert twelve_minus_one_return(
        price_t_minus_12=Decimal("100"), price_t_minus_1=Decimal("125")
    ) == Decimal("0.25")
    rows = momentum_12_1_scores(
        {"A": (Decimal("100"), Decimal("125")), "B": (Decimal("100"), Decimal("90"))}
    )
    assert rows[0].entity_id == "A" and rows[0].score > rows[1].score


def test_hqm_composite_prefers_consistently_stronger_name():
    rows = hqm_scores(
        {
            "A": {
                "1m": Decimal(".1"),
                "3m": Decimal(".2"),
                "6m": Decimal(".3"),
                "12m": Decimal(".4"),
            },
            "B": {
                "1m": Decimal("0"),
                "3m": Decimal(".1"),
                "6m": Decimal(".2"),
                "12m": Decimal(".3"),
            },
        }
    )
    scores = {row.entity_id: row.score for row in rows}
    assert scores["A"] > scores["B"]


def test_value_lower_positive_ratios_score_better_and_missing_is_excluded():
    rows = robust_value_scores(
        {
            "CHEAP": {
                "pe": Decimal("5"),
                "pb": Decimal("1"),
                "ps": Decimal("1"),
                "ev_ebitda": Decimal("4"),
                "ev_gross_profit": Decimal("2"),
            },
            "RICH": {
                "pe": Decimal("30"),
                "pb": Decimal("8"),
                "ps": Decimal("9"),
                "ev_ebitda": Decimal("20"),
                "ev_gross_profit": Decimal("15"),
            },
            "MISSING": {"pe": Decimal("1")},
        }
    )
    scores = {row.entity_id: row.score for row in rows}
    assert set(scores) == {"CHEAP", "RICH"}
    assert scores["CHEAP"] > scores["RICH"]


def test_equal_weights_sum_exactly_to_one():
    weights = equal_weight_targets(["C", "A", "B"])
    assert sum((row.weight for row in weights), Decimal(0)) == Decimal(1)
