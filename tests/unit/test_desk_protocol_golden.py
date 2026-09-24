"""Desk D5 direction signals, golden against EVERY protocol row: all 46
months of PROTOCOL-XSMOM.md and all 144 cards of PROTOCOL-PEAD.md.

The fixture (tests/fixtures/desk_protocol_golden.json.gz) is a closes-only
slice of artifacts/paper-trades/ohlc-panel.json (2021-09-24..2026-08-06,
all 37 names, the split-adjusted panel), the full sealed
earnings-calendar.json and the protocol rows copied from the two protocol
documents, with the sha256 of every source recorded inside it. The
research files are untracked artifacts, so the copy is the test's input.

Every oracle below is recomputed IN THE TEST from the raw closes (never by
calling the implementation) and must equal the protocol rows first; the
implementation must then equal both. Sessions come from the trex NYSE
calendar (the protocol calendar lists 2025-01-09 as a session; the NYSE
was closed that day and no panel name has a bar, RESEARCH-LEDGER.md).
XSMOM follows the operator's 2026-09-23 ruling: the TESTED no-skip
construction close(t)/close(t-273)-1.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import signals
from tree_options.time.calendar import StaticSessionCalendar

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "desk_protocol_golden.json.gz"
TREX_CAL = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    doc = json.loads(gzip.decompress(FIXTURE.read_bytes()))
    doc["panel"] = {n: {d: {"close": c} for d, c in s.items()} for n, s in doc["closes"].items()}
    return doc


@pytest.fixture(scope="module")
def trex_cal() -> StaticSessionCalendar:
    return StaticSessionCalendar(TREX_CAL, TREX_CAL.with_suffix(".sha256"))


def _first_session(cal: StaticSessionCalendar, month: str) -> date:
    y, m = (int(x) for x in month.split("-"))
    return next(s for s in cal.sessions() if (s.year, s.month) == (y, m))


def _oracle_top3(closes: dict[str, dict[str, str]], sessions: list[str], i: int) -> list[str]:
    """Rank the 36 non-SPY names by close[t]/close[t-273]-1 over fixed
    calendar sessions; a name missing any session of the window sits out;
    ties keep name order."""
    window = sessions[i - 273 : i + 1]
    scored = []
    for name in sorted(closes):
        if name == "SPY" or any(d not in closes[name] for d in window):
            continue
        c = closes[name]
        scored.append((name, Decimal(c[window[-1]]) / Decimal(c[window[0]]) - 1))
    assert len(scored) >= 30
    scored.sort(key=lambda x: -x[1])
    return [n for n, _ in scored[:3]]


class TestXsmom46Months:
    def test_fixture_holds_46_months(self, golden) -> None:
        months = [m for m, _ in golden["protocol_xsmom"]]
        assert len(months) == 46 and months[0] == "2022-11" and months[-1] == "2026-08"
        assert len(set(months)) == 46

    def test_every_protocol_month_reproduces(self, golden, trex_cal) -> None:
        sessions = [s.isoformat() for s in trex_cal.sessions()]
        mismatches = []
        for month, legs in golden["protocol_xsmom"]:
            first = _first_session(trex_cal, month)
            oracle = _oracle_top3(golden["closes"], sessions, sessions.index(first.isoformat()))
            assert oracle == legs, f"{month}: the oracle disagrees with the protocol row"
            res = signals.xsmom_top3(golden["panel"], first, trex_cal)
            if not (res.is_rebalance_day and res.fires and list(res.top3) == legs):
                mismatches.append((month, legs, res.top3))
        assert not mismatches, mismatches

    def test_leveraged_picks_are_logged_never_substituted(self, golden, trex_cal) -> None:
        """PROTOCOL-XSMOM 2023-01 carried SQQQ as a top-3 leg (it cost -831);
        the desk keeps it in top3 and logs it as having no options
        expression: the 4th-ranked name is never promoted."""
        res = signals.xsmom_top3(golden["panel"], _first_session(trex_cal, "2023-01"), trex_cal)
        assert list(res.top3) == ["XOM", "SQQQ", "XLE"]
        assert res.no_options_expression == ("SQQQ",)


class TestPead144Cards:
    def test_fixture_holds_144_cards(self, golden) -> None:
        rows = golden["protocol_pead"]
        assert len(rows) == 144
        assert sum(1 for _s, _n, pct in rows if pct.startswith("+")) == 63  # beat-proxy n=63

    def test_every_card_and_nothing_else(self, golden, trex_cal) -> None:
        """Across the protocol era every evaluated report with |move| >= 1.5%
        is a protocol card with the same 1dp move, and vice versa; only the
        positive ones fire (beats; PEAD-SIGN.md)."""
        want = {(s, n): pct for s, n, pct in golden["protocol_pead"]}
        lo, hi = min(s for s, _ in want), max(s for s, _ in want)
        closes = golden["closes"]
        got: dict[tuple[str, str], str] = {}
        fired: set[tuple[str, str]] = set()
        for s in trex_cal.sessions():
            if not lo <= s.isoformat() <= hi:
                continue
            res = signals.pead_beats(golden["panel"], golden["calendar"], s, trex_cal)
            for e in res.evaluated:
                if e.move is None:
                    continue
                ds = sorted(closes[e.name])
                k = ds.index(s.isoformat())
                oracle = Decimal(closes[e.name][ds[k]]) / Decimal(closes[e.name][ds[k - 1]]) - 1
                assert e.move == oracle
                assert e.fires == (oracle >= Decimal("0.015"))
                if abs(oracle) >= Decimal("0.015"):
                    got[(s.isoformat(), e.name)] = f"{oracle * 100:+.1f}"
                if e.fires:
                    fired.add((s.isoformat(), e.name))
        assert got == want
        assert fired == {k for k, pct in want.items() if pct.startswith("+")}
