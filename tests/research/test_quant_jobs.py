"""User assignments spool bounded research; HTTP never computes or trades."""

import fcntl
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.research.quant_jobs import enqueue, job_detail, set_stopped
from tree_options.research.runstate.store import open_runstate_store
from tree_options.research.runstate.worker import ResearchWorker
from tree_options.trex_web.quant_jobs_view import attach


@pytest.fixture(autouse=True)
def clean_source_for_hermetic_tests(monkeypatch):
    """Tests execute modified source; production retains the clean checkout gate."""
    monkeypatch.setattr("tree_options.research.quant_jobs._source_clean", lambda: True)


def request():
    return {
        "hypothesis": "Concentration changes net independent roundtrip returns",
        "dataset_id": "synthetic-machinery-v1",
        "capital": "5000",
        "max_candidates": 3,
        "generations": 0,
        "strategy_id": "equal_weight_us_equities",
        "top_n": 1,
        "reflect_glm53": False,
    }


def test_enqueue_is_idempotent_and_worker_publishes_real_campaign(tmp_path):
    workspace = tmp_path / "research"
    first, created = enqueue(workspace, request(), datasets_dir=tmp_path / "datasets")
    same, again = enqueue(workspace, request(), datasets_dir=tmp_path / "datasets")
    assert created and not again and first == same
    assert first["status"] == "queued"
    worker = ResearchWorker(workspace=workspace, catalog_provider=lambda: [])
    assert worker.step()
    detail = job_detail(workspace, first["run_id"])
    assert detail["job"]["status"] == "completed", detail
    assert detail["result"]["candidate_count"] == 2
    assert detail["result"]["data_class"] == "synthetic_fixture"
    assert detail["result"]["execution_authorized"] is False
    assert detail["provenance"][-1]["stage"] == "review_proposal"
    assert detail["provenance"][-1]["parents"]
    from tree_options.research.quant_jobs import jobs

    summary = jobs(workspace)[0]["result_summary"]
    assert summary["mean_net_return"] == detail["result"]["holdout"]["candidate"]["mean_net_return"]
    assert summary["exact_external_economics"] is False
    assert not worker.step()


@pytest.mark.parametrize(
    "change",
    [
        {"dataset_id": "../../secret"},
        {"capital": "NaN"},
        {"capital": 5000},
        {"max_candidates": True},
        {"max_candidates": 33},
        {"generations": 5},
        {"top_n": True},
        {"strategy_id": "robust_value_5metric"},
        {"python": "print('execute')"},
        {"reflect_glm53": "true"},
    ],
)
def test_bad_assignments_never_persist(tmp_path, change):
    with pytest.raises(ValueError):
        enqueue(tmp_path / "research", {**request(), **change}, datasets_dir=tmp_path)
    assert not (tmp_path / "research" / "runstate.sqlite3").exists()


def test_stop_resume_persist_independently_and_never_enqueue_completed(tmp_path):
    workspace = tmp_path / "research"
    job, _ = enqueue(workspace, request(), datasets_dir=tmp_path)
    stopped = set_stopped(workspace, job["run_id"], True)
    assert stopped["status"] == "stopped"
    stop = workspace / "quant-jobs" / job["run_id"] / "STOP"
    assert stop.exists()
    worker = ResearchWorker(workspace=workspace, catalog_provider=lambda: [])
    assert not worker.step()
    resumed = set_stopped(workspace, job["run_id"], False)
    assert resumed["status"] == "queued" and not stop.exists()
    assert worker.step()
    with pytest.raises(ValueError, match="completed"):
        set_stopped(workspace, job["run_id"], False)


def test_two_process_worker_ownership_blocks_claim(tmp_path):
    workspace = tmp_path / "research"
    job, _ = enqueue(workspace, request(), datasets_dir=tmp_path)
    with (workspace / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = ResearchWorker(workspace=workspace, catalog_provider=lambda: [])
        assert not worker.step()
    assert job_detail(workspace, job["run_id"])["job"]["status"] == "queued"


def test_worker_source_drift_fails_closed(tmp_path, monkeypatch):
    import tree_options.research.quant_jobs as jobs

    workspace = tmp_path / "research"
    job, _ = enqueue(workspace, request(), datasets_dir=tmp_path)
    monkeypatch.setattr(jobs, "engine_identity", lambda: "f" * 64)
    assert ResearchWorker(workspace=workspace, catalog_provider=lambda: []).step()
    detail = job_detail(workspace, job["run_id"])
    assert detail["job"]["status"] == "failed"
    assert detail["job"]["error"] == "research_source_changed"
    with open_runstate_store(workspace) as store:
        assert store.get("run", job["run_id"])["error"] == "research_source_changed"
    assert detail["result"] is None


def test_http_catalog_assignment_detail_and_controls_are_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
    workspace = tmp_path / "research"
    app = FastAPI()
    attach(app, workspace=workspace, datasets_dir=tmp_path / "datasets")
    with TestClient(
        app,
        base_url="http://localhost",
        client=("127.0.0.1", 12345),
        headers={"Origin": "http://localhost"},
    ) as client:
        catalog = client.get("/api/research/quant/datasets").json()
        assert catalog["datasets"][0]["promotion_allowed"] is False
        response = client.post("/api/research/quant/jobs", json=request())
        assert response.status_code == 202
        job = response.json()
        assert job["execution_authorized"] is False and job["live_money"] is False
        assert client.post("/api/research/quant/jobs", json=request()).status_code == 200
        detail = client.get(f"/api/research/quant/jobs/{job['run_id']}").json()
        assert detail["result"] is None and detail["job"]["status"] == "queued"
        assert len(client.get("/api/research/quant/jobs").json()["jobs"]) == 1
        assert client.post(f"/api/research/quant/jobs/{job['run_id']}/stop").status_code == 200
        assert client.post(f"/api/research/quant/jobs/{job['run_id']}/resume").status_code == 200
        assert (
            client.post(
                "/api/research/quant/jobs", content='{"capital":"1","capital":"2"}'
            ).status_code
            == 400
        )
        with open_runstate_store(workspace) as store:
            assert store.verify()["ok"]


def test_sleeves_are_distinct_research_identity_without_authority(tmp_path):
    one, _ = enqueue(
        tmp_path / "research", {**request(), "sleeve_id": "large-01"}, datasets_dir=tmp_path
    )
    two, _ = enqueue(
        tmp_path / "research", {**request(), "sleeve_id": "large-02"}, datasets_dir=tmp_path
    )
    assert one["run_id"] != two["run_id"]
    assert one["sleeve_id"] == "large-01"
    assert one["execution_authorized"] is False


@pytest.mark.parametrize("mutation_stage", ["registered", "queued"])
@pytest.mark.parametrize("revision", ["serialization", "outcome"])
def test_private_frozen_dataset_metadata_and_drift(tmp_path, monkeypatch, mutation_stage, revision):
    import tree_options.research.quant_jobs as jobs
    from tree_options.research.quant_campaign_io import make_fixture, spec_from_dict
    from tree_options.research.quant_jobs import dataset_catalog
    from tree_options.time.calendar import StaticSessionCalendar

    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    data = tmp_path / "datasets"
    data.mkdir()
    source = root / "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json"
    (data / "calendar.json").write_bytes(source.read_bytes())
    (data / "calendar.sha256").write_bytes(source.with_suffix(".sha256").read_bytes())
    calendar = StaticSessionCalendar(data / "calendar.json", data / "calendar.sha256")
    spec = make_fixture(calendar, "a" * 40, "b" * 64)
    (data / "panel.json").write_text(json.dumps(spec.to_dict()))
    row = {
        "dataset_id": "frozen-one",
        "label": "Frozen panel",
        "input_file": "panel.json",
        "input_sha256": hashlib.sha256((data / "panel.json").read_bytes()).hexdigest(),
        "calendar_file": "calendar.json",
        "calendar_checksum_file": "calendar.sha256",
        "data_class": "synthetic_fixture",
    }
    (data / "manifest.json").write_text(
        json.dumps({"schema": "trex-quant-datasets/1", "datasets": [row]})
    )
    catalog = dataset_catalog(data)
    assert catalog[1]["splits"]["train"]["period_count"] == 2
    assert catalog[1]["universe_count"] == 2
    assert "input_file" not in catalog[1] and "train" not in catalog[1]
    workspace = tmp_path / "research"
    assignment = {**request(), "dataset_id": "frozen-one"}
    if mutation_stage == "queued":
        job, _ = enqueue(workspace, assignment, datasets_dir=data)
        with open_runstate_store(workspace) as store:
            frozen = store.get("spec", job["run_id"])
            assert frozen["dataset"]["source_code_sha"] == "a" * 40
            assert frozen["dataset"]["input_sha256"] == row["input_sha256"]

    manifest_bytes = (data / "manifest.json").read_bytes()
    revised = spec.to_dict()
    if revision == "outcome":
        revised["train"][0]["closes"]["FIXTURE_A"] = "106"
    # Both revisions remain valid campaigns with unchanged time splits and
    # source identities. Removing the checksum must therefore remove the only
    # reason they are refused; malformed JSON is not a byte-custody oracle.
    parsed = spec_from_dict(revised)
    assert parsed.code_sha == spec.code_sha and parsed.lock_sha == spec.lock_sha
    (data / "panel.json").write_text(json.dumps(revised, indent=2) + "\n")
    assert hashlib.sha256((data / "panel.json").read_bytes()).hexdigest() != row["input_sha256"]
    assert (data / "manifest.json").read_bytes() == manifest_bytes

    if mutation_stage == "registered":
        with pytest.raises(ValueError, match="frozen dataset checksum changed"):
            enqueue(workspace, assignment, datasets_dir=data)
        assert not (workspace / "runstate.sqlite3").exists()
        return

    calls = []
    original_engine = jobs.run_campaign

    def observe_engine(*args, **kwargs):
        calls.append(args)
        return original_engine(*args, **kwargs)

    monkeypatch.setattr(jobs, "run_campaign", observe_engine)
    assert ResearchWorker(workspace=workspace, catalog_provider=lambda: []).step()
    detail = job_detail(workspace, job["run_id"])
    assert detail["job"]["status"] == "failed"
    assert detail["job"]["error"] == "research_dataset_changed"
    assert detail["result"] is None and detail["provenance"] == []
    assert not calls
    with open_runstate_store(workspace) as store:
        assert store.verify()["ok"]
        assert store.get("spec", job["run_id"]) == frozen


def test_uncertain_reflection_is_not_repeated_on_resume(tmp_path, monkeypatch):
    import tree_options.research.quant_jobs as jobs

    class Uncertain:
        identity = jobs.Glm53Proposer.identity
        calls = 0

        def __call__(self, feedback):
            Uncertain.calls += 1
            raise TimeoutError("uncertain remote reflection")

    monkeypatch.setattr(jobs, "Glm53Proposer", Uncertain)
    workspace = tmp_path / "research"
    job, _ = enqueue(
        workspace, {**request(), "generations": 1, "reflect_glm53": True}, datasets_dir=tmp_path
    )
    worker = ResearchWorker(workspace=workspace, catalog_provider=lambda: [])
    assert worker.step() and Uncertain.calls == 1
    assert job_detail(workspace, job["run_id"])["job"]["status"] == "failed"
    set_stopped(workspace, job["run_id"], False)
    assert worker.step() and Uncertain.calls == 1
    assert job_detail(workspace, job["run_id"])["job"]["status"] == "failed"


def test_restart_respects_stop_file_and_verifies_custody(tmp_path):
    workspace = tmp_path / "research"
    job, _ = enqueue(workspace, request(), datasets_dir=tmp_path)
    with open_runstate_store(workspace) as store:
        run = store.get("run", job["run_id"])
        store.replace("run", {**run, "status": "running"}, key=job["run_id"])
    set_stopped(workspace, job["run_id"], True)
    worker = ResearchWorker(workspace=workspace, catalog_provider=lambda: [])
    worker._requeue_interrupted()
    assert job_detail(workspace, job["run_id"])["job"]["status"] == "stopped"
    assert not worker.step()


def test_concurrent_identical_enqueues_do_not_collide(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    workspace = tmp_path / "research"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: enqueue(workspace, request(), datasets_dir=tmp_path), range(2))
        )
    assert results[0][0] == results[1][0]
    assert sorted(created for _job, created in results) == [False, True]
    with open_runstate_store(workspace) as store:
        assert store.verify()["objects"] == 2


def test_standby_worker_recovers_after_owner_dies(tmp_path):
    workspace = tmp_path / "research"
    job, _ = enqueue(workspace, request(), datasets_dir=tmp_path)
    with open_runstate_store(workspace) as store:
        run = store.get("run", job["run_id"])
        store.replace("run", {**run, "status": "running"}, key=job["run_id"])
    with (workspace / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = ResearchWorker(workspace=workspace, catalog_provider=lambda: [])
        worker._requeue_interrupted()
        assert not worker.step()
    assert worker.step()
    assert job_detail(workspace, job["run_id"])["job"]["status"] == "completed"


def test_dirty_source_refuses_before_persisting(tmp_path, monkeypatch):
    monkeypatch.setattr("tree_options.research.quant_jobs._source_clean", lambda: False)
    with pytest.raises(ValueError, match="clean source"):
        enqueue(tmp_path / "research", request(), datasets_dir=tmp_path)
    assert not (tmp_path / "research" / "runstate.sqlite3").exists()


def test_stop_during_completion_cannot_overwrite_terminal_results(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import tree_options.research.quant_jobs as jobs

    workspace = tmp_path / "research"
    job, _ = enqueue(workspace, request(), datasets_dir=tmp_path)
    completed_inner, release = Event(), Event()
    original = jobs.compute_job

    def pause_before_publish(*args):
        result = original(*args)
        completed_inner.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(jobs, "compute_job", pause_before_publish)
    worker = ResearchWorker(workspace=workspace, catalog_provider=lambda: [])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.step)
        assert completed_inner.wait(5)
        assert set_stopped(workspace, job["run_id"], True)["status"] == "stopping"
        release.set()
        assert future.result(timeout=5)
    assert job_detail(workspace, job["run_id"])["job"]["status"] == "stopped"
    assert job_detail(workspace, job["run_id"])["result"] is None
    set_stopped(workspace, job["run_id"], False)
    assert worker.step()
    assert job_detail(workspace, job["run_id"])["job"]["status"] == "completed"
    with pytest.raises(ValueError, match="completed"):
        set_stopped(workspace, job["run_id"], True)
    assert job_detail(workspace, job["run_id"])["result"] is not None
