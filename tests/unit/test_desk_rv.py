"""Desk variance proxies (FORECAST-001 definitions): hand-computed oracles."""

from __future__ import annotations

import pytest

from tree_options.desk import rv

# v = ln(O/Cp)^2 + 0.5 ln(H/L)^2 - (2 ln2 - 1) ln(C/O)^2 worked by hand for
# O=101 H=103 L=99 C=102 Cp=100:
#   ln(1.01)^2          = 9.900908408750885e-05
#   0.5*ln(103/99)^2    = 7.844419103162075e-04
#   0.386294*ln(102/101)^2 = 3.749672261776082e-05
V1 = 0.0008459542717859555
# O=100.5 H=102.5 L=98 C=99 with Cp=102
V2 = 0.001139925901800461


def test_variance_proxy_hand_value() -> None:
    assert rv.variance_proxy(101.0, 103.0, 99.0, 102.0, 100.0) == pytest.approx(V1, rel=1e-12)
    assert rv.variance_proxy(100.5, 102.5, 98.0, 99.0, 102.0) == pytest.approx(V2, rel=1e-12)


def test_variance_proxy_refuses_bad_inputs() -> None:
    assert rv.variance_proxy(0.0, 103.0, 99.0, 102.0, 100.0) is None
    assert rv.variance_proxy(101.0, 103.0, 99.0, 102.0, -1.0) is None
    # a flat day with no overnight move has zero variance: missing, not 0
    assert rv.variance_proxy(100.0, 100.0, 100.0, 100.0, 100.0) is None


def _bar(o: float, h: float, lo: float, c: float) -> dict[str, str]:
    return {"open": str(o), "high": str(h), "low": str(lo), "close": str(c), "volume": "1"}


def test_proxy_series_uses_previous_calendar_session() -> None:
    sessions = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    bars = {
        "2025-01-02": _bar(99.0, 100.5, 98.5, 100.0),
        "2025-01-03": _bar(101.0, 103.0, 99.0, 102.0),
        # 2025-01-06 missing: that session and the next have no proxy
        "2025-01-07": _bar(100.5, 102.5, 98.0, 99.0),
    }
    out = rv.proxy_series(bars, sessions)
    assert out[0] is None  # no previous close inside the series
    assert out[1] == pytest.approx(V1, rel=1e-12)
    assert out[2] is None
    assert out[3] is None  # its previous session (01-06) has no bar


def test_ewma_seeded_by_first_22_and_restarted_by_gaps() -> None:
    v: list[float | None] = [1.0] * 22 + [2.0, 2.0]
    out = rv.ewma_series(v, lam=0.94, seed=22)
    assert all(x is None for x in out[:21])
    assert out[21] == pytest.approx(1.0)  # seed: mean of the first 22
    # 0.94*1 + 0.06*2 = 1.06 ; 0.94*1.06 + 0.06*2 = 1.1164
    assert out[22] == pytest.approx(1.06)
    assert out[23] == pytest.approx(1.1164)
    gapped: list[float | None] = [1.0] * 22 + [None] + [3.0] * 22
    g = rv.ewma_series(gapped, lam=0.94, seed=22)
    assert g[22] is None
    assert all(x is None for x in g[23:44])  # a gap restarts the 22-run seed
    assert g[44] == pytest.approx(3.0)


def test_trailing_mean_requires_complete_window() -> None:
    v: list[float | None] = [1.0, 2.0, 3.0, None, 5.0, 6.0]
    assert rv.trailing_mean(v, 2, 3) == pytest.approx(2.0)
    assert rv.trailing_mean(v, 4, 3) is None
    assert rv.trailing_mean(v, 1, 3) is None  # window starts before the series


def test_yang_zhang_hand_value() -> None:
    # worked in a scratch computation from the textbook (Yang-Zhang 2000):
    # overnight o_i = ln(O_i/C_{i-1}), open-close c_i = ln(C_i/O_i),
    # RS_i = ln(H/C)ln(H/O) + ln(L/C)ln(L/O), k = 0.34/(1.34+(n+1)/(n-1));
    # var = s2_o + k s2_c + (1-k) mean(RS) with n-1 sample variances.
    bars = [
        (100.0, 101.0, 99.0, 100.5),
        (100.8, 102.0, 100.1, 101.7),
        (101.0, 101.9, 99.5, 99.8),
        (99.9, 100.6, 98.7, 100.2),
    ]
    opens = [b[0] for b in bars]
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    closes = [b[3] for b in bars]
    got = rv.yang_zhang_variance(opens, highs, lows, closes, prev_close=99.6)
    assert got == pytest.approx(0.0002080916948350857, rel=1e-12)
    assert rv.yang_zhang_variance(opens[:1], highs[:1], lows[:1], closes[:1], 99.6) is None
