"""Real cockpit routes use the existing worker; all HTTP actions stay broker-free."""

from fastapi.testclient import TestClient

from tree_options.research.runstate.worker import ResearchWorker
from tree_options.trex_web.app import create_app


def test_cockpit_assignment_allocation_and_proposal_flow(tmp_path, monkeypatch):
    import tree_options.research.quant_jobs as jobs

    monkeypatch.setattr(jobs, "_source_clean", lambda: True)
    research = tmp_path / "research"
    monkeypatch.setenv("RESEARCH_WORKSPACE_DIR", str(research))
    monkeypatch.setenv("TREX_PAPER_WORKSPACE", str(tmp_path / "paper"))
    monkeypatch.setenv("TREX_QUANT_DATASETS_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv("TREX_RESEARCH_WORKER", "0")
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
    monkeypatch.delenv("TREX_PAPER_CATALOG", raising=False)
    app = create_app(state_dir=str(tmp_path / "desk"), plans_dir=str(tmp_path / "plans"))
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
        headers={"Origin": "http://127.0.0.1"},
    ) as client:
        assert client.get("/api/paper/accounts").json()["setup_required"] is True
        allocation = client.post(
            "/api/paper/allocations",
            json={"idempotency_key": "operator-million", "account_alias": None},
        )
        assert allocation.status_code == 201
        sleeves = allocation.json()["sleeves"]
        assert len(sleeves) == 29
        assert sum(int(s["capital_usd"]) for s in sleeves) == 1000000
        request = {
            "hypothesis": "Compare concentration after declared costs",
            "dataset_id": "synthetic-machinery-v1",
            "capital": "50000",
            "strategy_id": "equal_weight_us_equities",
            "top_n": 1,
            "generations": 0,
            "sleeve_id": sleeves[0]["sleeve_id"],
        }
        queued = client.post("/api/research/quant/jobs", json=request)
        assert queued.status_code == 202
        run_id = queued.json()["run_id"]
        assert client.get(f"/api/research/quant/jobs/{run_id}").json()["result"] is None
        assert ResearchWorker(workspace=research, catalog_provider=lambda: []).step()
        detail = client.get(f"/api/research/quant/jobs/{run_id}").json()
        assert detail["job"]["status"] == "completed"
        assert detail["result"]["execution_authorized"] is False
        assert detail["result"]["data_class"] == "synthetic_fixture"
        assert (
            client.get("/api/research/quant/jobs").json()["jobs"][0]["result_summary"][
                "mean_net_return"
            ]
            is not None
        )
        proposal = client.post(
            "/api/paper/deployments",
            json={
                "idempotency_key": "review-only",
                "account_alias": "alpaca-paper-canary",
                "strategy_version": detail["result"]["winner"]["version_id"],
                "research_job_id": run_id,
                "sleeve_id": sleeves[0]["sleeve_id"],
                "intended_capital_usd": "50000",
                "max_gross_notional_usd": "100",
                "max_orders": 1,
                "ttl_seconds": 300,
            },
        )
        assert proposal.status_code == 201
        assert proposal.json()["status"] == "REVIEW_REQUIRED"
        assert "strategy_deployment_unqualified" in proposal.json()["blockers"]
        assert (
            client.get("/api/paper/allocations").json()["plans"][0]["sleeves"][0]["reserved_usd"]
            == "100"
        )
        assert not list(tmp_path.rglob("mandate.json"))
        assert not list(tmp_path.rglob("*.terminal.json"))
        assert (
            client.post(
                "/api/paper/deployments/" + proposal.json()["deployment_id"] + "/halt"
            ).status_code
            == 200
        )
        assert (
            client.get("/api/paper/allocations").json()["plans"][0]["sleeves"][0]["reserved_usd"]
            == "0"
        )
