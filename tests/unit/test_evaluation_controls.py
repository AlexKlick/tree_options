"""M5 statistical controls: deflated Sharpe, CSCV/PBO, concentration,
negative-control generators.  Exact hand-computed fixtures wherever the
math is small enough to check by hand."""

from __future__ import annotations

import math
import statistics

import pytest

from tree_options.evaluation.controls import (
    block_shuffle,
    concentration_hhi,
    cscv_pbo,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    random_scores,
)

NORMAL = statistics.NormalDist()
EULER = 0.5772156649015329


# ---- expected_max_sharpe --------------------------------------------------------------


def test_expected_max_sharpe_matches_the_declared_formula() -> None:
    want = math.sqrt(1.0) * (
        (1.0 - EULER) * NORMAL.inv_cdf(1.0 - 1.0 / 2)
        + EULER * NORMAL.inv_cdf(1.0 - 1.0 / (2 * math.e))
    )
    assert expected_max_sharpe(2, 1.0) == pytest.approx(want)
    # the (1 - gamma) term vanishes at N=2 (Phi^-1(1/2) = 0)
    assert expected_max_sharpe(2, 1.0) == pytest.approx(
        EULER * NORMAL.inv_cdf(1.0 - 1.0 / (2 * math.e))
    )
    assert expected_max_sharpe(10, 1.0) > expected_max_sharpe(2, 1.0)
    assert expected_max_sharpe(8, 4.0) == pytest.approx(2.0 * expected_max_sharpe(8, 1.0))
    with pytest.raises(ValueError, match="n_trials"):
        expected_max_sharpe(1, 1.0)
    with pytest.raises(ValueError, match="trial_variance"):
        expected_max_sharpe(2, 0.0)


# ---- deflated_sharpe_ratio ------------------------------------------------------------


def test_deflated_sharpe_hand_case_zero_mean_symmetric() -> None:
    # [1, -1, 1, -1]: SR = 0, skew 0, non-excess kurtosis 1, denominator 1
    result = deflated_sharpe_ratio([1.0, -1.0, 1.0, -1.0], n_trials=2, trial_variance=1.0)
    assert result is not None
    assert result.sharpe == 0.0
    assert result.skewness == 0.0
    assert result.kurtosis == pytest.approx(1.0)
    sr0 = expected_max_sharpe(2, 1.0)
    assert result.deflated == pytest.approx(NORMAL.cdf(-sr0 * math.sqrt(3.0)))
    assert result.deflated < 0.5  # a zero-Sharpe strategy never clears SR0


def test_deflated_sharpe_strong_strategy_clears_the_hurdle() -> None:
    # [2, 0, 2, 0]: mean 1, ddof=1 std sqrt(4/3), SR = sqrt(3)/2
    result = deflated_sharpe_ratio([2.0, 0.0, 2.0, 0.0], n_trials=2, trial_variance=1.0)
    assert result is not None
    assert result.sharpe == pytest.approx(math.sqrt(3.0) / 2.0)
    sr0 = expected_max_sharpe(2, 1.0)
    expected = NORMAL.cdf(((math.sqrt(3.0) / 2.0) - sr0) * math.sqrt(3.0))
    assert result.deflated == pytest.approx(expected)
    assert result.deflated > 0.5


def test_deflated_sharpe_unevaluable_and_refusals() -> None:
    assert deflated_sharpe_ratio([1.0], n_trials=2, trial_variance=1.0) is None
    assert deflated_sharpe_ratio([3.0, 3.0, 3.0], n_trials=2, trial_variance=1.0) is None
    with pytest.raises(ValueError, match="finite"):
        deflated_sharpe_ratio([1.0, math.nan], n_trials=2, trial_variance=1.0)
    with pytest.raises(ValueError, match="n_trials"):
        deflated_sharpe_ratio([1.0, 2.0], n_trials=1, trial_variance=1.0)


# ---- cscv_pbo -------------------------------------------------------------------------


def test_cscv_dominance_gives_zero_pbo_and_positive_logits() -> None:
    result = cscv_pbo([[1.0] * 8, [0.0] * 8], splits=4)
    assert result is not None
    assert (result.n_strategies, result.n_sessions, result.splits) == (2, 8, 4)
    assert result.combinations == 6  # C(4, 2)
    assert result.pbo == 0.0
    # rank 0 everywhere -> w = 2/3 -> lambda = ln 2
    assert result.mean_logit == pytest.approx(math.log(2.0))


def test_cscv_full_anti_dominance_gives_one() -> None:
    # splits=2: each single-block train crowns a different strategy that
    # then loses the complementary test block
    result = cscv_pbo([[10.0, -10.0], [-10.0, 10.0]], splits=2)
    assert result is not None
    assert result.combinations == 2
    assert result.pbo == 1.0
    assert result.n_below_median == 2
    assert result.mean_logit is not None and result.mean_logit < 0.0


def test_cscv_mixed_blocks_exact_hand_count() -> None:
    # A wins both positive blocks, loses both negative; B flat zero.
    # Train {0,1}: A best, OOS {2,3}: A -10 -> below. Train {2,3}: B best,
    # OOS {0,1}: B 0 vs A +10 -> below. The four mixed trains tie at 0 and
    # break to index 0 (A), whose OOS mean ties at 0 -> rank 0, not below.
    a = [10.0, 10.0, 10.0, 10.0, -10.0, -10.0, -10.0, -10.0]
    result = cscv_pbo([a, [0.0] * 8], splits=4)
    assert result is not None
    assert result.n_below_median == 2
    assert result.pbo == pytest.approx(2.0 / 6.0)


def test_cscv_unevaluable_and_refusals() -> None:
    assert cscv_pbo([[1.0, 2.0]], splits=2) is None  # needs >= 2 strategies
    assert cscv_pbo([[1.0, 2.0], [3.0, 4.0]], splits=4) is None  # sessions < splits
    assert cscv_pbo([[1.0, 2.0, 3.0], [3.0, 2.0, 1.0]], splits=2) is None  # not divisible
    with pytest.raises(ValueError, match="even"):
        cscv_pbo([[1.0, 2.0], [3.0, 4.0]], splits=3)
    with pytest.raises(ValueError, match="same sessions"):
        cscv_pbo([[1.0, 2.0], [1.0]], splits=2)


# ---- concentration_hhi ----------------------------------------------------------------


def test_hhi_equal_monopoly_and_normalization() -> None:
    assert concentration_hhi([1.0, 1.0, 1.0, 1.0]) == pytest.approx(0.25)
    assert concentration_hhi([5.0]) == 1.0
    assert concentration_hhi([3.0, 1.0]) == pytest.approx(0.625)
    assert concentration_hhi([2.0, 2.0]) == pytest.approx(0.5)  # 1/n; weights need not sum to 1
    with pytest.raises(ValueError, match="non-negative"):
        concentration_hhi([1.0, -0.1])
    with pytest.raises(ValueError, match="all be zero"):
        concentration_hhi([0.0, 0.0])
    with pytest.raises(ValueError, match="at least one"):
        concentration_hhi([])


# ---- block_shuffle + random_scores ----------------------------------------------------


def test_block_shuffle_identity_determinism_and_block_integrity() -> None:
    values = tuple(float(i) for i in range(8))
    assert block_shuffle(values, block_size=8, seed=5) == values
    assert block_shuffle(values, block_size=2, seed=5) == block_shuffle(
        values, block_size=2, seed=5
    )
    shuffled = block_shuffle(values, block_size=2, seed=5)
    assert sorted(shuffled) == sorted(values)
    # blocks keep their internal order; only block ORDER moves
    for a, b in ((0.0, 1.0), (2.0, 3.0), (4.0, 5.0), (6.0, 7.0)):
        assert shuffled.index(a) + 1 == shuffled.index(b)
    assert block_shuffle(values, block_size=2, seed=5) != block_shuffle(
        values, block_size=2, seed=6
    )


def test_block_shuffle_keeps_the_partial_tail_in_place() -> None:
    shuffled = block_shuffle([1.0, 2.0, 3.0, 4.0, 5.0], block_size=2, seed=9)
    assert shuffled[-1] == 5.0  # the trailing half-block never moves
    assert sorted(shuffled) == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_block_shuffle_refusals() -> None:
    with pytest.raises(ValueError, match="block_size"):
        block_shuffle([1.0], block_size=2, seed=0)
    with pytest.raises(ValueError, match="block_size"):
        block_shuffle([1.0, 2.0], block_size=0, seed=0)
    with pytest.raises(ValueError, match="finite"):
        block_shuffle([1.0, math.inf], block_size=1, seed=0)


def test_random_scores_deterministic_and_bounded() -> None:
    assert random_scores(64, seed=11) == random_scores(64, seed=11)
    assert random_scores(64, seed=11) != random_scores(64, seed=12)
    assert all(0.0 <= value < 1.0 for value in random_scores(256, seed=3))
    assert random_scores(0, seed=1) == ()
    # the byte contract: these literals ARE the negative control — any drift
    # (RNG change, bound change, seed plumbing) is a different control
    assert random_scores(3, seed=7) == (
        0.32383276483316237,
        0.15084917392450192,
        0.6509344730398537,
    )
    with pytest.raises(ValueError, match="count"):
        random_scores(-1, seed=1)
