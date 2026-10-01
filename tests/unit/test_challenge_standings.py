"""The cross-digest standings + the registered rule's clause evaluation.

Oracle discipline: every expectation is hand arithmetic over fixture run
summaries; nothing imports the accumulator to compute its own answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tree_options.desk import challenge

D_POST1 = "20261002T010000Z"
D_POST2 = "20261003T010000Z"
D_SEAL = "20261001T162152Z"  # the registration sample's last digest


def _run_summary(policy: str, *, pnl: int, minimum: int, boards: int, by_session: dict[str, float], calls: int = 10, fails: int = 0) -> dict:
    return {
        "schema": "desk-lab-run/1",
        "policy": policy,
        "status": "ok",
        "boards_shown": boards,
        "model_calls": calls,
        "model_failures": fails,
        "summary": {
            "entered": boards,
            "closed_capital_proxy": str(5000 + pnl),
            "minimum_closed_capital_proxy": str(minimum),
            "by_session": [
                {"session": s, "closed_pnl": str(v)} for s, v in by_session.items()
            ],
        },
    }


def _digest(store: Path, digest_id: str, *, status: str = "ok", runs: dict[str, dict] | None = None) -> None:
    d = store / "evaluations" / "challenge" / digest_id / "runs"
    d.mkdir(parents=True, exist_ok=True)
    (store / "evaluations" / "challenge" / digest_id / "digest.json").write_text(
        json.dumps({"schema": "desk-challenge/1", "status": status, "at": digest_id})
    )
    for policy, summary in (runs or {}).items():
        (d / policy).mkdir(parents=True, exist_ok=True)
        (d / policy / "summary.json").write_text(json.dumps(summary))


@pytest.fixture
def store(tmp_path: Path) -> Path:
    s = tmp_path / "store"
    # the seal-time registration sample: NEVER counts
    _digest(
        s,
        D_SEAL,
        runs={
            "gepa:winner": _run_summary("gepa:winner", pnl=999, minimum=5000, boards=99, by_session={"2026-09-01": 999.0}),
        },
    )
    # a skipped game never counts either
    _digest(s, "20261002T020000Z", status="quota_dry")
    # two counted games: winner beats no_trade every session; first_row ties it
    _digest(
        s,
        D_POST1,
        runs={
            "gepa:winner": _run_summary("gepa:winner", pnl=12, minimum=4600, boards=50, by_session={"2026-10-02": 10.0, "2026-10-01": -4.0}),
            "no_trade": _run_summary("no_trade", pnl=2, minimum=5000, boards=50, by_session={"2026-10-02": 1.0, "2026-10-01": 1.0}),
            "first_row": _run_summary("first_row", pnl=2, minimum=5000, boards=50, by_session={"2026-10-02": 1.0, "2026-10-01": 1.0}),
        },
    )
    _digest(
        s,
        D_POST2,
        runs={
            "gepa:winner": _run_summary("gepa:winner", pnl=5, minimum=4700, boards=49, by_session={"2026-10-04": 6.0}, calls=20, fails=1),
            "no_trade": _run_summary("no_trade", pnl=1, minimum=5000, boards=49, by_session={"2026-10-04": 1.0}),
            "first_row": _run_summary("first_row", pnl=1, minimum=5000, boards=49, by_session={"2026-10-04": 1.0}),
        },
    )
    return s


class TestAccumulate:
    def test_post_seal_games_only_and_hand_arithmetic(self, store: Path) -> None:
        st = challenge.accumulate_standings(store)
        assert st["games_counted"] == 2
        rows = {r["policy"]: r for r in st["policies"]}
        w = rows["gepa:winner"]
        assert w["games"] == 2
        assert w["boards"] == 50 + 49  # the seal-time 99 and the dry game never count
        assert w["closed_pnl_sum"] == "17"  # 12 + 5
        assert w["worst_minimum_capital"] == "4600"
        assert w["sessions_distinct"] == 3
        assert w["session_pnl"] == {
            f"{D_POST1}:2026-10-01": -4.0,
            f"{D_POST1}:2026-10-02": 10.0,
            f"{D_POST2}:2026-10-04": 6.0,
        }
        assert rows["no_trade"]["closed_pnl_sum"] == "3"
        assert st["registration_sample_through"] == challenge.REGISTRATION_SAMPLE_THROUGH


class TestRuleCheck:
    def test_clauses_against_hand_numbers(self, store: Path) -> None:
        st = challenge.accumulate_standings(store)
        checks = challenge.rule_check(st)
        assert len(checks) == 1  # controls are bars, not candidates
        c = checks[0]
        assert c["policy"] == "gepa:winner"
        clauses = {cl["clause"]: cl for cl in c["clauses"]}
        assert clauses[1]["pass"] is False  # 99 boards < 500, 3 sessions < 20
        assert clauses[2]["pass"] is True  # 17 > 3
        assert clauses[3]["detail"] != "no shared sessions"
        # diffs vs no_trade: (10-1), (-4-1), (6-1) = 9 - 5 + 5 = 9.0
        assert c["paired_vs_no_trade"]["diff_total"] == 9.0
        assert clauses[4]["pass"] is True  # first_row ties no_trade: CI at/below 0
        assert clauses[5]["pass"] is True  # 4600 >= 4500
        assert clauses[6]["pass"] is True  # 1 failure / 30 calls = 3.3% < 5%
        assert c["all_pass"] is False  # the sample clause is honestly short


class TestCli:
    def test_standings_writes_and_rule_check_prints(
        self, store: Path, tmp_path: Path, monkeypatch, capsys, static_calendar
    ) -> None:
        from tree_options.desk.__main__ import run_cli
        from datetime import datetime
        from tree_options.trex.clock import ET

        monkeypatch.setenv("DESK_STORE", str(store))
        rc = run_cli(
            ["challenge", "standings"],
            transport=None,
            sleep=lambda _s: None,
            now=datetime(2026, 10, 3, 12, 0, tzinfo=ET),
            cal=static_calendar,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "2 post-seal game(s)" in out
        written = json.loads((store / "evaluations" / "challenge" / "standings.json").read_text())
        assert written["games_counted"] == 2
        rc2 = run_cli(
            ["challenge", "rule-check"],
            transport=None,
            sleep=lambda _s: None,
            now=datetime(2026, 10, 3, 12, 0, tzinfo=ET),
            cal=static_calendar,
        )
        out2 = capsys.readouterr().out
        assert rc2 == 0
        assert "gepa:winner: not promotable yet" in out2
        assert "[x] 2. closed pnl above no_trade" in out2
        assert "[ ] 1. sample" in out2
