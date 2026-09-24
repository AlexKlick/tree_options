"""Desk D4 surface features from a recorded chain, on a synthetic chain whose
mids are planted by Black-Scholes (the hash-pinned ``bs_price``): ATM term,
constant maturity, 25-delta skew, implied earnings move, liquidity score,
IV rank and VRP, plus the ``features`` CLI.

Oracles are computed here from the planted parameters (hand formulas or a
bisection on the planted smile), never by calling the surface module.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tree_options.desk import har, store, surface
from tree_options.desk.__main__ import run_cli
from tree_options.synth_options.greeks import bs_abs_delta, bs_price
from tree_options.time.calendar import StaticSessionCalendar

REPO = Path(__file__).resolve().parents[2]
TREX_CAL = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
R = 0.04
S = 100.0
D = date(2026, 9, 22)
EXPIRIES = {
    date(2026, 10, 12): 20,
    date(2026, 11, 6): 45,
    date(2026, 12, 31): 100,
    date(2027, 4, 10): 200,
}
REPORT = "2026-10-20"  # event pair (10-20, 10-21): the 45/100/200 expiries hold it
SIGMA, JUMP = 0.25, 0.05
SLOPE = -0.10  # smile: iv(K) = atm + SLOPE * ln(K/F)


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return StaticSessionCalendar(TREX_CAL, TREX_CAL.with_suffix(".sha256"))


def _atm(dte: int) -> float:
    t = dte / 365.0
    if dte == 20:
        return 0.30  # expires before the event
    return math.sqrt((SIGMA**2 * t + JUMP**2) / t)


def _fwd(dte: int) -> float:
    return S * math.exp(R * dte / 365.0)


def _iv(k: float, dte: int) -> float:
    return _atm(dte) + SLOPE * math.log(k / _fwd(dte))


def _chain_doc() -> dict[str, Any]:
    cols: dict[str, list[Any]] = {
        c: []
        for c in (
            "occ",
            "exp",
            "right",
            "strike",
            "bid",
            "ask",
            "bid_size",
            "ask_size",
            "iv",
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "theo",
            "oi",
            "volume",
            "last",
            "last_time",
        )
    }
    for exp, dte in EXPIRIES.items():
        for i in range(121):
            k = 70.0 + 0.5 * i
            for right in ("C", "P"):
                mid = bs_price(
                    spot=S,
                    strike=k,
                    dte_calendar_days=dte,
                    iv=_iv(k, dte),
                    risk_free=R,
                    dividend_yield=0.0,
                    call_put=right,
                )  # type: ignore[arg-type]
                cols["occ"].append(f"AAPL{exp:%y%m%d}{right}{int(k * 1000):08d}")
                cols["exp"].append(exp.isoformat())
                cols["right"].append(right)
                cols["strike"].append(k)
                cols["bid"].append(mid - 0.01)
                cols["ask"].append(mid + 0.01)
                cols["oi"].append(1000 if k == int(k) else 100)
                for c in (
                    "bid_size",
                    "ask_size",
                    "iv",
                    "delta",
                    "gamma",
                    "theta",
                    "vega",
                    "rho",
                    "theo",
                    "volume",
                    "last",
                    "last_time",
                ):
                    cols[c].append(None)
    n = len(cols["occ"])
    return {
        "header": {
            "schema": store.SCHEMA,
            "session": D.isoformat(),
            "underlying": "AAPL",
            "underlying_quote": {"close": S, "current_price": S},
            "n": n,
        },
        "columns": cols,
    }


def _bisect(f: Any, lo: float, hi: float) -> float:
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if (f(lo) < 0) == (f(mid) < 0):
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _oracle_skew(dte: int) -> float:
    def put_d(k: float) -> float:
        return (
            -bs_abs_delta(
                spot=S,
                strike=k,
                dte_calendar_days=dte,
                iv=_iv(k, dte),
                risk_free=R,
                dividend_yield=0.0,
                call_put="P",
            )
            + 0.25
        )

    def call_d(k: float) -> float:
        return (
            bs_abs_delta(
                spot=S,
                strike=k,
                dte_calendar_days=dte,
                iv=_iv(k, dte),
                risk_free=R,
                dividend_yield=0.0,
                call_put="C",
            )
            - 0.25
        )

    kp = _bisect(put_d, 30.0, 100.0)
    kc = _bisect(call_d, 100.0, 250.0)
    return _iv(kp, dte) - _iv(kc, dte)


def _quotes() -> list[surface.Quote]:
    return surface.chain_quotes(_chain_doc(), session=D, spot=S, rate=R)


def test_atm_term_recovers_planted_ivs() -> None:
    term = surface.atm_term(_quotes(), spot=S, rate=R)
    assert [p.dte for p in term] == [20, 45, 100, 200]
    for p in term:
        assert p.iv == pytest.approx(_atm(p.dte), abs=1e-7)


def test_constant_maturity_and_term_slope() -> None:
    term = surface.atm_term(_quotes(), spot=S, rate=R)

    def cm(days: float, a: int, b: int) -> float:
        wa, wb = _atm(a) ** 2 * a, _atm(b) ** 2 * b
        return math.sqrt((wa + (wb - wa) * (days - a) / (b - a)) / days)

    iv = surface.constant_maturity(term)
    assert iv[30] == pytest.approx(cm(30, 20, 45), abs=1e-7)
    assert iv[60] == pytest.approx(cm(60, 45, 100), abs=1e-7)
    assert iv[90] == pytest.approx(cm(90, 45, 100), abs=1e-7)
    assert iv[180] == pytest.approx(cm(180, 100, 200), abs=1e-7)
    assert surface.term_slope(iv) == pytest.approx(cm(90, 45, 100) / cm(30, 20, 45) - 1, abs=1e-6)
    short = [p for p in term if p.dte >= 45]
    assert surface.constant_maturity(short)[30] is None  # never extrapolated


def test_skew25_matches_bisection_on_planted_smile() -> None:
    q = _quotes()
    per = surface.skew_by_expiry(q)
    for exp, dte in EXPIRIES.items():
        assert per[exp] == pytest.approx(_oracle_skew(dte), abs=2e-4)
    sk = surface.skew_at(per, EXPIRIES)
    s20, s45, s100 = _oracle_skew(20), _oracle_skew(45), _oracle_skew(100)
    assert sk[30] == pytest.approx(s20 + (s45 - s20) * 10 / 25, abs=2e-4)
    assert sk[90] == pytest.approx(s45 + (s100 - s45) * 45 / 55, abs=2e-4)


def test_implied_earnings_move_two_expiry_split(cal: StaticSessionCalendar) -> None:
    term = surface.atm_term(_quotes(), spot=S, rate=R)
    ev = surface.implied_event_move(term, [REPORT], D, cal)
    assert ev["next_report"] == REPORT
    assert ev["event_sessions"] == ["2026-10-20", "2026-10-21"]
    assert ev["expiries"] == ["2026-11-06", "2026-12-31"]
    assert ev["implied_move"] == pytest.approx(JUMP, abs=1e-6)
    assert ev["implied_mean_abs_move"] == pytest.approx(JUMP * math.sqrt(2 / math.pi), abs=1e-6)
    none = surface.implied_event_move(term, [], D, cal)
    assert none == {"next_report": None}


def test_historical_earnings_moves(cal: StaticSessionCalendar) -> None:
    closes = {"2026-07-20": 100.0, "2026-07-21": 101.0, "2026-07-22": 95.0, "2026-07-23": 96.0}
    bars = {d: {"close": str(c)} for d, c in closes.items()}
    got = surface.historical_event_moves(bars, ["2026-07-21", REPORT], D, cal)
    # pair (07-21, 07-22): |ln(C_07-22 / C_07-20)| = |ln 0.95|
    assert got == {"n": 1, "mean_abs_move": pytest.approx(abs(math.log(0.95)))}


def test_liquidity_score_counts_by_rule() -> None:
    expected = 0
    for _exp, dte in EXPIRIES.items():
        if not 30 <= dte <= 240:
            continue
        for i in range(0, 121, 2):  # OI 1000 only on whole strikes
            k = 70.0 + 0.5 * i
            for right in ("C", "P"):
                d = bs_abs_delta(
                    spot=S,
                    strike=k,
                    dte_calendar_days=dte,
                    iv=_iv(k, dte),
                    risk_free=R,
                    dividend_yield=0.0,
                    call_put=right,
                )  # type: ignore[arg-type]
                mid = bs_price(
                    spot=S,
                    strike=k,
                    dte_calendar_days=dte,
                    iv=_iv(k, dte),
                    risk_free=R,
                    dividend_yield=0.0,
                    call_put=right,
                )  # type: ignore[arg-type]
                if 0.2 <= d <= 0.8 and 0.02 <= 0.05 * mid:
                    expected += 1
    assert expected > 0
    assert surface.liquidity_score(_quotes()) == expected


def test_iv_rank_percentile_and_small_n(cal: StaticSessionCalendar) -> None:
    sessions = [s for s in cal.sessions() if s < D][-130:]
    hist = {s: 0.10 + 0.001 * i for i, s in enumerate(sessions)}  # 0.100 .. 0.229
    got = surface.iv_rank(0.15, hist, D, cal)
    # rank = (0.15 - 0.10) / (0.229 - 0.10); percentile = #{< 0.15} / 130 = 50/130
    assert got["n"] == 130 and got["low_n"] is False
    assert got["rank"] == pytest.approx(0.05 / 0.129)
    assert got["percentile"] == pytest.approx(50 / 130)
    few = surface.iv_rank(0.15, dict(list(hist.items())[:100]), D, cal)
    assert few["n"] == 100 and few["low_n"] is True
    assert got["outside_range"] is None
    # the chain-mid IV can leave the VWAP history's range: rank clamps, flagged
    below = surface.iv_rank(0.05, hist, D, cal)
    assert below["rank"] == 0.0 and below["percentile"] == 0.0
    assert below["outside_range"] == "below"
    above = surface.iv_rank(0.40, hist, D, cal)
    assert above["rank"] == 1.0 and above["percentile"] == 1.0
    assert above["outside_range"] == "above"


def test_vrp_hand_value() -> None:
    # var 0.004 over a 28-calendar-day window -> annual vol sqrt(0.004*365/28)
    assert surface.vrp(0.25, 0.004, 28) == pytest.approx(0.25 - math.sqrt(0.004 * 365 / 28))


# ------------------------------------------------------------------ CLI


def _panel(cal: StaticSessionCalendar) -> dict[str, Any]:
    rng = np.random.default_rng(3)
    sessions = [s for s in cal.sessions() if date(2024, 6, 3) <= s <= D]
    panel: dict[str, Any] = {}
    for name in ("AAPL", "SPY", "QQQ"):
        close = 100.0
        bars = {}
        for s in sessions:
            o = close * math.exp(0.005 * rng.normal())
            c = o * math.exp(0.012 * rng.normal())
            hi = max(o, c) * math.exp(abs(rng.normal()) * 0.004)
            lo = min(o, c) * math.exp(-abs(rng.normal()) * 0.004)
            bars[s.isoformat()] = {
                "open": f"{o:.4f}",
                "high": f"{hi:.4f}",
                "low": f"{lo:.4f}",
                "close": f"{c:.4f}",
                "volume": 100,
            }
            close = float(f"{c:.4f}")
        panel[name] = bars
    return panel


def test_features_cli_writes_the_session_file(
    tmp_path: Path, cal: StaticSessionCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    paper = tmp_path / "paper"
    paper.mkdir()
    monkeypatch.setenv("DESK_STORE", str(root))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(paper))
    chain = root / "chains" / D.isoformat() / "AAPL.json.gz"
    chain.parent.mkdir(parents=True)
    chain.write_bytes(store.encode_document(_chain_doc()))
    (root / "indices").mkdir()
    (root / "indices" / "DTB3.csv").write_text("observation_date,DTB3\n2026-09-21,4.00\n")
    panel = _panel(cal)
    (paper / "ohlc-panel.json").write_text(json.dumps(panel))
    (paper / "earnings-calendar.json").write_text(json.dumps({"AAPL": ["2026-07-30", REPORT]}))
    hist_sessions = [s for s in cal.sessions() if s < D][-130:]
    (root / "iv-history").mkdir()
    (root / "iv-history" / "vwap_atm.json").write_text(
        json.dumps(
            {
                "schema": "desk-ivhist/1",
                "names": {
                    "AAPL": {
                        "sessions": {
                            s.isoformat(): {"iv30": 0.2, "method": "interpolated"}
                            for s in hist_sessions
                        }
                    }
                },
            }
        )
    )
    (root / "iv-history" / "IVHIST-001-verdict.json").write_text(
        json.dumps({"labels": {"AAPL": "ok"}})
    )
    rc = run_cli(
        ["features", "--session", D.isoformat()],
        cal=cal,
        now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    assert rc == 0
    out = json.loads((root / "features" / f"{D.isoformat()}.json").read_text())
    assert out["schema"] == surface.FEATURES_SCHEMA and out["session"] == D.isoformat()
    f = out["names"]["AAPL"]
    assert f["iv"]["30"] == pytest.approx(
        math.sqrt((0.30**2 * 20 + (_atm(45) ** 2 * 45 - 0.30**2 * 20) * 10 / 25) / 30), abs=1e-6
    )
    assert f["iv_rank"]["n"] == 130 and f["iv_rank"]["history_label"] == "ok"
    assert f["earnings"]["next_report"] == REPORT
    fc = f["forecast"]
    expected_source = "har" if har.FORECAST_001_VERDICT == "PASS" else "rv22"
    assert fc["source"] == expected_source
    end = cal.nth_after(D, 20)
    c20 = round(
        (
            datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp()
            - datetime(D.year, D.month, D.day, tzinfo=UTC).timestamp()
        )
        / 86400
    )
    assert fc["window_calendar_days"] == c20
    assert fc["vrp_rv22"] == pytest.approx(f["iv"]["30"] - math.sqrt(fc["rv22_var"] * 365 / c20))
    assert fc["vrp_har"] == pytest.approx(f["iv"]["30"] - math.sqrt(fc["har_var"] * 365 / c20))
    assert fc["vrp"] == fc[f"vrp_{expected_source}"]
    assert fc["har_status"] == ("validated" if expected_source == "har" else "unvalidated")
    # a rerun rewrites the derived file with identical content (the panel is
    # the only input that can move, and it did not)
    first = (root / "features" / f"{D.isoformat()}.json").read_bytes()
    assert (
        run_cli(
            ["features", "--session", D.isoformat()],
            cal=cal,
            now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        )
        == 0
    )
    assert (root / "features" / f"{D.isoformat()}.json").read_bytes() == first


def test_features_cli_without_chains_exits_1(
    tmp_path: Path, cal: StaticSessionCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))
    assert run_cli(["features", "--session", D.isoformat()], cal=cal) == 1
    assert run_cli(["features", "--session", "2026-09-26"], cal=cal) == 2  # a Saturday
