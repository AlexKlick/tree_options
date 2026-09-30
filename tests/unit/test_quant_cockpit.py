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
