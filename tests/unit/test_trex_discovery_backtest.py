"""M4: retrospective valuation scenario (moneyness-matched rolling analogs).

Oracles are computed HERE with an independent Black-Scholes put formula
(never the repo pricer the module wraps), then compared.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.discovery.backtest import (
    LABEL,
    MAX_ARTIFACTS,
    calibrate_iv,
    find_structure,
    read_artifact,
    spread_value,
    valuation_scenario,
    write_artifact,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 22, 19, 0, tzinfo=ET)
DAY_MS = 86_400_000
R = 0.04


def _put(spot: float, strike: float, days: int, iv: float) -> float:
    t = days / 365.0
    vol = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (R + 0.5 * iv * iv) * t) / vol
    d2 = d1 - vol

    def n(x: float) -> float:
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    return strike * math.exp(-R * t) * n(-d2) - spot * n(-d1)


def _oracle_spread(spot: float, short: float, long_: float, days: int, iv: float) -> float:
    return _put(spot, long_, days, iv) - _put(spot, short, days, iv)


def _bars(closes: list[float]) -> list[tuple[int, float]]:
    base = 1_780_000_000_000
    return [(base + i * DAY_MS, c) for i, c in enumerate(closes)]


class TestPricing:
    @pytest.mark.parametrize(
        ("spot", "expected"), [(90.0, 5.0), (97.0, 3.0), (102.0, 0.0)]
    )
    def test_expiry_is_intrinsic_clipped_to_width(self, spot: float, expected: float) -> None:
        assert spread_value(spot, 95.0, 100.0, 0, 0.3) == pytest.approx(expected)

    @pytest.mark.parametrize("spot", [90.0, 97.5, 110.0])  # ITM / ATM-ish / OTM
    def test_matches_independent_black_scholes(self, spot: float) -> None:
        got = spread_value(spot, 95.0, 100.0, 30, 0.25)
        assert got == pytest.approx(_oracle_spread(spot, 95.0, 100.0, 30, 0.25), rel=1e-9)

    def test_value_rises_with_vol_for_otm_debit_spread(self) -> None:
        lo = spread_value(100.0, 85.0, 90.0, 30, 0.15)
        hi = spread_value(100.0, 85.0, 90.0, 30, 0.35)
        assert hi > lo

    def test_calibration_round_trips(self) -> None:
        target = _oracle_spread(100.0, 85.0, 90.0, 24, 0.27)
        iv = calibrate_iv(100.0, 85.0, 90.0, 24, target)
        assert iv == pytest.approx(0.27, abs=1e-4)

    def test_calibration_refuses_unbracketed_target(self) -> None:
        assert calibrate_iv(100.0, 85.0, 90.0, 24, 7.0) is None  # > width
        assert calibrate_iv(100.0, 85.0, 90.0, 24, 0.0) is None


class TestScenario:
    def test_flat_market_every_otm_analog_loses_its_debit(self) -> None:
        debit = _oracle_spread(100.0, 90.0, 95.0, 10, 0.30)
        out = valuation_scenario(
            short=90.0, long_=95.0, dte_days=10, spot_now=100.0,
            debit_mid=debit, debit_ask=None, bars=_bars([100.0] * 30), iv30=None,
        )
        assert out["error"] is None
        assert out["iv"] == pytest.approx(0.30, abs=1e-4)
        # starts 0..19 have their +10d expiry inside 30 daily bars
        assert out["analogs"]["count"] == 20
        assert out["analogs"]["wins"] == 0
        assert out["analogs"]["mean_pnl"] == pytest.approx(-debit * 100, rel=1e-4)
        assert out["pessimistic"] is None  # no ask -> no execution bound

    def test_crash_pays_width_minus_debit_and_final_is_intrinsic(self) -> None:
        closes = [100.0] * 15 + [80.0] * 15
        debit = _oracle_spread(100.0, 90.0, 95.0, 10, 0.30)
        out = valuation_scenario(
            short=90.0, long_=95.0, dte_days=10, spot_now=100.0,
            debit_mid=debit, debit_ask=debit + 0.10, bars=_bars(closes), iv30=None,
        )
        assert out["error"] is None
        # analogs opened at 100 that expire after the drop settle at full width
        assert out["analogs"]["best_pnl"] == pytest.approx((5.0 - debit) * 100, rel=1e-4)
        # the most recent analog opens at 80 (strikes 72/76) and stays flat -> loses
        recent = out["recent"]
        k_long, k_short = 95.0 / 100.0, 90.0 / 100.0
        recent_debit = _oracle_spread(80.0, k_short * 80, k_long * 80, 10, out["iv"])
        assert recent["entry_debit"] == pytest.approx(recent_debit, rel=1e-6)
        assert recent["final_pnl"] == pytest.approx(-recent_debit * 100, rel=1e-6)
        assert recent["series"]["points"][-1][1] == pytest.approx(recent["final_pnl"])
        # execution bound: every entry pays the observed half-spread more
        assert out["pessimistic"]["mean_pnl"] == pytest.approx(
            out["analogs"]["mean_pnl"] - 10.0, rel=1e-6
        )

    def test_vol_band_brackets_the_mean(self) -> None:
        closes = [100.0 - 0.3 * i for i in range(40)]
        debit = _oracle_spread(100.0, 90.0, 95.0, 10, 0.30)
        out = valuation_scenario(
            short=90.0, long_=95.0, dte_days=10, spot_now=100.0,
            debit_mid=debit, debit_ask=None, bars=_bars(closes), iv30=None,
        )
        band = out["iv_band_mean_pnl"]
        # higher vol = dearer entry for an OTM debit spread = lower hold P&L
        assert band["hi"] < out["analogs"]["mean_pnl"] < band["lo"]

    def test_too_few_windows_is_an_explained_error(self) -> None:
        out = valuation_scenario(
            short=90.0, long_=95.0, dte_days=40, spot_now=100.0,
            debit_mid=0.5, debit_ask=None, bars=_bars([100.0] * 30), iv30=20.0,
        )
        assert out["error"] and "analog" in out["error"]

    def test_iv30_fallback_when_calibration_fails(self) -> None:
        out = valuation_scenario(
            short=90.0, long_=95.0, dte_days=10, spot_now=100.0,
            debit_mid=9.0, debit_ask=None, bars=_bars([100.0] * 30), iv30=22.0,
        )
        assert out["error"] is None
        assert out["iv"] == pytest.approx(0.22)
        assert "iv30" in out["iv_source"]

    def test_bad_structure_rejected(self) -> None:
        out = valuation_scenario(
            short=95.0, long_=90.0, dte_days=10, spot_now=100.0,
            debit_mid=0.5, debit_ask=None, bars=_bars([100.0] * 30), iv30=20.0,
        )
        assert out["error"]


class TestArtifacts:
    def test_round_trip_and_key_guard(self, tmp_path: Path) -> None:
        write_artifact(tmp_path, "QQQ|20261016|642|657", {"key": "QQQ|20261016|642|657",
                                                           "label": LABEL})
        assert read_artifact(tmp_path, "QQQ|20261016|642|657")["label"] == LABEL
        assert read_artifact(tmp_path, "QQQ|20261016|642|658") is None

    def test_prune_keeps_newest(self, tmp_path: Path) -> None:
        import os
        import time

        for i in range(MAX_ARTIFACTS + 5):
            key = f"SPY|20261016|{500 + i}|{510 + i}"
            path = write_artifact(tmp_path, key, {"key": key})
            stamp = time.time() - 1000 + i
            os.utime(path, (stamp, stamp))
        kept = list((tmp_path / "backtests").glob("*.json"))
        assert len(kept) == MAX_ARTIFACTS
        newest = f"SPY|20261016|{500 + MAX_ARTIFACTS + 4}|{510 + MAX_ARTIFACTS + 4}"
        assert read_artifact(tmp_path, newest) is not None
        assert read_artifact(tmp_path, "SPY|20261016|500|510") is None  # oldest pruned

    def test_find_structure_from_latest_then_shadow(self, tmp_path: Path) -> None:
        (tmp_path / "latest.json").write_text(json.dumps({"payload": {
            "candidates": [{"underlying": "QQQ", "expiry": "20261016", "dte": 24,
                            "short_strike": 642.0, "long_strike": 657.0,
                            "debit_mid": 0.195, "debit_ask": 0.23}],
            "rejected": [],
        }}))
        (tmp_path / "shadow_book.json").write_text(json.dumps({"positions": [
            {"key": "SPY|20261120|700|710", "underlying": "SPY", "expiry": "20261120",
             "short_strike": 700.0, "long_strike": 710.0, "debit_paid": 0.8},
        ]}))
        row = find_structure(tmp_path, "QQQ|20261016|642|657")
        assert row is not None and row["debit_ask"] == pytest.approx(0.23)
        assert row["found_in"] == "latest scan"
        shadow = find_structure(tmp_path, "SPY|20261120|700|710")
        assert shadow is not None and shadow["found_in"] == "shadow book"
        assert shadow["debit_ask"] is None
        assert find_structure(tmp_path, "NVDA|20261016|100|110") is None
