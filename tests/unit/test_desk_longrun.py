"""The desk lab long-run harness: statistics against hand-computed cases,
the resumable/quota-aware executor with fakes, the digest's honesty
guarantees, and the v1 plug-ins against their oracles. No network, no LLM."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.test_desk_hindsight import ChoosingTransport, multi_day_bundle
from tree_options.desk import hindsight, lab, longrun
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.longrun import (
    PREREGISTERED_RULE,
    Board,
    ExecSettings,
    OutcomeCache,
    PolicySpec,
    Protocol,
)

# ------------------------------------------------------------------ fixtures

ROWS = [
    {"id": "l", "structure": "call_credit"},  # the loser (and first_row)
    {"id": "w", "structure": "put_credit"},  # the winner (and first bullish)
    {"id": "n", "structure": "call_debit"},
]  # never fills (unevaluable)
TABLE: dict[str, tuple[float, float] | None] = {"w": (12.0, 10.0), "l": (-4.0, -4.0), "n": None}
SESSIONS6 = [f"2026-06-0{d}" for d in range(1, 7)]


def boards_for(sessions: list[str], clocks: tuple[str, ...] = ("10:00", "13:00")) -> list[Board]:
    return [Board(f"s:{s}T{c}", s, c, [dict(r) for r in ROWS]) for s in sessions for c in clocks]


def table_outcome(snapshot: str, candidate: str, horizon: str | None) -> dict[str, float] | None:
    value = TABLE[candidate]
    return None if value is None else {"gross": value[0], "net": value[1]}


class FakeAsk:
    """Counts calls; picks via ``pick``; raises on snapshots in ``fail``."""

    def __init__(
        self,
        pick: Callable[[PolicySpec, Board], tuple[Any, Any, str]] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.pick = pick or (lambda spec, board: ("w", None, "fake"))
        self.fail = fail or set()
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, spec: PolicySpec, board: Board) -> tuple[Any, Any, str]:
        with self._lock:
            self.calls.append((spec.name, board.snapshot))
        if board.snapshot in self.fail:
            raise RuntimeError("provider down")
        return self.pick(spec, board)


def always_ok() -> tuple[bool, str]:
    return True, "fake meter"


PROTO = Protocol(draws=2000, random_seeds=200, incumbent="m")


def policies() -> list[PolicySpec]:
    return [PolicySpec("m", "model", repeats=2), *longrun.builtin_controls()]


def run(
    run_dir: Path,
    ask: FakeAsk,
    *,
    boards: list[Board] | None = None,
    quota: Callable[[], tuple[bool, str]] = always_ok,
    settings: ExecSettings | None = None,
    sleep: Callable[[float], None] = lambda s: None,
    protocol: Protocol = PROTO,
    **kwargs: Any,
) -> dict[str, Any]:
    return longrun.run_longrun(
        run_dir,
        boards=boards or boards_for(SESSIONS6[:3]),
        policies=policies(),
        outcome=table_outcome,
        ask=ask,
        quota_ok=quota,
        protocol=protocol,
        settings=settings or ExecSettings(concurrency=3, pause_s=60.0),
        sleep=sleep,
        **kwargs,
    )


def ok(choice: str | None, horizon: str | None = None) -> dict[str, Any]:
    return {"ok": True, "choice": choice, "horizon": horizon}


# ---------------------------------------------------------- statistics math


def test_session_sums_align_to_sessions_with_zeros() -> None:
    sums = longrun.session_sums(["a", "a", "c"], [1.5, 2.0, -1.0], ["a", "b", "c"])
    assert sums.tolist() == [3.5, 0.0, -1.0]


def test_bootstrap_ci_is_over_sessions_not_the_point_total() -> None:
    # two sessions [0, 10]: resampled sums are 0 / 10 / 20 with mass 1/4, 1/2,
    # 1/4, so the 2.5% and 97.5% percentiles are exactly 0 and 20
    assert longrun.bootstrap_ci([0.0, 10.0], draws=4000, seed=1) == (0.0, 20.0)
    # a constant series has no sampling spread
    assert longrun.bootstrap_ci([5.0, 5.0, 5.0], draws=2000, seed=1) == (15.0, 15.0)
    assert longrun.bootstrap_ci([], draws=2000, seed=1) == (0.0, 0.0)


def test_bootstrap_is_deterministic_under_its_seed() -> None:
    values = list(np.random.default_rng(3).normal(0, 25, size=40))
    first = longrun.bootstrap_ci(values, draws=3000, seed=11)
    assert first == longrun.bootstrap_ci(values, draws=3000, seed=11)
    lo, hi = first
    assert lo < sum(values) < hi


def test_sign_flip_p_exact_enumeration_hand_cases() -> None:
    assert longrun.sign_flip_p([1.0, 2.0, 3.0], draws=1000, seed=0) == pytest.approx(1 / 8)
    # [3, -1]: sums 2 (observed), 4, -2, -4 -> two of four are >= 2
    assert longrun.sign_flip_p([3.0, -1.0], draws=1000, seed=0) == pytest.approx(0.5)
    assert longrun.sign_flip_p([0.0], draws=1000, seed=0) == 1.0
    assert longrun.sign_flip_p([], draws=1000, seed=0) == 1.0


def test_sign_flip_p_monte_carlo_above_the_exact_limit() -> None:
    strong = [5.0] * 30
    p = longrun.sign_flip_p(strong, draws=2000, seed=4)
    assert p == pytest.approx(1 / 2001)
    assert longrun.sign_flip_p(strong, draws=2000, seed=4) == p


def test_holm_hand_case() -> None:
    adjusted = longrun.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adjusted == pytest.approx({"a": 0.03, "c": 0.06, "b": 0.06})
    assert longrun.holm({"x": 0.7, "y": 0.9}) == pytest.approx({"x": 1.0, "y": 1.0})


def test_stability_drop_one_and_half_split_hand_cases() -> None:
    stab = longrun.stability([10.0, -2.0, 3.0, 4.0], ["a", "b", "c", "d"])
    assert stab["total"] == 15.0
    assert (stab["drop_one_min"], stab["drop_one_max"]) == (5.0, 17.0)
    assert stab["drop_one_sign_flips"] == 0
    assert stab["most_influential_session"] == "a"
    assert stab["half_split"] == {
        "first": 8.0,
        "second": 7.0,
        "split_after": "b",
        "signs_agree": True,
    }
    fragile = longrun.stability([10.0, -12.0], ["a", "b"])
    assert fragile["drop_one_sign_flips"] == 1  # dropping b turns -2 into +10
    assert fragile["half_split"]["signs_agree"] is False


def test_paired_diff_hand_case() -> None:
    doc = longrun.paired([3.0, 1.0], [1.0, 1.0], draws=2000, seed=5)
    assert doc["diff_total"] == 2.0
    assert doc["ci95"] == [0.0, 4.0]  # diffs [2, 0]
    assert doc["p_one_sided"] == pytest.approx(0.5)  # sums 2, 2, -2, -2
    assert doc["sessions"] == 2


def test_random_null_exact_expectation_and_seed_floor() -> None:
    options = [np.array([10.0, 0.0]), np.array([4.0])]
    null = longrun.random_null(["s1", "s2"], ["s1", "s2"], options, 0.5, seeds=400, seed=9)
    assert null.expected_sessions.tolist() == [2.5, 2.0]
    assert set(np.unique(null.totals)) <= {0.0, 4.0, 10.0, 14.0}
    assert null.totals.mean() == pytest.approx(4.5, abs=0.8)
    with pytest.raises(ValueError, match="200"):
        longrun.random_null(["s1"], ["s1"], [np.array([1.0])], 0.5, seeds=199, seed=1)


def test_pick_null_places_the_realized_pick() -> None:
    doc = longrun.pick_null(
        20.0, [np.array([10.0, -10.0]), np.array([10.0, -10.0])], seeds=400, seed=2
    )
    assert doc["expected"] == 0.0
    assert doc["band95"] == [-20.0, 20.0]
    assert 0.6 < doc["percentile"] < 0.9  # only the best-of-both draw ties 20


def test_benchmark_rows_buy_and_hold_dollars_hand_case() -> None:
    rows = longrun.benchmark_rows(
        {
            "SPY": {"2026-05-29": 100.0, "2026-06-01": 110.0, "2026-06-03": 99.0},
            "NONE": {"2026-07-01": 5.0},
        },
        ["2026-06-01", "2026-06-02", "2026-06-03"],
        capital=5000.0,
        draws=2000,
        seed=1,
    )
    none, spy = rows
    assert none["status"] == "unavailable"
    assert spy["base_date"] == "2026-05-29"
    # 50 shares: +500 on 06-01, no close on 06-02, -550 on 06-03
    assert spy["net_total"] == -50.0
    assert spy["missing_sessions"] == 1
    assert spy["net_ci95"][0] <= -50.0 <= spy["net_ci95"][1]


def test_equal_weight_index_is_the_mean_of_normalized_closes() -> None:
    index = longrun.equal_weight_index(
        {"A": {"d0": 10.0, "d1": 11.0, "d2": 12.0}, "B": {"d0": 20.0, "d1": 18.0, "d2": 22.0}}, "d1"
    )
    assert index == pytest.approx({"d0": 1.0, "d1": 1.0, "d2": 1.15})


# ------------------------------------------------------------ policy rules


def test_board_and_policy_validation() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Board("s", "2026-06-01", "10:00", [{"id": "a"}, {"id": "a"}])
    with pytest.raises(ValueError, match="at least one row"):
        Board("s", "2026-06-01", "10:00", [])
    with pytest.raises(ValueError):
        Board("s", "not-a-date", "10:00", [{"id": "a"}])
    with pytest.raises(ValueError, match="needs a rule"):
        PolicySpec("x", "rule")
    with pytest.raises(ValueError, match="no rule"):
        PolicySpec("x", "model", rule=longrun.rule_no_trade)
    with pytest.raises(ValueError, match="null control"):
        PolicySpec("random", "control", rule=longrun.rule_no_trade)
    with pytest.raises(ValueError, match="duplicate policy"):
        longrun.arms_of([PolicySpec("m", "model"), PolicySpec("m", "model")])
    assert PolicySpec("m", "model", repeats=2).arm_names() == ["m#1", "m#2"]


def test_builtin_rules_are_deterministic() -> None:
    board = Board(
        "s",
        "2026-06-01",
        "10:00",
        [
            {"id": "a", "structure": "put_credit", "reward_risk": "0.5", "max_loss": "200"},
            {"id": "b", "structure": "put_credit", "reward_risk": "0.9", "max_loss": "250"},
            {"id": "c", "structure": "call_credit", "reward_risk": "2", "max_loss": "90"},
            {"id": "d", "structure": "call_debit", "direction": "bearish"},
        ],
    )
    assert longrun.rule_no_trade(board) == (None, None)
    assert longrun.rule_first_row("1d")(board) == ("a", "1d")
    assert longrun.rule_fixed_structure("put_credit")(board) == ("a", None)
    assert longrun.rule_fixed_structure("put_credit", key="max_reward_risk")(board) == ("b", None)
    assert longrun.rule_fixed_structure("put_credit", "eod", "min_max_loss")(board) == ("a", "eod")
    assert longrun.rule_fixed_structure("put_debit", "eod")(board) == (None, None)
    # an explicit direction wins over the structure's default direction
    assert not longrun.is_bullish(board.rows[3])
    assert longrun.rule_always_bullish()(board) == ("a", None)
    names = [p.name for p in longrun.builtin_controls()]
    assert names == [
        "no_trade",
        "first_row",
        "always_put_credit",
        "always_call_debit",
        "always_put_debit",
        "always_call_credit",
        "always_bullish",
        "random",
    ]


def test_unknown_choice_is_rejected_not_trusted() -> None:
    arm = longrun.arms_of([PolicySpec("m", "model")])[0]
    board = boards_for(["2026-06-01"])[0]
    rec = longrun.decide(arm, board, lambda spec, b: ("zzz", "1d", "x" * 500))
    assert rec["ok"] is True and rec["choice"] is None and rec["horizon"] is None
    assert rec["rejected_choice"] == "zzz" and len(rec["note"]) == 80
    rec = longrun.decide(arm, board, lambda spec, b: ("w", "eod", "fine"))
    assert (rec["choice"], rec["horizon"], rec["row"]) == ("w", "eod", 1)


# ---------------------------------------------------------------- scoring


def _score_case() -> tuple[list[Board], list[longrun.Arm], dict[str, dict[str, Any]]]:
    boards = boards_for(["2026-06-01", "2026-06-02"])  # b1,b2 | b3,b4
    specs = [
        PolicySpec("m", "model", repeats=2),
        PolicySpec("first_row", "control", rule=longrun.rule_first_row()),
        PolicySpec("no_trade", "control", rule=longrun.rule_no_trade),
        PolicySpec("always_call_debit", "rule", rule=longrun.rule_fixed_structure("call_debit")),
        PolicySpec("always_bullish", "control", rule=longrun.rule_always_bullish()),
    ]
    arms = longrun.arms_of(specs)
    sids = [b.snapshot for b in boards]
    receipts = {
        "m#1": {s: ok("w") for s in sids},
        "m#2": {**{s: ok("w") for s in sids[:3]}, sids[3]: ok(None)},
        "first_row": {s: ok("l") for s in sids},
        "no_trade": {s: ok(None) for s in sids},
        "always_call_debit": {s: ok("n") for s in sids},
        "always_bullish": {**{s: ok("w") for s in sids[:2]}, **{s: ok("l") for s in sids[2:]}},
    }
    return boards, arms, receipts


def test_score_run_matches_hand_computation() -> None:
    boards, arms, receipts = _score_case()
    proto = Protocol(draws=2000, random_seeds=200, incumbent="m", cutoff="2026-06-01")
    doc = longrun.score_run(boards, arms, receipts, OutcomeCache(table_outcome), proto)
    rows = {r["arm"]: r for r in doc["standings"]}
    # incumbent entry rate (1.0 + 0.75) / 2; per-board option mean (-4 + 10 + 0) / 3 = 2
    null = doc["random_null"]
    assert null["p_enter"] == 0.875 and null["matched_to"] == ["m#1", "m#2"]
    assert null["expected_total"] == 7.0  # 0.875 * 2 * 4 boards
    m1, m2 = rows["m#1"], rows["m#2"]
    assert (m1["net_total"], m1["gross_total"], m1["net_ci95"]) == (40.0, 48.0, [40.0, 40.0])
    assert (m2["net_total"], m2["entered"], m2["net_ci95"]) == (30.0, 3, [20.0, 40.0])
    assert m1["vs_random"]["diff_total"] == 33.0 and m1["vs_random"]["p_one_sided"] == 0.25
    assert m1["vs_first_row"]["diff_total"] == 56.0
    assert m1["vs_incumbent"] is None and m2["vs_incumbent"]["diff_total"] == -10.0
    assert rows["first_row"]["net_total"] == -16.0
    assert rows["first_row"]["vs_random"]["diff_total"] == -23.0
    # the regime baseline: sessions [20, -8]; m#1 [20, 20] beats it by [0, 28]
    assert rows["always_bullish"]["net_total"] == 12.0
    assert rows["always_bullish"]["vs_regime"] is None
    assert m1["vs_regime"]["diff_total"] == 28.0 and m1["vs_regime"]["ci95"] == [0.0, 56.0]
    assert m2["vs_regime"]["diff_total"] == 18.0
    acd = rows["always_call_debit"]
    assert (acd["entered"], acd["unevaluable"], acd["net_total"]) == (4, 4, 0.0)
    assert acd["net_per_evaluated_entry"] is None
    for row in doc["standings"]:  # every total carries its session CI
        lo, hi = row["net_ci95"]
        assert lo <= row["net_total"] <= hi
    assert doc["standings"][0]["arm"] == "m#1"  # ordered by the vs-random CI low
    aa = doc["aa"]
    assert aa["status"] == "valid" and aa["agreement"] == 0.75
    # repeat 1 minus repeat 2: sessions [0, +10] -> resampled sums 0/10/20
    assert aa["diff"]["diff_total"] == 10.0 and aa["diff"]["ci95"] == [0.0, 20.0]
    wf = doc["walk_forward"]
    assert (wf["status"], wf["cutoff"], wf["tune_sessions"], wf["test_sessions"]) == (
        "ok",
        "2026-06-01",
        1,
        1,
    )
    assert [f["policy"] for f in wf["finalists"]] == ["m", "always_call_debit"]
    assert (
        wf["finalists"][0]["test"]["vs_random"]["diff_total"] == 12.0
    )  # test rate (1+.5)/2=.75; mean option 2 on 2 boards: 15-3
    assert doc["promotion"]["promoted"] is False


def test_aa_flags_the_evaluation_invalid_when_the_repeats_differ() -> None:
    boards = boards_for(SESSIONS6, clocks=("10:00",))
    arms = longrun.arms_of([PolicySpec("m", "model", repeats=2)])
    receipts = {
        "m#1": {b.snapshot: ok("w") for b in boards},
        "m#2": {b.snapshot: ok("l") for b in boards},
    }
    doc = longrun.score_run(boards, arms, receipts, OutcomeCache(table_outcome), PROTO)
    assert doc["aa"]["significant"] is True
    assert doc["aa"]["status"] == "INVALID" and doc["evaluation_valid"] is False
    assert doc["aa"]["agreement"] == 0.0
    assert doc["headline"].startswith("EVALUATION INVALID")
    assert "EVALUATION INVALID" in longrun.digest_markdown(doc)
    same = {"m#1": receipts["m#1"], "m#2": receipts["m#1"]}
    valid = longrun.score_run(boards, arms, same, OutcomeCache(table_outcome), PROTO)
    assert valid["aa"]["status"] == "valid" and valid["aa"]["agreement"] == 1.0


def test_aa_not_run_leaves_the_evaluation_unvalidated() -> None:
    boards = boards_for(SESSIONS6[:2])
    arms = longrun.arms_of([PolicySpec("m", "model")])
    receipts = {"m": {b.snapshot: ok("w") for b in boards}}
    doc = longrun.score_run(
        boards,
        arms,
        receipts,
        OutcomeCache(table_outcome),
        Protocol(draws=2000, random_seeds=200, incumbent="m"),
    )
    assert doc["aa"]["status"] == "not_run" and doc["evaluation_valid"] is False
    assert doc["headline"].startswith("UNVALIDATED")


def test_walk_forward_caps_finalists_at_two() -> None:
    sessions = ["d1", "d2", "d3"]
    pooled = {
        "a": np.array([5.0, 5.0, 1.0]),
        "b": np.array([1.0, 1.0, 9.0]),
        "c": np.array([3.0, 3.0, 3.0]),
        "d": np.array([-1.0, 0.0, 0.0]),
        "e": np.array([4.0, 4.0, -9.0]),
    }
    for cap in (2, 5):  # even a caller asking for more gets at most two
        wf = longrun.walk_forward(
            pooled,
            np.zeros(3),
            sessions,
            incumbent=None,
            cutoff="d2",
            metric="total",
            max_finalists=cap,
            draws=2000,
            seed=1,
            alpha=0.05,
            aa_valid=True,
        )
        assert wf["max_finalists"] == 2
        assert [f["policy"] for f in wf["finalists"]] == ["a", "e"]  # tune totals 10, 8
    assert [r["policy"] for r in wf["ranking"]] == ["a", "e", "c", "b", "d"]
    a, e = wf["finalists"]
    assert (a["test"]["net_total"], e["test"]["net_total"]) == (1.0, -9.0)
    assert (a["holm_p"], e["holm_p"]) == (1.0, 1.0)  # raw one-session p 0.5 and 1.0
    assert a["eligible_for_operator_review"] is False
    with pytest.raises(ValueError, match="max_finalists"):
        Protocol(max_finalists=3)
    one = longrun.walk_forward(
        pooled,
        np.zeros(3),
        sessions,
        incumbent=None,
        cutoff="d3",
        metric="total",
        max_finalists=2,
        draws=2000,
        seed=1,
        alpha=0.05,
        aa_valid=True,
    )
    assert one["status"] == "not_applicable"


def test_walk_forward_eligibility_needs_every_clause() -> None:
    sessions = [f"d{i:02d}" for i in range(1, 25)]
    strong = np.full(24, 10.0)
    pooled = {"challenger": strong, "inc": np.zeros(24)}
    wf = longrun.walk_forward(
        pooled,
        np.zeros(24),
        sessions,
        incumbent="inc",
        cutoff="d12",
        metric="ci_low_diff_vs_random",
        max_finalists=2,
        draws=2000,
        seed=1,
        alpha=0.05,
        aa_valid=True,
        own_expected={name: np.zeros(24) for name in pooled},
        test_entries={name: np.full(24, 3) for name in pooled},
    )
    top = wf["finalists"][0]
    assert top["policy"] == "challenger" and top["eligible_for_operator_review"] is True
    assert all(v is True for v in top["rule_check"].values())
    invalid = longrun.walk_forward(
        pooled,
        np.zeros(24),
        sessions,
        incumbent="inc",
        cutoff="d12",
        metric="ci_low_diff_vs_random",
        max_finalists=2,
        draws=2000,
        seed=1,
        alpha=0.05,
        aa_valid=False,
        own_expected={name: np.zeros(24) for name in pooled},
        test_entries={name: np.full(24, 3) for name in pooled},
    )
    assert invalid["finalists"][0]["eligible_for_operator_review"] is False


# --------------------------------------------------------------- executor


def test_run_digest_leads_with_the_note_and_never_promotes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    result = run(run_dir, FakeAsk())
    assert result["status"] == "finished" and result["complete"] is True
    doc = json.loads((run_dir / "digest.json").read_text())
    assert list(doc)[:3] == ["schema", "untrusted_note", "promotion"]
    assert doc["promotion"] == {
        "promoted": False,
        "rule": PREREGISTERED_RULE,
        "pre_registered_at": doc["promotion"]["pre_registered_at"],
    }
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert not re.search(r'"promoted"\s*:\s*true', path.read_text(), re.I), path
    md = (run_dir / "digest.md").read_text()
    body = [line for line in md.splitlines() if line.strip()]
    assert body[1].startswith("> UNTRUSTED / NEVER PROMOTED")
    assert PREREGISTERED_RULE in md and "Promoted: False" in md
    assert "resampling_unit: session" in md and "draws 2000" in md
    arms = [
        "m#1",
        "m#2",
        "no_trade",
        "first_row",
        "always_put_credit",
        "always_call_debit",
        "always_put_debit",
        "always_call_credit",
        "always_bullish",
    ]
    assert sorted(doc["receipts"]) == sorted(arms)
    for arm in arms:
        assert doc["receipts"][arm] in md and Path(doc["receipts"][arm]).is_file()
    table = [line for line in md.splitlines() if line.startswith("| ") and "---" not in line]
    for line in table[1:]:  # never a bare total: every standings row shows a CI
        assert re.search(r"[+-]\d+\.\d\d \[[+-]\d", line), line
    plan = json.loads((run_dir / "plan.json").read_text())
    assert plan["protocol"] == PROTO.to_json() and plan["preregistered_rule"] == PREREGISTERED_RULE
    assert len((run_dir / "boards.jsonl").read_text().splitlines()) == 6


def test_resume_skips_boards_already_decided(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    first = FakeAsk()
    run(run_dir, first)
    assert len(first.calls) == 12  # 2 repeats x 6 boards
    again = FakeAsk()
    assert run(run_dir, again)["status"] == "finished"
    assert again.calls == []
    # a lost receipt (e.g. a killed process) is the only board asked again
    path = longrun.receipts_path(run_dir, "m#2")
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][:15])  # torn last line
    lost = json.loads(lines[-1])["snapshot"]
    third = FakeAsk()
    run(run_dir, third)
    assert third.calls == [("m", lost)]


def test_failures_are_recorded_never_fatal_and_retried_on_resume(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    boards = boards_for(SESSIONS6[:3])
    failing = {b.snapshot for b in boards if b.session == SESSIONS6[2]}
    result = run(run_dir, FakeAsk(fail=failing))
    assert result["status"] == "finished" and result["complete"] is False
    recs = [
        json.loads(line) for line in longrun.receipts_path(run_dir, "m#1").read_text().splitlines()
    ]
    bad = [r for r in recs if not r["ok"]]
    assert {r["snapshot"] for r in bad} == failing
    assert all(r["error"] == "RuntimeError: provider down" and r["choice"] is None for r in bad)
    doc = json.loads((run_dir / "digest.json").read_text())
    assert doc["boards"] == {
        "total": 6,
        "scored": 4,
        "excluded": 2,
        "sessions": {"count": 2, "first": SESSIONS6[0], "last": SESSIONS6[1]},
    }
    assert {r["arm"]: r["failures"] for r in doc["standings"]}["m#1"] == 2
    assert doc["headline"].startswith("PARTIAL")
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["arms"]["m#1"]["failures"] == 2 and progress["status"] == "finished"
    healthy = FakeAsk()
    assert run(run_dir, healthy)["complete"] is True
    assert sorted(healthy.calls) == sorted(("m", s) for s in failing for _ in range(2))
    assert json.loads((run_dir / "progress.json").read_text())["failures"] == 0


def test_quota_pause_drains_sleeps_and_resumes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    answers = iter([(False, "left=40 planned=60"), (False, "left=41 planned=60")])

    def quota() -> tuple[bool, str]:
        return next(answers, (True, "left=90 planned=60"))

    slept: list[tuple[float, str, str]] = []

    def sleep(seconds: float) -> None:
        progress = json.loads((run_dir / "progress.json").read_text())
        slept.append((seconds, progress["status"], progress["quota"]["reason"]))

    ask = FakeAsk()
    result = run(
        run_dir,
        ask,
        quota=quota,
        sleep=sleep,
        settings=ExecSettings(concurrency=2, pause_s=60.0, quota_every=100),
    )
    assert result["status"] == "finished"
    assert slept == [(60.0, "paused", "left=40 planned=60"), (60.0, "paused", "left=41 planned=60")]
    assert len(ask.calls) == 12
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["paused_s"] == 120.0 and progress["quota"]["ok"] is True


def test_quota_pause_budget_stops_resumably(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    ask = FakeAsk()
    result = run(
        run_dir,
        ask,
        quota=lambda: (False, "dry"),
        settings=ExecSettings(pause_s=60.0, max_pause_s=60.0),
    )
    assert result["status"] == "stopped:quota_pause_limit"
    assert ask.calls == [] and not (run_dir / "digest.json").exists()
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["status"] == "stopped:quota_pause_limit" and progress["paused_s"] == 60.0
    assert progress["arms"]["no_trade"]["done"] == 6  # rules never wait on the meter
    later = FakeAsk()
    assert run(run_dir, later)["status"] == "finished" and len(later.calls) == 12
    assert json.loads((run_dir / "progress.json").read_text())["paused_s"] == 60.0


def test_consecutive_failures_back_off(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    boards = boards_for(SESSIONS6[:3])
    ask = FakeAsk(fail={b.snapshot for b in boards})
    result = run(
        run_dir, ask, settings=ExecSettings(concurrency=1, failure_backoff_after=3, max_pause_s=0.0)
    )
    assert result["status"] == "stopped:failure_backoff_limit"
    assert len(ask.calls) == 3


def test_stop_file_stops_resumably(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / longrun.STOP_FILE).write_text("")
    ask = FakeAsk()
    assert run(run_dir, ask)["status"] == "stopped:stop_file" and ask.calls == []


def test_model_arms_are_interleaved(tmp_path: Path) -> None:
    ticks = iter(range(100_000))

    def clock() -> datetime:
        return datetime.fromtimestamp(1_790_000_000 + next(ticks), UTC)

    run(
        tmp_path / "run",
        FakeAsk(),
        boards=boards_for(SESSIONS6),
        settings=ExecSettings(concurrency=1),
        clock=clock,
    )
    stamped = sorted(
        (rec["at"], arm)
        for arm in ("m#1", "m#2")
        for rec in longrun.load_receipts(longrun.receipts_path(tmp_path / "run", arm)).values()
    )
    assert len(stamped) == 24
    first_half = [arm for _, arm in stamped[:12]]
    # repeats progress together: neither arm runs as one block
    assert 3 <= first_half.count("m#2") <= 9


def test_resume_refuses_changed_boards_or_protocol(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run(run_dir, FakeAsk())
    with pytest.raises(ValueError, match="boards changed"):
        run(run_dir, FakeAsk(), boards=boards_for(SESSIONS6[:2]))
    with pytest.raises(ValueError, match="pre-registered"):
        run(
            run_dir,
            FakeAsk(),
            protocol=Protocol(draws=2000, random_seeds=200, incumbent="m", cutoff="2026-06-02"),
        )


def test_progress_reports_per_arm_state(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run(run_dir, FakeAsk(pick=lambda spec, board: ("n", None, "")))
    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["schema"] == longrun.PROGRESS_SCHEMA and progress["digest"] == "digest.json"
    arm = progress["arms"]["m#1"]
    assert arm == {
        "policy": "m",
        "repeat": 1,
        "kind": "model",
        "done": 6,
        "total": 6,
        "entered": 6,
        "failures": 0,
        "unevaluable": 6,
        "net": 0.0,
    }
    assert progress["arms"]["always_put_credit"]["net"] == 60.0
    assert progress["total"] == 54 and progress["finished"] == 54 and progress["eta_s"] == 0.0


def test_score_only_scores_whatever_is_on_disk(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    ask = FakeAsk()
    result = run(run_dir, ask, score_only=True)
    assert ask.calls == [] and result["complete"] is False
    doc = json.loads((run_dir / "digest.json").read_text())
    assert doc["boards"]["scored"] == 0 and doc["headline"].startswith("PARTIAL")


# ------------------------------------------------------------ plug-ins/CLI


def test_quota_broker_meter_semantics() -> None:
    class Response:
        def __init__(self, body: dict[str, Any]) -> None:
            self.body = json.dumps(body).encode()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, *args: Any) -> bytes:
            return self.body

    def meter(left: float, planned: float) -> Callable[..., Response]:
        body = {
            "providers": {"minimax": {"meter": {"interval_pct": left, "planned_pct_now": planned}}}
        }
        return lambda url, timeout: Response(body)

    assert longrun.broker_quota(opener=meter(97.0, 85.2))() == (True, "left=97.0 planned=85.2")
    assert longrun.broker_quota(opener=meter(80.0, 85.2))()[0] is False
    assert longrun.broker_quota(opener=meter(83.5, 85.2))()[0] is True  # within the margin

    def down(url: str, timeout: float) -> Response:
        raise OSError("refused")

    assert longrun.broker_quota(opener=down)() == (True, "meter_unavailable:OSError")


def test_register_plugin_refuses_silent_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(longrun.PLUGINS["boards"], "pytest-x", lambda p, c: [])
    with pytest.raises(ValueError, match="already registered"):
        longrun.register_plugin("boards", "pytest-x", lambda p, c: [])
    with pytest.raises(ValueError, match="unknown boards plug-in"):
        longrun.plugin("boards", "nope")


def test_cli_run_and_status_with_registered_fakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tree_options.desk.__main__ import run_cli

    ask = FakeAsk()
    monkeypatch.setitem(longrun.PLUGINS["boards"], "pytest", lambda p, c: boards_for(SESSIONS6[:3]))
    monkeypatch.setitem(longrun.PLUGINS["outcome"], "pytest", lambda p, c: table_outcome)
    monkeypatch.setitem(longrun.PLUGINS["ask"], "pytest", lambda p, c: ask)
    bench = tmp_path / "bench.json"
    bench.write_text(json.dumps({"SPY": {"2026-05-29": 100.0, "2026-06-03": 101.0}}))
    config = tmp_path / "longrun.json"
    config.write_text(
        json.dumps(
            {
                "out_root": "out",
                "incumbent": "m",
                "concurrency": 2,
                "boards": {"plugin": "pytest"},
                "outcome": {"plugin": "pytest"},
                "ask": {"plugin": "pytest"},
                "quota": {"plugin": "always"},
                "benchmarks": {"plugin": "file", "path": "bench.json"},
                "protocol": {"draws": 1000, "random_seeds": 200},
                "policies": [
                    {"name": "m", "kind": "model", "repeats": 2},
                    {
                        "name": "pc-eod",
                        "kind": "rule",
                        "builtin": "fixed_structure",
                        "structure": "put_credit",
                        "horizon": "eod",
                    },
                ],
            }
        )
    )
    assert run_cli(["longrun", "run", "--config", str(config)]) == 0
    out = json.loads(capsys.readouterr().out)
    run_dir = Path(out["run_dir"])
    assert run_dir.parent == tmp_path / "out" and out["status"] == "finished"
    assert len(ask.calls) == 12
    doc = json.loads((run_dir / "digest.json").read_text())
    assert "pc-eod" in doc["receipts"] and "always_bullish" in doc["receipts"]
    spy = doc["benchmarks"][0]
    assert spy["name"] == "SPY" and spy["net_total"] == 50.0
    assert json.loads((run_dir / "config.json").read_text())["incumbent"] == "m"
    assert run_cli(["longrun", "status", "--dir", str(tmp_path / "out")]) == 0
    status = capsys.readouterr().out
    assert run_dir.name in status and "digest: " in status and "m#1: 6/6" in status
    assert run_cli(["longrun", "status", "--dir", str(tmp_path / "empty")]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"boards": {"plugin": "nope"}, "outcome": {}, "policies": []}))
    assert run_cli(["longrun", "run", "--config", str(bad)]) == 2


@pytest.fixture
def v1_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "test-key-material-2")
    raw = multi_day_bundle(date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24))
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(raw))
    return path, raw


def test_v1_plugins_match_decision_packet_and_hindsight(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, raw = v1_bundle
    ctx = longrun.PluginContext(config_dir=tmp_path)
    boards = longrun.plugin("boards", "v1")({"bundle": str(path)}, ctx)
    assert boards
    outcome = longrun.plugin("outcome", "v1")({}, ctx)
    evaluable = 0
    for board in boards:
        day = date.fromisoformat(board.session)
        packet = iag.decision_packet(raw, day, board.clock)
        assert board.snapshot == packet["snapshot_id"]
        assert board.rows == lab.board_rows(packet)
        oracle = hindsight.board_outcomes(raw, day, board.clock)
        for cid in board.ids:
            got = outcome(board.snapshot, cid, "ignored-horizon")
            if cid in oracle:
                evaluable += 1
                assert got == {"gross": float(oracle[cid]), "net": float(oracle[cid])}
            else:
                assert got is None
    assert evaluable > 0
    assert outcome(boards[0].snapshot, "not-on-the-board", None) is None


def test_v1_end_to_end_with_a_fake_transport(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, _ = v1_bundle
    config = tmp_path / "v1.json"
    config.write_text(
        json.dumps(
            {
                "out_root": str(tmp_path / "out"),
                "incumbent": "m31",
                "boards": {"plugin": "v1", "bundle": str(path)},
                "outcome": {"plugin": "v1"},
                "ask": {"plugin": "v1", "provider": "minimax-flash"},
                "protocol": {"draws": 1000, "random_seeds": 200},
                "policies": [{"name": "m31", "kind": "model", "repeats": 2}],
            }
        )
    )
    transport = ChoosingTransport(0)
    result = longrun.run_from_config(config, shared={"transport": transport}, limit=4)
    assert result["status"] == "finished" and result["complete"] is True
    assert len(transport.calls) == 8
    assert {c["body"]["model"] for c in transport.calls} == {"MiniMax-M3.1-Flash-Preview"}
    doc = json.loads((Path(result["run_dir"]) / "digest.json").read_text())
    assert doc["boards"]["total"] == 4 and doc["aa"]["agreement"] == 1.0
    rows = {r["arm"]: r for r in doc["standings"]}
    assert rows["m31#1"]["vs_first_row"]["diff_total"] == 0.0  # row 0 == first_row
    assert rows["m31#1"]["net_total"] == rows["first_row"]["net_total"]


# --------------------------------------------------------------- v2 plug-ins


class HorizonTransport:
    """Picks row ``index`` of each v2 board with a fixed horizon."""

    def __init__(self, index: int = 0, horizon: str = "eod") -> None:
        self.index, self.horizon = index, horizon
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body), "timeout": timeout})
        prompt = json.loads(json.loads(body)["messages"][0]["content"])
        rows = prompt["board"]
        choice = rows[self.index]["id"] if self.index < len(rows) else None
        content = json.dumps({"choice": choice, "horizon": self.horizon, "note": "fixed"})
        envelope = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        return 200, json.dumps(envelope).encode()


def test_v2_plugins_match_env_v2_and_table_equals_live(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    from tree_options.desk import outcomes

    path, raw = v1_bundle
    ctx = longrun.PluginContext(config_dir=tmp_path)
    boards = longrun.plugin("boards", "v2")({"bundle": str(path)}, ctx)
    assert boards
    index = outcomes.prepare_index(raw)
    for board in boards:
        day = date.fromisoformat(board.session)
        context = lab.board_context(index, day, board.clock)
        expect = lab.board_rows_v2(iag.decision_packet(raw, day, board.clock), context)
        assert board.rows == expect
        assert board.context == dict(context.public)  # only the model-visible part
    live = longrun.plugin("outcome", "v2")({"sync": 2}, ctx)
    table_path = tmp_path / "table.jsonl"
    with table_path.open("w") as stream:
        for row in outcomes.outcome_table(index, costs=outcomes.CostModel(), leg_sync_minutes=2):
            stream.write(json.dumps(row, default=str) + "\n")
    table = longrun.plugin("outcome", "v2")({"table": str(table_path)}, ctx)
    compared = 0
    for board in boards:
        for cid in board.ids:
            for horizon in (None, "intraday", "eod", "hold:5", "expiry"):
                got_live, got_table = (
                    live(board.snapshot, cid, horizon),
                    table(board.snapshot, cid, horizon),
                )
                assert got_live == got_table
                if got_live is not None:
                    compared += 1
                    assert got_live["net"] < got_live["gross"]  # costs are charged
    assert compared > 0


def test_v2_end_to_end_scores_the_chosen_horizon(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, _ = v1_bundle
    config = tmp_path / "v2.json"
    config.write_text(
        json.dumps(
            {
                "out_root": str(tmp_path / "out"),
                "incumbent": "m31",
                "boards": {"plugin": "v2", "bundle": str(path)},
                "outcome": {"plugin": "v2", "sync": 2},
                "ask": {"plugin": "v2", "provider": "minimax-flash"},
                "protocol": {"draws": 1000, "random_seeds": 200},
                "policies": [{"name": "m31", "kind": "model", "repeats": 2}],
            }
        )
    )
    transport = HorizonTransport(0, "eod")
    result = longrun.run_from_config(config, shared={"transport": transport}, limit=4)
    assert result["status"] == "finished" and result["complete"] is True
    assert len(transport.calls) == 8
    content = json.loads(transport.calls[0]["body"]["messages"][0]["content"])
    assert set(content) == {"task", "context", "board"} and "eod" in content["task"]
    run_dir = Path(result["run_dir"])
    receipts = [
        json.loads(line)
        for line in longrun.receipts_path(run_dir, "m31#1").read_text().splitlines()
    ]
    assert receipts and all(r.get("horizon") == "eod" for r in receipts if r.get("choice"))
    from tree_options.trex.discovery.llm import PROVIDERS

    provider_default = PROVIDERS["minimax-flash"]["extra"].get("reasoning_effort")
    assert all(
        call["body"].get("reasoning_effort") == provider_default for call in transport.calls
    )  # no override without an effort param


def test_v2_ask_effort_is_a_per_policy_override(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, _ = v1_bundle
    config = tmp_path / "v2e.json"
    config.write_text(
        json.dumps(
            {
                "out_root": str(tmp_path / "out"),
                "incumbent": "m31",
                "boards": {"plugin": "v2", "bundle": str(path)},
                "outcome": {"plugin": "v2", "sync": 2},
                "ask": {"plugin": "v2", "provider": "minimax-flash"},
                "protocol": {"draws": 1000, "random_seeds": 200},
                "policies": [
                    {"name": "m31", "kind": "model"},
                    {
                        "name": "m31-low",
                        "kind": "model",
                        "ask": {"plugin": "v2", "provider": "minimax-flash", "effort": "low"},
                    },
                ],
            }
        )
    )
    transport = HorizonTransport(0, "eod")
    result = longrun.run_from_config(config, shared={"transport": transport}, limit=2)
    assert result["status"] == "finished"
    from tree_options.trex.discovery.llm import PROVIDERS

    default = str(PROVIDERS["minimax-flash"]["extra"].get("reasoning_effort"))
    assert default != "low"  # else this test cannot tell the override from the default
    efforts = sorted(str(call["body"].get("reasoning_effort")) for call in transport.calls)
    assert efforts == sorted([default, default, "low", "low"])  # 2 boards x (default, low)
    bad = json.loads(config.read_text())
    bad["policies"][1]["ask"]["effort"] = "extreme"
    config.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="effort must be one of"):
        longrun.run_from_config(config, shared={"transport": transport}, limit=2)


def test_v2_ask_budget_overrides_timeout_and_max_tokens(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    from tree_options.trex.discovery.llm import PROVIDERS

    path, _ = v1_bundle
    base = {
        "out_root": str(tmp_path / "out"),
        "incumbent": "m31",
        "boards": {"plugin": "v2", "bundle": str(path)},
        "outcome": {"plugin": "v2", "sync": 2},
        "protocol": {"draws": 1000, "random_seeds": 200},
        "policies": [{"name": "m31", "kind": "model"}],
    }
    config = tmp_path / "budget.json"
    config.write_text(
        json.dumps(
            {
                **base,
                "ask": {
                    "plugin": "v2",
                    "provider": "minimax-flash",
                    "timeout": 300,
                    "max_tokens": 20000,
                },
            }
        )
    )
    transport = HorizonTransport(0, "eod")
    assert (
        longrun.run_from_config(config, shared={"transport": transport}, limit=2)["status"]
        == "finished"
    )
    assert [c["timeout"] for c in transport.calls] == [300.0, 300.0]
    assert [c["body"]["max_tokens"] for c in transport.calls] == [20000, 20000]
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({**base, "ask": {"plugin": "v2", "provider": "minimax-flash"}}))
    transport = HorizonTransport(0, "eod")
    longrun.run_from_config(plain, shared={"transport": transport}, limit=1)
    spec = PROVIDERS["minimax-flash"]
    assert transport.calls[0]["timeout"] == float(spec["timeout"])
    assert transport.calls[0]["body"]["max_tokens"] == spec["max_tokens"]
    for bad in ({"timeout": 0}, {"timeout": 901}, {"max_tokens": 0}):
        plain.write_text(json.dumps({**base, "ask": {"plugin": "v2", **bad}}))
        with pytest.raises(ValueError, match="timeout must be in"):
            longrun.run_from_config(plain, shared={"transport": transport}, limit=1)


# ----------------------------------------- truncation self-heal + failure tally


class TruncatingTransport:
    """Answers like HorizonTransport(0, 'eod') but the first ``truncate``
    calls return a cut-off reply (finish_reason=length): M3.1-Flash's
    always-on thinking can eat the whole max_tokens window."""

    def __init__(self, truncate: int = 1) -> None:
        self.truncate, self.seen = truncate, 0
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append({"body": json.loads(body), "timeout": timeout})
        self.seen += 1
        rows = json.loads(json.loads(body)["messages"][0]["content"])["board"]
        if self.seen <= self.truncate:
            cut = '{"choice": "trunc'  # a parseable-looking prefix, still refused
            return 200, json.dumps(
                {"choices": [{"message": {"content": cut}, "finish_reason": "length"}]}
            ).encode()
        content = json.dumps({"choice": rows[0]["id"], "horizon": "eod", "note": "healed"})
        return 200, json.dumps(
            {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        ).encode()


def test_v2_ask_self_heals_one_truncation_from_the_provider_budget(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    from tree_options.trex.discovery.llm import PROVIDERS

    path, _ = v1_bundle
    config = tmp_path / "heal.json"
    config.write_text(
        json.dumps(
            {
                "out_root": str(tmp_path / "out"),
                "incumbent": "m31",
                "boards": {"plugin": "v2", "bundle": str(path)},
                "outcome": {"plugin": "v2", "sync": 2},
                "ask": {"plugin": "v2", "provider": "minimax-flash"},
                "protocol": {"draws": 1000, "random_seeds": 200},
                "policies": [{"name": "m31", "kind": "model"}],
            }
        )
    )
    transport = TruncatingTransport(truncate=1)
    result = longrun.run_from_config(config, shared={"transport": transport}, limit=1)
    assert result["status"] == "finished" and result["complete"] is True
    spec = PROVIDERS["minimax-flash"]
    assert 2 * spec["max_tokens"] <= 48000  # else this case silently tests the cap
    assert len(transport.calls) == 2  # one escalating retry, never more
    first, second = transport.calls
    assert first["body"]["max_tokens"] == spec["max_tokens"]  # the spec start
    assert first["timeout"] == float(spec["timeout"])
    assert second["body"]["max_tokens"] == 2 * spec["max_tokens"]
    assert second["timeout"] == 2 * float(spec["timeout"])
    rec = json.loads(
        longrun.receipts_path(Path(result["run_dir"]), "m31").read_text().splitlines()[0]
    )
    assert rec["ok"] is True and rec["choice"]
    assert rec["escalated"] is True
    assert rec["max_tokens"] == 2 * spec["max_tokens"]
    assert rec["timeout"] == 2 * float(spec["timeout"])
    # a healed run is a clean run: no failure tally anywhere
    doc = json.loads((Path(result["run_dir"]) / "digest.json").read_text())
    assert all("failure_reasons" not in row for row in doc["standings"])
    assert "Failure tally" not in (Path(result["run_dir"]) / "digest.md").read_text()


def test_v2_ask_escalation_is_capped_and_a_second_truncation_still_fails(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, _ = v1_bundle
    base = {
        "out_root": str(tmp_path / "out"),
        "incumbent": "m31",
        "boards": {"plugin": "v2", "bundle": str(path)},
        "outcome": {"plugin": "v2", "sync": 2},
        "ask": {"plugin": "v2", "provider": "minimax-flash", "timeout": 300, "max_tokens": 30000},
        "protocol": {"draws": 1000, "random_seeds": 200},
        "policies": [{"name": "m31", "kind": "model"}],
    }
    config = tmp_path / "cap.json"
    config.write_text(json.dumps(base))
    transport = TruncatingTransport(truncate=1)
    result = longrun.run_from_config(config, shared={"transport": transport}, limit=1)
    assert result["status"] == "finished" and result["complete"] is True
    first, second = transport.calls
    assert (first["body"]["max_tokens"], first["timeout"]) == (30000, 300.0)
    assert (second["body"]["max_tokens"], second["timeout"]) == (48000, 600.0)  # the cap
    rec = json.loads(
        longrun.receipts_path(Path(result["run_dir"]), "m31").read_text().splitlines()[0]
    )
    assert rec["ok"] is True and rec["escalated"] is True and rec["max_tokens"] == 48000

    # truncated at the escalated budget too: exactly one retry, then the failure stands
    always = tmp_path / "always.json"
    always.write_text(json.dumps(base))
    transport = TruncatingTransport(truncate=99)
    result = longrun.run_from_config(always, shared={"transport": transport}, limit=1)
    assert result["status"] == "finished"
    assert len(transport.calls) == 2
    rec = json.loads(
        longrun.receipts_path(Path(result["run_dir"]), "m31").read_text().splitlines()[0]
    )
    assert rec["ok"] is False and "truncated" in rec["error"]
    assert rec.get("escalated") is None  # a failed retry never claims the flag
    run_dir = Path(result["run_dir"])
    doc = json.loads((run_dir / "digest.json").read_text())
    rows = {r["arm"]: r for r in doc["standings"]}
    assert rows["m31"]["failures"] == 1
    assert rows["m31"]["failure_reasons"] == {"truncated": 1}
    assert doc["complete"] is False  # the board never got an ok receipt
    md = (run_dir / "digest.md").read_text()
    assert "Failure tally" in md and "truncated 1" in md
    assert "PARTIAL RUN" in md  # the tally says the run is incomplete
    view = {r["arm"]: r for r in longrun._project_digest(doc)["standings"]}
    assert view["m31"]["failure_reasons"] == {"truncated": 1}


class FailoverTransport:
    """Raises for the primary provider's URL (a NON-timeout transport outage -
    ConnectionError, not TimeoutError, so it never triggers the timeout
    escalation and goes straight to the fallback) and answers like
    HorizonTransport(0, eod) for every other provider."""

    def __init__(self, fail_hosts: tuple[str, ...] = ("api.minimax.io",)) -> None:
        self.fail_hosts, self.calls = fail_hosts, []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body), "timeout": timeout})
        if any(host in url for host in self.fail_hosts):
            raise ConnectionError("simulated provider outage")
        rows = json.loads(json.loads(body)["messages"][0]["content"])["board"]
        content = json.dumps({"choice": rows[0]["id"], "horizon": "eod", "note": "backup"})
        return 200, json.dumps(
            {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        ).encode()


def test_v2_ask_falls_over_to_the_backup_provider_once(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, _ = v1_bundle
    config = tmp_path / "failover.json"
    config.write_text(
        json.dumps(
            {
                "out_root": str(tmp_path / "out"),
                "incumbent": "m31",
                "boards": {"plugin": "v2", "bundle": str(path)},
                "outcome": {"plugin": "v2", "sync": 2},
                "ask": {
                    "plugin": "v2",
                    "provider": "minimax-flash",
                    "effort": "low",
                    "timeout": 300,
                    "max_tokens": 20000,
                    "fallback_provider": "zai",
                },
                "protocol": {"draws": 1000, "random_seeds": 200},
                "policies": [{"name": "m31", "kind": "model"}],
            }
        )
    )
    transport = FailoverTransport()
    result = longrun.run_from_config(config, shared={"transport": transport}, limit=1)
    assert result["status"] == "finished" and result["complete"] is True
    assert [c["url"].split("/chat")[0] for c in transport.calls] == [
        "https://api.minimax.io/v1",
        "https://api.z.ai/api/coding/paas/v4",
    ]
    backup = transport.calls[1]
    assert backup["body"]["max_tokens"] == 20000  # the generic budget rides along
    assert "reasoning_effort" not in backup["body"]  # never the primary's extras
    assert backup["timeout"] == 300.0
    rec = json.loads(
        longrun.receipts_path(Path(result["run_dir"]), "m31").read_text().splitlines()[0]
    )
    assert rec["ok"] is True and rec["choice"]
    assert rec["provider"] == "zai" and rec["fallback"] is True
    assert rec.get("escalated") is None
    # both providers down: exactly two calls, the failure stands as the backup's error
    both = FailoverTransport(("api.minimax.io", "api.z.ai"))
    down = tmp_path / "down.json"
    down.write_text(config.read_text())
    result = longrun.run_from_config(down, shared={"transport": both}, limit=1)
    assert result["status"] == "finished"
    assert len(both.calls) == 2
    rec = json.loads(
        longrun.receipts_path(Path(result["run_dir"]), "m31").read_text().splitlines()[0]
    )
    assert rec["ok"] is False and "zai: ConnectionError" in rec["error"]
    doc = json.loads((Path(result["run_dir"]) / "digest.json").read_text())
    rows = {r["arm"]: r for r in doc["standings"]}
    assert rows["m31"]["failure_reasons"] == {"other": 1}


class TimeoutThenAnswerTransport(FailoverTransport):
    """Times out (a read timeout = still-thinking, not stuck) for the first
    ``timeouts`` calls to the minimax host, then answers normally."""

    def __init__(self, timeouts: int = 1) -> None:
        super().__init__(fail_hosts=())
        self.timeouts, self.seen_minimax = timeouts, 0

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        if "api.minimax.io" in url:
            self.seen_minimax += 1
            if self.seen_minimax <= self.timeouts:
                self.calls.append({"url": url, "body": json.loads(body), "timeout": timeout})
                raise TimeoutError("still thinking")
        return super().__call__(url, body, headers, timeout)


def test_v2_ask_escalates_a_timeout_once_then_answers(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, _ = v1_bundle
    config = tmp_path / "slow.json"
    config.write_text(
        json.dumps(
            {
                "out_root": str(tmp_path / "out"),
                "incumbent": "m31",
                "boards": {"plugin": "v2", "bundle": str(path)},
                "outcome": {"plugin": "v2", "sync": 2},
                "ask": {"plugin": "v2", "provider": "minimax-flash", "timeout": 300},
                "protocol": {"draws": 1000, "random_seeds": 200},
                "policies": [{"name": "m31", "kind": "model"}],
            }
        )
    )
    transport = TimeoutThenAnswerTransport(timeouts=1)
    result = longrun.run_from_config(config, shared={"transport": transport}, limit=1)
    assert result["status"] == "finished" and result["complete"] is True
    assert len(transport.calls) == 2  # one escalating retry, never more
    assert [c["timeout"] for c in transport.calls] == [300.0, 600.0]
    rec = json.loads(
        longrun.receipts_path(Path(result["run_dir"]), "m31").read_text().splitlines()[0]
    )
    assert rec["ok"] is True and rec["choice"]
    assert rec["timeout_escalated"] is True and rec["timeout"] == 600.0
    assert rec["provider"] == "minimax-flash"
    assert rec.get("escalated") is None  # distinct from the truncation escalation

    # always timing out: ONE retry, then the failure stands
    stuck = TimeoutThenAnswerTransport(timeouts=99)
    slow = tmp_path / "stuck.json"
    slow.write_text(config.read_text())
    result = longrun.run_from_config(slow, shared={"transport": stuck}, limit=1)
    assert result["status"] == "finished"
    assert len(stuck.calls) == 2
    rec = json.loads(
        longrun.receipts_path(Path(result["run_dir"]), "m31").read_text().splitlines()[0]
    )
    assert rec["ok"] is False and "TimeoutError" in rec["error"]

    # with a fallback: two timed-out minimax attempts, then zai answers
    both = tmp_path / "fb.json"
    both.write_text(
        json.dumps(
            {
                **json.loads(config.read_text()),
                "ask": {
                    "plugin": "v2",
                    "provider": "minimax-flash",
                    "timeout": 300,
                    "fallback_provider": "zai",
                },
            }
        )
    )
    chain = TimeoutThenAnswerTransport(timeouts=99)
    result = longrun.run_from_config(both, shared={"transport": chain}, limit=1)
    assert result["status"] == "finished" and result["complete"] is True
    assert [c["url"].split("/chat")[0] for c in chain.calls] == [
        "https://api.minimax.io/v1",
        "https://api.minimax.io/v1",
        "https://api.z.ai/api/coding/paas/v4",
    ]
    rec = json.loads(
        longrun.receipts_path(Path(result["run_dir"]), "m31").read_text().splitlines()[0]
    )
    assert rec["ok"] is True and rec["fallback"] is True and rec["provider"] == "zai"


def test_v2_ask_refuses_a_bad_fallback_provider(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, _ = v1_bundle
    base = {
        "out_root": str(tmp_path / "out"),
        "incumbent": "m31",
        "boards": {"plugin": "v2", "bundle": str(path)},
        "outcome": {"plugin": "v2", "sync": 2},
        "protocol": {"draws": 1000, "random_seeds": 200},
        "policies": [{"name": "m31", "kind": "model"}],
    }
    transport = HorizonTransport(0, "eod")
    for bad, match in (
        ("nonexistent", "fallback_provider must be one of"),
        ("minimax-flash", "fallback_provider must differ"),
    ):
        config = tmp_path / f"bad-{bad}.json"
        config.write_text(
            json.dumps(
                {
                    **base,
                    "ask": {"plugin": "v2", "provider": "minimax-flash", "fallback_provider": bad},
                }
            )
        )
        with pytest.raises(ValueError, match=match):
            longrun.run_from_config(config, shared={"transport": transport}, limit=1)
    # a policy whose own provider equals the fallback simply never falls over
    same = tmp_path / "same.json"
    same.write_text(
        json.dumps(
            {
                **base,
                "incumbent": "zz",
                "ask": {"plugin": "v2", "provider": "minimax-flash", "fallback_provider": "zai"},
                "policies": [{"name": "zz", "kind": "model", "provider": "zai"}],
            }
        )
    )
    transport = FailoverTransport(("api.minimax.io",))  # zai answers directly
    assert (
        longrun.run_from_config(same, shared={"transport": transport}, limit=1)["status"]
        == "finished"
    )
    assert len(transport.calls) == 1 and "api.z.ai" in transport.calls[0]["url"]


def test_score_run_tallies_failure_reasons_per_arm() -> None:
    boards = boards_for(["2026-06-01", "2026-06-02"])  # 4 boards
    arms = longrun.arms_of([PolicySpec("m", "model", repeats=2)])
    sids = [b.snapshot for b in boards]

    def failed(error: str) -> dict[str, Any]:
        return {"ok": False, "error": error}

    receipts = {
        "m#1": {
            sids[0]: failed("LlmError: minimax-flash: reply truncated at max_tokens"),
            sids[1]: failed("LlmError: minimax-flash: TimeoutError"),
            sids[2]: failed("LlmError: minimax-flash: HTTP 429"),
            sids[3]: failed("LlmError: minimax-flash: no JSON object in model output"),
        },
        "m#2": {s: ok("w") for s in sids},
    }
    doc = longrun.score_run(boards, arms, receipts, OutcomeCache(table_outcome), PROTO)
    rows = {r["arm"]: r for r in doc["standings"]}
    tally = rows["m#1"]["failure_reasons"]
    assert tally == {"truncated": 1, "timeout": 1, "http": 1, "other": 1}
    assert sum(tally.values()) == rows["m#1"]["failures"] == 4  # never disagree
    assert "failure_reasons" not in rows["m#2"]  # a clean arm shows none
    md = longrun.digest_markdown(doc)
    assert "Failure tally" in md
    section = md.split("## Failure tally", 1)[1].split("\n## ", 1)[0]
    assert "- m#1: http 1, other 1, timeout 1, truncated 1" in section  # sorted reasons
    assert "m#2" not in section  # a clean arm is not tallied
    assert "PARTIAL RUN" not in md  # complete run: no partial label
    partial = longrun.score_run(
        boards, arms, receipts, OutcomeCache(table_outcome), PROTO, complete=False
    )
    assert "PARTIAL RUN" in longrun.digest_markdown(partial)
    assert "receipts incomplete" in longrun.digest_markdown(partial)


# ------------------------------------------------------------ pair arms
#
# A rule arm may name TWO board rows as one package, "idA+idB" (the beta-neutral
# short-vol trade). Grammar, outcome resolution, scoring, purge and end-to-end.


def test_pair_receipt_accepts_exactly_two_distinct_board_ids() -> None:
    board = boards_for(["2026-06-01"])[0]  # ids: l, w, n
    rec = longrun._validated(board, "w+l", "hold:5", "")
    assert rec["choice"] == "w+l" and rec["horizon"] == "hold:5"
    assert rec["legs"] == ["w", "l"] and rec["rows"] == [1, 0] and rec["row"] is None
    for bad in ("w+w", "w+zz", "w+l+n", "w +l", "w+", "+w", "l+n+w"):
        refused = longrun._validated(board, bad, None, "")
        assert refused["choice"] is None and refused["row"] is None, bad
        assert "rejected_choice" in refused and "legs" not in refused, bad
    # a single row id keeps its exact pre-pair receipt shape (backward compatible)
    assert longrun._validated(board, "w", None, "") == {
        "note": "",
        "choice": "w",
        "horizon": None,
        "row": 1,
    }


def test_decide_records_a_rule_pair_receipt() -> None:
    board = boards_for(["2026-06-01"])[0]
    arm = longrun.arms_of([PolicySpec("pair", "rule", rule=lambda b: ("w+l", "hold:5"))])[0]
    rec = longrun.decide(arm, board, None)
    assert rec["ok"] is True and rec["choice"] == "w+l" and rec["legs"] == ["w", "l"]
    assert rec["rows"] == [1, 0] and rec["horizon"] == "hold:5" and rec["row"] is None


def test_pair_outcome_sums_legs_both_costs_and_either_no_fill_is_none() -> None:
    # hand oracle over TABLE: w gross 12 net 10 (cost 2), l gross -4 net -4 (cost 0)
    cache = OutcomeCache(table_outcome)
    snap = "s:2026-06-01T10:00"
    assert cache.get(snap, "w+l", None) == (8.0, 6.0)  # gross and net both summed
    assert cache.get(snap, "l+w", None) == (8.0, 6.0)  # leg order does not matter
    assert cache.get(snap, "w+n", None) is None  # n never fills -> the pair is unevaluable
    assert cache.net(snap, "w+n", None) == 0.0
    assert cache.get(snap, "w", None) == (12.0, 10.0)  # singles still resolve unchanged


def test_pair_exit_at_is_the_later_leg() -> None:
    def outcome(snapshot: str, cid: str, horizon: str | None) -> dict[str, float] | None:
        value = table_outcome(snapshot, cid, horizon)
        if value is None:
            return None
        # l exits on the entry day, w one day later, n never
        day = f"2026-06-0{1 + ['l', 'w', 'n'].index(cid)}"
        return {**value, "exit_at": f"{day}T20:00:00+00:00"}

    cache = OutcomeCache(outcome)
    snap = "s:2026-06-01T10:00"
    assert cache.exit_at(snap, "l", None) == "2026-06-01T20:00:00+00:00"
    assert cache.exit_at(snap, "w", None) == "2026-06-02T20:00:00+00:00"
    # the later leg decides even when it is the SECOND one named
    assert cache.exit_at(snap, "l+w", None) == "2026-06-02T20:00:00+00:00"
    assert cache.exit_at(snap, "w+l", None) == "2026-06-02T20:00:00+00:00"
    assert cache.exit_at(snap, "w+n", None) is None  # unevaluable: no exit


def test_single_choice_receipts_keep_their_exact_pre_pair_shape(tmp_path: Path) -> None:
    # old receipts (no legs) load and score exactly as before; the format is
    # additive only, so a resume with an unchanged config behaves identically
    board = boards_for(["2026-06-01"])[0]
    arm = longrun.arms_of([PolicySpec("r", "rule", rule=lambda b: ("w", None))])[0]
    rec = longrun.decide(arm, board, None)
    assert set(rec) == {
        "schema",
        "arm",
        "policy",
        "repeat",
        "kind",
        "snapshot",
        "session",
        "board_rows",
        "note",
        "choice",
        "horizon",
        "row",
        "ok",
        "latency_s",
    }
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(rec) + "\n")
    assert longrun.load_receipts(path) == {board.snapshot: rec}


def test_pair_arm_scores_on_the_paired_scoreboard() -> None:
    boards = boards_for(SESSIONS6[:3])
    specs = [PolicySpec("pair", "rule", rule=lambda b: ("w+l", None)), *longrun.builtin_controls()]
    arms = longrun.arms_of(specs)
    receipts = {
        arm.name: {b.snapshot: longrun.decide(arm, b, None) for b in boards} for arm in arms
    }
    doc = longrun.score_run(
        boards, arms, receipts, OutcomeCache(table_outcome), Protocol(draws=1000, random_seeds=200)
    )
    row = next(r for r in doc["standings"] if r["arm"] == "pair")
    assert row["entered"] == 6 and row["unevaluable"] == 0
    assert row["net_total"] == 36.0  # 6 boards x (10 + -4): both legs, both costs
    assert row["gross_total"] == 48.0
    # the protocol note documents the single-row null comparison, never mixes it
    assert "SINGLE-ROW random null" in doc["protocol"]["pair_arms"]
    assert "pair_arms" in longrun.digest_markdown(doc)


def test_theory_pair_rule_runs_end_to_end_on_the_v1_bundle(
    v1_bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    # the tiny v1 fixture's boards carry only call-side structures, so this
    # plumbing check pairs call_debit with call_credit; the beta-neutral
    # put_credit+call_credit config itself is pinned in the theory and
    # redigest tests
    path, _ = v1_bundle
    config = tmp_path / "pair-v1.json"
    config.write_text(
        json.dumps(
            {
                "out_root": str(tmp_path / "out"),
                "incumbent": "m31",
                "boards": {"plugin": "v1", "bundle": str(path)},
                "outcome": {"plugin": "v1"},
                "ask": {"plugin": "v1", "provider": "minimax-flash"},
                "protocol": {"draws": 1000, "random_seeds": 200},
                "policies": [
                    {"name": "m31", "kind": "model", "repeats": 2},
                    {
                        "name": "call_pair_h5",
                        "kind": "control",
                        "builtin": "theory",
                        "structures": ["call_debit", "call_credit"],
                        "require_all": True,
                        "pair": True,
                        "horizon": "hold:5",
                    },
                ],
            }
        )
    )
    result = longrun.run_from_config(config, shared={"transport": ChoosingTransport(0)}, limit=4)
    assert result["status"] == "finished" and result["complete"] is True
    run_dir = Path(result["run_dir"])
    ctx = longrun.PluginContext(config_dir=tmp_path, shared={"transport": None})
    boards = longrun.plugin("boards", "v1")({"bundle": str(path)}, ctx)
    boards = boards[:4]
    outcome = longrun.plugin("outcome", "v1")({}, ctx)
    receipts = [
        json.loads(line)
        for line in longrun.receipts_path(run_dir, "call_pair_h5").read_text().splitlines()
    ]
    by_snapshot = {r["snapshot"]: r for r in receipts if r.get("ok")}
    # independent oracle from the boards alone: the v1 rows carry no underlying, so the
    # pair joins the FIRST row of each listed structure of every board that holds both
    # (board_order key), else the arm skips
    expected_net = 0.0
    for board in boards:
        firsts = {
            s: next((r["id"] for r in board.rows if r["structure"] == s), None)
            for s in ("call_debit", "call_credit")
        }
        rec = by_snapshot.get(board.snapshot)
        if any(v is None for v in firsts.values()):
            assert rec is None or rec["choice"] is None
            continue
        legs = [firsts["call_debit"], firsts["call_credit"]]
        assert rec is not None and rec["choice"] == "+".join(legs)
        assert rec["legs"] == legs
        values = [outcome(board.snapshot, leg, "hold:5") for leg in legs]
        if any(v is None for v in values):
            continue  # a no-fill on either leg scores 0, counted unevaluable
        expected_net += sum(float(v["net"]) for v in values if v is not None)
    doc = json.loads((run_dir / "digest.json").read_text())
    row = next(r for r in doc["standings"] if r["arm"] == "call_pair_h5")
    assert row["net_total"] == round(expected_net, 2)
    assert row["kind"] == "control" and row["entered"] > 0
