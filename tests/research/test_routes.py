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


def _build_app(tmp_path: Path, *, engine_fn=None) -> tuple[TestClient, object]:
    """Build a minimal FastAPI app with the research routes mounted.

    We do NOT import the full trex_web app (which would require the
    desk evidence store at /var/state/trex-desk/evidence/). Instead we
    use a private app + the research_view attach directly. This proves
    the route surface without depending on the live environment.

    The worker thread is DISABLED (tests drive ``worker.step()``
    synchronously so lifecycle transitions are deterministic and engine
    invocations can be counted)."""
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
    worker = attach_research(app, workspace=fake_workspace,
                             candidate_scopes_root=fake_scopes,
                             engine_fn=engine_fn, start_worker=False)
    return TestClient(app), worker


def test_forecast_endpoint_returns_410_gone(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
    r = client.get("/api/research/forecast")
    assert r.status_code == 410
    assert "forecast_out_of_scope_for_rl1" in r.json()["error"]


def test_scenarios_endpoint_returns_410_gone(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
    r = client.get("/api/research/scenarios")
    assert r.status_code == 410
    assert "scenarios_out_of_scope_for_rl1" in r.json()["error"]


def test_candidates_endpoint_lists_catalog(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
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
    client, _worker = _build_app(tmp_path)
    r = client.get("/api/research/candidates?family=test-scope")
    assert r.status_code == 200
    assert len(r.json()["candidates"]) == 1
    r2 = client.get("/api/research/candidates?family=nope")
    assert len(r2.json()["candidates"]) == 0


def test_unknown_candidate_returns_404(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
    r = client.get("/api/research/candidates/nope")
    assert r.status_code == 404


def test_compare_post_spools_a_run(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    r = client.post("/api/research/compare", json={
        "candidate_ids": [cid],
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
    assert r2.json()["run_id"] == body["run_id"]
    assert r2.json()["status"] == "queued"


def test_identical_post_is_idempotent_at_the_http_boundary(tmp_path: Path) -> None:
    """RL1-03: the pre-custody code embedded ``queued_at`` in the
    immutable spec payload, so reposting an identical spec collided
    with itself and returned 500. The canonical spec payload is now
    exactly the hashed form; a duplicate POST returns the SAME run."""
    client, _worker = _build_app(tmp_path)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    spec = {"candidate_ids": [cid], "starting_capital": "10000",
            "common_start": "2024-01-02", "common_end": "2026-09-25"}
    first = client.post("/api/research/compare", json=spec)
    second = client.post("/api/research/compare", json=spec)
    third = client.post("/api/research/compare", json=spec)
    assert first.status_code == 202
    assert second.status_code == 200  # same run, no conflict, no 500
    assert third.status_code == 200
    assert first.json()["run_id"] == second.json()["run_id"] == third.json()["run_id"]


@pytest.mark.parametrize("capital", ["-1", "0", "NaN", "Infinity", "-Infinity"])
def test_invalid_capital_never_gets_queued(tmp_path: Path, capital: str) -> None:
    """RL1-03: negative/zero/non-finite capital was accepted and
    persisted as a queued run. Validation now precedes any write."""
    client, _worker = _build_app(tmp_path)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    r = client.post("/api/research/compare", json={
        "candidate_ids": [cid], "starting_capital": capital,
        "common_start": "2024-01-02", "common_end": "2026-09-25",
    })
    assert r.status_code == 400
    # nothing persisted: validation precedes any store write
    db = tmp_path / "workspace" / "runstate.sqlite3"
    if db.exists():
        from tree_options.research.runstate.store import RunstateStore
        probe = RunstateStore(db)
        try:
            assert probe.all("run") == ()
            assert probe.all("spec") == ()
        finally:
            probe.close()


def test_unknown_candidate_is_refused_before_persisting(tmp_path: Path) -> None:
    """RL1-03: an unknown candidate id was queued (202) and only
    rejected later at result fetch. Membership is now a pre-write 400."""
    client, _worker = _build_app(tmp_path)
    r = client.post("/api/research/compare", json={
        "candidate_ids": ["nope-not-in-catalog"], "starting_capital": "10000",
        "common_start": "2024-01-02", "common_end": "2026-09-25",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "candidate_not_in_catalog"


def test_reversed_date_range_is_refused(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    r = client.post("/api/research/compare", json={
        "candidate_ids": [cid], "starting_capital": "10000",
        "common_start": "2026-09-25", "common_end": "2024-01-02",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "research.plan.invalid_window"


def test_undeclared_window_is_refused(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    r = client.post("/api/research/compare", json={
        "candidate_ids": [cid], "starting_capital": "10000",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "research.plan.window_required"


def test_json_array_body_is_a_client_error(tmp_path: Path) -> None:
    """RL1-03: a JSON array body produced HTTP 500 (AttributeError
    outside the parser's catch). It is a 400 client error now."""
    client, _worker = _build_app(tmp_path)
    r = client.post("/api/research/compare", json=["not", "an", "object"])
    assert r.status_code == 400


def test_compare_post_rejects_invalid_spec(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
    r = client.post("/api/research/compare", json={
        "candidate_ids": [],  # empty — invalid
        "starting_capital": "not-a-number",
    })
    assert r.status_code == 400


def test_unknown_run_returns_404(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
    r = client.get("/api/research/runs/nope")
    assert r.status_code == 404


def test_evidence_endpoint_returns_envelope_for_known_candidate(tmp_path: Path) -> None:
    client, _worker = _build_app(tmp_path)
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
    cid = r.json()["candidates"][0]["id"]

    # The spec must pass validation (membership + plan) so the request
    # reaches the store and hits the unwritable-workspace degradation.
    r2 = client.post("/api/research/compare", json={
        "candidate_ids": [cid],
        "starting_capital": "10000",
        "common_start": "2024-01-02",
        "common_end": "2026-09-25",
    })
    assert r2.status_code == 503
    assert r2.json()["detail"]["error"] == "research_workspace_unwritable"

    r3 = client.get("/api/research/runs/any")
    assert r3.status_code == 503


def test_run_lifecycle_completed_result_is_immutable_and_read_only(
        tmp_path: Path) -> None:
    """RL1-03 end-to-end custody: POST spools a queued run; GET before
    compute reports pending WITHOUT invoking the engine; one worker
    step computes and publishes; repeated GETs return the SAME stored
    artifact with zero further engine invocations; the result is bound
    to its inputs (engine + snapshot + calendar identities)."""
    from tree_options.research.comparison.engine import run_comparison

    calls: list[int] = []

    def counting_engine(spec, candidates, *, baseline=None, **kwargs):
        calls.append(1)
        return run_comparison(spec, candidates, baseline=baseline, **kwargs)

    client, worker = _build_app(tmp_path, engine_fn=counting_engine)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]  # WITHDRAWN fixture: honest rejection path
    r = client.post("/api/research/compare", json={
        "candidate_ids": [cid], "starting_capital": "10000",
        "common_start": "2024-01-02", "common_end": "2026-09-25",
    })
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    pending = client.get(f"/api/research/runs/{run_id}/result").json()
    assert pending["status"] == "queued"
    assert pending["result"] is None
    assert calls == []  # GET never computes

    assert worker.step() is True
    done = client.get(f"/api/research/runs/{run_id}").json()
    assert done["status"] == "completed"
    assert done["result_sha256"]

    body = client.get(f"/api/research/runs/{run_id}/result").json()
    assert body["status"] == "completed"
    assert body["engine_sha256"]
    assert body["input_snapshot_sha256"]
    assert body["calendar_sha256"]
    summary = body["result"]["candidates"][0]
    assert summary["rejection_reason"] is not None
    assert "WITHDRAWN" in summary["rejection_reason"]

    again = client.get(f"/api/research/runs/{run_id}/result").json()
    assert again == body  # identical artifact, byte for byte
    assert calls == [1]  # exactly ONE engine invocation for the whole flow
    assert worker.step() is False  # spool is empty


def test_failed_run_records_its_error_honestly(tmp_path: Path) -> None:
    def exploding_engine(spec, candidates, *, baseline=None, **kwargs):
        raise RuntimeError("synthetic engine failure")

    client, worker = _build_app(tmp_path, engine_fn=exploding_engine)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    run_id = client.post("/api/research/compare", json={
        "candidate_ids": [cid], "starting_capital": "10000",
        "common_start": "2024-01-02", "common_end": "2026-09-25",
    }).json()["run_id"]
    assert worker.step() is True
    status = client.get(f"/api/research/runs/{run_id}").json()
    assert status["status"] == "failed"
    assert "synthetic engine failure" in status["error"]
    result = client.get(f"/api/research/runs/{run_id}/result").json()
    assert result["status"] == "failed"
    assert result["result"] is None
    assert result["error"]


def test_pre_custody_format_run_is_blocked_never_rerun(tmp_path: Path) -> None:
    """A spec record written by the pre-RL1-03 code (metadata embedded
    in the immutable payload) is preserved, reported as blocked, and
    never silently recomputed."""
    from tree_options.research.runstate.store import open_runstate_store

    client, worker = _build_app(tmp_path)
    cat = client.get("/api/research/candidates").json()
    cid = cat["candidates"][0]["id"]
    spec = {"candidate_ids": [cid], "starting_capital": "10000",
            "common_start": "2024-01-02", "common_end": "2026-09-25"}
    run_id = client.post("/api/research/compare", json=spec).json()["run_id"]
    # Simulate a legacy record: metadata embedded in the spec payload
    # under the same key the old code used.
    with open_runstate_store(tmp_path / "workspace") as store:
        store.replace("spec", {**spec, "id": run_id, "status": "queued",
                               "queued_at": "2026-09-25T20:59:00"}, key=run_id)
        # and remove the modern run record to mimic the legacy shape
        store.conn.execute("DELETE FROM objects WHERE kind = 'run'")
        store.conn.execute("DELETE FROM audit WHERE kind = 'run'")

    status = client.get(f"/api/research/runs/{run_id}").json()
    assert status["status"] == "blocked"
    assert "pre-custody" in status["error"]
    assert worker.step() is False  # the legacy spec is never claimed
