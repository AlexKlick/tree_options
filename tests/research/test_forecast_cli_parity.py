"""RL-3 oracle: API/CLI parity for forecast runs.

The read-only CLI prints from the SAME stored record the API GET
serves; the oracles pin equality on EVERY shared identity field and
full wire equality (the payload SHAPES differ by design — the CLI adds
the spec record). Includes the refusal-envelope variant.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.trex_web.research_view import attach

GOOD_BODY = {
    "source": "synthetic-forecast-v1",
    "horizon": 5,
    "evaluation_start": "2019-06-03",
}


def _cli(run_id: str, ws: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[3] / "src")
        + os.pathsep + env.get("PYTHONPATH", ""))
    result = subprocess.run(
        [sys.executable, "-m", "tree_options.research",
         "inspect", "--forecast", run_id, "--workspace", str(ws)],
        capture_output=True, text=True, env=env, check=True,
    )
    return json.loads(result.stdout)


def _assert_parity(api_body: dict, cli_payload: dict,
                   *, require_snapshot: bool) -> None:
    assert api_body["run_id"] == cli_payload["run_id"]
    assert api_body["status"] == cli_payload["run"]["status"]
    for field in ("result_sha256", "engine_sha256"):
        assert api_body[field] == cli_payload[field], field
    if require_snapshot:
        # a computed run binds its input snapshot and BOTH calendars; a
        # refusal is an honest "no computation happened" record and
        # omits them on BOTH surfaces by design
        for field in ("input_snapshot_sha256", "calendar_sha256",
                      "session_authority_sha256"):
            assert api_body[field] == cli_payload[field], field
    else:
        assert "input_snapshot_sha256" not in api_body
        assert "input_snapshot_sha256" not in cli_payload
    assert api_body["result"] == cli_payload["wire"]


def test_cli_forecast_inspect_matches_api_result(tmp_path: Path) -> None:
    ws = tmp_path / "rs"
    ws.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    app = FastAPI()
    worker = attach(app, workspace=ws, candidate_scopes_root=artifacts,
                    start_worker=False)
    assert worker is not None
    client = TestClient(app)
    r = client.post("/api/research/forecast", json=GOOD_BODY)
    run_id = r.json()["run_id"]
    assert worker.step() is True

    api_body = client.get(f"/api/research/runs/{run_id}/result").json()
    cli_payload = _cli(run_id, ws)
    _assert_parity(api_body, cli_payload, require_snapshot=True)
    # the stored spec carries the full surface (defaults included)
    assert all(cli_payload["forecast_spec"][k] == v
               for k, v in GOOD_BODY.items())
    assert api_body["result"]["refusal"] is None


def test_cli_parity_holds_for_the_refusal_envelope(
        tmp_path: Path) -> None:
    ws = tmp_path / "rs"
    ws.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    app = FastAPI()
    worker = attach(app, workspace=ws, candidate_scopes_root=artifacts,
                    start_worker=False)
    assert worker is not None
    client = TestClient(app)
    r = client.post("/api/research/forecast", json={
        **GOOD_BODY, "evaluation_start": "2020-10-01"})
    run_id = r.json()["run_id"]
    assert worker.step() is True

    api_body = client.get(f"/api/research/runs/{run_id}/result").json()
    assert api_body["result"]["refusal"] == \
        "research.forecast.insufficient_origins"
    cli_payload = _cli(run_id, ws)
    _assert_parity(api_body, cli_payload, require_snapshot=False)
    assert cli_payload["wire"]["refusal"] == \
        "research.forecast.insufficient_origins"
    # A refused run still publishes content-bound identity fields
    assert cli_payload["engine_sha256"]
