"""Forecast metrics (RL-3) — every oracle hand-derived, including the
one that makes the false-CRPS claim un-implementable.

The U[1, 2] counterexample below is the campaign's court evidence that
the aggregate score is a GRID score, not CRPS: exact CRPS is derived in
this test from the definition CRPS = E|X - y| - 1/2E|X - X'| (with
E|X - X'| = (b - a)/3 for U[a, b], a textbook result re-derived in the
comment), while the grid score is computed term by term.
"""

from __future__ import annotations

import math
from typing import ClassVar

import pytest

from tree_options.evaluation.diagnostics import block_bootstrap_ci
from tree_options.research.forecast.contracts import (
    BOOTSTRAP_BLOCK,
    QUANTILE_GRID,
)
from tree_options.research.forecast.metrics import (
    coverage_bootstrap_ci,
    dm_on_differentials,
    empirical_coverage,
    grid_quantile_score,
    mean_interval_width,
    per_origin_grid_losses,
    pinball_losses,
    skill_matched,
    wilson_interval,
)


class TestPinball:
    def test_single_points_by_hand(self) -> None:
        # y = 100, q = 105, tau = 0.9: forecast too HIGH ->
        # (1 - tau) * (q - y) = 0.1 * 5 = 0.5
        out = pinball_losses([100.0], [[105.0]], [0.9])
        assert out[0.9] == pytest.approx(0.5)
        # y = 105, q = 100, tau = 0.9: forecast too LOW ->
        # tau * (y - q) = 0.9 * 5 = 4.5
        out = pinball_losses([105.0], [[100.0]], [0.9])
        assert out[0.9] == pytest.approx(4.5)
        # tau = 0.5 is symmetric: |y - q| / 2 either way
        assert pinball_losses([100.0], [[105.0]], [0.5])[0.5] == pytest.approx(2.5)
        assert pinball_losses([105.0], [[100.0]], [0.5])[0.5] == pytest.approx(2.5)

    def test_symmetric_sample_mean_is_tau_free(self) -> None:
        # Hand oracle: q = 100 against y in {100, 110, 90}. The three
        # pinballs are 0, 10*tau, 10*(1 - tau); their mean is 10/3 for
        # EVERY tau — the tau asymmetry cancels on a symmetric sample.
        for tau in QUANTILE_GRID:
            out = pinball_losses([100.0, 110.0, 90.0], [[100.0]] * 3, [tau])
            assert out[tau] == pytest.approx(10.0 / 3.0)

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="shape mismatch"):
            pinball_losses([1.0, 2.0], [[1.0]], [0.5])


class TestGridScoreIsNotCRPS:
    def test_uniform_counterexample(self) -> None:
        # Forecast F = Uniform[1, 2] (quantile function Q(tau) = 1 + tau),
        # observation y = 1.5, the declared grid.
        taus = list(QUANTILE_GRID)
        y = [1.5]
        q = [[1.0 + t for t in taus]]
        per_tau = pinball_losses(y, q, taus)
        # Term by term: for tau <= 0.5 the quantile is below y
        # (tau * (0.5 - tau)); for tau >= 0.5 it is above
        # ((1 - tau) * (tau - 0.5)). Hand values:
        #   0.05 -> 0.05*0.45 = 0.0225 ; 0.25 -> 0.25*0.25 = 0.0625
        #   0.5  -> 0.0        ;         0.75 -> 0.25*0.25 = 0.0625
        #   0.95 -> 0.05*0.45 = 0.0225
        expected = {0.05: 0.0225, 0.25: 0.0625, 0.5: 0.0, 0.75: 0.0625, 0.95: 0.0225}
        for tau, want in expected.items():
            assert per_tau[tau] == pytest.approx(want)
        # grid mean pinball = 0.17/5 = 0.034, so the grid score is 0.068
        grid = grid_quantile_score(expected.values())
        assert grid == pytest.approx(2.0 * 0.034)
        # Exact CRPS, derived from the definition in THIS test:
        #   E|X - 1.5| = integral_1^1.5 (1.5 - x) dx
        #              + integral_1.5^2 (x - 1.5) dx = 1/8 + 1/8 = 1/4
        #   E|X - X'|  = (b - a)/3 = 1/3 for U[a, b]
        #   CRPS = 1/4 - (1/2)(1/3) = 1/12
        e_abs_x_y = 0.25
        e_abs_x_x = 1.0 / 3.0
        crps = e_abs_x_y - 0.5 * e_abs_x_x
        assert crps == pytest.approx(1.0 / 12.0)
        # The grid score is materially BELOW exact CRPS (18% low) — the
        # two must never be conflated:
        assert grid < crps
        assert crps - grid == pytest.approx(1.0 / 12.0 - 0.068, abs=1e-6)

    def test_grid_score_is_exactly_twice_the_grid_mean(self) -> None:
        vals = [0.1, 0.2, 0.3, 0.4, 0.5]
        assert grid_quantile_score(vals) == pytest.approx(2.0 * 0.3)

    def test_empty_pinball_refused(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            grid_quantile_score([])


class TestCoverage:
    def test_bounds_are_inclusive_by_declaration(self) -> None:
        hits, n = empirical_coverage([5.0], [5.0], [6.0])
        assert (hits, n) == (1, 1)
        hits, n = empirical_coverage([6.0], [5.0], [6.0])
        assert (hits, n) == (1, 1)
        hits, n = empirical_coverage([6.0001], [5.0], [6.0])
        assert (hits, n) == (0, 1)

    def test_counts_over_a_series(self) -> None:
        y = [5.0, 5.5, 6.0, 6.5, 4.9]
        lo = [5.0] * 5
        hi = [6.0] * 5
        # inside: 5.0, 5.5, 6.0 ; outside: 6.5, 4.9
        assert empirical_coverage(y, lo, hi) == (3, 5)

    def test_width(self) -> None:
        assert mean_interval_width([1.0, 2.0], [3.0, 6.0]) == pytest.approx(3.0)


class TestWilson:
    def test_hand_computed_interval(self) -> None:
        # hits = 8, n = 20, z = 1.96 — the interval recomputed here from
        # the published formula, independently of the implementation:
        z, n, k = 1.96, 20, 8
        p = k / n
        z2 = z * z
        denom = 1.0 + z2 / n
        center = (p + z2 / (2.0 * n)) / denom
        half = (z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))) / denom
        lo, hi = wilson_interval(8, 20, z=1.96)
        assert lo == pytest.approx(center - half)
        assert hi == pytest.approx(center + half)
        assert 0.21 < lo < 0.23 and 0.60 < hi < 0.63  # hand sanity band

    def test_edges_stay_unit_and_are_exact(self) -> None:
        # At hits = 0 the Wilson lower bound is exactly 0; at hits = n
        # the upper is exactly 1 (sqrt(z^2/(4n^2)) = z/(2n) collapses
        # the half-width onto the center offset).
        lo0, hi0 = wilson_interval(0, 20, z=1.96)
        assert lo0 == pytest.approx(0.0, abs=1e-12)
        assert 0.0 < hi0 < 0.2
        lon, hin = wilson_interval(20, 20, z=1.96)
        assert hin == pytest.approx(1.0, abs=1e-12)
        assert 0.8 < lon < 1.0

    def test_invalid_counts_refused(self) -> None:
        with pytest.raises(ValueError):
            wilson_interval(21, 20)
        with pytest.raises(ValueError):
            wilson_interval(1, 0)


class TestBootstrap:
    def test_deterministic_under_a_fixed_seed(self) -> None:
        hits = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0]
        a = coverage_bootstrap_ci(hits, block_size=2, iterations=200, seed=42)
        b = coverage_bootstrap_ci(hits, block_size=2, iterations=200, seed=42)
        assert a is not None and b is not None
        assert (a.lower, a.upper) == (b.lower, b.upper)
        assert 0.0 <= a.lower <= a.upper <= 1.0

    def test_block_two_matches_the_shared_helper(self) -> None:
        # The CI must be the shared moving-block bootstrap AT THE
        # REQUESTED BLOCK SIZE (checkpoint B, surviving mutation 3):
        # recompute with ``evaluation.diagnostics.block_bootstrap_ci``
        # verbatim and demand exact agreement. The trailing inequality
        # self-validates the oracle — block 1 must genuinely differ on
        # this sample, else this test could not kill an
        # independent-observation mutant and must be re-derived.
        hits = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]

        def mean_stat(sample: list[float]) -> float | None:
            return sum(sample) / len(sample) if sample else None

        got = coverage_bootstrap_ci(hits, block_size=BOOTSTRAP_BLOCK, iterations=500, seed=7)
        want = block_bootstrap_ci(
            hits,
            statistic=mean_stat,
            block_size=BOOTSTRAP_BLOCK,
            iterations=500,
            seed=7,
            confidence=0.95,
        )
        assert got is not None and want is not None
        assert (got.lower, got.upper) == (want.lower, want.upper)
        indep = block_bootstrap_ci(
            hits, statistic=mean_stat, block_size=1, iterations=500, seed=7, confidence=0.95
        )
        assert indep is not None
        assert (indep.lower, indep.upper) != (got.lower, got.upper)

    def test_degenerate_coverage_returns_none(self) -> None:
        # All hits / all misses: every resample reproduces the same
        # proportion, so a bootstrap interval is not uncertainty
        # evidence — the caller records the degenerate reason and keeps
        # Wilson.
        assert coverage_bootstrap_ci([1.0] * 10, block_size=2, iterations=100, seed=1) is None
        assert coverage_bootstrap_ci([0.0] * 10, block_size=2, iterations=100, seed=1) is None
        assert coverage_bootstrap_ci([1.0], block_size=2, iterations=100, seed=1) is None


class TestSkillMatched:
    def test_hand_skill(self) -> None:
        out = skill_matched([2.0, 2.0, 2.0], [4.0, 4.0, 4.0], paired_floor=2)
        assert out["pinball_skill"] == pytest.approx(0.5)
        assert out["paired_n"] == 3
        assert out["reason"] is None

    def test_equal_losses_score_zero(self) -> None:
        out = skill_matched([1.0, 3.0], [3.0, 1.0], paired_floor=2)
        assert out["pinball_skill"] == pytest.approx(0.0)

    def test_paired_floor_refuses_a_number(self) -> None:
        # Two models can each clear the origin floor with a tiny
        # intersection — no skill number may be emitted.
        out = skill_matched([2.0, 2.0], [4.0, 4.0], paired_floor=8)
        assert out["pinball_skill"] is None
        assert out["paired_n"] == 2
        assert out["reason"] == "paired_cohort_insufficient"

    def test_nonpositive_benchmark_refuses_a_ratio(self) -> None:
        out = skill_matched([2.0, 2.0], [0.0, 0.0], paired_floor=2)
        assert out["pinball_skill"] is None
        assert out["reason"] == "baseline loss not positive"

    def test_length_mismatch_refused(self) -> None:
        with pytest.raises(ValueError, match="share a length"):
            skill_matched([1.0], [1.0, 2.0], paired_floor=1)


class TestDMHandOracle:
    """d = [1, 2, 3, 4], derived by hand from the population
    autocovariance definition (Bartlett weights 1 - j/(lag+1)):

        mean = 2.5 ; dev = [-1.5, -0.5, 0.5, 1.5]
        gamma_0 = (2.25 + 0.25 + 0.25 + 2.25)/4 = 1.25
        gamma_1 = (dev1*dev0 + dev2*dev1 + dev3*dev2)/4
               = (0.75 - 0.25 + 0.75)/4 = 0.3125
        lag 1: lrv = 1.25 + 2*(1/2)*0.3125 = 1.5625
               stat = 2.5 / sqrt(1.5625/4) = 2.5/0.625 = 4.0
               p    = 1 - Phi(4) = erfc(4/sqrt(2))/2
        lag 0: lrv = 1.25 ; stat = 2.5/sqrt(1.25/4) = 4.472136
    """

    D: ClassVar[list[float]] = [1.0, 2.0, 3.0, 4.0]

    def test_lag_1(self) -> None:
        out = dm_on_differentials(self.D, lag=1)
        assert out is not None
        assert out.n == 4
        assert out.mean == pytest.approx(2.5)
        assert out.stat == pytest.approx(4.0)
        assert out.p_one_sided == pytest.approx(0.5 * math.erfc(4.0 / math.sqrt(2.0)))

    def test_lag_0(self) -> None:
        out = dm_on_differentials(self.D, lag=0)
        assert out is not None
        assert out.stat == pytest.approx(4.47213595499958)

    def test_negation_reverses_the_statistic(self) -> None:
        out = dm_on_differentials([-v for v in self.D], lag=1)
        assert out is not None
        assert out.stat == pytest.approx(-4.0)
        assert out.p_one_sided == pytest.approx(1.0 - 0.5 * math.erfc(4.0 / math.sqrt(2.0)))

    def test_zero_variance_returns_none(self) -> None:
        # A constant differential carries no test — None on the wire
        # with a reason, never an invented p-value.
        assert dm_on_differentials([2.0, 2.0, 2.0], lag=1) is None
        assert dm_on_differentials([], lag=1) is None


class TestPerOriginGridLosses:
    def test_hand_value(self) -> None:
        # Single origin, single tau = 0.5, y = 12 vs q = 10:
        # pinball = 0.5*2 = 1 ; grid loss = 2*mean = 2.
        out = per_origin_grid_losses([12.0], [[10.0]], [0.5])
        assert out[0] == pytest.approx(2.0)

    def test_orientation_at_asymmetric_tau(self) -> None:
        # Hand-derived at tau = 0.25 (checkpoint B, surviving mutation
        # 2): y = 10 vs q = 8 (forecast too LOW) charges tau*(y - q)
        # = 0.5, grid loss 2*0.5 = 1; y = 10 vs q = 12 (too HIGH)
        # charges (1 - tau)*(q - y) = 1.5, grid loss 3. A reversed
        # weighting returns 3 and 1 — the pairing of value to case is
        # the oracle (tau = 0.5 cannot detect orientation).
        out = per_origin_grid_losses([10.0, 10.0], [[8.0], [12.0]], [0.25])
        assert out[0] == pytest.approx(1.0)
        assert out[1] == pytest.approx(3.0)

    def test_perfect_forecast_is_zero(self) -> None:
        out = per_origin_grid_losses([10.0, 10.0], [[10.0] * 5] * 2, QUANTILE_GRID)
        assert out[0] == pytest.approx(0.0)
        assert out[1] == pytest.approx(0.0)
