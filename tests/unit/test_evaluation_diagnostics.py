"""M5 model diagnostics: exact hand-computed fixtures, declared conventions."""

from __future__ import annotations

import math
import random
import statistics

import pytest

from tree_options.evaluation.diagnostics import (
    block_bootstrap_ci,
    brier_score,
    calibration_bins,
    ndcg_at_k,
    quantile_spread,
    selected_tail_precision,
)

MEAN = statistics.fmean


# ---- block_bootstrap_ci ---------------------------------------------------------------


def test_bootstrap_constant_series_collapses_to_the_point() -> None:
    ci = block_bootstrap_ci([5.0] * 8, statistic=MEAN, block_size=3, iterations=50, seed=7)
    assert ci is not None
    assert (ci.lower, ci.upper, ci.point) == (5.0, 5.0, 5.0)
    assert ci.valid_iterations == 50


def test_bootstrap_block_size_equal_to_n_sees_only_the_whole_sample() -> None:
    values = tuple(float(i) for i in range(1, 11))
    ci = block_bootstrap_ci(values, statistic=MEAN, block_size=10, iterations=20, seed=3)
    assert ci is not None
    assert (ci.lower, ci.upper) == (5.5, 5.5)


def test_bootstrap_is_deterministic_given_the_seed_and_consumes_draws_in_order() -> None:
    values = (0.0, 1.0)
    first = block_bootstrap_ci(values, statistic=MEAN, block_size=1, iterations=3, seed=42)
    second = block_bootstrap_ci(values, statistic=MEAN, block_size=1, iterations=3, seed=42)
    assert first == second
    # the RNG-consumption contract: ceil(n/block) ordered draws per iteration
    rng = random.Random(42)
    expected: list[float] = []
    for _ in range(3):
        resample = [values[rng.randrange(2)] for _ in range(2)]
        expected.append(MEAN(resample))
    assert first is not None
    assert first.lower == min(expected)
    assert first.upper == max(expected)
    # the nearest-rank percentile contract on a LUMPY distribution: with
    # ties among the resample statistics the 2.5% bound is NOT the minimum,
    # and the ceil-then-decrement index is load-bearing — the pinned bounds
    # below sit strictly inside the sampled range (min resample mean 1.295,
    # the no-decrement index would read 1.665)
    lumpy = (0.0, 0.37, 1.48, 3.33, 5.92, 9.25, 13.32, 18.13)
    ci = block_bootstrap_ci(lumpy, statistic=MEAN, block_size=2, iterations=50, seed=11)
    assert ci is not None
    assert ci.valid_iterations == 50
    assert (ci.lower, ci.upper) == (1.295, 10.915)


def test_bootstrap_bounds_stay_inside_the_sample_range_and_vary_by_seed() -> None:
    values = tuple(float(i) for i in range(20))
    common = dict(statistic=MEAN, block_size=3, iterations=100)
    a = block_bootstrap_ci(values, seed=1, **common)
    b = block_bootstrap_ci(values, seed=2, **common)
    assert a is not None and b is not None
    assert 0.0 <= a.lower <= a.upper <= 19.0
    assert (a.lower, a.upper) != (b.lower, b.upper)


def test_bootstrap_refusals_and_none_propagation() -> None:
    assert block_bootstrap_ci([1.0], statistic=MEAN, block_size=1, iterations=5, seed=0) is None
    assert (
        block_bootstrap_ci(
            [1.0, 2.0], statistic=lambda _v: None, block_size=1, iterations=5, seed=0
        )
        is None
    )
    with pytest.raises(ValueError, match="block_size"):
        block_bootstrap_ci([1.0, 2.0], statistic=MEAN, block_size=3, iterations=5, seed=0)
    with pytest.raises(ValueError, match="iterations"):
        block_bootstrap_ci([1.0, 2.0], statistic=MEAN, block_size=1, iterations=0, seed=0)
    with pytest.raises(ValueError, match="confidence"):
        block_bootstrap_ci(
            [1.0, 2.0], statistic=MEAN, block_size=1, iterations=5, seed=0, confidence=1.5
        )
    with pytest.raises(ValueError, match="finite"):
        block_bootstrap_ci([1.0, math.nan], statistic=MEAN, block_size=1, iterations=5, seed=0)


def test_bootstrap_skips_unevaluable_resamples_not_the_whole_interval() -> None:
    # a statistic that refuses constant resamples (Codex round-1: the None
    # path only ever exited at the POINT statistic before) — the surviving
    # resamples still bound the interval
    def skip_constant(sample: tuple[float, ...]) -> float | None:
        return None if len(set(sample)) == 1 else MEAN(sample)

    ci = block_bootstrap_ci(
        (0.0, 1.0), statistic=skip_constant, block_size=1, iterations=10, seed=0
    )
    assert ci is not None
    assert ci.valid_iterations == 6
    assert (ci.lower, ci.upper, ci.point) == (0.5, 0.5, 0.5)


# ---- ndcg_at_k ------------------------------------------------------------------------


def test_ndcg_perfect_inverse_and_partial_orderings() -> None:
    assert ndcg_at_k([3.0, 1.0, 2.0], [3.0, 1.0, 2.0], k=3) == 1.0
    # relevance (label>median 2) = (1,0,0); ranking by score puts the one
    # relevant name last: DCG = 1/log2(4) = 0.5, IDCG = 1
    assert ndcg_at_k([1.0, 2.0, 3.0], [3.0, 1.0, 2.0], k=3) == pytest.approx(0.5)
    dcg = 1.0 / math.log2(4.0) + 1.0 / math.log2(5.0)
    idcg = 1.0 / math.log2(2.0) + 1.0 / math.log2(3.0)
    assert ndcg_at_k([3.0, 4.0, 1.0, 2.0], [1.0, 2.0, 3.0, 4.0], k=4) == pytest.approx(dcg / idcg)
    # k truncates before any relevant name
    assert ndcg_at_k([3.0, 4.0, 1.0, 2.0], [1.0, 2.0, 3.0, 4.0], k=2) == 0.0


def test_ndcg_breaks_score_ties_by_ascending_index() -> None:
    # relevance (label > median 2) = (0, 1, 0); the tied 5s order idx0 then
    # idx1, so the relevant name sits at position 2: 1/log2(3)
    assert ndcg_at_k([5.0, 5.0, 1.0], [1.0, 3.0, 2.0], k=3) == pytest.approx(1.0 / math.log2(3.0))


def test_ndcg_unevaluable_and_refusals() -> None:
    assert ndcg_at_k([1.0, 2.0], [3.0, 3.0], k=2) is None  # nothing relevant
    assert ndcg_at_k([1.0], [1.0], k=1) is None  # singleton cross-section
    with pytest.raises(ValueError, match="k must be"):
        ndcg_at_k([1.0, 2.0], [1.0, 2.0], k=0)
    with pytest.raises(ValueError, match="same length"):
        ndcg_at_k([1.0], [1.0, 2.0], k=1)


# ---- selected_tail_precision ----------------------------------------------------------


def test_tail_precision_exact_topk_behaviour() -> None:
    labels = [1.0, 2.0, 3.0, 4.0]  # relevance (0, 0, 1, 1)
    assert selected_tail_precision([1.0, 2.0, 3.0, 4.0], labels, k=2) == 1.0
    assert selected_tail_precision([1.0, 2.0, 3.0, 4.0], labels, k=3) == pytest.approx(2.0 / 3.0)
    assert selected_tail_precision([4.0, 3.0, 2.0, 1.0], labels, k=2) == 0.0
    assert selected_tail_precision([1.0, 2.0], [3.0, 3.0], k=1) is None
    with pytest.raises(ValueError, match="k must be"):
        selected_tail_precision([1.0, 2.0], [1.0, 2.0], k=0)


# ---- quantile_spread -----------------------------------------------------------------


def test_quantile_spread_monotone_and_inverse() -> None:
    scores = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    labels = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    assert quantile_spread(scores, labels, quantiles=2) == 30.0  # 50 - 20
    assert quantile_spread(scores, labels, quantiles=3) == 40.0  # 55 - 15
    assert quantile_spread(scores, [60.0, 50.0, 40.0, 30.0, 20.0, 10.0], quantiles=2) == -30.0


def test_quantile_spread_uneven_allocation_is_rank_proportional() -> None:
    # n=5, q=2: bucket = r*2//5 puts THREE names in the bottom (ranks 0-2)
    # and two in the top — floor-equal groups would move the rank-2 name
    # into the top and read 10/3 instead of the declared allocation's 5
    assert (
        quantile_spread([0.0, 1.0, 2.0, 3.0, 4.0], [0.0, 0.0, 0.0, 0.0, 10.0], quantiles=2) == 5.0
    )


def test_quantile_spread_unevaluable_and_refusals() -> None:
    assert quantile_spread([1.0, 2.0], [1.0, 2.0], quantiles=3) is None  # n < quantiles
    with pytest.raises(ValueError, match="quantiles must be"):
        quantile_spread([1.0, 2.0], [1.0, 2.0], quantiles=1)
    with pytest.raises(ValueError, match="same length"):
        quantile_spread([1.0], [1.0, 2.0], quantiles=2)


# ---- brier_score + calibration_bins ---------------------------------------------------


def test_brier_perfect_anti_and_flat_forecasts() -> None:
    assert brier_score([1.0, 0.0], [True, False]) == 0.0
    assert brier_score([0.0, 1.0], [True, False]) == 1.0
    assert brier_score([0.5, 0.5, 0.5], [True, True, False]) == pytest.approx(0.25)
    with pytest.raises(ValueError, match="between 0 and 1"):
        brier_score([1.5], [True])
    with pytest.raises(ValueError, match="bool"):
        brier_score([0.5], [1])  # type: ignore[list-item]


def test_calibration_bins_aggregate_forecasts_within_one_bin() -> None:
    # both forecasts land in bin 0 (0.1 and 0.2 at bins=2): the bin's means
    # are over BOTH members — an overwrite instead of accumulation would
    # report the last forecast's 0.10 as the bin mean
    bins = calibration_bins([0.1, 0.2], [True, False], bins=2)
    assert (bins[0].count, bins[0].mean_probability, bins[0].mean_outcome) == (
        2,
        pytest.approx(0.15),
        0.5,
    )
    assert (bins[1].count, bins[1].mean_probability, bins[1].mean_outcome) == (0, None, None)


def test_calibration_bins_partition_and_empty_means() -> None:
    bins = calibration_bins([0.05, 0.15, 0.95], [False, True, True], bins=10)
    assert len(bins) == 10
    assert (bins[0].count, bins[0].mean_probability, bins[0].mean_outcome) == (1, 0.05, 0.0)
    assert (bins[1].count, bins[1].mean_probability, bins[1].mean_outcome) == (1, 0.15, 1.0)
    assert (bins[9].count, bins[9].mean_probability, bins[9].mean_outcome) == (1, 0.95, 1.0)
    assert (bins[4].count, bins[4].mean_probability, bins[4].mean_outcome) == (0, None, None)
    assert (bins[0].lower, bins[0].upper) == (0.0, 0.1)
    # p=1.0 lands in the LAST bin, never outside the partition
    edge = calibration_bins([1.0], [True], bins=4)
    assert (edge[3].count, edge[3].mean_probability, edge[3].mean_outcome) == (1, 1.0, 1.0)
    with pytest.raises(ValueError, match="bins must be"):
        calibration_bins([0.5], [True], bins=0)
