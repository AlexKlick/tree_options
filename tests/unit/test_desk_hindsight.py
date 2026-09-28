"""The hindsight oracle: replay cross-checks, gap math, exclusion rules.

The oracle property: selecting any candidate hindsight scores, via
``iag.replay(decisions={snapshot: candidate_id})`` over the same sessions,
must reproduce EXACTLY the outcome ``board_outcomes`` claims for that id.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_intraday_action_graph import HIGH, LOW, _bar, _bundle
from tests.unit.test_desk_lab import UNDER, FakeTransport
from tree_options.desk import hindsight
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.lab import LabConfig, run_lab

T0 = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
DAYS = [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]


def multi_day_bundle(*days: date) -> dict[str, Any]:
    """The single-day fixture merged over consecutive days (bars append)."""
    low: list[dict[str, Any]] = []
    high: list[dict[str, Any]] = []
    for day in days:
        one = _bundle(day)
        low.extend(one["contracts"][LOW]["results"])
        high.extend(one["contracts"][HIGH]["results"])
    return {"schema": "desk-option-minute-bars/1", "contracts": {
        LOW: {"ticker": LOW, "timespan": "minute", "results": low},
        HIGH: {"ticker": HIGH, "timespan": "minute", "results": high}}}


class ChoosingTransport:
    """Picks the row at ``index`` of each board (0 = top reward/risk)."""

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body)})
        rows = json.loads(json.loads(body)["messages"][0]["content"])["board"]
        choice = rows[self.index]["id"] if self.index < len(rows) else None
        content = json.dumps({"choice": choice, "note": "fixed index"})
        envelope = {"choices": [{"message": {"content": content},
                                 "finish_reason": "stop"}]}
        return 200, json.dumps(envelope).encode()


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "test-key-material")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "test-key-material-2")


def _exit_pnl(result: dict[str, Any], snapshot: str) -> Decimal | None:
    """The modeled-exit PnL replay attributed to this snapshot's action."""
    action = f"a:{snapshot}"
    closes = [edge["to"] for edge in result["edges"]
              if edge["from"] == action and edge["kind"] == "later_mark"]
    assert len(closes) <= 1
    if not closes:
        return None
    node = next(n for n in result["nodes"] if n["id"] == closes[0])
    return Decimal(node["pnl"])


def _lab_run(raw: dict[str, Any], tmp_path: Path, transport: Any,
             boards_cap: int = 24) -> dict[str, Any]:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(raw))
    return run_lab(LabConfig(bundle=bundle_path, policy="model:zai", sessions=1,
                             boards_cap=boards_cap,
                             lab_root=tmp_path / "lab"),
                   windows=UNDER, transport=transport, now=T0)


# ------------------------------------------------------------------ oracle


def test_board_outcomes_replay_oracle_single_day() -> None:
    day = date(2026, 9, 24)
    raw = _bundle(day)
    outcomes = hindsight.board_outcomes(raw, day, "10:00", sessions=[day])
    assert set(outcomes.values()) == {Decimal("10.0"), Decimal("-10.0")}
    snapshot = f"s:{day}T10:00"
    for candidate_id, claimed in outcomes.items():
        result = iag.replay(raw, [day], {snapshot: candidate_id})
        assert _exit_pnl(result, snapshot) == claimed, candidate_id


def test_board_outcomes_replay_oracle_every_board_of_the_window() -> None:
    raw = multi_day_bundle(*DAYS)
    for day in DAYS:
        for clock in iag.schedule_for(day):
            outcomes = hindsight.board_outcomes(raw, day, clock, sessions=DAYS)
            snapshot = f"s:{day}T{clock}"
            for candidate_id, claimed in outcomes.items():
                result = iag.replay(raw, DAYS, {snapshot: candidate_id})
                assert _exit_pnl(result, snapshot) == claimed, candidate_id


def test_no_outcome_candidates_are_absent_never_zero() -> None:
    day = date(2026, 9, 24)
    raw = _bundle(day)
    # the last board of the day HAS candidates but no later bars: replay
    # would refuse the entry (missing_later_entry_bars), so no outcome exists
    assert iag.decision_packet(raw, day, "10:45")["candidates"]
    assert hindsight.board_outcomes(raw, day, "10:45", sessions=[day]) == {}
    # the default window (every bundle session from the day on) agrees
    assert hindsight.board_outcomes(raw, day, "10:45") == {}


def test_open_at_window_end_is_not_an_outcome() -> None:
    day = date(2026, 9, 24)
    # entry fills exist (the 14:01 bars) but every later bar is stale at the
    # next snapshot, so the trade never closes inside the window: replay
    # reports it as open_at_end with no final PnL, so there is no outcome
    raw = {"schema": "desk-option-minute-bars/1", "contracts": {
        LOW: {"ticker": LOW, "timespan": "minute", "results": [
            _bar(day, 13, 59, "4"), _bar(day, 14, 1, "4.1"),
            _bar(day, 14, 2, "4.15")]},
        HIGH: {"ticker": HIGH, "timespan": "minute", "results": [
            _bar(day, 13, 59, "2"), _bar(day, 14, 1, "2.1"),
            _bar(day, 14, 2, "2.15")]}}}
    packet = iag.decision_packet(raw, day, "10:00")
    assert packet["candidates"]  # the board is real
    assert hindsight.board_outcomes(raw, day, "10:00", sessions=[day]) == {}


def test_board_outcomes_rejects_clocks_outside_the_window() -> None:
    day = date(2026, 9, 24)
    with pytest.raises(ValueError):
        hindsight.board_outcomes(_bundle(day), day, "10:07")
    with pytest.raises(ValueError):
        hindsight.board_outcomes(_bundle(day), day, "10:00",
                                 sessions=[date(2026, 9, 23)])


# --------------------------------------------------------------- gap report


def test_gap_math_chosen_vs_best_with_feature_rows(tmp_path: Path) -> None:
    day = date(2026, 9, 24)
    raw = _bundle(day)
    # rows[1] is the credit orientation: outcome -10 while the best (debit)
    # achieves +10 on the same board
    document = _lab_run(raw, tmp_path, ChoosingTransport(1))
    report = hindsight.gap_report(document, raw)
    assert report["totals"]["boards"] == 1  # the 10:45 board has no outcomes
    board = report["boards"][0]
    assert board["snapshot"] == f"s:{day}T10:00"
    assert board["chosen"] is not None
    assert Decimal(board["chosen_outcome"]) == Decimal("-10.0")
    assert Decimal(board["best_outcome"]) == Decimal("10.0")
    assert Decimal(board["gap"]) == Decimal("20.0")
    assert board["best_in_shown_rows"] is True
    # the feature rows reuse lab.board_rows fields, aliased
    fields = {"id", "structure", "width", "premium", "max_loss", "max_gain",
              "reward_risk", "long_recent_move", "short_recent_move",
              "data_kind"}
    assert set(board["chosen_row"]) == fields
    assert set(board["best_row"]) == fields
    assert board["chosen_row"]["id"] == board["chosen"]
    assert board["best_row"]["id"] == board["best"]
    assert Decimal(report["totals"]["gap_sum"]) == Decimal("20.0")


def test_skip_and_unknown_choices_gap_at_the_best_outcome(tmp_path: Path) -> None:
    day = date(2026, 9, 24)
    raw = _bundle(day)
    document = _lab_run(raw, tmp_path, FakeTransport("bogus"))
    report = hindsight.gap_report(document, raw)
    board = report["boards"][0]
    assert board["chosen"] is None
    assert board["chosen_outcome"] is None
    assert board["chosen_row"] is None
    assert Decimal(board["gap"]) == Decimal(board["best_outcome"]) == Decimal("10.0")
    assert report["totals"]["entered_choices"] == 0


def test_no_outcome_boards_are_excluded_not_zeroed(tmp_path: Path) -> None:
    day = date(2026, 9, 24)
    raw = _bundle(day)
    document = _lab_run(raw, tmp_path, FakeTransport("first"), boards_cap=2)
    assert len(document["receipts"]) == 2  # both 10:00 and 10:45 were shown
    report = hindsight.gap_report(document, raw)
    assert [b["snapshot"] for b in report["boards"]] == [f"s:{day}T10:00"]


def test_top_gaps_orders_by_gap_desc_deterministically(tmp_path: Path) -> None:
    day = date(2026, 9, 24)
    raw = _bundle(day)
    report = hindsight.gap_report(_lab_run(raw, tmp_path, FakeTransport("bogus")),
                                  raw)
    ranked = hindsight.top_gaps(report, limit=5)
    assert ranked
    gaps = [Decimal(b["gap"]) for b in ranked]
    assert gaps == sorted(gaps, reverse=True)
    assert all(set(b) >= {"snapshot", "chosen", "best", "gap", "chosen_row",
                          "best_row"} for b in ranked)
