"""Desk statistics helpers (FORECAST-001 / IVHIST-001 scoring): hand oracles.

Every expected value is a literal worked by hand (shown in the comments), never
a call into the implementation.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from tree_options.desk import stats


def test_qlike_and_mse_hand_values() -> None:
    # QLIKE(rv=2, f=1) = 2 - ln 2 - 1
    assert stats.qlike(2.0, 1.0) == pytest.approx(1.0 - math.log(2.0), abs=1e-15)
    assert stats.qlike(1.0, 1.0) == 0.0
    # QLIKE(rv=1, f=4) = 0.25 - ln 0.25 - 1 = -0.75 + 1.3862943611198906
    assert stats.qlike(1.0, 4.0) == pytest.approx(0.6362943611198906, abs=1e-15)
    assert stats.mse(3.0, 1.0) == 4.0


def test_newey_west_bartlett_hand_series() -> None:
    # x = 1,2,3,4: mean 2.5, deviations -1.5 -0.5 0.5 1.5
    # gamma0 = (2.25+0.25+0.25+2.25)/4 = 1.25
    # gamma1 = ((-0.5)(-1.5) + (0.5)(-0.5) + (1.5)(0.5))/4 = 0.3125
    # lag 1: weight 1 - 1/2 = 0.5 -> 1.25 + 2*0.5*0.3125 = 1.5625
    assert stats.newey_west_lrv([1.0, 2.0, 3.0, 4.0], 0) == pytest.approx(1.25)
    assert stats.newey_west_lrv([1.0, 2.0, 3.0, 4.0], 1) == pytest.approx(1.5625)


def test_dm_test_one_sided_hand_value() -> None:
    # mean 2.5, Omega (lag 1) 1.5625, T 4: DM = 2.5 / sqrt(1.5625/4) = 4.0
    res = stats.dm_test([1.0, 2.0, 3.0, 4.0], lag=1)
    assert res.n == 4
    assert res.mean == pytest.approx(2.5)
    assert res.stat == pytest.approx(4.0)
    # 1 - Phi(4) = 0.5*erfc(4/sqrt 2) = 3.167124183311998e-05
    assert res.p_one_sided == pytest.approx(3.167124183311998e-05, rel=1e-9)


def test_dm_test_degenerate_is_none() -> None:
    assert stats.dm_test([], lag=0) is None
    assert stats.dm_test([1.0, 1.0, 1.0], lag=1) is None  # zero long-run variance


def test_pearson_and_median() -> None:
    # perfectly linear -> 1; reversed -> -1
    assert stats.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert stats.pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    # x = 1,2,3,4 ; y = 1,3,2,4: cov = (2.25+(-0.5)(-0.5)... worked: sum dx*dy
    # dx = -1.5 -0.5 0.5 1.5 ; dy = -1.5 0.5 -0.5 1.5 -> 2.25-0.25-0.25+2.25 = 4
    # sum dx^2 = 5, sum dy^2 = 5 -> r = 0.8
    assert stats.pearson([1.0, 2.0, 3.0, 4.0], [1.0, 3.0, 2.0, 4.0]) == pytest.approx(0.8)
    assert stats.pearson([1.0, 1.0], [1.0, 2.0]) is None
    assert stats.median([3.0, 1.0, 2.0]) == 2.0
    assert stats.median([4.0, 1.0, 2.0, 3.0]) == 2.5


def test_driscoll_kraay_lag0_equals_clustered_by_date() -> None:
    # y = a + b x with two "names" per date; with lag 0 the DK covariance is
    # the date-clustered sandwich (X'X)^-1 (sum_t s_t s_t') (X'X)^-1.
    x = np.array([0.0, 1.0, 2.0, 3.0, 1.0, 2.0])
    y = np.array([1.0, 2.9, 5.2, 7.0, 3.1, 4.8])
    dates = [0, 0, 1, 1, 2, 2]
    design = np.column_stack([np.ones_like(x), x])
    beta, resid = stats.ols(design, y)
    # normal-equation oracle for the point estimate
    xtx = design.T @ design
    expected_beta = np.linalg.solve(xtx, design.T @ y)
    assert beta == pytest.approx(expected_beta, abs=1e-12)
    cov = stats.driscoll_kraay(design, resid, dates, lag=0)
    scores = [
        design[[0, 1]].T @ resid[[0, 1]],
        design[[2, 3]].T @ resid[[2, 3]],
        design[[4, 5]].T @ resid[[4, 5]],
    ]
    meat = sum(np.outer(s, s) for s in scores)
    bread = np.linalg.inv(xtx)
    assert cov == pytest.approx(bread @ meat @ bread, abs=1e-14)
