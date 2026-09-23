"""XSMOM-12-1 study machinery (scripts/xsmom_12_1_study.py).

Hermetic: synthetic panels on the checked-in trex NYSE calendar, temporary
files, no network. Every oracle is recomputed here by plain index arithmetic
over the session list (never by calling the study's walk), per the lane rule.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO_ROOT
from tree_options.time.calendar import StaticSessionCalendar

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import xsmom_12_1_study as study  # type: ignore[import-not-found]  # scripts/

TREX = REPO_ROOT / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
RT = 0.0005


@pytest.fixture(scope="module")
def cal() -> Any:
    raw = StaticSessionCalendar(TREX, TREX.with_suffix(".sha256"))
    return study.ClosureCorrectedCalendar(raw, study.STATIC_CALENDAR_MISSING_CLOSURES)


def _sessions(cal: Any, first: str, last: str) -> list[str]:
    return [s.isoformat() for s in cal.sessions() if first <= s.isoformat() <= last]


def _synthetic_panel(days: list[str], n_names: int = 33) -> dict[str, dict[str, dict[str, str]]]:
    """Deterministic, distinct price paths (no ties in the rankings)."""
    panel: dict[str, dict[str, dict[str, str]]] = {}
    for k in range(n_names):
        price = 100.0 + k
        bars: dict[str, dict[str, str]] = {}
        for j, d in enumerate(days):
            step = ((k * 7919 + j * 104729 + k * j * 31) % 1000 - 497) / 40000
            price *= 1 + step + (k - n_names / 2) * 0.00004
            bars[d] = {"close": f"{price:.4f}"}
        panel[f"N{k:02d}"] = bars
    return panel


def _closes(panel: dict[str, Any], name: str, days: list[str]) -> list[float]:
    return [float(panel[name][d]["close"]) for d in days]


def _oracle_cells(
    panel: dict[str, Any], days: list[str], *, skip: int, hold: int, monthly: bool
) -> dict[tuple[str, str], dict[str, list[tuple[str, float]]]]:
    """Independent walk over a GAP-FREE synthetic panel whose rows are
    exactly `days` (so a session offset is a row offset)."""
    out: dict[tuple[str, str], dict[str, list[tuple[str, float]]]] = {}
    names = sorted(panel)
    closes = {n: _closes(panel, n, days) for n in names}
    for i, d in enumerate(days):
        if monthly and i > 0 and days[i - 1][:7] == d[:7]:
            continue
        if i < 273 or i + hold >= len(days):
            continue
        rows = []
        for n in names:
            c = closes[n]
            rows.append((n, c[i - skip] / c[i - 273] - 1, c[i + hold] / c[i] - 1))
        rows.sort(key=lambda x: -x[1])
        era = "holdout" if d <= "2024-09-03" else "full"
        base = statistics.fmean(r for _n, _s, r in rows)
        for form, pick in (("top3", rows[:3]), ("tercile", rows[: len(rows) // 3])):
            cell = out.setdefault((form, era), {"trades": [], "base": []})
            cell["base"].append((d, base))
            cell["trades"].extend((d, r) for _n, _s, r in pick)
    return out


def _oracle_stats(trades: list[tuple[str, float]]) -> tuple[int, int, float, float, float]:
    by_day: dict[str, list[float]] = {}
    for d, r in trades:
        by_day.setdefault(d, []).append(r - RT)
    daily = [sum(v) / len(v) for v in by_day.values()]
    mean = sum(daily) / len(daily)
    sd = math.sqrt(sum((x - mean) ** 2 for x in daily) / (len(daily) - 1))
    hit = sum(1 for _d, r in trades if r - RT > 0) / len(trades)
    return len(trades), len(daily), hit, mean, mean / (sd / math.sqrt(len(daily)))


# ---- calendar -------------------------------------------------------------


def test_the_corrected_calendar_drops_only_the_carter_closure(cal: Any) -> None:
    raw = StaticSessionCalendar(TREX, TREX.with_suffix(".sha256"))
    assert raw.is_session(date(2025, 1, 9)), "the pinned generator predates the closure"
    assert not cal.is_session(date(2025, 1, 9))
    assert tuple(s for s in raw.sessions() if s != date(2025, 1, 9)) == cal.sessions()
    assert cal.nth_after(date(2025, 1, 8), 1) == date(2025, 1, 10)
    assert cal.ordinal(date(2025, 1, 10)) == cal.ordinal(date(2025, 1, 8)) + 1


# ---- stats (legacy iter003 / xu_xsmom conventions) --------------------------


def test_stats_of_is_day_clustered_net_of_5bp_with_ddof_1() -> None:
    trades = [("d1", 0.01), ("d1", 0.03), ("d2", -0.01)]
    s = study.stats_of(trades)
    # net 0.0095, 0.0295, -0.0105 -> daily 0.0195, -0.0105 -> mean 0.0045
    assert s.n == 3 and s.days == 2
    assert s.hit == pytest.approx(2 / 3)
    assert s.mean == pytest.approx(0.0045)
    assert s.t == pytest.approx(0.0045 / (statistics.stdev([0.0195, -0.0105]) / math.sqrt(2)))
    assert study.stats_of([]).n == 0


# ---- the walk against the independent oracle --------------------------------


@pytest.mark.parametrize(("skip", "hold"), [(21, 20), (0, 20), (21, 60), (0, 60)])
def test_monthly_walk_matches_the_index_oracle(cal: Any, skip: int, hold: int) -> None:
    days = _sessions(cal, "2023-06-01", "2025-03-31")
    panel = _synthetic_panel(days)
    signal_days = study.monthly_signal_days(cal, days[0], days[-1])
    cells = study.run_walk(panel, sorted(panel), cal, signal_days, skip=skip, holds=(hold,))
    oracle = _oracle_cells(panel, days, skip=skip, hold=hold, monthly=True)
    assert set(oracle) == {(f, e) for f, h, e in cells}
    for (form, era), want in oracle.items():
        got = cells[(form, hold, era)]
        assert [d for d, _ in got.base] == [d for d, _ in want["base"]]
        s, o = study.stats_of(got.trades), _oracle_stats(want["trades"])
        assert (s.n, s.days) == o[:2]
        assert s.hit == pytest.approx(o[2])
        assert s.mean == pytest.approx(o[3], rel=1e-9)
        assert s.t == pytest.approx(o[4], rel=1e-9)
        b = study.stats_of(got.base)
        assert b.mean == pytest.approx(_oracle_stats(want["base"])[3], rel=1e-9)


def test_the_era_boundary_is_signal_on_or_before_2024_09_03(cal: Any) -> None:
    days = _sessions(cal, "2023-06-01", "2025-03-31")
    panel = _synthetic_panel(days)
    signal_days = study.monthly_signal_days(cal, days[0], days[-1])
    assert date(2024, 9, 3) in signal_days, "Labor Day makes 09-03 the first September session"
    cells = study.run_walk(panel, sorted(panel), cal, signal_days, skip=21, holds=(20,))
    holdout_days = {d for d, _ in cells[("top3", 20, "holdout")].base}
    full_days = {d for d, _ in cells[("top3", 20, "full")].base}
    assert "2024-09-03" in holdout_days and max(holdout_days) == "2024-09-03"
    assert min(full_days) == "2024-10-01"


def test_12_1_skips_the_latest_month_and_no_skip_does_not(cal: Any) -> None:
    days = _sessions(cal, "2023-06-01", "2025-03-31")
    panel = _synthetic_panel(days)
    t = "2024-11-01"
    i = days.index(t)
    # JUMP: flat at 100 until t-21, then doubled -- a pure latest-month winner.
    panel["JUMP"] = {d: {"close": "100" if j <= i - 21 else "200"} for j, d in enumerate(days)}
    cells_12_1 = study.run_walk(
        panel, sorted(panel), cal, [date.fromisoformat(t)], skip=21, holds=(20,)
    )
    cells_ns = study.run_walk(
        panel, sorted(panel), cal, [date.fromisoformat(t)], skip=0, holds=(20,)
    )
    assert cells_ns[("top3", 20, "full")].picks[t][0][0] == "JUMP"
    assert "JUMP" not in [n for n, _ in cells_12_1[("top3", 20, "full")].picks[t]]


def test_a_missing_session_in_the_lookback_or_hold_window_excludes_the_name(cal: Any) -> None:
    days = _sessions(cal, "2023-06-01", "2025-03-31")
    panel = _synthetic_panel(days, n_names=31)
    t = "2024-11-01"
    i = days.index(t)
    del panel["N30"][days[i + 10]]  # inside (t, t+20]
    del panel["N29"][days[i - 100]]  # inside [t-273, t]
    del panel["N28"][days[i + 40]]  # inside (t+20, t+60]: only h60 excludes it
    cells = study.run_walk(
        panel, sorted(panel), cal, [date.fromisoformat(t)], skip=21, holds=(20, 60)
    )
    # 31 names - N30 (hold gap) - N29 (lookback gap) = 29 < 30 -> no signal at all
    for hold in (20, 60):
        cell = cells.get(("tercile", hold, "full"))
        assert cell is None or cell.base == []
    # with two more clean names, h20 ranks 31 (N28 kept), h60 ranks 30 (N28 out)
    extra = _synthetic_panel(days, n_names=35)
    panel["N31"], panel["N32"] = extra["N31"], extra["N32"]
    cells = study.run_walk(
        panel, sorted(panel), cal, [date.fromisoformat(t)], skip=21, holds=(20, 60)
    )
    assert cells[("tercile", 20, "full")].sizes == [31]
    assert cells[("tercile", 60, "full")].sizes == [30]


def test_run_study_reports_every_cell_with_its_own_metadata(cal: Any) -> None:
    """All 32 keys; each row's first/last signal and size come from ITS cell
    (the oracle walk), never from another loop's leftover."""
    days = _sessions(cal, "2023-06-01", "2025-03-31")
    a = _synthetic_panel(days, n_names=33)
    b = {f"X{n}": bars for n, bars in _synthetic_panel(days, n_names=34).items()}
    universes = [
        study.Universe("orig-36", a, sorted(a), "sha-a", "a.json"),
        study.Universe("XU-62", b, sorted(b), "sha-b", "b.json"),
    ]
    result = study.run_study(universes, cal)
    rows = result["rows"]
    assert len(rows) == 32
    for (cons, u, form, hold, era), row in rows.items():
        panel = a if u == "orig-36" else b
        skip = dict(study.CONSTRUCTIONS)[cons]
        want = _oracle_cells(panel, days, skip=skip, hold=hold, monthly=True).get((form, era))
        if want is None:
            assert row["empty"], (cons, u, form, hold, era)
            continue
        assert row["first"] == want["base"][0][0] and row["last"] == want["base"][-1][0]
        assert row["size_min"] == len(panel)
        assert row["stats"]["n"] == len(want["trades"])
    assert isinstance(result["candidate"], bool) and len(result["decision"]) == 2


# ---- decision rule ----------------------------------------------------------


def _conds(**over: float) -> dict[tuple[str, str, str, int, str], float | None]:
    base: dict[tuple[str, str, str, int, str], float | None] = {}
    for u in study.UNIVERSES:
        base[("12-1", u, "top3", 20, "holdout")] = 0.010
        base[("12-1", u, "top3", 20, "full")] = 0.010
        base[("no-skip", u, "top3", 20, "holdout")] = 0.005
    for key, value in over.items():
        cons, uni, era = key.split("__")
        base[({"t121": "12-1", "ns": "no-skip"}[cons], uni.replace("_", "-"), "top3", 20, era)] = (
            value
        )
    return base


def test_decision_candidate_only_when_every_leg_holds() -> None:
    ok, _ = study.decide(_conds())
    assert ok is True
    for bad in (
        {"t121__orig_36__holdout": -0.001},
        {"t121__XU_62__full": 0.0},
        {"ns__XU_62__holdout": 0.011},  # comparator beats 12-1 in holdout
    ):
        assert study.decide(_conds(**bad))[0] is False, bad
    assert study.decide(_conds(ns__orig_36__holdout=0.010))[0] is True, ">= ties pass"


def test_decision_fails_closed_on_a_missing_cell() -> None:
    conds = _conds()
    conds[("12-1", "XU-62", "top3", 20, "full")] = None
    assert study.decide(conds)[0] is False


# ---- deflated Sharpe: iter005 dsr_block, transcribed here as the oracle ------


def test_dsr_matches_the_iter005_formula() -> None:
    vals = [0.012, -0.004, 0.02, 0.007, -0.011, 0.015, 0.003, 0.009, -0.002, 0.018, 0.001, 0.006]
    trials = [0.1, 0.25, -0.05, 0.3, 0.12, 0.2]
    n = len(vals)
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    sr = m / sd
    g3 = (n / ((n - 1) * (n - 2))) * sum(((v - m) / sd) ** 3 for v in vals)
    g4 = (
        (n * (n + 1) / ((n - 1) * (n - 2) * (n - 3))) * sum(((v - m) / sd) ** 4 for v in vals)
        - 3 * (n - 1) ** 2 / ((n - 2) * (n - 3))
        + 3
    )
    nd = statistics.NormalDist()
    g = 0.5772156649
    sr0 = math.sqrt(statistics.variance(trials)) * (
        (1 - g) * nd.inv_cdf(1 - 1 / 32) + g * nd.inv_cdf(1 - 1 / (32 * math.e))
    )
    want = nd.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr * sr))

    got = study.dsr(vals, trials, 32)

    assert got["sr"] == pytest.approx(sr)
    assert got["skew"] == pytest.approx(g3)
    assert got["kurtosis"] == pytest.approx(g4)
    assert got["sr0"] == pytest.approx(sr0)
    assert got["dsr"] == pytest.approx(want)
    # below the kurtosis estimator's 4-observation floor: not evaluable
    assert math.isnan(study.dsr(vals[:3], trials, 32)["dsr"])


# ---- inputs: the sealed pre-registration and the locked panel ---------------


def test_the_prereg_hash_is_verified_against_the_constant_and_the_sidecar(tmp_path: Path) -> None:
    doc = tmp_path / "P.md"
    doc.write_bytes(b"sealed text\n")
    digest = hashlib.sha256(b"sealed text\n").hexdigest()
    (tmp_path / "P.md.sha256").write_text(f"{digest}  P.md\n2026-09-23T14:51:29-06:00\n")

    assert study.verify_prereg(doc, digest) == digest
    with pytest.raises(study.PreregMismatch):
        study.verify_prereg(doc, "0" * 64)
    (tmp_path / "P.md.sha256").write_text(f"{'1' * 64}  P.md\n")
    with pytest.raises(study.PreregMismatch):
        study.verify_prereg(doc, digest)


def test_the_live_panel_is_read_under_a_shared_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = tmp_path / "ohlc-panel.json"
    panel.write_text('{"SPY": {"2024-01-02": {"close": "470.1"}}}')
    lock = tmp_path / "ohlc-panel.json.lock"
    lock.write_text("")
    calls: list[int] = []
    real = fcntl.flock

    def spy(fd: Any, op: int) -> None:
        calls.append(op)
        real(fd, op)

    monkeypatch.setattr(study.fcntl, "flock", spy)
    data, sha = study.read_panel(panel, lock)
    assert data["SPY"]["2024-01-02"]["close"] == "470.1"
    assert sha == hashlib.sha256(panel.read_bytes()).hexdigest()
    assert calls and calls[0] == fcntl.LOCK_SH


def test_cut_panel_keeps_only_sessions_on_or_before_the_cut() -> None:
    panel = {
        "A": {
            "2026-09-10": {"close": "1"},
            "2026-09-11": {"close": "2"},
            "2026-09-14": {"close": "3"},
        }
    }
    assert study.cut_panel(panel, "2026-09-11") == {
        "A": {"2026-09-10": {"close": "1"}, "2026-09-11": {"close": "2"}}
    }


# ---- validation parsers ----------------------------------------------------


def test_published_rows_parse_both_legacy_row_shapes(tmp_path: Path) -> None:
    md = tmp_path / "X.md"
    md.write_text(
        "| construction | n |\n|---|---|\n"
        "| 252-skip21-top3 h20 holdout | 1425 | 475 | 59.0% | +1.282% | 4.80 | base +1.172% "
        "| cond +0.109% | orig37 +1.392% | **CANDIDATE** |\n"
        "| 252-skip21-tercile h60 full | 5364 | 447 | 59.4% | +7.477% | 11.12 | base +4.737% "
        "| cond +2.740% | **CANDIDATE** |\n"
        "| 60-skip5-top3 h20 full | 1 | 1 | 1% | +1% | 1 | base +1% | cond +1% | - |\n"
    )
    rows = study.published_rows(md, prefix="252-skip21-")
    assert rows == {
        "252-skip21-top3 h20 holdout": [
            "1425", "475", "59.0%", "+1.282%", "4.80", "base +1.172%", "cond +0.109%",
            "**CANDIDATE**",
        ],
        "252-skip21-tercile h60 full": [
            "5364", "447", "59.4%", "+7.477%", "11.12", "base +4.737%", "cond +2.740%",
            "**CANDIDATE**",
        ],
    }  # fmt: skip


def test_row_fields_render_in_the_legacy_format() -> None:
    s = study.Stats(n=1425, days=475, hit=0.59, mean=0.01282, t=4.8)
    b = study.Stats(n=475, days=475, hit=0.5, mean=0.01172, t=1.0)
    assert study.row_fields(s, b) == [
        "1425", "475", "59.0%", "+1.282%", "4.80", "base +1.172%", "cond +0.110%",
        "**CANDIDATE**",
    ]  # fmt: skip


def test_protocol_rows_parse_legs_and_month_pnl(tmp_path: Path) -> None:
    md = tmp_path / "P.md"
    md.write_text(
        "| entry month | legs | month P&L | cum P&L |\n|---|---|---|---|\n"
        "| 2022-11 | XOM: -14, XLE: +6, LLY: +130 | +122 | +122 |\n"
        "| 2022-12 | XOM: -13, XLE: -94, LLY: -32 | -138 | -16 |\n"
        "\nmonths traded: 2 | monthly win rate: 50.0% | cumulative: -16 USD\n"
    )
    assert study.protocol_rows(md) == {
        "2022-11": ("XOM: -14, XLE: +6, LLY: +130", "+122"),
        "2022-12": ("XOM: -13, XLE: -94, LLY: -32", "-138"),
    }
