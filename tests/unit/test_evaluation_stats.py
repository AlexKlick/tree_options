"""M2-proper workstream E: deterministic evaluation statistics."""

from __future__ import annotations

import math
from datetime import date

import pytest

from tree_options.evaluation.stats import (
    ScoredLabel,
    assess_false_positives,
    backtest_summary,
    exact_binomial_upper_tail,
    max_allowed_rejections,
    one_sample_t_statistic,
    per_session_rank_ics,
    spearman_rank_ic,
)


def test_spearman_perfect_and_inverse_order() -> None:
    assert spearman_rank_ic([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == 1.0
    assert spearman_rank_ic([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]) == -1.0


def test_spearman_assigns_average_ranks_for_ties() -> None:
    assert spearman_rank_ic([1.0, 1.0, 2.0], [1.0, 2.0, 3.0]) == pytest.approx(math.sqrt(3.0) / 2.0)
    # A tie pair beside singletons pins the WITHIN-GROUP average itself:
    # rank pattern (1.5, 1.5, 3, 4) gives exactly 3/sqrt(10), while any
    # non-averaged tie ranking (e.g. last-position 1,1,3,4) gives a
    # different coefficient — the n=3 case above is invariant to that
    # shift, so it cannot own this invariant alone.
    assert spearman_rank_ic([1.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]) == pytest.approx(
        3.0 / math.sqrt(10.0)
    )


@pytest.mark.parametrize(
    ("scores", "labels"),
    [([1.0], [2.0]), ([1.0, 1.0], [2.0, 3.0]), ([1.0, 2.0], [3.0, 3.0])],
)
def test_spearman_returns_none_for_unevaluable_cross_sections(scores, labels) -> None:  # type: ignore[no-untyped-def]
    assert spearman_rank_ic(scores, labels) is None


def test_spearman_rejects_mismatch_and_non_finite_values() -> None:
    with pytest.raises(ValueError, match="same length"):
        spearman_rank_ic([1.0], [1.0, 2.0])
    with pytest.raises(ValueError, match="finite"):
        spearman_rank_ic([1.0, math.nan], [1.0, 2.0])


def test_per_session_rank_ics_group_and_sort_deterministically() -> None:
    d1 = date(2026, 1, 2)
    d2 = date(2026, 1, 5)
    observations = [
        ScoredLabel("SEC-B", d2, 2.0, 20.0),
        ScoredLabel("SEC-A", d1, 1.0, 30.0),
        ScoredLabel("SEC-C", d2, 3.0, 30.0),
        ScoredLabel("SEC-B", d1, 2.0, 20.0),
        ScoredLabel("SEC-A", d2, 1.0, 10.0),
        ScoredLabel("SEC-C", d1, 3.0, 10.0),
    ]
    result = per_session_rank_ics(observations)
    assert [item.session for item in result] == [d1, d2]
    assert [item.n for item in result] == [3, 3]
    assert [item.ic for item in result] == [-1.0, 1.0]


def test_per_session_rank_ics_omit_unevaluable_sessions() -> None:
    only = date(2026, 1, 2)
    assert per_session_rank_ics([ScoredLabel("SEC-A", only, 1.0, 2.0)]) == ()


def test_one_sample_t_statistic_uses_sample_variance() -> None:
    assert one_sample_t_statistic([1.0, 2.0, 3.0]) == pytest.approx(2.0 * math.sqrt(3.0))


@pytest.mark.parametrize("values", [[], [1.0], [0.0, 0.0], [2.0, 2.0]])
def test_one_sample_t_statistic_returns_none_when_not_identifiable(values) -> None:  # type: ignore[no-untyped-def]
    assert one_sample_t_statistic(values) is None


def test_exact_binomial_upper_tail_and_threshold() -> None:
    assert exact_binomial_upper_tail(2, 10, 0.05) == pytest.approx(0.0861383559)
    assert max_allowed_rejections(10, nominal_rate=0.05, alpha=0.05) == 2


def test_false_positive_assessment_owns_the_exact_boundary() -> None:
    passing = assess_false_positives(
        [2.1, -2.2, 0.2, 0.1, -0.4, 0.3, 0.8, -0.7, 1.1, -1.2],
        critical_abs_t=1.96,
        nominal_rate=0.05,
        alpha=0.05,
    )
    assert passing.rejections == 2
    assert passing.max_allowed_rejections == 2
    assert passing.passes

    failing = assess_false_positives(
        [2.1, -2.2, 2.3, 0.1, -0.4, 0.3, 0.8, -0.7, 1.1, -1.2],
        critical_abs_t=1.96,
        nominal_rate=0.05,
        alpha=0.05,
    )
    assert failing.rejections == 3
    assert not failing.passes


def test_backtest_summary_compounds_and_handles_absent_hits() -> None:
    summary = backtest_summary(
        session_returns=[0.10, -0.05],
        turnovers=[0.5, 0.25],
        label_hits=[True, False, True],
    )
    assert summary.total_return == pytest.approx(0.045)
    assert summary.mean_turnover == pytest.approx(0.375)
    assert summary.hit_rate == pytest.approx(2 / 3)
    assert backtest_summary([], [], []).hit_rate is None
