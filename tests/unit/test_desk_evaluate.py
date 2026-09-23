"""FORECAST-001 scoring: loss comparisons, DM wiring, the pre-registered
verdict rule, the encompassing/blend decision and an end-to-end run on a
synthetic panel. Oracles are worked from the loss formulas here."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tree_options.desk import evaluate
from tree_options.time.calendar import StaticSessionCalendar

REPO = Path(__file__).resolve().parents[2]
TREX_CAL = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return StaticSessionCalendar(TREX_CAL, TREX_CAL.with_suffix(".sha256"))


def _q(rv: float, f: float) -> float:
    return rv / f - math.log(rv / f) - 1.0


def test_compare_pools_by_date_then_dm() -> None:
    rows = []
    factors = [2.0, 4.0, 2.0, 4.0]
    for t, k in enumerate(factors):
        for name, rv in (("A", 0.01), ("B", 0.02)):
            rows.append(
                evaluate.ScoreRow(name=name, t=t, rv=rv, har=rv, rv22=k * rv, ewma=None, iv2=None)
            )
    cell = evaluate.compare(rows, "rv22", "qlike", lag=0)
    d = [_q(1.0, k) for k in factors]  # HAR perfect: d_t = QLIKE(bench)
    mean = sum(d) / 4
    var0 = sum((x - mean) ** 2 for x in d) / 4
    assert cell["n_dates"] == 4 and cell["n_rows"] == 8
    assert cell["mean_loss_har"] == 0.0
    assert cell["mean_loss_bench"] == pytest.approx(mean)
    assert cell["dm"] == pytest.approx(mean / math.sqrt(var0 / 4))
    assert cell["p_one_sided"] == pytest.approx(0.5 * math.erfc(cell["dm"] / math.sqrt(2)))
    empty = evaluate.compare(rows, "iv2", "qlike", lag=0)
    assert empty["status"] == "NOT_EVALUABLE" and empty["n_rows"] == 0


def test_mse_cell() -> None:
    rows = [
        evaluate.ScoreRow("A", 0, 1.0, 1.5, 3.0, None, None),
        evaluate.ScoreRow("A", 1, 1.0, 0.5, 2.0, None, None),
        evaluate.ScoreRow("A", 2, 1.0, 1.0, 1.5, None, None),
    ]
    cell = evaluate.compare(rows, "rv22", "mse", lag=1)
    # har: .25 .25 0 ; bench: 4 1 .25
    assert cell["mean_loss_har"] == pytest.approx(0.5 / 3)
    assert cell["mean_loss_bench"] == pytest.approx(5.25 / 3)


def test_verdict_needs_both_primary_cells() -> None:
    def cells(p20: float | None, p63: float | None) -> dict[str, Any]:
        return {
            "20": {"rv22": {"qlike": {"p_one_sided": p20}}},
            "63": {"rv22": {"qlike": {"p_one_sided": p63}}},
        }

    assert evaluate.verdict(cells(0.01, 0.049)) == "PASS"
    assert evaluate.verdict(cells(0.01, 0.05)) == "FAIL"  # strict p < 0.05
    assert evaluate.verdict(cells(0.2, 0.01)) == "FAIL"
    assert evaluate.verdict(cells(None, 0.01)) == "FAIL"


def test_encompassing_allows_informative_iv_only() -> None:
    rng = np.random.default_rng(4)
    rows = []
    for t in range(300):
        for name in ("A", "B"):
            lf_har = -8.0 + rng.normal() * 0.5
            lf_iv = -8.0 + rng.normal() * 0.5
            lrv = 0.5 * lf_har + 0.5 * lf_iv - 4.0 + rng.normal() * 0.2
            rows.append(
                evaluate.ScoreRow(
                    name, t, math.exp(lrv), math.exp(lf_har), None, None, math.exp(lf_iv)
                )
            )
    enc = evaluate.encompassing(rows, lag=4)
    assert enc["allowed"] is True
    assert enc["c2"] == pytest.approx(0.5, abs=0.05)
    noise = [
        evaluate.ScoreRow(r.name, r.t, r.rv, r.har, None, None, math.exp(-8.0 + rng.normal() * 0.5))
        for r in rows
    ]
    enc2 = evaluate.encompassing(noise, lag=4)
    assert enc2["allowed"] is False
    assert evaluate.encompassing([], lag=4) == {
        "status": "NOT_EVALUABLE",
        "allowed": False,
        "reason": "no IV-ok rows",
    }


def _panel(cal: StaticSessionCalendar) -> dict[str, Any]:
    rng = np.random.default_rng(12)
    sessions = [s for s in cal.sessions() if date(2023, 1, 3) <= s <= date(2025, 6, 30)]
    panel: dict[str, Any] = {}
    for name in ("AAPL", "SPY", "QQQ"):
        close, lv = 100.0, math.log(0.012)
        bars = {}
        for s in sessions:
            lv = math.log(0.012) + 0.97 * (lv - math.log(0.012)) + 0.15 * rng.normal()
            sig = math.exp(lv)
            o = close * math.exp(0.4 * sig * rng.normal())
            c = o * math.exp(sig * rng.normal())
            hi = max(o, c) * math.exp(abs(rng.normal()) * sig / 3)
            lo = min(o, c) * math.exp(-abs(rng.normal()) * sig / 3)
            bars[s.isoformat()] = {
                "open": f"{o:.4f}",
                "high": f"{hi:.4f}",
                "low": f"{lo:.4f}",
                "close": f"{c:.4f}",
                "volume": 1,
            }
            close = float(f"{c:.4f}")
        panel[name] = bars
    del panel["QQQ"]["2025-06-30"]  # QQQ ends a session early: the cutoff follows it
    return panel


def test_run_end_to_end_reports_every_cell(cal: StaticSessionCalendar) -> None:
    panel = _panel(cal)
    earnings = {"AAPL": ["2024-10-31", "2025-01-30", "2025-05-01"]}
    iv_hist = {
        "names": {
            "SPY": {
                "sessions": {
                    s.isoformat(): {"iv30": 0.2, "method": "interpolated"}
                    for s in cal.sessions()
                    if date(2024, 9, 3) <= s <= date(2025, 6, 27)
                }
            }
        }
    }
    res = evaluate.run_forecast_001(
        panel,
        earnings,
        cal,
        names=("AAPL", "SPY", "QQQ"),
        iv_history=iv_hist,
        iv_labels={"SPY": "ok", "AAPL": "low-fidelity"},
        provenance={"panel_sha256": "x"},
    )
    assert res["cutoff"] == "2025-06-27"
    assert res["study"] == "FORECAST-001"
    for h in ("5", "20", "63", "126"):
        for bench in ("rv22", "ewma", "iv2"):
            for loss in ("qlike", "mse"):
                assert "n_rows" in res["cells"][h][bench][loss]
    assert res["cells"]["20"]["iv2"]["qlike"]["n_rows"] > 0  # SPY only (IV-ok)
    assert res["verdict"] in ("PASS", "FAIL")
    assert len(res["fits"]["20"]) == 10  # 2024-09 .. 2025-06 monthly refits
    first = res["fits"]["20"][0]
    assert first["month"] == "2024-09" and first["through"] == "2024-08-30"
    assert set(res["blend"]) == {"5", "20", "63", "126"}
    assert "per_name" in res and "AAPL" in res["per_name"]["20"]
    md = evaluate.render_markdown(res)
    assert "FORECAST-001" in md and "| 20 | rv22 | qlike |" in md
