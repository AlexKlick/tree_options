"""Desk D5 signals: XSMOM-TOP3 monthly and PEAD beats, golden against the
research program's protocol rows.

The fixture (tests/fixtures/desk_golden_panel.json.gz) is a closes-only
slice of artifacts/paper-trades/ohlc-panel.json (2025-02-25..2026-06-01,
all 37 names) plus the full earnings-calendar.json, with the source
sha256s recorded inside it. Every oracle below is recomputed IN THE TEST
from those raw closes (never by calling the implementation), and pinned
to the literal rows of PROTOCOL-XSMOM.md / PROTOCOL-PEAD.md.

Ranking convention (found while building these goldens, 2026-09-23): the
prose rule says close(t-21)/close(t-273)-1, but iter004.py (which wrote
PROTOCOL-XSMOM.md) and every XSMOM research script compute
closes[p]/closes[p-skip-lookback]-1, i.e. close(t)/close(t-273)-1 with no
skip. The protocol rows reproduce only under the latter (46/46 months;
the skip-21 reading differs in 19/46). ``top3`` follows the evidence;
``top3_skip21`` reports the prose reading beside it.
"""

from __future__ import annotations

import ast
import gzip
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import signals

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "desk_golden_panel.json.gz"
DESK_SRC = Path(__file__).resolve().parents[2] / "src" / "tree_options" / "desk"


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    doc = json.loads(gzip.decompress(FIXTURE.read_bytes()))
    panel = {n: {d: {"close": c} for d, c in s.items()} for n, s in doc["closes"].items()}
    return {"panel": panel, "calendar": doc["calendar"], "closes": doc["closes"]}


def _oracle_rank(closes: dict[str, dict[str, str]], session: str, skip: int) -> list[str]:
    """Independent re-derivation: rank the 36 non-SPY names by
    close[p-skip]/close[p-273]-1 over each name's own session index."""
    scored = []
    for name in sorted(closes):
        if name == "SPY":
            continue
        ds = sorted(closes[name])
        p = ds.index(session)
        assert p >= 273, "fixture too short for the lookback"
        c = [Decimal(closes[name][d]) for d in ds]
        scored.append((name, c[p - skip] / c[p - 273] - 1))
    scored.sort(key=lambda x: -x[1])
    return [n for n, _ in scored]


# PROTOCOL-XSMOM.md rows (entry month -> legs in rank order)
PROTOCOL_XSMOM = {
    "2026-04-01": ["AMD", "INTC", "GOOGL"],
    "2026-05-01": ["INTC", "AMD", "AVGO"],
    "2026-06-01": ["INTC", "AMD", "TQQQ"],
}


class TestXsmomGolden:
    @pytest.mark.parametrize("session", sorted(PROTOCOL_XSMOM))
    def test_protocol_rows_reproduce(self, golden, session: str, static_calendar) -> None:
        oracle = _oracle_rank(golden["closes"], session, skip=0)
        assert oracle[:3] == PROTOCOL_XSMOM[session]  # the oracle IS the protocol
        res = signals.xsmom_top3(golden["panel"], date.fromisoformat(session), static_calendar)
        assert res.is_rebalance_day and res.fires
        assert list(res.top3) == PROTOCOL_XSMOM[session]
        assert res.n_ranked == 36 and not res.excluded
        assert [n for n, _ in res.ranked] == oracle

    @pytest.mark.parametrize("session", sorted(PROTOCOL_XSMOM))
    def test_scores_match_raw_closes(self, golden, session: str, static_calendar) -> None:
        res = signals.xsmom_top3(golden["panel"], date.fromisoformat(session), static_calendar)
        closes = golden["closes"]
        for name, score in res.scores.items():
            ds = sorted(closes[name])
            p = ds.index(session)
            want = Decimal(closes[name][ds[p]]) / Decimal(closes[name][ds[p - 273]]) - 1
            assert score == want

    def test_prose_skip21_reading_reported_beside(self, golden, static_calendar) -> None:
        for session in PROTOCOL_XSMOM:
            res = signals.xsmom_top3(golden["panel"], date.fromisoformat(session), static_calendar)
            assert list(res.top3_skip21) == _oracle_rank(golden["closes"], session, skip=21)[:3]
        # the two readings disagree on these protocol months (documented above)
        may = signals.xsmom_top3(golden["panel"], date(2026, 5, 1), static_calendar)
        jun = signals.xsmom_top3(golden["panel"], date(2026, 6, 1), static_calendar)
        assert list(may.top3_skip21) == ["INTC", "AMD", "GOOGL"]
        assert list(jun.top3_skip21) == ["INTC", "AMD", "SOXX"]
        assert not may.conventions_agree and not jun.conventions_agree

    def test_mid_month_is_not_a_rebalance_day(self, golden, static_calendar) -> None:
        res = signals.xsmom_top3(golden["panel"], date(2026, 5, 4), static_calendar)
        assert not res.is_rebalance_day and not res.fires
        assert len(res.top3) == 3  # still ranked, for display


# PROTOCOL-PEAD.md rows in the fixture window: (entry, name) -> move, 1dp %
PROTOCOL_PEAD = {
    ("2026-04-24", "INTC"): "23.6",
    ("2026-04-30", "GOOGL"): "10.0",
    ("2026-04-30", "META"): "-8.6",
    ("2026-04-30", "MSFT"): "-3.9",
    ("2026-04-30", "QCOM"): "15.1",
    ("2026-05-01", "AAPL"): "3.2",
    ("2026-05-01", "LLY"): "3.1",
    ("2026-05-06", "AMD"): "18.6",
    ("2026-05-20", "HD"): "2.7",
    ("2026-05-21", "NVDA"): "-1.8",
}


def _oracle_move(closes: dict[str, dict[str, str]], name: str, session: str) -> Decimal:
    ds = sorted(closes[name])
    i = ds.index(session)
    return Decimal(closes[name][ds[i]]) / Decimal(closes[name][ds[i - 1]]) - 1


class TestPeadGolden:
    @pytest.mark.parametrize(
        ("session", "beats", "evaluated"),
        [
            ("2026-04-24", ["INTC"], ["INTC"]),
            ("2026-04-30", ["GOOGL", "QCOM"], ["AMZN", "GOOGL", "META", "MSFT", "QCOM"]),
            ("2026-05-01", ["AAPL", "LLY"], ["AAPL", "LLY", "MA"]),
            ("2026-05-21", [], ["NVDA"]),
            ("2026-04-27", [], ["PG"]),  # Friday report -> Monday session, +0.1%
        ],
    )
    def test_session_events(self, golden, session, beats, evaluated, static_calendar) -> None:
        res = signals.pead_beats(
            golden["panel"], golden["calendar"], date.fromisoformat(session), static_calendar
        )
        assert sorted(e.name for e in res.evaluated) == evaluated
        assert [e.name for e in res.beats] == beats
        for e in res.evaluated:
            want = _oracle_move(golden["closes"], e.name, session)
            assert e.move == want
            assert e.fires == (want >= Decimal("0.015"))
            key = (session, e.name)
            if key in PROTOCOL_PEAD:  # |move| >= 1.5%: a protocol row
                assert f"{want * 100:.1f}" == PROTOCOL_PEAD[key]
            else:
                assert abs(want) < Decimal("0.015")

    def test_every_fixture_protocol_row_is_evaluated(self, golden, static_calendar) -> None:
        for (session, name), pct in PROTOCOL_PEAD.items():
            res = signals.pead_beats(
                golden["panel"], golden["calendar"], date.fromisoformat(session), static_calendar
            )
            (event,) = [e for e in res.evaluated if e.name == name]
            assert f"{event.move * 100:.1f}" == pct
            assert event.fires == (Decimal(pct) > 0)


# ---------------------------------------------------------- synthetic edges


def _synthetic(static_calendar, n_sessions: int = 300, end: date = date(2026, 6, 1)):
    """36 tradables + SPY with distinct constant growth (rank = name order)."""
    cal = static_calendar
    i = cal.ordinal(end)
    days = [d.isoformat() for d in cal.sessions()[i - n_sessions + 1 : i + 1]]
    names = [f"N{k:02d}" for k in range(36)] + ["SPY"]
    panel: dict[str, dict[str, dict[str, str]]] = {}
    for k, name in enumerate(names):
        g = Decimal(1) + Decimal(k) / Decimal(100000)
        px = Decimal(100)
        series = {}
        for d in days:
            series[d] = {"close": str(px)}
            px = (px * g).quantize(Decimal("0.000001"))
        panel[name] = series
    return panel, days, names


class TestSyntheticEdges:
    def test_gaps_and_short_history_are_excluded(self, static_calendar) -> None:
        panel, days, _ = _synthetic(static_calendar)
        # N35 (the would-be leader): a long hole (8 sessions)
        for d in days[100:108]:
            del panel["N35"][d]
        # N34: a short gap (5 sessions, 7 calendar days) now also excludes it
        # (fixed NYSE offsets; the old 10-day guard let it rank on a
        # silently lengthened window)
        for d in days[150:155]:
            del panel["N34"][d]
        # N33 has too little history
        for d in days[:40]:
            del panel["N33"][d]
        res = signals.xsmom_top3(
            panel, date(2026, 6, 1), static_calendar, names=[n for n in panel if n != "SPY"]
        )
        assert res.excluded == {"N35": "gap", "N34": "gap", "N33": "insufficient_history"}
        assert list(res.top3) == ["N32", "N31", "N30"]
        assert res.data_gaps == ("N34", "N35")

    def test_one_missing_session_in_the_window_excludes_the_name(self, static_calendar) -> None:
        """Codex P2: before, one dropped bar made close[p-273] reach 274
        exchange sessions back and the name still ranked."""
        panel, days, _ = _synthetic(static_calendar)
        del panel["N35"][days[200]]
        res = signals.xsmom_top3(
            panel, date(2026, 6, 1), static_calendar, names=[n for n in panel if n != "SPY"]
        )
        assert res.excluded == {"N35": "gap"}
        assert list(res.top3) == ["N34", "N33", "N32"]

    def test_offsets_are_nyse_sessions_and_old_holes_do_not_matter(self, static_calendar) -> None:
        panel, days, _ = _synthetic(static_calendar)
        i = static_calendar.ordinal(date(2026, 6, 1))
        start = static_calendar.sessions()[i - 273].isoformat()
        skip = static_calendar.sessions()[i - 21].isoformat()
        del panel["N35"][days[5]]  # older than the 273-session window: irrelevant
        res = signals.xsmom_top3(
            panel, date(2026, 6, 1), static_calendar, names=[n for n in panel if n != "SPY"]
        )
        assert not res.excluded and res.top3[0] == "N35"
        bars = panel["N35"]
        want = Decimal(bars["2026-06-01"]["close"]) / Decimal(bars[start]["close"]) - 1
        assert res.scores["N35"] == want
        want21 = Decimal(bars[skip]["close"]) / Decimal(bars[start]["close"]) - 1
        assert res.scores_skip21["N35"] == want21

    def test_too_few_ranked_never_fires(self, static_calendar) -> None:
        panel, _days, names = _synthetic(static_calendar)
        for name in names[:7]:
            del panel[name]
        res = signals.xsmom_top3(
            panel, date(2026, 6, 1), static_calendar, names=[n for n in names if n != "SPY"]
        )
        assert res.is_rebalance_day and res.n_ranked == 29 and not res.fires

    def test_missing_session_bar_is_excluded(self, static_calendar) -> None:
        panel, _days, names = _synthetic(static_calendar)
        del panel["N01"]["2026-06-01"]
        res = signals.xsmom_top3(
            panel, date(2026, 6, 1), static_calendar, names=[n for n in names if n != "SPY"]
        )
        assert res.excluded == {"N01": "no_session_bar"}

    @pytest.mark.parametrize(("close", "fires"), [("101.5", True), ("101.49", False)])
    def test_pead_threshold_is_inclusive(self, static_calendar, close: str, fires: bool) -> None:
        panel, days, _ = _synthetic(static_calendar)
        panel["N00"][days[-2]] = {"close": "100"}
        panel["N00"][days[-1]] = {"close": close}
        report = days[-2]  # after-close report -> first post-report session = the last day
        res = signals.pead_beats(
            panel, {"N00": [report]}, date.fromisoformat(days[-1]), static_calendar
        )
        (event,) = res.evaluated
        assert event.fires is fires and event.report_date == report
        assert [e.name for e in res.beats] == (["N00"] if fires else [])

    def test_pead_skips_names_without_bars(self, static_calendar) -> None:
        panel, days, _ = _synthetic(static_calendar)
        res = signals.pead_beats(
            panel, {"ZZZZ": [days[-2]], "SPY": []}, date.fromisoformat(days[-1]), static_calendar
        )
        assert res.evaluated == [] and res.beats == []
        # a name in the panel but without the session bar is evaluated, never fired
        del panel["N00"][days[-1]]
        res = signals.pead_beats(
            panel, {"N00": [days[-2]]}, date.fromisoformat(days[-1]), static_calendar
        )
        (event,) = res.evaluated
        assert not event.fires and event.reason == "no_session_bar" and event.move is None

    def test_pead_never_fires_across_a_missing_prior_bar(self, static_calendar) -> None:
        panel, days, _ = _synthetic(static_calendar)
        panel["N00"][days[-3]] = {"close": "100"}
        del panel["N00"][days[-2]]  # the report-day bar is missing
        panel["N00"][days[-1]] = {"close": "110"}
        res = signals.pead_beats(
            panel, {"N00": [days[-2]]}, date.fromisoformat(days[-1]), static_calendar
        )
        (event,) = res.evaluated
        assert not event.fires and event.reason == "prior_gap" and res.beats == []


# ------------------------------------------------------ banned-by-construction


class TestBannedByConstruction:
    def test_allowed_banned_context_are_disjoint(self) -> None:
        assert signals.ALLOWED_DIRECTION == frozenset({"xsmom_top3", "pead_beat"})
        assert not signals.ALLOWED_DIRECTION & signals.BANNED
        assert not signals.ALLOWED_DIRECTION & signals.CONTEXT_ONLY
        assert not signals.BANNED & signals.CONTEXT_ONLY
        # refuted families named in RESEARCH-LEDGER.md / CRON-paper-engine.md
        for name in (
            "volspike",
            "mom_top_tercile",
            "pead_miss",
            "short_any",
            "sector_rotation",
            "xsmom_60skip5",
            "ts_momentum",
            "squeeze",
            "short_vol_gated",
        ):
            assert name in signals.BANNED

    def test_require_direction_signal(self) -> None:
        signals.require_direction_signal("xsmom_top3")
        signals.require_direction_signal("pead_beat")
        with pytest.raises(signals.BannedSignalError):
            signals.require_direction_signal("volspike")
        with pytest.raises(signals.BannedSignalError):
            signals.require_direction_signal("trend")  # context only
        with pytest.raises(signals.BannedSignalError):
            signals.require_direction_signal("made_up")

    def test_desk_never_imports_research_rule_code(self) -> None:
        """desk/ imports only the stdlib and tree_options (the paper-trades
        scripts run as subprocesses, never as imports)."""
        offenders = []
        for py in sorted(DESK_SRC.rglob("*.py")):
            for node in ast.walk(ast.parse(py.read_text())):
                mods: list[str] = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                for mod in mods:
                    top = mod.split(".")[0]
                    if top != "tree_options" and top not in sys.stdlib_module_names:
                        offenders.append(f"{py.name}: {mod}")
        assert DESK_SRC.is_dir() and not offenders, offenders
