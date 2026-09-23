"""Desk D2 IV history (IVHIST-001): a tiny synthetic Polygon-shaped cache with
VWAPs planted by Black-Scholes, and the benchmark verdict/label rules.

Oracles: the planted IVs and the total-variance interpolation worked by hand
here (the pricer ``bs_price`` is the hash-pinned model, used only to plant
premiums).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import ivhist
from tree_options.synth_options.greeks import bs_price
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trex.clock import ET

REPO = Path(__file__).resolve().parents[2]
TREX_CAL = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
R = 0.04


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return StaticSessionCalendar(TREX_CAL, TREX_CAL.with_suffix(".sha256"))


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp()) * 1000


def _bar(d: date, vw: float) -> dict[str, Any]:
    return {"v": 10, "vw": vw, "o": vw, "c": vw, "h": vw, "l": vw, "t": _ms(d), "n": 3}


def _write(cache: Path, name: str, body: dict[str, Any]) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{name}.json").write_text(json.dumps(body))


def _occ(root: str, exp: date, right: str, strike: float) -> str:
    return f"O:{root}{exp.strftime('%y%m%d')}{right}{round(strike * 1000):08d}"


def _dte(a: date, b: date) -> int:
    return round(
        (
            datetime(b.year, b.month, b.day, tzinfo=UTC).timestamp()
            - datetime(a.year, a.month, a.day, tzinfo=UTC).timestamp()
        )
        / 86400
    )


D1 = date(2025, 3, 3)  # both expiries: interpolated
D2 = date(2025, 3, 24)  # only the April expiry left (March expired): extrapolated
D3 = date(2025, 3, 5)  # no unadjusted spot bar: NOT_EVALUABLE
E1 = date(2025, 3, 21)  # third Friday
E2 = date(2025, 4, 17)  # Thursday before Good Friday 2025-04-18 (a monthly)
SPOT = {D1: 100.0, D2: 102.0}
PLANTED = {E1: 0.20, E2: 0.25}


def _build_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "massive-cache"
    _write(
        cache,
        "spy-spot",
        {
            "ticker": "SPY",
            "adjusted": False,
            "resultsCount": 2,
            "results": [_bar(D1, SPOT[D1]), _bar(D2, SPOT[D2])],
        },
    )
    # an adjusted=true stock body must never be used as spot
    _write(
        cache,
        "spy-adjusted",
        {"ticker": "SPY", "adjusted": True, "resultsCount": 1, "results": [_bar(D3, 50.0)]},
    )
    n = 0
    for exp, iv in PLANTED.items():
        for strike in (95.0, 100.0, 105.0):
            for right in ("C", "P"):
                bars = []
                for d in (D1, D2, D3):
                    if d >= exp:
                        continue
                    spot = SPOT.get(d, 100.0)
                    premium = bs_price(
                        spot=spot,
                        strike=strike,
                        dte_calendar_days=_dte(d, exp),
                        iv=iv,
                        risk_free=R,
                        dividend_yield=0.0,
                        call_put=right,  # type: ignore[arg-type]
                    )
                    bars.append(_bar(d, premium))
                n += 1
                _write(
                    cache,
                    f"opt-{n}",
                    {
                        "ticker": _occ("SPY", exp, right, strike),
                        "adjusted": True,
                        "resultsCount": len(bars),
                        "results": bars,
                    },
                )
    # a duplicate file of the 105 call disagreeing on D1: that bar is dropped
    _write(
        cache,
        "opt-dup",
        {
            "ticker": _occ("SPY", E1, "C", 105.0),
            "adjusted": True,
            "resultsCount": 1,
            "results": [_bar(D1, 9.99)],
        },
    )
    # an adjusted deliverable (digit in the root) is excluded
    _write(
        cache,
        "opt-adj",
        {
            "ticker": _occ("SPY1", E1, "C", 100.0),
            "adjusted": True,
            "resultsCount": 1,
            "results": [_bar(D1, 1.0)],
        },
    )
    # a contract master body is ignored
    _write(cache, "master", {"results": [{"cfi": "OCASPS", "ticker": "O:SPY250321C00100000"}]})
    return cache


def test_parse_option_ticker() -> None:
    assert ivhist.parse_option_ticker("O:SPY250321C00587500") == (
        "SPY",
        date(2025, 3, 21),
        "C",
        587.5,
    )
    assert ivhist.parse_option_ticker("O:XOM1241220C00100000") is None  # adjusted root
    assert ivhist.parse_option_ticker("SPY") is None


def test_monthly_expiry_includes_holiday_thursday(cal: StaticSessionCalendar) -> None:
    assert ivhist.is_monthly_expiry_session(date(2025, 3, 21), cal)
    assert ivhist.is_monthly_expiry_session(date(2025, 4, 17), cal)  # Good Friday 04-18
    assert not ivhist.is_monthly_expiry_session(date(2025, 4, 18), cal)
    assert not ivhist.is_monthly_expiry_session(date(2025, 3, 14), cal)


def test_atm_iv_interpolates_in_log_moneyness() -> None:
    f = 100.0
    strikes = [
        ivhist.StrikeIV(95.0, 0.30, 0.30, 0.30),
        ivhist.StrikeIV(98.0, 0.26, 0.26, 0.26),
        ivhist.StrikeIV(103.0, 0.22, 0.22, 0.22),
    ]
    # between 98 (m=ln .98) and 103 (m=ln 1.03): weight = -ln.98/(ln1.03-ln.98)
    w = -math.log(0.98) / (math.log(1.03) - math.log(0.98))
    got = ivhist.atm_iv(strikes, f)
    assert got is not None
    assert got[0] == pytest.approx(0.26 + (0.22 - 0.26) * w, abs=1e-12)
    assert got[1] == "bracket"
    one = ivhist.atm_iv([ivhist.StrikeIV(102.0, 0.21, 0.2, 0.22)], f)
    assert one == (0.21, "one-sided")  # |ln 1.02| <= 0.03
    assert ivhist.atm_iv([ivhist.StrikeIV(104.0, 0.21, 0.2, 0.22)], f) is None


def test_constant_maturity_rules() -> None:
    # bracket: w = iv^2 tau, linear in tau
    w1, w2 = 0.2**2 * 18, 0.25**2 * 45
    expect = math.sqrt((w1 + (w2 - w1) * (30 - 18) / (45 - 18)) / 30)
    assert ivhist.constant_maturity_30([(18, 0.2), (45, 0.25)]) == (
        pytest.approx(expect, abs=1e-15),
        "interpolated",
    )
    assert ivhist.constant_maturity_30([(45, 0.25)]) == (0.25, "extrapolated")
    assert ivhist.constant_maturity_30([(12, 0.25)]) == (
        None,
        "no expiry brackets 30d or lies in 15..60d",
    )
    assert ivhist.constant_maturity_30([(30, 0.3), (60, 0.2)]) == (0.3, "interpolated")


def test_build_history_on_synthetic_cache(tmp_path: Path, cal: StaticSessionCalendar) -> None:
    cache = _build_cache(tmp_path)
    scan = ivhist.scan_cache(cache, ("SPY",), date(2025, 3, 1), date(2025, 3, 31), cal)
    assert scan.stats["adjusted_root_skipped"] == 1
    assert scan.stats["conflicting_bars_dropped"] == 1
    assert scan.stats["master_bodies"] == 1
    rates = ivhist.RateSource.constant(R)
    doc = ivhist.build_history(scan, ("SPY", "GLD"), [D1, D2, D3], rates)
    spy = doc["names"]["SPY"]["sessions"]
    # D1: E1 (tau 18) and E2 (tau 45) both at their planted flat IVs
    w1, w2 = 0.20**2 * 18, 0.25**2 * 45
    expect = math.sqrt((w1 + (w2 - w1) * 12 / 27) / 30)
    assert spy[D1.isoformat()]["method"] == "interpolated"
    assert spy[D1.isoformat()]["iv30"] == pytest.approx(expect, abs=1e-7)
    assert [e[1] for e in spy[D1.isoformat()]["expiries"]] == [18, 45]
    # D2: March expired before D2's window -> only April (tau 24): extrapolated
    assert spy[D2.isoformat()]["method"] == "extrapolated"
    assert spy[D2.isoformat()]["iv30"] == pytest.approx(0.25, abs=1e-7)
    assert spy[D3.isoformat()] == {"status": "NOT_EVALUABLE", "reason": "no unadjusted spot bar"}
    assert doc["names"]["GLD"]["sessions"] == {}
    assert doc["assumptions"]["dividend_yield"] == 0.0


def test_rate_source_reads_fred_point_in_time(tmp_path: Path) -> None:
    p = tmp_path / "DTB3.csv"
    p.write_text("observation_date,DTB3\n2025-03-03,4.20\n2025-03-04,.\n2025-03-05,4.10\n")
    rs = ivhist.RateSource.from_csv(p)
    assert rs.rate_on(date(2025, 3, 3)) == pytest.approx(0.042)
    assert rs.rate_on(date(2025, 3, 4)) == pytest.approx(0.042)  # '.' is missing
    assert rs.rate_on(date(2025, 3, 7)) == pytest.approx(0.041)
    assert rs.rate_on(date(2025, 3, 1)) is None


def test_read_index_csv_both_layouts(tmp_path: Path) -> None:
    a = tmp_path / "VIX_History.csv"
    a.write_text(
        "DATE,OPEN,HIGH,LOW,CLOSE\n09/19/2026,15.0,16.0,14.0,15.5\n09/22/2026,14.6,14.9,14.1,14.21\n"
    )
    b = tmp_path / "GVZ_History.csv"
    b.write_text("DATE,GVZ\n09/22/2026,23.59\n")
    assert ivhist.read_index_csv(a) == {date(2026, 9, 19): 15.5, date(2026, 9, 22): 14.21}
    assert ivhist.read_index_csv(b) == {date(2026, 9, 22): 23.59}


def _history(series: dict[str, list[float]], sessions: list[date]) -> dict[str, Any]:
    names: dict[str, Any] = {}
    for name, vals in series.items():
        names[name] = {
            "sessions": {
                d.isoformat(): {"iv30": v / 100.0, "method": "interpolated"}
                for d, v in zip(sessions, vals, strict=True)
            }
        }
    names["GLD"] = {"sessions": {}}
    names["SMH"] = {"sessions": {}}
    return {"schema": ivhist.SCHEMA, "names": names}


def test_evaluate_pairs_and_labels(cal: StaticSessionCalendar) -> None:
    sessions = [s for s in cal.sessions() if date(2025, 1, 2) <= s][:300]
    idx = [20.0 + 5.0 * math.sin(i / 10.0) for i in range(300)]
    alternating = [x + (10.0 if i % 2 == 0 else -10.0) for i, x in enumerate(idx)]
    series = {
        "SPY": [x - 3.0 for x in idx],  # bias -3: FAIL
        "QQQ": [x + 1.5 for x in idx],  # PASS
        "IWM": alternating,  # median bias 0, low correlation: FAIL
        "AAPL": [x + 1.0 for x in idx],
        "AMZN": [x - 1.99 for x in idx],  # just inside the 2.0 bar
        "GOOGL": [x for x in idx],
        "KO": [x for x in idx],  # unbenchmarked single stock
    }
    indices = {k: dict(zip(sessions, idx, strict=True)) for k in ivhist.INDEX_NAMES}
    res = ivhist.evaluate(_history(series, sessions), indices)
    pairs = res["pairs"]
    assert pairs["VIX"]["status"] == "FAIL"
    assert pairs["VIX"]["median_bias"] == pytest.approx(-3.0)
    assert pairs["VXN"]["status"] == "PASS" and pairs["VXN"]["n"] == 300
    assert pairs["RVX"]["status"] == "FAIL"
    assert pairs["RVX"]["median_bias"] == pytest.approx(0.0, abs=1e-9)
    assert pairs["RVX"]["corr"] < 0.85
    assert pairs["VXAZN"]["status"] == "PASS"
    assert pairs["GVZ"]["status"] == "NOT_EVALUABLE"
    labels = res["labels"]
    assert labels["SPY"] == "low-fidelity" and labels["QQQ"] == "ok"
    assert labels["KO"] == "ok"  # all three single-stock pairs pass
    assert labels["GLD"] == "not-evaluable" and labels["SMH"] == "not-evaluable"
    assert res["overall"] == "PARTIAL"
    # one single-stock pair failing demotes every unbenchmarked single stock
    series["GOOGL"] = [x + 2.5 for x in idx]
    res2 = ivhist.evaluate(_history(series, sessions), indices)
    assert res2["labels"]["KO"] == "low-fidelity"
    assert res2["labels"]["GOOGL"] == "low-fidelity"


def test_evaluate_small_sample_is_not_evaluable(cal: StaticSessionCalendar) -> None:
    sessions = [s for s in cal.sessions() if date(2025, 1, 2) <= s][:249]
    idx = [20.0] * 249
    indices = {k: dict(zip(sessions, idx, strict=True)) for k in ivhist.INDEX_NAMES}
    res = ivhist.evaluate(_history({"SPY": idx}, sessions), indices)
    assert res["pairs"]["VIX"]["status"] == "NOT_EVALUABLE"
    assert res["labels"]["SPY"] == "low-fidelity"
