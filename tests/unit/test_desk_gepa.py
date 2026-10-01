"""GEPA lane: Pareto purity, reflection parsing, archive round-trip, and
the lab.py policy-prompt wiring (additive changes)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_intraday_action_graph import _bundle
from tests.unit.test_desk_lab import ON_PLAN, UNDER, FakeTransport
from tree_options.desk import gepa, lab
from tree_options.desk.lab import LabConfig, run_lab
from tree_options.trex.discovery.llm import PROVIDERS

T0 = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)


class ReflectionTransport:
    """Returns one fixed JSON reply; records every call."""

    def __init__(self, reply: dict[str, Any] | None = None, status: int = 200) -> None:
        self.reply = reply or {}
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body)})
        content = json.dumps(self.reply) if self.status == 200 else ""
        envelope = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        return self.status, json.dumps(envelope).encode()


def _policy(
    prompt: str, *, pnl: str = "0", worst: str | None = None, runs: int = 1
) -> dict[str, Any]:
    record = gepa.new_policy(prompt, generation=0, parents=[], created_by="test")
    stats = gepa.empty_stats()
    stats.update({"closed_pnl_sum": pnl, "worst_minimum_capital": worst, "runs": runs})
    record["stats"] = stats
    return record


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "test-key-material")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "test-key-material-2")


# ----------------------------------------------------------------- pareto


def test_pareto_drops_dominated_keeps_order_deterministic() -> None:
    a = _policy("alpha p", pnl="10", worst="4900")
    # b: equal stats, FEWER prompt tokens -> b dominates a, a is dropped
    b = _policy("beta", pnl="10", worst="4900")
    c = _policy("gamma prompt here", pnl="-5", worst="4800")  # dominated
    d = _policy("delta", pnl="10", worst="4800")  # dominated by b (worse cap)
    # e: better worst capital but more tokens -> non-dominated against b
    e = _policy("epsilon with more tokens", pnl="10", worst="4950")
    front = gepa.pareto_front([c, d, a, b, e])
    assert [p["id"] for p in front] == [e["id"], b["id"]]


def test_pareto_keeps_exact_ties_in_a_stable_order() -> None:
    x = _policy("same length one", pnl="3", worst="4900")
    y = _policy("same length two", pnl="3", worst="4900")
    front = gepa.pareto_front([x, y])
    assert {p["id"] for p in front} == {x["id"], y["id"]}
    assert [p["id"] for p in front] == sorted([x["id"], y["id"]])


def test_pareto_unmeasured_policies_never_dominate_measured() -> None:
    never_ran = _policy("never ran at all", pnl="0", worst=None, runs=0)
    measured = _policy("m", pnl="-20", worst="4700", runs=1)
    front = gepa.pareto_front([never_ran, measured])
    assert [p["id"] for p in front] == [measured["id"]]


# ------------------------------------------------------------- reflection


def test_reflection_accepts_valid_and_drops_malformed_variants() -> None:
    transport = ReflectionTransport(
        {
            "diagnosis": "the policy leaves gaps where premium moved first",
            "revised_prompt": "Enter only when the premium is fresh and wide.",
            "variants": [
                "Skip after two losses.",
                7,
                "",
                "Take debit after a down move.",
                "a fifth variant",
                None,
            ],
        }
    )
    result = gepa.reflect(
        "zai", [{"snapshot": "s:x", "gap": "12.0"}], "champion prompt", transport=transport
    )
    assert result["status"] == "ok"
    assert result["revised_prompt"] == "Enter only when the premium is fresh and wide."
    # non-strings, empties dropped; the first 2 valid variants kept
    assert result["variants"] == ["Skip after two losses.", "Take debit after a down move."]
    assert result["diagnosis"] == "the policy leaves gaps where premium moved first"
    # the model saw the champion prompt and the gap evidence, nothing else
    payload = json.loads(transport.calls[0]["body"]["messages"][0]["content"])
    assert payload["champion_prompt"] == "champion prompt"
    assert payload["gap_boards"] == [{"snapshot": "s:x", "gap": "12.0"}]
    assert "Return STRICT JSON" in payload["task"]


def test_reflection_http500_is_recorded_not_raised() -> None:
    transport = ReflectionTransport(status=500)
    result = gepa.reflect("zai", [], "champion", transport=transport)
    assert result["status"] == "failed"
    assert "HTTP 500" in result["error"]


def test_reflection_runs_on_minimax_flash_with_the_flash_model() -> None:
    transport = ReflectionTransport({"diagnosis": "d", "revised_prompt": "p", "variants": []})
    result = gepa.reflect("minimax-flash", [], "champion", transport=transport)
    assert result["status"] == "ok"
    assert transport.calls[0]["url"] == "https://api.minimax.io/v1/chat/completions"
    assert transport.calls[0]["body"]["model"] == "MiniMax-M3.1-Flash-Preview"


def test_minimax_flash_provider_entry_is_additive() -> None:
    spec = PROVIDERS["minimax-flash"]
    assert spec["base_url"] == "https://api.minimax.io/v1"
    assert spec["model"] == "MiniMax-M3.1-Flash-Preview"
    assert spec["key_env"] == PROVIDERS["minimax"]["key_env"]
    assert spec["max_tokens"] == 12000  # thinking-heavy v2 boards truncated at 4000
    assert spec["timeout"] == 120.0
    # M3.1's server default, sent explicitly (never thinking-disabled)
    assert spec["extra"] == {"reasoning_effort": "max"}
    assert spec["verify_model"] is True
    # minimax moved from M3 to M3.1 (2026-09-28) at effort high; zai untouched
    assert PROVIDERS["minimax"]["model"] == "MiniMax-M3.1-Flash-Preview"
    assert PROVIDERS["minimax"]["extra"] == {"reasoning_effort": "high"}
    assert PROVIDERS["zai"]["model"] == "glm-5.3-flash"


def test_reflection_without_any_valid_proposal_records_none() -> None:
    transport = ReflectionTransport({"diagnosis": "nothing to change", "variants": [1, 2]})
    result = gepa.reflect("zai", [], "champion", transport=transport)
    assert result["status"] == "ok"
    assert result["revised_prompt"] is None
    assert result["variants"] == []


# ---------------------------------------------------------------- archive


def test_policy_id_is_deterministic_and_prompt_bound() -> None:
    assert gepa.policy_id_for("same prompt") == gepa.policy_id_for("same prompt")
    assert gepa.policy_id_for("same prompt") != gepa.policy_id_for("other")


def test_archive_round_trip_skips_torn_and_foreign_files(tmp_path: Path) -> None:
    policy = gepa.new_policy(
        "round trip prompt", generation=2, parents=["abc"], created_by="reflect:zai"
    )
    gepa.save_policy(tmp_path, policy)
    (tmp_path / "policies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policies" / "torn.json").write_text("{not json")
    (tmp_path / "policies" / "foreign.json").write_text(
        json.dumps({"id": "x", "prompt": "not a gepa record"})
    )
    loaded = gepa.load_archive(tmp_path)
    assert [p["id"] for p in loaded] == [policy["id"]]
    assert loaded[0]["parents"] == ["abc"]
    assert loaded[0]["stats"]["runs"] == 0
    assert gepa.load_archive(tmp_path / "empty") == []


def test_state_round_trip_dedupes_and_caps(tmp_path: Path) -> None:
    gepa.save_state(tmp_path, {"used_sessions": ["2026-09-24", "2026-09-24", "2026-09-25"]})
    assert gepa.load_state(tmp_path)["used_sessions"] == ["2026-09-24", "2026-09-25"]
    assert gepa.load_state(tmp_path / "nothing")["used_sessions"] == []


def test_fold_run_uses_only_the_mechanical_summary() -> None:
    record = gepa.new_policy("folding prompt", generation=1, parents=[], created_by="test")
    run = {
        "at": "2026-09-28T04:00:00+00:00",
        "boards_shown": 3,
        "summary": {
            "entered": 2,
            "modeled_wins": 2,
            "modeled_losses": 0,
            "closed_capital_proxy": "5020.0",
            "minimum_closed_capital_proxy": "5010.0",
        },
    }
    gepa.fold_run(record, run)
    stats = record["stats"]
    assert stats["runs"] == 1 and stats["boards"] == 3
    assert stats["entered"] == 2 and stats["wins"] == 2 and stats["losses"] == 0
    assert stats["closed_pnl_sum"] == "20.0"
    assert stats["worst_minimum_capital"] == "5010.0"
    gepa.fold_run(record, dict(run))  # cumulative fold
    assert record["stats"]["runs"] == 2
    assert record["stats"]["closed_pnl_sum"] == "40.0"
    assert record["stats"]["worst_minimum_capital"] == "5010.0"


# ------------------------------------------------- lab.py additive wiring


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "id": "c0",
            "structure": "put_credit",
            "width": "2",
            "premium": "1.0",
            "max_loss": "100",
            "max_gain": "100",
            "reward_risk": "1.00",
            "long_recent_move": None,
            "short_recent_move": None,
            "data_kind": "x",
        }
    ]


def test_board_prompt_swaps_only_the_policy_sentence() -> None:
    custom_sentence = "Always take the cheapest defined-risk debit row."
    default_task = json.loads(lab.board_prompt(_rows())[0]["content"])["task"]
    custom_task = json.loads(
        lab.board_prompt(_rows(), policy_prompt=custom_sentence)[0]["content"]
    )["task"]
    assert custom_task.startswith(custom_sentence)
    assert not custom_task.startswith(lab.POLICY_SENTENCE[:30])
    # the tail (risk caps + JSON contract) is byte-identical
    assert custom_task == custom_sentence + default_task[len(lab.POLICY_SENTENCE) :]
    assert "Capital 5000, max loss per trade 300" in custom_task
    assert '{"choice": "<row id>" | null' in custom_task


def test_ask_board_carries_the_policy_prompt_to_the_transport() -> None:
    transport = FakeTransport("first")
    lab.ask_board("zai", _rows(), transport=transport, policy_prompt="Custom policy sentence here.")
    content = transport.calls[0]["body"]["messages"][0]["content"]
    task = json.loads(content)["task"]
    assert task.startswith("Custom policy sentence here.")
    assert task.endswith("No other text.")


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_bundle(date(2026, 9, 24))))
    return path


def test_run_lab_runs_archive_policies_as_gepa_ids(bundle: Path, tmp_path: Path) -> None:
    prompt = "Custom policy sentence with several words."
    transport = FakeTransport("first")
    config = LabConfig(
        bundle=bundle,
        policy="gepa:abc123def456",
        sessions=1,
        lab_root=tmp_path / "lab",
        policy_prompt=prompt,
    )
    document = run_lab(config, windows=UNDER, transport=transport, now=T0)
    assert document["status"] == "ok"
    assert document["policy"] == "gepa:abc123def456"
    assert document["model_calls"] >= 1
    assert document["policy_prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    # the evolved prompt actually reached the model
    sent = json.loads(transport.calls[0]["body"]["messages"][0]["content"])
    assert sent["task"].startswith("Custom policy sentence")
    # gepa policies are model policies: the quota gate applies to them too
    skipped = run_lab(
        LabConfig(
            bundle=bundle,
            policy="gepa:abc123def456",
            sessions=1,
            lab_root=tmp_path / "lab",
            policy_prompt=prompt,
        ),
        windows=ON_PLAN,
        transport=transport,
        now=T0,
    )
    assert skipped["status"] == "skipped"


def test_gepa_policy_provider_is_the_flash_volume_lane() -> None:
    assert lab.is_model_policy("gepa:abc123")
    assert lab.model_provider("gepa:abc123") == "zai"
    assert lab.is_model_policy("model:minimax")
    assert not lab.is_model_policy("no_trade")
