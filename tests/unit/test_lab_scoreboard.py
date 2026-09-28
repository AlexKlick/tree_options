"""The lab scoreboard: aggregation math and the never-promoted advisory."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from tree_options.desk.lab_scoreboard import (
    ADVISORY_MIN_RUNS,
    aggregate,
    best_advisory,
)


def _run(lab: Path, name: str, policy: str, *, pnl: str, minimum: str = "5000",
         boards: int = 10, entered: int = 5, wins: int = 2, calls: int = 10,
         failures: int = 0) -> None:
    run_dir = lab / name
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps({
        "policy": policy, "boards_shown": boards, "model_calls": calls,
        "model_failures": failures,
        "summary": {"entered": entered, "modeled_wins": wins,
                    "modeled_losses": entered - wins,
                    "closed_capital_proxy": str(Decimal("5000") + Decimal(pnl)),
                    "minimum_closed_capital_proxy": minimum}}))


def test_aggregate_folds_runs_and_ignores_torn_files(tmp_path: Path):
    lab = tmp_path / "lab"
    _run(lab, "a-model-zai", "model:zai", pnl="-100", minimum="4800")
    _run(lab, "b-model-zai", "model:zai", pnl="+50", minimum="4900", wins=4)
    _run(lab, "c-no-trade", "no_trade", pnl="0", boards=0, entered=0, wins=0, calls=0)
    (lab / "torn").mkdir()
    (lab / "torn" / "summary.json").write_text("{not json")
    board = aggregate(lab)
    zai = board["policies"]["model:zai"]
    assert (zai["runs"], zai["boards"], zai["model_calls"]) == (2, 20, 20)
    assert zai["closed_pnl_sum"] == "-50.00" or Decimal(zai["closed_pnl_sum"]) == Decimal("-50")
    assert zai["worst_minimum_capital"] == "4800"
    assert zai["kinds"] == ["model"]
    assert board["policies"]["no_trade"]["kinds"] == ["rules"]
    assert "torn" not in json.dumps(board)


def test_advisory_needs_min_runs_and_is_never_promoted(tmp_path: Path):
    lab = tmp_path / "lab"
    for i in range(ADVISORY_MIN_RUNS - 1):
        _run(lab, f"a{i}-model-zai", "model:zai", pnl="+999")
    assert best_advisory(aggregate(lab)) is None
    for i in range(ADVISORY_MIN_RUNS):
        _run(lab, f"b{i}-no-trade", "no_trade", pnl="0", calls=0)
    # a third policy with ONE big-pnl run stays below the minimum: excluded
    _run(lab, "c-model-minimax", "model:minimax", pnl="+10")
    advice = best_advisory(aggregate(lab))
    assert advice is not None and advice["policy"] == "no_trade"
    assert advice["promoted"] is False
    assert advice["stats"]["runs"] == ADVISORY_MIN_RUNS


def test_highest_pnl_wins_and_empty_lab_has_no_advice(tmp_path: Path):
    lab = tmp_path / "lab"
    for i in range(ADVISORY_MIN_RUNS):
        _run(lab, f"x{i}-model-minimax", "model:minimax", pnl="-20")
        _run(lab, f"y{i}-no-trade", "no_trade", pnl="0", calls=0)
    advice = best_advisory(aggregate(lab))
    assert advice["policy"] == "no_trade"  # -0 beats -60: even a loser-less baseline
    assert best_advisory(aggregate(tmp_path / "empty")) is None
