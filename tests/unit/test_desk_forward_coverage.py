"""Eight-clock coverage evidence from the captured forward bars (C1/C3 lane).

``forward-verify``'s ``ok: true`` asserts only the THREE outcome-table
clocks (10:00/10:15/15:15 ET). RESTART-THRESHOLD C1 requires observation
at the EIGHT decision clocks of ``intraday_action_graph.SCHEDULE``, and
the campaign exit's scope note says the restart scorer must compute that
coverage from the captured full-session bars itself. These tests pin
``forward_coverage`` -- the scorer that exists so a future restart scorer
can cite it.

Every expected number below is hand-derived from the window rule (a bar
in the half-open [C, C+15min) ET covers clock C; a bar at exactly C+15:00
is stale, C+14:59 fresh) and written as a literal. The fixtures build
timestamps with the stdlib only; no expectation is computed by calling the
scorer under test.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.desk import forward_coverage as fc
from tree_options.desk import forward_minutes as fm
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.__main__ import run_cli

ET = ZoneInfo("America/New_York")
DOCS = Path(__file__).resolve().parents[2] / "docs" / "desk"

#: hand-copied from docs/desk/RESTART-THRESHOLD.md C1 (the sealed list)
SEALED_EIGHT = ("10:00", "10:45", "11:30", "12:15", "13:00", "13:45", "14:30", "15:15")
#: the six morning clocks all end before 14:00; the last two do not
MORNING_SIX = ("10:00", "10:45", "11:30", "12:15", "13:00", "13:45")

SESSION_EDT = date(2026, 10, 5)  # Monday, America/New_York still EDT (UTC-4)
SESSION_EST = date(2026, 11, 5)  # Thursday, after the Nov 1 fall-back (UTC-5)
SELECT_ON = "2026-10-03"
NOW = datetime(2026, 10, 5, 17, 30, tzinfo=ET)
SHA = "a" * 64

FULL = "O:IWM260516C00240000"
PARTIAL = "O:QQQ261120C00480000"
BUDGET = "O:SPY261016C00570000"


# ------------------------------------------------------------ fixtures


def _bar_ms(session: date, hour: int, minute: int, second: int = 0) -> int:
    return int(datetime.combine(session, time(hour, minute, second), tzinfo=ET).timestamp() * 1000)


def _bars_at(session: date, moments: list[tuple[int, int] | tuple[int, int, int]]):
    return [
        {"t": _bar_ms(session, *m), "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 5} for m in moments
    ]


def _every_minute(session: date, start: tuple[int, int], end: tuple[int, int]):
    """Bars on every minute of [start, end] ET inclusive -- fixture only."""
    lo = start[0] * 60 + start[1]
    hi = end[0] * 60 + end[1]
    return _bars_at(session, [(k // 60, k % 60) for k in range(lo, hi + 1)])


def _bars_doc(
    session: date,
    contracts: dict[str, dict],
    *,
    schema: str | None = None,
    drop_session: bool = False,
    selected_on: str = SELECT_ON,
    sha: str = SHA,
) -> dict:
    doc = {
        "schema": schema or fm.BARS_SCHEMA,
        "session": session.isoformat(),
        "selected_on": selected_on,
        "selection_source_sha256": sha,
        "contracts": contracts,
    }
    if drop_session:
        del doc["session"]
    return doc


def _ok(bars: list[dict]) -> dict:
    return {"status": "ok", "n_bars": len(bars), "bars": bars}


@pytest.fixture()
def forward_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "desk-forward-minutes"
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "desk-store"))
    monkeypatch.setenv("DESK_FORWARD_DIR", str(root))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DESK_REPO_ROOT", raising=False)
    return root


def _write_selection(
    root: Path, *, tickers: list[str], sha: str = SHA, selected_on: str = SELECT_ON
) -> None:
    target = root / "selection" / f"{selected_on}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": fm.SELECTION_SCHEMA,
                "selected_on": selected_on,
                "source_sha256": sha,
                "contracts": [{"ticker": t} for t in tickers],
                "tickers": tickers,
            }
        )
    )


def _write_bars(root: Path, session: date, contracts: dict[str, dict], **kw) -> None:
    target = root / "bars" / f"{session.isoformat()}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_bars_doc(session, contracts, **kw)))


# ------------------------------------------------------------ the seal


class TestSealPins:
    def test_the_eight_clocks_are_the_sealed_documents_list(self) -> None:
        assert fc.EIGHT_CLOCKS == SEALED_EIGHT

    def test_the_eight_clocks_are_intraday_action_graphs_schedule(self) -> None:
        assert fc.EIGHT_CLOCKS == iag.SCHEDULE

    def test_the_sealed_document_still_names_that_schedule(self) -> None:
        text = (DOCS / "RESTART-THRESHOLD.md").read_text()
        assert "10:00, 10:45, 11:30, 12:15, 13:00, 13:45, 14:30, 15:15" in text, (
            "the sealed restart rule moved off the eight-clock schedule: "
            "forward_coverage must be revisited"
        )

    def test_the_age_window_is_the_latest_horizon(self) -> None:
        assert fc.AGE_WINDOW_SECONDS == 15 * 60


# ------------------------------------------------------------ pure scorer


class TestClockCoverage:
    def test_full_session_bars_cover_all_eight_clocks(self) -> None:
        doc = fc.clock_coverage(
            _bars_doc(SESSION_EDT, {FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0)))})
        )
        assert list(doc["clocks"]) == list(SEALED_EIGHT)
        for clock in SEALED_EIGHT:
            row = doc["clocks"][clock]
            assert row == {"covered": 1, "total": 1, "pct": 100.0, "missing_contracts": []}
        # hand arithmetic: 8 of 8 (session, clock) points, all clocks full
        assert doc["summary"]["scheduled_points"] == 8
        assert doc["summary"]["covered_points"] == 8
        assert doc["summary"]["covered_points_pct"] == 100.0
        assert doc["summary"]["clocks_fully_covered"] == 8
        assert doc["summary"]["min_clock_pct"] == 100.0
        assert doc["summary"]["all_clocks_ge_pct"] == {"50": True, "90": True}
        assert doc["contracts"][FULL]["covered_clocks"] == list(SEALED_EIGHT)
        assert doc["contracts"][FULL]["missing_clocks"] == []
        assert doc["contracts"][FULL]["gap_reason"] == ""
        assert doc["contracts"][FULL]["n_bars"] == 390  # 09:31..16:00 inclusive

    def test_bars_stopping_at_1400_cover_the_morning_six_only(self) -> None:
        doc = fc.clock_coverage(
            _bars_doc(SESSION_EDT, {FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (14, 0)))})
        )
        for clock in MORNING_SIX:
            # [13:45, 14:00) still has bars (13:45..13:59); the 14:00 bar
            # itself is not needed
            assert doc["clocks"][clock]["covered"] == 1
        for clock in ("14:30", "15:15"):
            assert doc["clocks"][clock] == {
                "covered": 0,
                "total": 1,
                "pct": 0.0,
                "missing_contracts": [FULL],
            }
        # hand arithmetic: 6/8 points = 75.0%, min 0.0 at the last two
        assert doc["summary"]["covered_points"] == 6
        assert doc["summary"]["covered_points_pct"] == 75.0
        assert doc["summary"]["clocks_fully_covered"] == 6
        assert doc["summary"]["min_clocks"] == ["14:30", "15:15"]
        assert doc["summary"]["all_clocks_ge_pct"] == {"50": False, "90": False}
        assert doc["contracts"][FULL]["missing_clocks"] == ["14:30", "15:15"]
        assert doc["contracts"][FULL]["gap_reason"] == "no bar in [clock, clock+15min) ET"

    def test_the_age_window_edge_is_half_open(self) -> None:
        # AT: a bar at exactly the clock; FRESH: C+14:59; STALE: exactly C+15:00
        contracts = {
            "O:IWM260516C00238000": _ok(_bars_at(SESSION_EDT, [(10, 0, 0)])),
            "O:IWM260516C00242000": _ok(_bars_at(SESSION_EDT, [(10, 14, 59)])),
            "O:IWM260516C00244000": _ok(_bars_at(SESSION_EDT, [(10, 15, 0)])),
        }
        doc = fc.clock_coverage(_bars_doc(SESSION_EDT, contracts))
        row = doc["clocks"]["10:00"]
        # hand arithmetic: AT and FRESH are inside [10:00, 10:15); STALE is not
        assert row == {
            "covered": 2,
            "total": 3,
            "pct": 66.67,
            "missing_contracts": ["O:IWM260516C00244000"],
        }
        # the stale bar belongs to no window at all (10:15 is not a clock)
        for clock in SEALED_EIGHT[1:]:
            assert doc["clocks"][clock]["covered"] == 0
        assert doc["contracts"]["O:IWM260516C00244000"]["n_bars"] == 1
        assert doc["contracts"]["O:IWM260516C00244000"]["missing_clocks"] == list(SEALED_EIGHT)
        # hand arithmetic: 2 of 24 points
        assert doc["summary"]["covered_points"] == 2
        assert doc["summary"]["scheduled_points"] == 24

    def test_est_and_edt_sessions_anchor_to_the_same_wall_clocks(self) -> None:
        # hand arithmetic: Oct 5 -> Nov 5 is 31 days; the wall-clock 10:00
        # stamps differ by 31 days MINUS one DST hour (EDT UTC-4 vs EST UTC-5)
        delta = _bar_ms(SESSION_EST, 10, 0) - _bar_ms(SESSION_EDT, 10, 0)
        assert delta - 31 * 86_400_000 == 3_600_000
        for session in (SESSION_EDT, SESSION_EST):
            doc = fc.clock_coverage(
                _bars_doc(session, {FULL: _ok(_every_minute(session, (9, 31), (16, 0)))})
            )
            assert doc["summary"]["covered_points"] == 8

    def test_capture_gaps_stay_in_every_denominator_with_their_reason(self) -> None:
        contracts = {
            FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0))),
            BUDGET: {"status": "budget", "bars": []},
            "O:QQQ261120C00482000": {"status": "invalid", "reason": "truncated", "bars": []},
        }
        doc = fc.clock_coverage(_bars_doc(SESSION_EDT, contracts))
        for clock in SEALED_EIGHT:
            # hand arithmetic: only FULL prints; sorted ticker order
            assert doc["clocks"][clock] == {
                "covered": 1,
                "total": 3,
                "pct": 33.33,
                "missing_contracts": ["O:QQQ261120C00482000", BUDGET],
            }
        assert doc["summary"]["covered_points"] == 8
        assert doc["summary"]["scheduled_points"] == 24
        assert doc["contracts"][BUDGET]["gap_reason"] == "capture status: budget"
        assert doc["contracts"][BUDGET]["covered_clocks"] == []
        assert doc["contracts"]["O:QQQ261120C00482000"]["gap_reason"].startswith(
            "capture status: invalid"
        )
        assert doc["summary"]["all_clocks_ge_pct"] == {"50": False, "90": False}

    def test_a_shorter_schedule_scores_only_its_clocks(self) -> None:
        doc = fc.clock_coverage(
            _bars_doc(SESSION_EDT, {FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (14, 0)))}),
            ("10:00", "15:15"),
        )
        assert list(doc["clocks"]) == ["10:00", "15:15"]
        # hand arithmetic: 1 of 2 points = 50.0%; the every-clock fact fails
        # because 15:15 is at 0.0%
        assert doc["clocks"]["15:15"]["covered"] == 0
        assert doc["summary"]["covered_points_pct"] == 50.0
        assert doc["summary"]["all_clocks_ge_pct"] == {"50": False, "90": False}

    def test_unusable_documents_are_refused_with_fixed_text(self) -> None:
        with pytest.raises(ValueError, match="no contracts"):
            fc.clock_coverage(_bars_doc(SESSION_EDT, {}))
        with pytest.raises(ValueError, match="not a forward minute-bars"):
            fc.clock_coverage(_bars_doc(SESSION_EDT, {FULL: _ok([])}, schema="other/1"))
        with pytest.raises(ValueError, match="no parsable session"):
            fc.clock_coverage(
                _bars_doc(
                    SESSION_EDT, {FULL: _ok(_bars_at(SESSION_EDT, [(10, 0)]))}, drop_session=True
                )
            )
        bad_bar = [{"t": "10:00", "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 5}]
        with pytest.raises(ValueError, match="invalid bar timestamp"):
            fc.clock_coverage(_bars_doc(SESSION_EDT, {FULL: _ok(bad_bar)}))

    def test_per_underlying_facts_beside_the_session_table(self) -> None:
        iwm_morning = "O:IWM260516C00242000"
        contracts = {
            FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0))),
            iwm_morning: _ok(_bars_at(SESSION_EDT, [(10, 5)])),
            PARTIAL: _ok(_every_minute(SESSION_EDT, (9, 31), (14, 0))),
            BUDGET: {"status": "budget", "bars": []},
        }
        doc = fc.clock_coverage(_bars_doc(SESSION_EDT, contracts))
        under = doc["underlyings"]
        assert sorted(under) == ["IWM", "QQQ", "SPY"]
        iwm = under["IWM"]
        # hand arithmetic: 2 IWM contracts; 10:00 has both, every other
        # clock only FULL: 2 + 7 x 1 = 9 of 16 points = 56.25%
        assert iwm["contracts"] == 2
        assert iwm["clocks"]["10:00"] == {"covered": 2, "total": 2, "pct": 100.0}
        for clock in SEALED_EIGHT[1:]:
            assert iwm["clocks"][clock] == {"covered": 1, "total": 2, "pct": 50.0}
        assert iwm["covered_points"] == 9
        assert iwm["scheduled_points"] == 16
        assert iwm["covered_points_pct"] == 56.25
        assert iwm["min_clock_pct"] == 50.0
        assert iwm["clocks_with_any_contract_covered"] == 8
        assert iwm["all_clocks_ge_pct"] == {"50": True, "90": False}
        qqq = under["QQQ"]
        # hand arithmetic: one contract printing to 14:00: 6 of 8 points
        assert qqq["contracts"] == 1
        assert qqq["covered_points"] == 6
        assert qqq["covered_points_pct"] == 75.0
        assert qqq["clocks"]["14:30"] == {"covered": 0, "total": 1, "pct": 0.0}
        assert qqq["clocks_with_any_contract_covered"] == 6
        assert qqq["all_clocks_ge_pct"] == {"50": False, "90": False}
        spy = under["SPY"]
        # a budget gap is a member of its underlying's denominator too
        assert spy["contracts"] == 1
        assert spy["covered_points"] == 0
        assert spy["scheduled_points"] == 8
        assert spy["covered_points_pct"] == 0.0
        assert spy["clocks_with_any_contract_covered"] == 0
        # the per-underlying numbers sum to the session's: 9 + 6 + 0 of 32
        assert doc["summary"]["covered_points"] == 15
        assert doc["summary"]["scheduled_points"] == 32

    def test_a_captured_entrys_underlying_field_wins(self) -> None:
        # the capture copies the selection's "underlying" into every entry
        entry = _ok(_bars_at(SESSION_EDT, [(10, 0)])) | {"underlying": "SPY"}
        doc = fc.clock_coverage(_bars_doc(SESSION_EDT, {FULL: entry}))
        assert list(doc["underlyings"]) == ["SPY"]

    def test_bad_schedules_are_refused(self) -> None:
        doc = _bars_doc(SESSION_EDT, {FULL: _ok(_bars_at(SESSION_EDT, [(10, 0)]))})
        for schedule in (
            (),
            ["10:00", "10:00"],
            ["24:00"],
            ["10:60"],
            ["10:00:00"],
            ["ab:cd"],
            ["10"],
        ):
            with pytest.raises(ValueError, match=r"clock|schedule"):
                fc.clock_coverage(doc, schedule)


# ------------------------------------------------------------ session scorer


class TestScoreSession:
    def test_scores_writes_the_doc_and_records_provenance(self, forward_store) -> None:
        _write_selection(forward_store, tickers=[FULL, PARTIAL])
        _write_bars(
            forward_store,
            SESSION_EDT,
            {
                FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0))),
                PARTIAL: _ok(_every_minute(SESSION_EDT, (9, 31), (14, 0))),
            },
        )
        doc = fc.score_session(SESSION_EDT, now=NOW)
        assert doc["schema"] == fc.COVERAGE_SCHEMA
        assert doc["execution_authorized"] is False
        assert doc["selection_check"]["provenance"] == "match"
        # hand arithmetic: 6 clocks at 2/2 + 14:30 and 15:15 at 1/2 = 14/16
        assert doc["summary"]["covered_points"] == 14
        assert doc["summary"]["scheduled_points"] == 16
        assert doc["summary"]["covered_points_pct"] == 87.5
        written = json.loads((forward_store / "coverage" / "2026-10-05.json").read_text())
        assert written["summary"] == doc["summary"]
        assert len(written["bars_sha256"]) == 64

    def test_no_bars_document_is_an_honest_input_error(self, forward_store) -> None:
        _write_selection(forward_store, tickers=[FULL])
        with pytest.raises(fc.CoverageInputError) as exc:
            fc.score_session(SESSION_EDT, now=NOW)
        assert exc.value.exit_code == fc.NO_BARS_EXIT
        assert "forward-minutes" in str(exc.value)

    def test_no_selection_anywhere_is_an_honest_input_error(self, forward_store) -> None:
        _write_bars(
            forward_store, SESSION_EDT, {FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0)))}
        )
        with pytest.raises(fc.CoverageInputError) as exc:
            fc.score_session(SESSION_EDT, now=NOW)
        assert exc.value.exit_code == fc.MISSING_SELECTION_EXIT
        assert "forward-select" in str(exc.value)

    def test_a_roster_mismatch_is_recorded_not_enforced(self, forward_store) -> None:
        _write_selection(forward_store, tickers=[FULL, PARTIAL, BUDGET], sha="b" * 64)
        _write_bars(
            forward_store,
            SESSION_EDT,
            {
                FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0))),
            },
        )
        doc = fc.score_session(SESSION_EDT, now=NOW)
        check = doc["selection_check"]
        assert check["provenance"] == "mismatch"
        assert check["missing_from_bars"] == sorted([PARTIAL, BUDGET])
        assert check["extra_in_bars"] == []
        assert doc["summary"]["contracts"] == 1  # the bars document is the denominator


# ------------------------------------------------------------ cli


class TestCli:
    def test_prints_the_eight_clock_table_and_decides_nothing(
        self, forward_store, static_calendar, capsys
    ) -> None:
        _write_selection(forward_store, tickers=[FULL, PARTIAL])
        _write_bars(
            forward_store,
            SESSION_EDT,
            {
                FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0))),
                PARTIAL: _ok(_every_minute(SESSION_EDT, (9, 31), (14, 0))),
            },
        )
        rc = run_cli(["forward-coverage", "--session", "2026-10-05"], now=NOW, cal=static_calendar)
        assert rc == 0
        out = capsys.readouterr().out
        assert "forward-coverage: 2026-10-05 selection 2026-10-03 provenance=match" in out
        for clock in SEALED_EIGHT:  # all eight clocks are in the printed table
            assert f"\n{clock}  " in out
        # hand arithmetic: the two afternoon clocks are 1/2 with PARTIAL named
        assert "14:30  1/2   50.0%  " + PARTIAL in out
        assert "15:15  1/2   50.0%  " + PARTIAL in out
        assert "10:00  2/2  100.0%" in out
        assert "points: 14/16 covered (87.5%); clocks fully covered 6/8" in out
        assert "fact: every clock >= 50% covered: yes" in out
        assert "fact: every clock >= 90% covered: no" in out
        assert "sealed call" in out

    def test_missing_bars_document_exits_three(
        self, forward_store, static_calendar, capsys
    ) -> None:
        _write_selection(forward_store, tickers=[FULL])
        rc = run_cli(["forward-coverage", "--session", "2026-10-05"], now=NOW, cal=static_calendar)
        assert rc == fc.NO_BARS_EXIT
        assert "no bars document" in capsys.readouterr().err

    def test_missing_selection_exits_four(self, forward_store, static_calendar, capsys) -> None:
        _write_bars(
            forward_store, SESSION_EDT, {FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0)))}
        )
        rc = run_cli(["forward-coverage", "--session", "2026-10-05"], now=NOW, cal=static_calendar)
        assert rc == fc.MISSING_SELECTION_EXIT
        assert "forward-select" in capsys.readouterr().err

    def test_a_document_with_no_contracts_exits_one(
        self, forward_store, static_calendar, capsys
    ) -> None:
        _write_selection(forward_store, tickers=[FULL])
        _write_bars(forward_store, SESSION_EDT, {})
        rc = run_cli(["forward-coverage", "--session", "2026-10-05"], now=NOW, cal=static_calendar)
        assert rc == 1
        assert "no contracts" in capsys.readouterr().err

    def test_the_roster_mismatch_line_is_printed_as_a_fact(
        self, forward_store, static_calendar, capsys
    ) -> None:
        _write_selection(forward_store, tickers=[FULL, BUDGET], sha="b" * 64)
        _write_bars(
            forward_store, SESSION_EDT, {FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0)))}
        )
        rc = run_cli(["forward-coverage", "--session", "2026-10-05"], now=NOW, cal=static_calendar)
        assert rc == 0
        out = capsys.readouterr().out
        assert "provenance=mismatch" in out
        assert "missing_from_bars=1" in out

    def test_prints_the_per_underlying_facts(self, forward_store, static_calendar, capsys) -> None:
        _write_selection(forward_store, tickers=[FULL, PARTIAL])
        _write_bars(
            forward_store,
            SESSION_EDT,
            {
                FULL: _ok(_every_minute(SESSION_EDT, (9, 31), (16, 0))),
                PARTIAL: _ok(_every_minute(SESSION_EDT, (9, 31), (14, 0))),
            },
        )
        rc = run_cli(["forward-coverage", "--session", "2026-10-05"], now=NOW, cal=static_calendar)
        assert rc == 0
        out = capsys.readouterr().out
        # hand arithmetic: IWM 8/8; QQQ 6/8 with 14:30/15:15 at 0.0%
        assert (
            "underlying IWM: points 8/8 covered (100.0%); min clock 100.0%; "
            "clocks with any contract covered 8/8" in out
        )
        assert (
            "underlying QQQ: points 6/8 covered (75.0%); min clock 0.0%; "
            "clocks with any contract covered 6/8" in out
        )


# ------------------------------------------------------------ backfill


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every way the forward lane could reach a vendor raises and is recorded."""
    import socket

    from tree_options.data import massive_client

    calls: list[str] = []

    def refuse(name: str):
        def _refuse(*_a, **_k):
            calls.append(name)
            raise AssertionError(f"backfill touched the network: {name}")

        return _refuse

    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))
    monkeypatch.setattr(massive_client.MassiveClient, "get_json", refuse("MassiveClient.get_json"))
    monkeypatch.setattr(fm, "build_client", refuse("forward_minutes.build_client"))
    monkeypatch.setattr(fm, "client_from_environment", refuse("client_from_environment"))
    return calls


def _bank(root: Path, session: date) -> None:
    _write_bars(
        root,
        session,
        {
            FULL: _ok(_every_minute(session, (9, 31), (16, 0))),
            PARTIAL: _ok(_every_minute(session, (9, 31), (14, 0))),
        },
    )


class TestBackfill:
    SESSIONS = (date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7))

    def _bank_all(self, root: Path) -> None:
        _write_selection(root, tickers=[FULL, PARTIAL])
        for session in self.SESSIONS:
            _bank(root, session)
        # neither a non-date file nor a sibling directory is a banked session
        (root / "bars" / "README.json").write_text("{}")
        (root / "bars" / "2026-10-08.json.tmp").write_text("{}")

    def test_banked_sessions_lists_the_dated_bars_on_or_after_since(self, forward_store) -> None:
        self._bank_all(forward_store)
        assert fc.banked_sessions(date(2026, 10, 5)) == list(self.SESSIONS[1:])
        assert fc.banked_sessions(date(2026, 10, 8)) == []

    def test_since_scores_every_banked_session_without_network(
        self, forward_store, static_calendar, capsys, no_network
    ) -> None:
        self._bank_all(forward_store)
        rc = run_cli(["forward-coverage", "--since", "2026-10-05"], now=NOW, cal=static_calendar)
        assert rc == 0
        assert no_network == []
        written = sorted(p.name for p in (forward_store / "coverage").iterdir())
        assert written == ["2026-10-05.json", "2026-10-06.json", "2026-10-07.json"]
        for name in written:
            doc = json.loads((forward_store / "coverage" / name).read_text())
            # hand arithmetic: 14 of 16 points every session
            assert doc["summary"]["covered_points"] == 14
            assert doc["underlyings"]["QQQ"]["covered_points"] == 6
        # files only: no wire budget ledger, no new bars, no verify verdicts
        assert not (forward_store / "budget").exists()
        assert not (forward_store / "verify").exists()
        assert sorted(p.name for p in (forward_store / "bars").iterdir()) == [
            "2026-10-02.json",
            "2026-10-05.json",
            "2026-10-06.json",
            "2026-10-07.json",
            "2026-10-08.json.tmp",
            "README.json",
        ]
        out = capsys.readouterr().out
        assert "forward-coverage: 2026-10-06 points 14/16 covered (87.5%)" in out
        assert "IWM 8/8 (100.0%)" in out and "QQQ 6/8 (75.0%)" in out
        assert "scored 3/3 banked sessions since 2026-10-05 (no wire)" in out

    def test_backfill_returns_one_row_per_session(self, forward_store, no_network) -> None:
        self._bank_all(forward_store)
        rows = fc.backfill(date(2026, 10, 6), now=NOW)
        assert [(r.session, r.exit_code) for r in rows] == [
            (date(2026, 10, 6), 0),
            (date(2026, 10, 7), 0),
        ]
        assert all(r.doc is not None and r.error == "" for r in rows)
        assert no_network == []

    def test_a_bad_session_is_reported_and_the_rest_still_score(
        self, forward_store, static_calendar, capsys, no_network
    ) -> None:
        self._bank_all(forward_store)
        _write_bars(forward_store, date(2026, 10, 6), {})  # nothing to score
        rc = run_cli(["forward-coverage", "--since", "2026-10-05"], now=NOW, cal=static_calendar)
        assert rc == 1
        assert "2026-10-06" in capsys.readouterr().err
        written = sorted(p.name for p in (forward_store / "coverage").iterdir())
        assert written == ["2026-10-05.json", "2026-10-07.json"]
        assert no_network == []

    def test_nothing_banked_since_exits_three(self, forward_store, static_calendar, capsys) -> None:
        self._bank_all(forward_store)
        rc = run_cli(["forward-coverage", "--since", "2026-10-08"], now=NOW, cal=static_calendar)
        assert rc == fc.NO_BARS_EXIT
        assert "no banked session" in capsys.readouterr().err

    def test_since_and_session_are_exclusive(self, forward_store, static_calendar) -> None:
        rc = run_cli(
            ["forward-coverage", "--since", "2026-10-05", "--session", "2026-10-05"],
            now=NOW,
            cal=static_calendar,
        )
        assert rc == 2
