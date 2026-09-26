"""FastAPI route tests — RL §11 acceptance matrix rows that the routes cover.

Coverage:
    * Forecast semantics — ``GET /api/research/forecast`` returns 410
      (RL-3 out of scope).
    * Access control — the catalog adapter path-overlap guard refuses to
      wire the lane if any RESEARCH_* env var collides with desk paths.
    * End-to-end smoke: catalog returns the campaign scope list; the
      ``evidence`` endpoint returns an envelope; the compare POST spools
      a run.

The desk evidence store is read-only by the research lane; the
fixtures use ``tmp_path`` everywhere and never write to the live store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tree_options.research import paths as research_paths

# -- Path-overlap guard (boundary test) ------------------------------------


def test_attach_research_refuses_overlapping_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attach-time guard refuses to wire the lane if the workspace
    path collides with a desk path."""
    from tree_options.desk.paths import state_root
    monkeypatch.setenv("RESEARCH_WORKSPACE_DIR", str(state_root()))
    # Importing attach triggers the guard indirectly via path resolution;
    # the direct call is:
    with pytest.raises(RuntimeError, match="collides"):
        research_paths.assert_no_overlap_with_desk()


# -- In-process smoke --------------------------------------------------------


def _build_app(tmp_path: Path) -> TestClient:
    """Build a minimal FastAPI app with the research routes mounted.

    We do NOT import the full trex_web app (which would require the
    desk evidence store at /var/state/trex-desk/evidence/). Instead we
    use a private app + the research_view attach directly. This proves
    the route surface without depending on the live environment."""
    from fastapi import FastAPI

    from tree_options.trex_web.research_view import attach as attach_research

    fake_workspace = tmp_path / "workspace"
    fake_scopes = tmp_path / "scopes"
    fake_scopes.mkdir(parents=True)

    # Seed one minimal sealed-round scope so the catalog has something
    # to surface. Use a non-PASS disposition to exercise that path.
    (fake_scopes / "test-scope").mkdir()
    (fake_scopes / "test-scope" / "sealed-round.json").write_text(json.dumps({
        "family_verdict": "WITHDRAWN",
        "frozen_inputs": {"calibration_v3_sha256": "deadbeef"},
        "round": {},
    }))

    app = FastAPI()
    attach_research(app, workspace=fake_workspace, candidate_scopes_root=fake_scopes)
    return TestClient(app)


def test_forecast_endpoint_returns_410_gone(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    r = client.get("/api/research/forecast")
    assert r.status_code == 410
    assert "forecast_out_of_scope_for_rl1" in r.json()["error"]


def test_scenarios_endpoint_returns_410_gone(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    r = client.get("/api/research/scenarios")
    assert r.status_code == 410
    assert "scenarios_out_of_scope_for_rl1" in r.json()["error"]


def test_candidates_endpoint_lists_catalog(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    r = client.get("/api/research/candidates")
    assert r.status_code == 200
    body = r.json()
    assert "candidates" in body
    assert len(body["candidates"]) == 1
    c = body["candidates"][0]
    assert c["family"] == "test-scope"
    assert c["disposition"] == "WITHDRAWN"
    assert c["plot_funded_account"] is False


def test_candidates_endpoint_filters_by_family(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    r = client.get("/api/research/candidates?family=test-scope")
    assert r.status_code == 200
    assert len(r.json()["candidates"]) == 1
    r2 = client.get("/api/research/candidates?family=nope")
    assert len(r2.json()["candidates"]) == 0


def test_unknown_candidate_returns_404(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    r = client.get("/api/research/candidates/nope")
    assert r.status_code == 404


def test_compare_post_spools_a_run(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    r = client.post("/api/research/compare", json={
        "candidate_ids": ["test-scope-?"],
        "starting_capital": "10000.00",
        "common_start": "2024-01-02",
        "common_end": "2026-09-25",
    })
    assert r.status_code == 202
    body = r.json()
    assert "run_id" in body
    assert body["status"] == "queued"
    # The run shows up under /api/research/runs/{id}
    r2 = client.get(f"/api/research/runs/{body['run_id']}")
    assert r2.status_code == 200
    assert r2.json()["id"] == body["run_id"]


def test_compare_post_rejects_invalid_spec(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    r = client.post("/api/research/compare", json={
        "candidate_ids": [],  # empty — invalid
        "starting_capital": "not-a-number",
    })
    assert r.status_code == 400


def test_unknown_run_returns_404(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    r = client.get("/api/research/runs/nope")
    assert r.status_code == 404


def test_evidence_endpoint_returns_envelope_for_known_candidate(tmp_path: Path) -> None:
    client = _build_app(tmp_path)
    # First get the catalog to discover the actual id.
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    r = client.get(f"/api/research/candidates/{cid}/evidence")
    assert r.status_code == 200
    body = r.json()
    assert body["candidate_id"] == cid
    # The fixture is sealed_campaign; envelope shape includes
    # hypothesis/estimand/versions/reproduction_command.
    assert body["hypothesis"]
    assert body["estimand"]
    assert body["reproduction_command"]


def test_malformed_scope_surfaces_instead_of_vanishing(tmp_path: Path) -> None:
    """A scope whose sealed-round.json is not an object must appear in
    the catalog as a DATA-GATED row (adapter backstop), never be
    silently dropped from the catalog."""
    from fastapi import FastAPI

    from tree_options.trex_web.research_view import attach as attach_research

    fake_scopes = tmp_path / "scopes"
    fake_scopes.mkdir()
    good = fake_scopes / "good-scope"
    good.mkdir()
    (good / "sealed-round.json").write_text(json.dumps({
        "family_verdict": "WITHDRAWN",
        "frozen_inputs": {"calibration_v3_sha256": "deadbeef"},
        "round": {},
    }))
    bad = fake_scopes / "bad-scope"
    bad.mkdir()
    (bad / "sealed-round.json").write_text("12345")  # valid JSON, not an object

    app = FastAPI()
    attach_research(app, workspace=tmp_path / "ws", candidate_scopes_root=fake_scopes)
    client = TestClient(app)
    body = client.get("/api/research/candidates").json()["candidates"]
    families = {c["family"] for c in body}
    assert families == {"good-scope", "bad-scope"}
    bad_row = next(c for c in body if c["family"] == "bad-scope")
    assert bad_row["disposition"] == "DATA-GATED-NOT-RUN"
    assert "adapter" in bad_row["ineligibility_reason"]


def test_attach_survives_unwritable_workspace(tmp_path: Path) -> None:
    """Panel survival: an unwritable workspace must never take the
    routes down at attach time. attach() tolerates the mkdir failure;
    the catalog GET keeps working; the runstate-backed endpoints
    degrade to 503 with the ReadWritePaths hint — the same degradation
    the discovery spool uses in ``trex_web.app``."""
    from fastapi import FastAPI

    from tree_options.trex_web.research_view import attach as attach_research

    fake_scopes = tmp_path / "scopes"
    fake_scopes.mkdir()
    (fake_scopes / "test-scope").mkdir()
    (fake_scopes / "test-scope" / "sealed-round.json").write_text(json.dumps({
        "family_verdict": "WITHDRAWN",
        "frozen_inputs": {"calibration_v3_sha256": "deadbeef"},
        "round": {},
    }))
    # A workspace path occupied by an existing FILE: mkdir(exist_ok=True)
    # raises FileExistsError — same OSError class as a read-only unit
    # sandbox that lacks the research dir in ReadWritePaths.
    workspace_as_file = tmp_path / "not-a-dir"
    workspace_as_file.write_text("occupied")

    app = FastAPI()
    attach_research(app, workspace=workspace_as_file, candidate_scopes_root=fake_scopes)
    client = TestClient(app)

    r = client.get("/api/research/candidates")
    assert r.status_code == 200
    assert len(r.json()["candidates"]) == 1

    r2 = client.post("/api/research/compare", json={
        "candidate_ids": ["test-scope-?"],
        "starting_capital": "10000",
    })
    assert r2.status_code == 503
    assert r2.json()["detail"]["error"] == "research_workspace_unwritable"

    r3 = client.get("/api/research/runs/any")
    assert r3.status_code == 503


def test_compare_result_endpoint_returns_engine_output(tmp_path: Path) -> None:
    """End-to-end: POST a compare, then read /result. The engine
    returns an ineligible summary because the test fixture's
    disposition is WITHDRAWN — that's the correct, honest answer."""
    client = _build_app(tmp_path)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    r = client.post("/api/research/compare", json={
        "candidate_ids": [cid],
        "starting_capital": "10000",
    })
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    r2 = client.get(f"/api/research/runs/{run_id}/result")
    assert r2.status_code == 200
    body = r2.json()
    assert body["spec"]["candidate_ids"] == [cid]
    assert len(body["candidates"]) == 1
    summary = body["candidates"][0]
    assert summary["rejection_reason"] is not None
    assert "WITHDRAWN" in summary["rejection_reason"]
