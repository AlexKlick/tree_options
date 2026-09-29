"""Environment v1 byte-identity pins (desk env v2 is ADDITIVE).

The digests below were computed on dd1fe8c, before the v2 outcome engine
and board existed. v2 adds new functions; the v1 board, prompt, choice
parser, replay accounting, decision packet and lab-run document must stay
byte-identical, so every consumer of v1 evidence (challenge, hindsight,
GEPA, scoreboard) keeps reading the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_hindsight import DAYS, multi_day_bundle
from tests.unit.test_desk_intraday_action_graph import _bundle
from tests.unit.test_desk_lab import UNDER, FakeTransport
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk import lab

T0 = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
STRUCTURES = ("put_credit", "put_debit", "call_credit", "call_debit")


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def v1_candidates() -> list[dict[str, Any]]:
    """20 deterministic candidates with distinct reward/risk (v1 sorts by it)."""
    out = []
    for i in range(20):
        structure = STRUCTURES[i % 4]
        rr = f"{(i * 37) % 23 / 4 + 0.25:.2f}"
        out.append({"id": f"{i:016x}", "structure": structure, "underlying": "SPY",
                    "expiry": "2026-10-16", "long": "O:SPY261016C00500000",
                    "short": "O:SPY261016C00505000", "width": "5",
                    "observed_premium": f"{1 + i / 10:.2f}", "max_loss_proxy": str(100 + i),
                    "max_gain_proxy": str(200 - i), "reward_to_risk_proxy": rr,
                    "long_recent_trade_move": None if i % 3 else "0.0123",
                    "short_recent_trade_move": "0.0045",
                    "data_kind": "last-traded-minute-close"})
    return out


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "test-key-material")


def test_v1_board_rows_prompt_and_parser_are_byte_identical() -> None:
    rows = lab.board_rows({"candidates": v1_candidates()})
    assert len(rows) == lab.BOARD_ROWS == 12
    assert _digest(rows) == PINS["board_rows"]
    assert _digest(lab.board_prompt(rows)) == PINS["board_prompt"]
    assert _digest(lab.board_prompt(rows, policy_prompt="Custom policy.")) == PINS["board_prompt_custom"]
    assert lab.POLICY_SENTENCE == (
        "You are a paper-trading policy choosing ONE defined-risk option "
        "spread board row, or skipping.")
    assert lab.parse_choice({"choice": "c0", "note": "n"}, {"c0"}) == ("c0", "n")
    assert lab.parse_choice({"choice": "zz"}, {"c0"}) == (None, "unknown id rejected: zz")
    assert lab.parse_choice({"choice": None, "horizon": "eod"}, {"c0"}) == (None, "")


def test_v1_replay_and_decision_packet_are_byte_identical() -> None:
    raw = multi_day_bundle(*DAYS)
    assert _digest(iag.replay(raw, DAYS)) == PINS["replay_no_trade"]
    assert _digest(iag.replay(raw, DAYS, policy="call_debit")) == PINS["replay_call_debit"]
    assert _digest(iag.replay(raw, DAYS, policy="put_credit")) == PINS["replay_put_credit"]
    packet = iag.decision_packet(raw, DAYS[0], "10:00")
    assert _digest(packet) == PINS["packet"]
    choice = sorted(c["id"] for c in packet["candidates"])[0]
    decided = iag.replay(raw, DAYS, {f"s:{DAYS[0]}T10:00": choice})
    assert _digest(decided) == PINS["replay_decided"]


def test_v1_lab_run_document_is_byte_identical(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(_bundle(date(2026, 9, 24))))
    document = lab.run_lab(lab.LabConfig(bundle=bundle, policy="model:zai", sessions=1,
                                         lab_root=tmp_path / "lab"),
                           windows=UNDER, transport=FakeTransport("first"), now=T0)
    stable = {k: v for k, v in document.items() if k != "run_dir"}
    stable["receipts"] = [{k: v for k, v in r.items() if k != "latency_s"}
                          for r in document["receipts"]]
    assert _digest(stable) == PINS["lab_run"]


def test_v1_model_provider_mapping_is_unchanged() -> None:
    assert lab.model_provider("gepa:abc123def456") == "zai"
    assert lab.model_provider("model:zai") == "zai"
    assert lab.model_provider("model:minimax") == "minimax"
    assert lab.model_provider("model:local") == "local"
    with pytest.raises(ValueError):
        lab.model_provider("no_trade")


PINS = {  # computed on dd1fe8c (pre-v2); a v1 change must be deliberate, never a side effect
    "board_rows": "a81d54268ef83933acdeba9a8f6b2adc257904e7fb3ebce92c5e6965c3746970",
    "board_prompt": "26e5a0cd8561c3329722938fe06d361dcdeb442a3aa35ea5e93a28999e95d36a",
    "board_prompt_custom": "5d76298e6998e7a64efb68baabb6047f5d703f2726b8534e6b815d5b138f34e1",
    "replay_no_trade": "290b23337a9b56fcf70429495b9f1e9b234da3da088e52b425a0375224d8fcaf",
    "replay_call_debit": "b02f76e9ef8c4f8926ef51c448845a7a2726d2bcaa0d4f456b93e027109bf0c1",
    "replay_put_credit": "62ddcbcbe8449dbacee6d7e434923e447ce39a147abe9a8e2d579a9705cb8f36",
    "packet": "bdfd93b3a3de8d28da44393366573dc9b985ce6acb64fcd3b2f5d8fc92cdf850",
    "replay_decided": "b11945cab92b7fe39c9b7fb3e355eeec82c095a1eddea13d812f64aa70eb41ca",
    "lab_run": "884ccd09b3a545b9efc43b7a0f0bc74b9ec5f05f643defc39f0ff816f0169021",
}
