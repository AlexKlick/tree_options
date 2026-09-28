"""The virtual floor projects bounded research data without broker effects."""

import json

import pytest
from fastapi.testclient import TestClient

from tree_options.desk.trade_floor import project_replay
from tree_options.trex_web.app import create_app


def replay_fixture():
    return {
        "schema": "desk-trade-floor-replay/1", "id": "sample-run",
        "source_head": "a" * 40,
        "source_manifest_sha256": "b" * 64,
        "sample_manifest_sha256": "c" * 64,
        "provider_manifest_sha256": "d" * 64,
        "replay_manifest_sha256": "e" * 64,
        "starting_capital": "5000", "excluded_calibration_snapshot": "w1-01",
        "limitations": ["trade bars are not fills"],
        "execution_enabled": False, "research_only": True,
        "private_bars": [{"secret": "must not leave API"}],
        "windows": [{"id": "w1", "start": "2026-01-01", "end": "2026-03-31",
                     "series": 6, "traded_minute_bars": 100, "rounds": 1,
                     "final_scores": [
                         {"id": "zai", "label": "Z.ai", "entered": 1,
                          "wins": 1, "losses": 0, "closed_capital_proxy": "5010"},
                         {"id": "flash", "label": "Z.ai Flash", "entered": 0,
                          "wins": 0, "losses": 0, "closed_capital_proxy": "5000"},
                         {"id": "minimax", "label": "MiniMax", "entered": 0,
                          "wins": 0, "losses": 0, "closed_capital_proxy": "5000"},
                     ]}],
        "rounds": [{"id": "w1-02", "window": "w1", "snapshot_id": "s:2026-01-02T10:00",
                    "as_of": "2026-01-02T15:00:00+00:00", "all_as_of_candidates": 1,
                    "candidates": [{"id": "candidate-one", "symbol": "SPY",
                                    "structure": "put_credit", "dte": 20, "width": "1",
                                    "premium_proxy": "0.4", "max_loss_proxy": "60",
                                    "max_gain_proxy": "40", "reward_to_risk_proxy": "0.67",
                                    "private_legs": ["secret"]}],
                    "traders": [
                        {"id": "zai", "label": "Z.ai", "selected_id": "candidate-one",
                         "action": "entered", "action_reason": "selected",
                         "model_reason": "bounded risk", "entry_loss_proxy": "65",
                         "eventual_pnl_proxy": "10", "private_raw": "secret"},
                        {"id": "flash", "label": "Z.ai Flash", "selected_id": None,
                         "action": "skip", "action_reason": "no_selection",
                         "model_reason": "skip", "entry_loss_proxy": None,
                         "eventual_pnl_proxy": None},
                        {"id": "minimax", "label": "MiniMax", "selected_id": None,
                         "action": "skip", "action_reason": "no_selection",
                         "model_reason": "skip", "entry_loss_proxy": None,
                         "eventual_pnl_proxy": None},
                    ]}],
    }


def test_project_replay_whitelists_and_checks_score_and_risk():
    doc = replay_fixture()
    projected = project_replay(doc)
    assert projected["execution_enabled"] is False
    assert "private_bars" not in projected
    assert "private_legs" not in projected["rounds"][0]["candidates"][0]
    assert "private_raw" not in projected["rounds"][0]["traders"][0]

    doc["rounds"][0]["candidates"][0]["max_loss_proxy"] = "301"
    with pytest.raises(ValueError, match="risk cap"):
        project_replay(doc)
    doc["rounds"][0]["candidates"][0]["max_loss_proxy"] = "60"
    doc["windows"][0]["final_scores"][0]["closed_capital_proxy"] = "5100"
    with pytest.raises(ValueError, match="score disagrees"):
        project_replay(doc)


def test_trade_floor_api_is_get_only_and_fails_closed(world):
    out = world.root / "desk-store/evaluations/trade-floor"
    out.mkdir(parents=True)
    file = out / "floor-sample.json"
    file.write_text(json.dumps(replay_fixture()))
    client = TestClient(create_app(
        state_dir=str(world.root / "legacy"), plans_dir=str(world.root / "plans"),
        discovery_dir=str(world.root / "discovery"),
        desk_state_dir=str(world.root / "desk-state"),
        desk_store_dir=str(world.root / "desk-store")))
    response = client.get("/api/desk/trade-floor")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["schema"] == "desk-trade-floor-list/1"
    assert body["execution_enabled"] is False
    assert len(body["replays"][0]["rounds"]) == 1
    assert "private_bars" not in response.text
    assert "private_legs" not in response.text
    assert client.post("/api/desk/trade-floor").status_code == 405

    bad = replay_fixture()
    bad["execution_enabled"] = True
    file.write_text(json.dumps(bad))
    unavailable = client.get("/api/desk/trade-floor")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"] == "evidence_unavailable"
