"""The imported packet is a proposal fixture and never a trading grant."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tree_options.action_graph.proposal import ROOT, ModelError, load_design_example, validate
from tree_options.trex_web.app import create_app


def _fixture() -> tuple[dict, dict, dict]:
    return tuple(
        json.loads((ROOT / name).read_text())
        for name in (
            "research-to-paper.plan.json",
            "operation-registry.fixture.json",
            "action-plan.schema.json",
        )
    )


def test_example_is_checked_but_has_no_execution_authority() -> None:
    plan, receipt = load_design_example()
    assert receipt["valid_structure"] is True
    assert receipt["node_count"] == 18
    assert receipt["dependency_count"] == 20
    assert plan["execution_authorized"] is False
    assert receipt["execution_authorized"] is False
    assert receipt["broker_contacted"] is False


@pytest.mark.parametrize("change", ["authorized", "effect", "cycle", "hash"])
def test_proposal_refuses_authority_and_broken_bindings(change: str) -> None:
    plan, registry, schema = _fixture()
    plan = copy.deepcopy(plan)
    if change == "authorized":
        plan["execution_authorized"] = True
    elif change == "effect":
        plan["nodes"][0]["effect_class"] = "paper_effect"
    elif change == "cycle":
        plan["nodes"][0]["dependencies"].append(
            {"node_id": plan["nodes"][-1]["id"], "on_outcomes": ["completed"]}
        )
    else:
        plan["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ModelError):
        validate(plan, registry, schema)


def test_cockpit_exposes_only_get_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREX_RESEARCH_WORKER", "0")
    client = TestClient(
        create_app(
            state_dir=str(tmp_path / "state"),
            plans_dir=str(tmp_path / "plans"),
            discovery_dir=str(tmp_path / "discovery"),
            desk_state_dir=str(tmp_path / "desk"),
        )
    )
    response = client.get("/api/action-model/example")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["receipt"]["execution_authorized"] is False
    assert client.post("/api/action-model/example").status_code == 405


def test_snaptrade_provider_is_behind_same_governed_effect_boundary():
    _, registry, schema = _fixture()
    plan = json.loads((ROOT / "research-to-broker-paper.plan.json").read_text())
    receipt = validate(plan, registry, schema)
    assert receipt["execution_authorized"] is False
    assert receipt["broker_contacted"] is False
    assert "broker.snaptrade.submit" in json.dumps(plan)
