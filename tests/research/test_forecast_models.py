"""Forecast models (RL-3) — rw_full / rw_window / ar1_direct, each
pinned by hand construction.

The ar1_direct oracle rebuilds the series in THIS test from the
declared recursion (the definition, not the implementation) and
hand-derives the direct h-step error quantiles at taus {0, 0.5, 1}
(order statistics: min / median / max of the error set). A forbidden
scaled-ONE-step implementation FAILS that oracle — its band width is
the one-step residual width, which the direct h-step errors do not
have. The theoretical innovation factor sqrt(sum phi^(2j)) is pinned
separately as a unit check of the FACT, not of any estimator.
"""
from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from tree_options.research.forecast.contracts import (
    EMPIRICAL_WINDOW_SESSIONS,
    QUANTILE_GRID,
)
from tree_options.research.forecast.harness import (
    ar1_direct,
    bind,
    rw_full,
    rw_window,
)

# taus chosen so (K - 1) * tau is integral for K = 5 -> exact order
# statistics under the declared linear interpolation convention.
ORDER_STAT_TAUS = (0.0, 0.25, 0.5, 0.75, 1.0)


class TestRwFull:
    def test_equal_changes_put_every_quantile_at_one_value(self) -> None:
        # closes = (100, 110, 121): both eligible h=1 changes are
        # ln(1.1), so EVERY quantile is ln(121) + ln(1.1) = ln(133.1).
        closes = (100.0, 110.0, 121.0)
        out = rw_full(closes, h=1, taus=QUANTILE_GRID)
        assert out is not None
        for v in out:
            assert v == pytest.approx(math.log(133.1), rel=1e-12)

    def test_exact_order_statistics(self) -> None:
        # changes {-0.10, -0.05, 0, +0.05, +0.10} built into the levels;
        # at ORDER_STAT_TAUS the quantiles are the order statistics.
        changes = [-0.10, -0.05, 0.0, 0.05, 0.10]
        closes = [100.0]
        for d in changes:
            closes.append(closes[-1] * math.exp(d))
        closes = tuple(closes)
        out = rw_full(closes, h=1, taus=ORDER_STAT_TAUS)
        assert out is not None
        for got, d in zip(out, changes, strict=True):
            assert got == pytest.approx(
                math.log(closes[-1]) + d, rel=1e-12)

    def test_too_few_pairs_refuses(self) -> None:
        assert rw_full((100.0, 101.0), h=5, taus=QUANTILE_GRID) is None


class TestRwWindow:
    def test_differs_from_full_and_equals_truncated_full(self) -> None:
        # 253 eligible changes: three extreme losers first, then 250
        # calm ones. rw_window keeps exactly the trailing 250; rw_full
        # is polluted by the extremes.
        rng = np.random.default_rng(7)
        calm = rng.normal(0.0, 0.01, size=250)
        changes = [-0.5, -0.5, -0.5, *calm.tolist()]
        closes = [100.0]
        for d in changes:
            closes.append(closes[-1] * math.exp(d))
        closes = tuple(closes)
        win = rw_window(closes, h=1, taus=(0.0, 1.0))
        full = rw_full(closes, h=1, taus=(0.0, 1.0))
        assert win is not None and full is not None
        # the window's minimum change is calm; the full history's is
        # the -0.5 extreme:
        assert win[0] > math.log(closes[-1]) - 0.4
        assert full[0] == pytest.approx(math.log(closes[-1]) - 0.5,
                                        rel=1e-9)
        # rw_window == rw_full on the series truncated to the last 250
        # changes (3 leading closes dropped):
        truncated = tuple(closes[3:])
        ref = rw_full(truncated, h=1, taus=(0.0, 1.0))
        assert ref is not None
        assert win == pytest.approx(ref, rel=1e-12)
        assert len(changes) == 3 + EMPIRICAL_WINDOW_SESSIONS


class TestAr1Direct:
    def test_constant_series_refuses(self) -> None:
        # A constant log level is rank-deficient: no fit, no forecast.
        assert ar1_direct((100.0,) * 12, h=2, taus=QUANTILE_GRID) is None

    def test_explosive_phi_refuses(self) -> None:
        # x_{i+1} = 1.5 * x_i + e_i with tiny alternating noise: the
        # fitted phi is ~1.5 >= 1 — refused, never extrapolated.
        x = [1.0]
        for k in range(14):
            x.append(1.5 * x[-1] + (0.01 if k % 2 else -0.01))
        closes = tuple(math.exp(v) for v in x)
        assert ar1_direct(closes, h=2, taus=QUANTILE_GRID) is None

    def test_direct_h_step_bands_by_construction(self) -> None:
        # Declared recursion (the definition): x_{i+1} = 0.2 + 0.5 x_i
        # + e_i, e alternating -0.1 / +0.1, x_0 = 1. The FITTED params
        # are not the true ones (fixed e is not design-orthogonal), so
        # the oracle derives the least-squares fit itself from the 2x2
        # NORMAL EQUATIONS by Cramer's rule — independent arithmetic,
        # not the implementation's lstsq — then builds the direct h=2
        # errors and their min/median/max order statistics.
        h = 2
        x = [1.0]
        for k in range(14):
            x.append(0.2 + 0.5 * x[-1] + (-0.1 if k % 2 == 0 else 0.1))
        closes = tuple(math.exp(v) for v in x)
        out = ar1_direct(closes, h=h, taus=(0.0, 0.5, 1.0))
        assert out is not None

        # normal equations for y = b0 + p*x over the (x_i, x_{i+1}) pairs
        n = len(x) - 1
        sx = sum(x[:-1])
        sxx = sum(v * v for v in x[:-1])
        sy = sum(x[1:])
        sxy = sum(a * b for a, b in itertools.pairwise(x))
        det = n * sxx - sx * sx
        b0 = (sy * sxx - sx * sxy) / det
        p = (n * sxy - sx * sy) / det

        last = len(x) - 1
        geom = (1.0 - p ** h) / (1.0 - p)

        def point(idx: int) -> float:
            return b0 * geom + (p ** h) * x[idx]

        errors = sorted(x[u + h] - point(u) for u in range(last - h + 1))
        expected = [point(last) + errors[0],
                    point(last) + errors[len(errors) // 2],
                    point(last) + errors[-1]]
        for got, want in zip(out, expected, strict=True):
            assert got == pytest.approx(want, abs=1e-9)

    def test_zero_noise_path_fits_exactly(self) -> None:
        # With e = 0 the recursion IS the exact AR path: the fit
        # recovers (0.2, 0.5) and every direct h-step error is zero, so
        # every quantile collapses onto the point forecast.
        h = 2
        x = [1.0]
        for _ in range(14):
            x.append(0.2 + 0.5 * x[-1])
        closes = tuple(math.exp(v) for v in x)
        out = ar1_direct(closes, h=h, taus=(0.0, 0.5, 1.0))
        assert out is not None
        geom = (1.0 - 0.5 ** h) / (1.0 - 0.5)
        point = 0.2 * geom + (0.5 ** h) * x[-1]
        for got in out:
            assert got == pytest.approx(point, abs=1e-9)

    def test_h_step_bands_are_not_scaled_one_step(self) -> None:
        # The same construction: the ONE-step residuals are exactly the
        # injected {-0.1, +0.1} (width 0.2 at the log scale). The
        # direct h=2 error set has a DIFFERENT width — an
        # implementation that scales one-step residuals would reproduce
        # 0.2 and fail this oracle.
        beta0, phi, h = 0.2, 0.5, 2
        x = [1.0]
        for k in range(14):
            x.append(beta0 + phi * x[-1] + (-0.1 if k % 2 == 0 else 0.1))
        closes = tuple(math.exp(v) for v in x)
        out = ar1_direct(closes, h=h, taus=(0.0, 1.0))
        assert out is not None
        assert (out[1] - out[0]) != pytest.approx(0.2, abs=1e-6)
        # and the theoretical unit check of the FACT the scaled-one-step
        # construction gets wrong at long horizons:
        factor = math.sqrt(sum(0.95 ** (2 * j) for j in range(20)))
        assert factor == pytest.approx(2.9897051452848196, rel=1e-9)


class TestBind:
    def test_bound_closures_do_not_interfere(self) -> None:
        closes = tuple(100.0 * (1.01 ** i) for i in range(40))
        narrow = bind(rw_full, h=5, taus=(0.5,))
        wide = bind(rw_full, h=20, taus=(0.05, 0.95))
        a = narrow(closes)
        b = wide(closes)
        assert a is not None and b is not None
        assert len(a) == 1 and len(b) == 2
        # rebinding does not leak state into the earlier closure:
        a2 = narrow(closes)
        assert a2 is not None and a2 == a
