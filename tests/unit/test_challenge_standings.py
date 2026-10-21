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


def _run_summary(
    policy: str,
    *,
    pnl: int,
    minimum: int,
    boards: int,
    by_session: dict[str, float],
    calls: int = 10,
    fails: int = 0,
) -> dict:
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
            "by_session": [{"session": s, "closed_pnl": str(v)} for s, v in by_session.items()],
        },
    }


def _digest(
    store: Path, digest_id: str, *, status: str = "ok", runs: dict[str, dict] | None = None
) -> None:
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
            "gepa:winner": _run_summary(
                "gepa:winner", pnl=999, minimum=5000, boards=99, by_session={"2026-09-01": 999.0}
            ),
        },
    )
    # a skipped game never counts either
    _digest(s, "20261002T020000Z", status="quota_dry")
    # two counted games: winner beats no_trade every session; first_row ties it
    _digest(
        s,
        D_POST1,
        runs={
            "gepa:winner": _run_summary(
                "gepa:winner",
                pnl=12,
                minimum=4600,
                boards=50,
                by_session={"2026-10-02": 10.0, "2026-10-01": -4.0},
            ),
            "no_trade": _run_summary(
                "no_trade",
                pnl=2,
                minimum=5000,
                boards=50,
                by_session={"2026-10-02": 1.0, "2026-10-01": 1.0},
            ),
            "first_row": _run_summary(
                "first_row",
                pnl=2,
                minimum=5000,
                boards=50,
                by_session={"2026-10-02": 1.0, "2026-10-01": 1.0},
            ),
        },
    )
    _digest(
        s,
        D_POST2,
        runs={
            "gepa:winner": _run_summary(
                "gepa:winner",
                pnl=5,
                minimum=4700,
                boards=49,
                by_session={"2026-10-04": 6.0},
                calls=20,
                fails=1,
            ),
            "no_trade": _run_summary(
                "no_trade", pnl=1, minimum=5000, boards=49, by_session={"2026-10-04": 1.0}
            ),
            "first_row": _run_summary(
                "first_row", pnl=1, minimum=5000, boards=49, by_session={"2026-10-04": 1.0}
            ),
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

    def test_overlapping_bundles_sum_per_session_and_match_the_total(self, tmp_path: Path) -> None:
        # one game, two bundles (two runs of the same policy) that both hold
        # 2026-10-06: the session's paired value is the SUM of both rows, so
        # the series adds up to closed_pnl_sum (an overwrite kept 2.0 only)
        s = tmp_path / "store"
        _digest(
            s,
            D_POST1,
            runs={
                "w-bundle-a": _run_summary(
                    "gepa:w",
                    pnl=7,
                    minimum=5000,
                    boards=10,
                    by_session={"2026-10-05": 3.0, "2026-10-06": 4.0},
                ),
                "w-bundle-b": _run_summary(
                    "gepa:w",
                    pnl=5,
                    minimum=5000,
                    boards=10,
                    by_session={"2026-10-06": 2.0, "2026-10-07": 3.0},
                ),
            },
        )
        row = challenge.accumulate_standings(s)["policies"][0]
        assert row["session_pnl"] == {
            f"{D_POST1}:2026-10-05": 3.0,
            f"{D_POST1}:2026-10-06": 6.0,  # 4 + 2
            f"{D_POST1}:2026-10-07": 3.0,
        }
        assert row["closed_pnl_sum"] == "12"  # 7 + 5
        assert sum(row["session_pnl"].values()) == 12.0


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

    @staticmethod
    def _replayed(tmp_path: Path, games: int) -> dict:
        """``games`` nightly digests replaying the SAME 20 frozen sessions:
        the arm makes +3 / -2 alternately (mean +0.5 per session), no_trade 0."""
        sessions = [f"2026-08-{d:02d}" for d in range(1, 21)]
        arm = {s: (3.0 if i % 2 == 0 else -2.0) for i, s in enumerate(sessions)}
        s = tmp_path / f"store-{games}"
        for night in range(games):
            _digest(
                s,
                f"202610{night + 2:02d}T010000Z",
                runs={
                    "gepa:arm": _run_summary(
                        "gepa:arm", pnl=10, minimum=5000, boards=600, by_session=arm
                    ),
                    "no_trade": _run_summary(
                        "no_trade",
                        pnl=0,
                        minimum=5000,
                        boards=600,
                        by_session=dict.fromkeys(sessions, 0.0),
                    ),
                    "first_row": _run_summary(
                        "first_row",
                        pnl=0,
                        minimum=5000,
                        boards=600,
                        by_session=dict.fromkeys(sessions, 0.0),
                    ),
                },
            )
        return challenge.accumulate_standings(s)

    def test_replaying_the_same_sessions_never_manufactures_significance(
        self, tmp_path: Path
    ) -> None:
        from tree_options.desk.longrun import paired

        one = challenge.rule_check(self._replayed(tmp_path, 1))[0]
        twelve_st = self._replayed(tmp_path, 12)
        twelve = challenge.rule_check(twelve_st)[0]
        # 20 market sessions either way: replays are not new observations,
        # so the CI, the p-value and the clause verdict are IDENTICAL
        assert one["paired_vs_no_trade"]["sessions"] == 20
        assert twelve["paired_vs_no_trade"]["sessions"] == 20
        assert twelve["paired_vs_no_trade"]["game_sessions"] == 240
        assert twelve["paired_vs_no_trade"]["ci95"] == one["paired_vs_no_trade"]["ci95"]
        assert (
            twelve["paired_vs_no_trade"]["p_one_sided"] == one["paired_vs_no_trade"]["p_one_sided"]
        )
        # one average game: 10 x (+3) + 10 x (-2) = +10
        assert twelve["paired_vs_no_trade"]["diff_total"] == 10.0
        clause3 = {c["clause"]: c for c in twelve["clauses"]}[3]
        assert clause3["pass"] is False  # +0.5/session over 20 sessions is not significant
        # the defect this pins: the same 240 keys bootstrapped i.i.d. DO clear 0
        row = {r["policy"]: r for r in twelve_st["policies"]}["gepa:arm"]
        iid = paired(list(row["session_pnl"].values()), [0.0] * 240, draws=2000, seed=7)
        assert iid["ci95"][0] > 0


class TestCli:
    def test_standings_writes_and_rule_check_prints(
        self, store: Path, tmp_path: Path, monkeypatch, capsys, static_calendar
    ) -> None:
        from datetime import datetime

        from tree_options.desk.__main__ import run_cli
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
