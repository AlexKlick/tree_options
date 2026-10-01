"""SPEC 1 vixfloor: the VIX gate, the gated arms, and the standalone scorer's
plumbing against hand-computed cases (independent oracles: every expected
number below is derived in the test's own arithmetic, never by calling the
module under test). No network, no LLM, no live run dir."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from tree_options.desk import vixfloor
from tree_options.desk.longrun import Board, OutcomeCache, Protocol, rule_fixed_structure
from tree_options.desk.vixfloor import (
    deterministic_receipts,
    evaluate_arm,
    load_outcome_fn,
    load_vix_closes,
    per_entry_mean_ci,
    split_contrast,
    vix_percentiles,
    vixfloor_rule,
)

# ------------------------------------------------------------------ fixtures

ROWS = [
    {"id": "l", "structure": "call_credit", "direction": "bearish"},
    {"id": "w", "structure": "put_credit", "direction": "bullish"},
    {"id": "n", "structure": "call_debit", "direction": "bullish"},
]
SESSIONS3 = ["2026-06-01", "2026-06-02", "2026-06-03"]


def boards_for(sessions: list[str]) -> list[Board]:
    return [
        Board(f"s:{s}T{c}", s, c, [dict(r) for r in ROWS])
        for s in sessions
        for c in ("10:00", "13:00")
    ]


def table_outcome(snapshot: str, candidate: str, horizon: str | None) -> dict[str, float] | None:
    value = {"w": (12.0, 10.0), "l": (-4.0, -4.0), "n": None}[candidate]
    return None if value is None else {"gross": value[0], "net": value[1]}


SMALL_PROTO = Protocol(draws=1000, random_seeds=200, random_horizons=("expiry",))


# --------------------------------------------------------------- the VIX gate


def test_vix_percentiles_hand_computed() -> None:
    # closes: 26th=10, 27th=11, 28th=12, 29th=11, 01st=10, 02nd=13, 03rd=10.5
    closes = {
        date(2026, 5, 26): 10.0,
        date(2026, 5, 27): 11.0,
        date(2026, 5, 28): 12.0,
        date(2026, 5, 29): 11.0,
        date(2026, 6, 1): 10.0,
        date(2026, 6, 2): 13.0,
        date(2026, 6, 3): 10.5,
    }
    out = vix_percentiles(closes, ["2026-06-03", "2026-06-04"], window=5)
    # session 06-03: v = 13 (06-02); window = 11,12,11,10,13 -> four below 13
    assert out["2026-06-03"] == pytest.approx(4 / 5)
    # session 06-04: v = 10.5 (06-03); window = 12,11,10,13,10.5 -> only 10 below
    assert out["2026-06-04"] == pytest.approx(1 / 5)


def test_vix_percentiles_strict_below_and_window_inclusive() -> None:
    closes = {date(2026, 5, 26): 9.0, date(2026, 5, 27): 10.0, date(2026, 5, 28): 10.0}
    # session 05-29: v = 10 (05-28); window = 9,10,10 -> ties NOT below -> 1/3
    assert vix_percentiles(closes, ["2026-05-29"], window=3)["2026-05-29"] == pytest.approx(1 / 3)


def test_vix_percentiles_fail_loud() -> None:
    with pytest.raises(ValueError, match="no VIX session strictly before"):
        vix_percentiles({date(2026, 5, 26): 10.0}, ["2026-05-26"])
    with pytest.raises(ValueError, match="prior VIX sessions"):
        vix_percentiles(
            {date(2026, 5, 26): 10.0, date(2026, 5, 27): 10.0}, ["2026-05-28"], window=252
        )


def test_load_vix_closes(tmp_path: Path) -> None:
    path = tmp_path / "VIX.csv"
    path.write_text(
        "date,open,high,low,close\n2026-05-26,1,2,3,14.5\n2026-05-27,4,5,6,15\n", encoding="utf-8"
    )
    assert load_vix_closes(path) == {date(2026, 5, 26): 14.5, date(2026, 5, 27): 15.0}
    with pytest.raises(ValueError, match="no VIX rows"):
        load_vix_closes(tmp_path / "missing.csv")


# ------------------------------------------------------------------ the arms


def test_gate_boundary_and_split() -> None:
    boards = boards_for(SESSIONS3)
    pct = {"2026-06-01": 0.5, "2026-06-02": 0.4999, "2026-06-03": 0.0}
    lo = vixfloor_rule(pct, side="lo")
    hi = vixfloor_rule(pct, side="hi")
    # cut is strict on the lo side: 0.5 is NOT < 0.5
    assert lo(boards[0]) == (None, None) and hi(boards[0]) == ("w", "expiry")
    assert lo(boards[2]) == ("w", "expiry") and hi(boards[2]) == (None, None)
    assert lo(boards[4]) == ("w", "expiry")


def test_rule_is_survivor_when_gate_open() -> None:
    board = boards_for(SESSIONS3)[0]
    open_pct = {"2026-06-01": 0.1}
    rule = vixfloor_rule(open_pct, side="lo")
    # the ungated survivor picks the FIRST bullish row by board order: "w"
    assert rule(board) == ("w", "expiry")
    gated = vixfloor_rule({"2026-06-01": 0.9}, side="lo")
    assert gated(board) == (None, None)
    with pytest.raises(ValueError, match="no VIX percentile"):
        vixfloor_rule({}, side="lo")(board)


def test_descriptive_base_override() -> None:
    board = boards_for(SESSIONS3)[0]
    pct = {"2026-06-01": 0.1}
    put_credit = vixfloor_rule(
        pct, side="lo", base=rule_fixed_structure("put_credit", horizon="expiry")
    )
    call_debit = vixfloor_rule(
        pct, side="lo", base=rule_fixed_structure("call_debit", horizon="hold:5")
    )
    assert put_credit(board) == ("w", "expiry")
    assert call_debit(board) == ("n", "hold:5")
    with pytest.raises(ValueError, match="side"):
        vixfloor_rule(pct, side="mid")


def test_deterministic_receipts_are_the_aa_twin() -> None:
    boards = boards_for(SESSIONS3)
    pct = {"2026-06-01": 0.1, "2026-06-02": 0.6, "2026-06-03": 0.2}
    rule = vixfloor_rule(pct, side="lo")
    first = deterministic_receipts(rule, boards)
    second = deterministic_receipts(rule, boards)
    assert first == second  # a deterministic rule must agree with itself 100%
    assert first["s:2026-06-01T10:00"] == {
        "ok": True,
        "choice": "w",
        "horizon": "expiry",
        "note": "vixfloor deterministic rule",
    }
    assert first["s:2026-06-02T10:00"]["choice"] is None


# --------------------------------------------------------- the outcome table


def test_load_outcome_fn_semantics(tmp_path: Path) -> None:
    path = tmp_path / "table.jsonl"
    rows = [
        {
            "snapshot": "s1",
            "candidate_id": "a",
            "exit_mode": "expiry",
            "status": "closed",
            "gross": 12.0,
            "net": 10.0,
            "exit_at": "2026-06-05T20:00:00+00:00",
        },
        {
            "snapshot": "s1",
            "candidate_id": "a",
            "exit_mode": "intraday",
            "status": "closed",
            "gross": 5.0,
            "net": 3.0,
            "exit_at": None,
        },
        {
            "snapshot": "s1",
            "candidate_id": "b",
            "exit_mode": "expiry",
            "status": "no_fill",
            "gross": None,
            "net": None,
            "exit_at": None,
        },
        {
            "snapshot": "s1",
            "candidate_id": "c",
            "exit_mode": "expiry",
            "status": "closed",
            "gross": None,
            "net": None,
            "exit_at": None,
        },
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    lookup = load_outcome_fn(path)
    assert lookup("s1", "a", "expiry") == {
        "gross": 12.0,
        "net": 10.0,
        "exit_at": "2026-06-05T20:00:00+00:00",
    }
    assert lookup("s1", "a", None) == {"gross": 5.0, "net": 3.0, "exit_at": None}  # default
    assert lookup("s1", "b", "expiry") is None  # no_fill -> unevaluable
    assert lookup("s1", "c", "expiry") is None  # null net -> unevaluable
    assert lookup("s1", "zz", "expiry") is None  # absent -> unevaluable


# ------------------------------------------------------------- the statistics


def test_per_entry_mean_ci_constant_and_two_session() -> None:
    flat = per_entry_mean_ci(np.full(5, 10.0), np.ones(5), draws=500, seed=7)
    assert flat["n_entries"] == 5 and flat["mean"] == 10.0
    assert flat["ci95"] == [10.0, 10.0]  # every resample: sum(10 k')/k'
    # session sums 20 over 2 entries -> per-entry mean 10 (20+30)/(2+1)
    agg = per_entry_mean_ci(np.array([20.0, 30.0]), np.array([2.0, 1.0]), draws=100, seed=7)
    assert agg["n_entries"] == 3 and agg["mean"] == pytest.approx(50 / 3, abs=0.01)
    two = per_entry_mean_ci(np.array([30.0, 10.0]), np.array([1.0, 1.0]), draws=1000, seed=7)
    # resampling 2 sessions: means are 30 (p=1/4), 20 (p=1/2), 10 (p=1/4)
    assert two["mean"] == 20.0 and two["ci95"] == [10.0, 30.0]
    empty = per_entry_mean_ci(np.zeros(3), np.zeros(3), draws=100, seed=7)
    assert empty["mean"] is None and empty["ci95"] is None


def test_split_contrast_hand_computed() -> None:
    # 4 sessions: lo pair nets 10 each, hi pair nets 30 each, one entry apiece
    nets = np.array([10.0, 10.0, 30.0, 30.0])
    counts = np.ones(4)
    mask = np.array([True, True, False, False])
    out = split_contrast(nets, counts, mask, draws=500, seed=7, perm_draws=500)
    assert out["gap"] == -20.0  # 10 - 30
    assert out["ci95_cluster_bootstrap"] == [-20.0, -20.0]  # both arms constant
    # size-2 splits of {10,10,30,30}: like pairs gap +-20 (2/6), mixed pairs 0
    # (4/6) -> every gap >= -20, and |gap| = 20 only for the like pairs
    assert out["permutation_null"]["p_one_sided_ge"] == 1.0
    assert out["permutation_null"]["share_abs_ge"] == pytest.approx(1 / 3, abs=0.03)
    assert out["permutation_null"]["band95"] == [-20.0, 20.0]
    # degenerate: identical sessions -> gap 0, never beaten
    zero = split_contrast(
        np.full(4, 10.0),
        np.ones(4),
        np.array([True, False, True, False]),
        draws=200,
        seed=7,
        perm_draws=200,
    )
    assert zero["gap"] == 0.0 and zero["permutation_null"]["p_one_sided_ge"] == 1.0


# --------------------------------------------- one arm through the harness


def test_evaluate_arm_hand_computed() -> None:
    boards = boards_for(SESSIONS3)
    # sessions 1-2 lo (4 boards), session 3 hi (2 boards)
    pct = {"2026-06-01": 0.1, "2026-06-02": 0.2, "2026-06-03": 0.9}
    spec = vixfloor.family_policies(pct)[0]  # bull_expiry_vixlo
    assert spec.name == "bull_expiry_vixlo"
    doc, digest = evaluate_arm(spec, boards, table_outcome, SMALL_PROTO, repeats=2)
    arm = doc["arms"]["bull_expiry_vixlo#1"]
    # 4 entered (sessions 1-2), all pick "w" (first bullish row), all fill at 10.0
    assert arm["entered"] == 4 and arm["evaluated"] == 4
    assert arm["net_total"] == 40.0 and arm["net_per_evaluated_entry"] == 10.0
    assert arm["entry_rate"] == round(4 / 6, 4)
    # the null is matched to THIS arm: p_enter 4/6 over the 3-option menu
    assert digest["random_null"]["p_enter"] == round(4 / 6, 4)
    # options mean per board = (-4 + 10 + 0) / 3 = 2; 6 boards -> expected
    # total = 6 * (4/6) * 2 = 8
    assert digest["random_null"]["expected_total"] == 8.0
    assert arm["vs_random"]["diff_total"] == 32.0
    # the A/A twin: identical deterministic repeats
    assert doc["aa"]["status"] == "valid" and doc["aa"]["agreement"] == 1.0
    assert (
        doc["arms"]["bull_expiry_vixlo#1"]["net_total"]
        == doc["arms"]["bull_expiry_vixlo#2"]["net_total"]
    )
    hi_doc, _ = evaluate_arm(vixfloor.family_policies(pct)[1], boards, table_outcome, SMALL_PROTO)
    hi = hi_doc["arms"]["bull_expiry_vixhi"]
    assert hi["entered"] == 2 and hi["evaluated"] == 2 and hi["net_total"] == 20.0


# ------------------------------------------------- the full evaluation (small)


def write_small_corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A VIX CSV whose percentiles gate sessions (lo, lo, hi) + boards + table.

    D_0..D_251 close 100 flat; D_252 = 50; D_253 = 50; D_254 = 300; D_255 = 100.
    s1 = D_253: v=50, window 100x251 + 50 -> 0 below -> pct 0.0 (lo).
    s2 = D_254: v=50, window 100x250 + 50 + 50 -> 0 below -> pct 0.0 (lo).
    s3 = D_255: v=300, window 100x249 + 50 + 50 + 300 -> 251 below -> 0.996 (hi).
    """
    base = date(2025, 1, 1)
    lines = ["date,open,high,low,close"]
    for k in range(256):
        close = 100.0
        if k in (252, 253):
            close = 50.0
        elif k == 254:
            close = 300.0
        lines.append(f"{(base + timedelta(days=k)).isoformat()},1,2,3,{close}")
    vix = tmp_path / "VIX.csv"
    vix.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sessions = [(base + timedelta(days=k)).isoformat() for k in (253, 254, 255)]
    boards = boards_for(sessions)
    boards_path = tmp_path / "boards.jsonl"
    with boards_path.open("w", encoding="utf-8") as stream:
        for b in boards:
            stream.write(
                json.dumps(
                    {
                        "snapshot": b.snapshot,
                        "session": b.session,
                        "clock": b.clock,
                        "rows": b.rows,
                        "context": None,
                    }
                )
                + "\n"
            )
    table = tmp_path / "table.jsonl"
    with table.open("w", encoding="utf-8") as stream:
        for b in boards:
            for cid, row in (
                ("w", {"status": "closed", "gross": 12.0, "net": 10.0}),
                ("l", {"status": "closed", "gross": -4.0, "net": -4.0}),
                ("n", {"status": "no_fill", "gross": None, "net": None}),
            ):
                for mode in ("intraday", "eod", "hold:5", "expiry"):
                    stream.write(
                        json.dumps(
                            {"snapshot": b.snapshot, "candidate_id": cid, "exit_mode": mode, **row}
                        )
                        + "\n"
                    )
    return boards_path, vix, table


def test_run_evaluation_small_corpus(tmp_path: Path) -> None:
    boards_path, vix, table = write_small_corpus(tmp_path)
    doc = vixfloor.run_evaluation(
        boards_path, vix, table, draws=1000, seed=7, random_seeds=200, perm_draws=200, cutoff=None
    )
    assert doc["vix_gate"]["sessions_below_cut"] == 2
    assert doc["vix_gate"]["sessions_at_or_above"] == 1
    assert doc["survivor"]["entered"] == 6 and doc["survivor"]["evaluated"] == 6
    assert doc["survivor"]["net_total"] == 60.0
    assert doc["survivor"]["net_per_evaluated_entry"] == 10.0
    lo = doc["arms"]["bull_expiry_vixlo"]["arms"]["bull_expiry_vixlo#1"]
    hi = doc["arms"]["bull_expiry_vixhi"]["arms"]["bull_expiry_vixhi#1"]
    assert lo["entered"] == 4 and lo["evaluated"] == 4 and lo["net_total"] == 40.0
    assert hi["entered"] == 2 and hi["evaluated"] == 2 and hi["net_total"] == 20.0
    # the 6-board corpus is far under the pre-registered floors -> NOT_EVALUABLE
    assert doc["arms"]["bull_expiry_vixlo"]["floor_met"] is False
    assert doc["family"]["all_arms_above_n_floor"] is False
    assert doc["family"]["not_evaluable_reason"] is not None
    # both arms average 10.0 per entry -> the contrast gap is exactly 0
    assert doc["secondary_contrast"]["gap"] == 0.0
    assert doc["secondary_contrast"]["permutation_null"]["p_one_sided_ge"] == 1.0
    # Holm over the 2-arm family, keys = the pre-registered names
    assert set(doc["family"]["holm_p"]) == {"bull_expiry_vixlo", "bull_expiry_vixhi"}
    # descriptive arms: put_credit also picks "w"; call_debit picks the no-fill "n"
    assert doc["descriptive"]["put_credit_expiry_vixlo"]["net_total"] == 40.0
    assert doc["descriptive"]["call_debit_hold5_vixlo"]["evaluated"] == 0
    assert doc["descriptive"]["call_debit_hold5_vixlo"]["net_total"] == 0.0
    assert doc["schema"] == "desk-vixfloor-eval/1"
    assert OutcomeCache(table_outcome).get("s", "w", None) is not None


def test_n_floors_are_the_preregistered_ones() -> None:
    assert vixfloor.N_FLOORS == {"bull_expiry_vixlo": 300, "bull_expiry_vixhi": 120}
    assert vixfloor.CUT == 0.5 and vixfloor.WINDOW == 252


def test_split_bootstrap_keeps_repeated_sampled_sessions(monkeypatch):
    class Draws:
        def integers(self, low, high, size):
            return np.array([0, 0, 1])

        def choice(self, total, size, replace):
            return np.arange(size)

    monkeypatch.setattr(np.random, "default_rng", lambda seed: Draws())
    result = split_contrast(
        np.array([3.0, 30.0, 90.0, 6.0, 60.0, 180.0]),
        np.array([1.0, 2.0, 3.0, 1.0, 2.0, 3.0]),
        np.array([True, True, True, False, False, False]),
        draws=1,
        seed=1,
        perm_draws=1,
    )
    # Repeated index0 must count twice: (3+3+30)/(1+1+2) - (6+6+60)/(1+1+2).
    assert result["ci95_cluster_bootstrap"] == [-9.0, -9.0]


def test_outcome_loader_rejects_conflicting_identity(tmp_path):
    path = tmp_path / "table.jsonl"
    row = {
        "snapshot": "s",
        "candidate_id": "x",
        "exit_mode": "expiry",
        "status": "closed",
        "gross": 12,
        "net": 10,
    }
    path.write_text(json.dumps(row) + "\n" + json.dumps({**row, "net": 11}) + "\n")
    with pytest.raises(ValueError, match="collision"):
        load_outcome_fn(path)


def test_result_records_exact_loaded_source_custody_and_refuses_promotion(tmp_path):
    import hashlib

    boards, vix, table = write_small_corpus(tmp_path)
    result = vixfloor.run_evaluation(
        boards, vix, table, draws=1000, random_seeds=200, perm_draws=100
    )
    assert result["verdict"] == "NOT_PROMOTABLE"
    assert result["execution_authorized"] is False
    assert result["exact_external_economics"] is False
    assert result["inputs"]["source_hashes"] == {
        "boards_sha256": hashlib.sha256(boards.read_bytes()).hexdigest(),
        "vix_sha256": hashlib.sha256(vix.read_bytes()).hexdigest(),
        "outcome_table_sha256": hashlib.sha256(table.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_vix_close_invalid_values_fail_closed(tmp_path, value):
    path = tmp_path / "VIX.csv"
    path.write_text(f"date,close\n2026-05-26,{value}\n")
    with pytest.raises(ValueError, match="finite and positive"):
        load_vix_closes(path)


def test_vixfloor_payload_binds_real_source_and_shared_scorer(tmp_path, monkeypatch):
    boards, vix, table = write_small_corpus(tmp_path)
    result = vixfloor.run_evaluation(
        boards, vix, table, draws=1000, random_seeds=200, perm_draws=100
    )
    assert result["engine"]["sha256"] == vixfloor.vixfloor_engine_manifest()["sha256"]
    original = vixfloor.vixfloor_engine_manifest()
    own = tmp_path / "vixfloor-source"
    own.write_bytes(b"first vixfloor source")
    monkeypatch.setattr(vixfloor, "__file__", str(own))
    first = vixfloor.vixfloor_engine_manifest()
    own.write_bytes(b"changed vixfloor source")
    assert vixfloor.vixfloor_engine_manifest()["sha256"] != first["sha256"] != original["sha256"]
    monkeypatch.setattr(vixfloor, "longrun_engine_identity", lambda: "a" * 64)
    first = vixfloor.vixfloor_engine_manifest()
    monkeypatch.setattr(vixfloor, "longrun_engine_identity", lambda: "b" * 64)
    assert vixfloor.vixfloor_engine_manifest()["sha256"] != first["sha256"]
