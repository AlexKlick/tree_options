import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.trex_web.quant_view import attach


def test_cockpit_preserves_evidence_classes_and_never_owns_broker(tmp_path):
    app = FastAPI()
    attach(app, workspace=tmp_path / "research", execution_state=tmp_path / "broker")
    client = TestClient(app)
    response = client.get("/api/research/quant")
    assert response.status_code == 200
    data = response.json()
    assert len(data["strategies"]) == 7
    assert data["evidence_classes"] == [
        "BACKTEST",
        "DETERMINISTIC REPLAY",
        "SIMULATED EXECUTION",
        "BROKER PAPER",
        "LIVE",
    ]
    assert data["execution"]["state"] == "NOT_OBSERVED"
    assert data["live_money"] is False
    assert client.post("/api/research/quant/submit", json={}).status_code == 404


def test_invalid_or_stale_projection_never_reports_ready(tmp_path):
    import json
    from datetime import UTC, datetime

    from tree_options.time.sessions import shift_instant

    app = FastAPI()
    state = tmp_path / "broker"
    state.mkdir()
    attach(app, workspace=tmp_path / "research", execution_state=state)
    (state / "projection.json").write_text(
        json.dumps(
            {
                "environment": "BROKER PAPER",
                "live_money": False,
                "observed_at": shift_instant(datetime.now(UTC), -100).isoformat(),
                "ready": True,
                "owner_held": True,
            }
        )
    )
    result = TestClient(app).get("/api/research/quant").json()
    assert result["execution"]["state"] == "STALE"
    assert result["execution"]["ready"] is False
    (state / "projection.json").write_text("{invalid")
    assert TestClient(app).get("/api/research/quant").status_code == 503


@pytest.mark.parametrize(
    "corruption", ["naive_time", "missing_findings", "text_ready", "text_exact"]
)
def test_malformed_execution_projection_refuses_incomplete_proof(tmp_path, corruption):
    import json
    from datetime import UTC, datetime

    source = {
        "environment": "BROKER PAPER",
        "live_money": False,
        "observed_at": datetime.now(UTC).isoformat(),
        "ready": False,
        "owner_held": True,
        "executions": [
            {
                "intent_id": "one",
                "state": "SENT",
                "broker_state": "AMBIGUOUS",
                "reconciliation_clean": False,
                "findings": ["UNKNOWN"],
                "evidence_verdict": "REFUSED",
                "exact_economics": False,
                "records": [],
            }
        ],
    }
    if corruption == "naive_time":
        source["observed_at"] = "2026-09-29T20:00:00"
    elif corruption == "missing_findings":
        del source["executions"][0]["findings"]
    elif corruption == "text_ready":
        source["ready"] = "false"
    else:
        source["executions"][0]["exact_economics"] = "false"
    state = tmp_path / "broker"
    state.mkdir()
    (state / "projection.json").write_text(json.dumps(source))
    app = FastAPI()
    attach(app, workspace=tmp_path / "research", execution_state=state)
    assert TestClient(app).get("/api/research/quant").status_code == 503
