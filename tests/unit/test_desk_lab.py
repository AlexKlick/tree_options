"""The historical lab: quota-gated model runs over the frozen bundle.

The model transport is faked (no network, no keys burned in tests); the
oracle for a run's accounting is the replay module itself, called
independently with the same decisions.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_intraday_action_graph import _bundle
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.lab import (
    BURN_NOTE,
    LabConfig,
    ask_board,
    board_prompt,
    board_rows,
    burn_allowed,
    latest_sessions,
    parse_choice,
    run_lab,
)
from tree_options.trex.grant_policy import WINDOWS_SCHEMA, QuotaWindow

T0 = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
UNDER = (QuotaWindow(name="zai", actual_left_pct=Decimal("94.0"),
                     planned_left_pct=Decimal("51.9")),)
ON_PLAN = (QuotaWindow(name="zai", actual_left_pct=Decimal("40.0"),
                       planned_left_pct=Decimal("50.0")),)


def _windows_file(path: Path) -> Path:
    path.write_text(json.dumps({
        "schema": WINDOWS_SCHEMA,
        "windows": [{"name": "zai", "actual_left_pct": "94.0",
                     "planned_left_pct": "51.9"}]}))
    return path


class FakeTransport:
    """Returns OpenAI-shaped completions; the reply picks board row ``mode``.

    mode: 'first' echoes the first row id, 'bogus' an unknown id,
    'garbage' a non-JSON reply, 'http500' an HTTP failure."""

    def __init__(self, mode: str = "first") -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body)})
        if self.mode == "http500":
            return 500, b""
        prompt = json.loads(body)["messages"][0]["content"]
        rows = json.loads(prompt)["board"]
        if self.mode == "garbage":
            content = "I refuse your format"
        elif self.mode == "bogus":
            content = json.dumps({"choice": "not-a-real-id", "note": "x"})
        else:
            content = json.dumps({"choice": rows[0]["id"], "note": "top rr"})
        envelope = {"choices": [{"message": {"content": content},
                                 "finish_reason": "stop"}]}
        return 200, json.dumps(envelope).encode()


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_bundle(date(2026, 9, 24))))
    return path


@pytest.fixture(autouse=True)
def _fake_zai_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "test-key-material")


# ------------------------------------------------------------------ gates


def test_burn_gate_is_under_using_only():
    assert burn_allowed(UNDER)
    assert not burn_allowed(ON_PLAN)
    assert not burn_allowed(())


def test_a_model_policy_without_quota_headroom_skips(bundle, tmp_path):
    config = LabConfig(bundle=bundle, policy="model:zai", lab_root=tmp_path / "lab")
    document = run_lab(config, windows=ON_PLAN, now=T0)
    assert (document["status"], document["reason"]) == ("skipped", BURN_NOTE)
    assert not (tmp_path / "lab").exists() or not list((tmp_path / "lab").iterdir())


def test_a_rules_policy_runs_without_quota(bundle, tmp_path):
    config = LabConfig(bundle=bundle, policy="no_trade", lab_root=tmp_path / "lab")
    document = run_lab(config, windows=(), now=T0, transport=FakeTransport())
    assert document["status"] == "ok"
    assert document["model_calls"] == 0
    assert document["summary"]["entered"] == 0


# ------------------------------------------------------------- model runs


def test_model_choices_flow_into_the_replay_accounting(bundle, tmp_path):
    transport = FakeTransport("first")
    config = LabConfig(bundle=bundle, policy="model:zai", sessions=1,
                       lab_root=tmp_path / "lab")
    document = run_lab(config, windows=UNDER, transport=transport, now=T0)
    assert document["status"] == "ok"
    assert document["model_calls"] == len(transport.calls) >= 1
    assert document["model_failures"] == 0
    # oracle: the same decisions through replay() reproduce the summary
    decisions = {r["snapshot"]: r["choice"] for r in document["receipts"]
                 if r.get("choice")}
    raw = json.loads(bundle.read_bytes())
    sessions = latest_sessions(raw, 1)
    expected = iag.replay(raw, sessions, decisions)
    assert document["summary"]["entered"] == expected["entered"]
    assert document["summary"]["closed_capital_proxy"] == expected["closed_capital_proxy"]
    # the run is evidence on disk
    run_dir = Path(document["run_dir"])
    assert (run_dir / "summary.json").exists() and (run_dir / "receipts.jsonl").exists()
    assert transport.calls[0]["url"].endswith("/chat/completions")
    assert "Bearer" not in json.dumps(document)  # keys never leak into evidence


def test_a_bogus_choice_is_rejected_not_adopted(bundle, tmp_path):
    config = LabConfig(bundle=bundle, policy="model:zai", sessions=1,
                       lab_root=tmp_path / "lab")
    document = run_lab(config, windows=UNDER, transport=FakeTransport("bogus"), now=T0)
    assert document["model_failures"] == 0  # the call succeeded; the CHOICE was refused
    assert all(r["choice"] is None for r in document["receipts"])
    assert any("unknown id rejected" in (r.get("note") or "") for r in document["receipts"])
    assert document["summary"]["entered"] == 0


def test_provider_failures_are_recorded_and_the_run_continues(bundle, tmp_path):
    config = LabConfig(bundle=bundle, policy="model:zai", sessions=1,
                       boards_cap=3, lab_root=tmp_path / "lab")
    document = run_lab(config, windows=UNDER, transport=FakeTransport("http500"),
                       now=T0)
    assert document["model_calls"] >= 1
    assert document["model_failures"] == document["model_calls"]
    assert all("HTTP 500" in (r.get("error") or "") for r in document["receipts"])
    assert document["summary"]["entered"] == 0


def test_two_runs_never_share_a_directory(bundle, tmp_path):
    root = tmp_path / "lab"
    first = run_lab(LabConfig(bundle=bundle, policy="model:zai", sessions=1,
                              lab_root=root), windows=UNDER,
                    transport=FakeTransport(), now=T0)
    second = run_lab(LabConfig(bundle=bundle, policy="model:zai", sessions=1,
                               lab_root=root), windows=UNDER,
                     transport=FakeTransport(), now=T0)
    assert first["run_dir"] != second["run_dir"]


# ------------------------------------------------------------------ boards


def test_board_rows_are_capped_aliased_and_choice_validated():
    rows = [{"id": f"c{i}", "structure": "put_credit", "width": "2",
             "observed_premium": "1.0", "max_loss_proxy": "100", "max_gain_proxy": "100",
             "reward_to_risk_proxy": "1.00", "long_recent_trade_move": None,
             "short_recent_trade_move": None, "data_kind": "x"} for i in range(30)]
    board = board_rows({"candidates": rows})
    assert len(board) == 12 and "ticker" not in json.dumps(board).lower()
    prompt = board_prompt(board)
    content = prompt[0]["content"]
    assert '\\"choice\\"' in content and '"board"' in content  # the task is JSON-embedded
    assert parse_choice({"choice": "c0"}, {"c0"}) == ("c0", "")
    assert parse_choice({"choice": None}, {"c0"}) == (None, "")
    picked, note = parse_choice({"choice": "zz", "note": "hi"}, {"c0"})
    assert (picked, note) == (None, "unknown id rejected: zz")


def test_ask_board_uses_the_injected_transport():
    transport = FakeTransport("first")
    reply = ask_board("zai", board_rows({"candidates": [
        {"id": "c1", "structure": "put_credit", "width": "2", "observed_premium": "1.0",
         "max_loss_proxy": "100", "max_gain_proxy": "100", "reward_to_risk_proxy": "1.00",
         "long_recent_trade_move": None, "short_recent_trade_move": None,
         "data_kind": "x"}]}), transport=transport)
    assert reply["choice"].startswith("c") or reply["choice"] is None
    assert len(transport.calls) == 1
