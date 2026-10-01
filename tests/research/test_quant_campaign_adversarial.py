"""Adversarial research callbacks and strict, hermetic GLM/input boundaries."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from tree_options.research import quant_campaign
from tree_options.research.quant import FrozenUniverse, QuantSnapshot
from tree_options.research.quant_backtest import ReplayPeriod
from tree_options.research.quant_campaign import CampaignSpec, Proposal, run_campaign
from tree_options.research.quant_campaign_io import Glm53Proposer, spec_from_dict, strict_json
from tree_options.research.runstate.store import RunstateStoreError, open_runstate_store
from tree_options.strategy_lab.contracts import Observation
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trex.discovery import llm


@pytest.fixture
def campaign_inputs(tmp_path):
    sessions = [date(2026, 9, day) for day in (21, 22, 23, 24, 25, 28)]
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            {
                "calendar": "fixture",
                "timezone": "America/New_York",
                "open": "09:30",
                "close": "16:00",
                "sessions": [day.isoformat() for day in sessions],
            }
        )
    )
    checksum = tmp_path / "calendar.sha256"
    checksum.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    calendar = StaticSessionCalendar(path, checksum)
    periods = []
    for decision in sessions[::2]:
        at = calendar.session_close(decision)
        snapshot = QuantSnapshot(
            FrozenUniverse(decision, ("A", "B"), "synthetic", "a" * 64),
            at,
            tuple(
                Observation(
                    symbol, at, at, {"close": Decimal("100")}, "fixture", f"{symbol}-{decision}"
                )
                for symbol in ("A", "B")
            ),
        )
        next_session = calendar.nth_after(decision, 1)
        periods.append(
            ReplayPeriod(
                snapshot,
                calendar.session_open(next_session),
                calendar.session_close(next_session),
                {"A": Decimal("100"), "B": Decimal("100")},
                {"A": Decimal("120"), "B": Decimal("90")},
            )
        )
    spec = CampaignSpec(
        "Concentration under declared cost assumptions",
        (periods[0],),
        (periods[1],),
        (periods[2],),
        "a" * 40,
        "b" * 64,
        (Proposal("equal_weight_us_equities", 1, "Concentration challenger"),),
        max_candidates=4,
        data_class="synthetic_fixture",
    )
    return spec, calendar


def test_callback_cannot_mutate_evaluated_configuration_or_feedback_digest(
    tmp_path, campaign_inputs
):
    spec, calendar = campaign_inputs

    def tamper(feedback):
        for row in feedback["training"]:
            row["config"]["parameters"]["top_n"] = 777
            row["metrics"]["mean_net_return"] = "Infinity"
            row["metrics"]["periods"][0]["research_run"]["strategy_version"] = "fabricated-version"
        return []

    root = tmp_path / "research"
    result = run_campaign(
        root, spec, calendar, proposer=tamper, proposer_identity="adversarial-fixture/1"
    )
    with open_runstate_store(root) as store:
        retained = store.get("quant_version", result["winner"]["version_id"])
        assert retained["config"] == result["winner"]["parameters"] == {"top_n": 1}
        reflections = [
            row
            for row in store.all("quant_provenance")
            if row.get("schema") == "quant-reflection-result/1"
        ]
        claims = [
            row
            for row in store.all("quant_provenance")
            if row.get("schema") == "quant-reflection-claim/1"
        ]
        assert reflections[0]["feedback_sha256"] == claims[0]["feedback_sha256"]
        assert "Infinity" not in json.dumps(result)
        assert "fabricated-version" not in json.dumps(result)


def test_pluggable_receipt_cannot_persist_credentials(tmp_path, campaign_inputs):
    spec, calendar = campaign_inputs

    class Proposer:
        def __init__(self):
            self.last_receipt = {
                "requested_model": "glm-5.3",
                "returned_model": "glm-5.3",
                "response_id": "fake-completion-1",
                "response_sha256": "c" * 64,
                "Authorization": "Bearer DO_NOT_PERSIST_FAKE_CREDENTIAL",
            }

        def __call__(self, feedback):
            return []

    root = tmp_path / "research"
    try:
        run_campaign(
            root, spec, calendar, proposer=Proposer(), proposer_identity="receipt-fixture/1"
        )
    except ValueError:
        pass
    with open_runstate_store(root) as store:
        assert "DO_NOT_PERSIST_FAKE_CREDENTIAL" not in json.dumps(store.all("quant_provenance"))


def test_tampered_retained_result_is_refused(tmp_path, campaign_inputs):
    spec, calendar = campaign_inputs
    root = tmp_path / "research"
    result = run_campaign(root, spec, calendar)
    with sqlite3.connect(root / "runstate.sqlite3") as conn:
        conn.execute(
            "UPDATE objects SET payload_json = ? WHERE kind = 'result' AND object_key = ?",
            (json.dumps({**result, "live_money": True}), result["campaign_id"]),
        )
    with pytest.raises(RunstateStoreError, match="tampering"):
        run_campaign(root, spec, calendar)


@pytest.mark.parametrize("field", ["max_candidates", "generations"])
def test_boolean_budget_or_generation_refused(campaign_inputs, field):
    spec, _ = campaign_inputs
    raw = spec.to_dict()
    raw[field] = True
    with pytest.raises(ValueError, match="budget"):
        spec_from_dict(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_authorized", True),
        ("fees", "zero"),
        ("objective", "gross_return_only"),
        ("registration", "preregistered"),
    ],
)
def test_derived_semantics_cannot_be_claimed_by_input(campaign_inputs, field, value):
    raw = campaign_inputs[0].to_dict()
    raw[field] = value
    with pytest.raises(ValueError, match="semantics"):
        spec_from_dict(raw)


def test_duplicate_nested_input_identity_is_refused(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"parameters":{"top_n":1,"top_n":2}}')
    with pytest.raises(ValueError, match="duplicate"):
        strict_json(path)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_input_is_refused(tmp_path, value):
    path = tmp_path / "nonfinite.json"
    path.write_text('{"risk_penalty":' + value + "}")
    with pytest.raises(ValueError, match="nonfinite"):
        strict_json(path)


def provider_envelope(model="glm-5.3", identifier="completion-fixture-1"):
    return {
        "id": identifier,
        "model": model,
        "choices": [{"finish_reason": "stop", "message": {"content": '{"proposals":[]}'}}],
        "unexpected_provider_metadata": "DO_NOT_PERSIST_PROVIDER_BODY",
    }


@pytest.mark.parametrize("model", [None, "glm-5.3-flash", "different-model"])
def test_glm_proposer_refuses_missing_or_mismatched_served_model(monkeypatch, model):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "FAKE_PRIVATE_TOKEN")
    monkeypatch.setattr(
        llm, "urllib_post", lambda *args: (200, json.dumps(provider_envelope(model=model)).encode())
    )
    with pytest.raises(llm.LlmError):
        Glm53Proposer()({"training": []})


def test_glm_receipt_contains_only_safe_model_identity(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "FAKE_PRIVATE_TOKEN")
    raw = json.dumps(provider_envelope()).encode()
    calls = []

    def provider(url, body, headers, timeout):
        calls.append(json.loads(body))
        assert headers["Authorization"] == "Bearer FAKE_PRIVATE_TOKEN"
        return 200, raw

    monkeypatch.setattr(llm, "urllib_post", provider)
    proposer = Glm53Proposer()
    assert proposer({"training": []}) == []
    assert calls[0]["model"] == "glm-5.3"
    assert proposer.last_receipt == {
        "requested_model": "glm-5.3",
        "returned_model": "glm-5.3",
        "response_id": "completion-fixture-1",
        "response_sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert "FAKE_PRIVATE_TOKEN" not in json.dumps(proposer.last_receipt)
    assert "DO_NOT_PERSIST_PROVIDER_BODY" not in json.dumps(proposer.last_receipt)


def test_conflicting_duplicate_model_fields_are_refused(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "FAKE_PRIVATE_TOKEN")
    raw = (
        json.dumps(provider_envelope())
        .replace('"model": "glm-5.3"', '"model":"glm-5.3-flash","model":"glm-5.3"')
        .encode()
    )
    monkeypatch.setattr(llm, "urllib_post", lambda *args: (200, raw))
    with pytest.raises(llm.LlmError):
        Glm53Proposer()({"training": []})


def test_later_failed_call_does_not_retain_old_model_receipt(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "FAKE_PRIVATE_TOKEN")
    raw = json.dumps(provider_envelope()).encode()
    monkeypatch.setattr(llm, "urllib_post", lambda *args: (200, raw))
    proposer = Glm53Proposer()
    proposer({"training": []})
    assert proposer.last_receipt is not None
    monkeypatch.setattr(llm, "urllib_post", lambda *args: (503, b""))
    with pytest.raises(llm.LlmError):
        proposer({"training": []})
    assert proposer.last_receipt is None


def test_conflicting_duplicate_proposal_parameters_are_refused(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_ZAI", "FAKE_PRIVATE_TOKEN")
    envelope = provider_envelope()
    envelope["choices"][0]["message"]["content"] = (
        '{"proposals":[{"strategy_id":"equal_weight_us_equities","parameters":{"top_n":1,"top_n":2},"rationale":"Do not repair contradictions"}]}'
    )
    monkeypatch.setattr(llm, "urllib_post", lambda *args: (200, json.dumps(envelope).encode()))
    with pytest.raises(llm.LlmError):
        Glm53Proposer()({"training": []})


def test_one_session_result_candidly_limits_performance_claims(tmp_path, campaign_inputs):
    spec, calendar = campaign_inputs
    result = run_campaign(tmp_path / "research", replace(spec, generations=0), calendar)
    assert result["evidence_kind"] == "synthetic_backtest"
    limitations = " ".join(result["limitations"])
    assert "one-session" in limitations and "no compounded" in limitations
    assert "not intraday drawdown" in limitations
    assert "not independently qualified" in limitations
    assert result["exact_external_economics"] is False
    assert result["live_money"] is False


def test_stop_after_reflection_blocks_validation_before_next_evaluation(
    tmp_path, campaign_inputs, monkeypatch
):
    spec, calendar = campaign_inputs
    root = tmp_path / "research"
    observed = []
    original = quant_campaign.evaluate_periods

    def record_evaluation(*args, **kwargs):
        observed.append((root / "STOP").exists())
        return original(*args, **kwargs)

    def halt_after_reflection(feedback):
        (root / "STOP").touch()
        return []

    monkeypatch.setattr(quant_campaign, "evaluate_periods", record_evaluation)
    with pytest.raises(ValueError, match="halted"):
        run_campaign(
            root, spec, calendar, proposer=halt_after_reflection, proposer_identity="stop-fixture/1"
        )
    assert observed and not any(observed)
    with open_runstate_store(root) as store:
        assert not store.all("result")
