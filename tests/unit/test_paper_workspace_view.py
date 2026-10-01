from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.trex.paper_workspace import PaperWorkspace
from tree_options.trex_web.paper_view import attach


def client(tmp_path):
    app = FastAPI()
    attach(app, PaperWorkspace(tmp_path))
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))


def test_safe_read_only_setup_without_any_configuration(tmp_path):
    with client(tmp_path) as browser:
        accounts = browser.get("/api/paper/accounts")
        assert accounts.status_code == 200 and accounts.json()["accounts"] == []
        assert browser.get("/api/paper/setup").json()["campaign_deployment_supported"] is False
        assert browser.get("/api/paper/deployments").json()["deployments"] == []
        assert browser.get("/api/paper/allocations").json()["plans"] == []


def test_owner_controls_disabled_and_cross_origin_refused(tmp_path, monkeypatch):
    with client(tmp_path) as browser:
        body = {"idempotency_key": "allocation", "account_alias": None}
        assert browser.post("/api/paper/allocations", json=body).status_code == 403
        monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
        assert (
            browser.post(
                "/api/paper/allocations", json=body, headers={"Origin": "http://evil.example"}
            ).status_code
            == 403
        )
        assert (
            browser.post(
                "/api/paper/allocations",
                json=body,
                headers={"Origin": "http://127.0.0.1", "X-Forwarded-For": "127.0.0.1"},
            ).status_code
            == 403
        )
        assert not list(tmp_path.rglob("allocation-plan.json"))


def test_proposal_and_halt_only_never_create_effect_or_mandate(tmp_path, monkeypatch):
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
    with client(tmp_path) as browser:
        headers = {"Origin": "http://127.0.0.1"}
        allocation = browser.post(
            "/api/paper/allocations",
            headers=headers,
            json={"idempotency_key": "allocation", "account_alias": None},
        )
        assert allocation.status_code == 201
        proposal = browser.post(
            "/api/paper/deployments",
            headers=headers,
            json={
                "idempotency_key": "proposal",
                "account_alias": "alpaca-paper-canary",
                "strategy_version": "momentum_12_1/v1",
                "research_job_id": "job-1",
                "sleeve_id": allocation.json()["sleeves"][0]["sleeve_id"],
                "intended_capital_usd": "50000",
                "max_gross_notional_usd": "500",
                "max_orders": 2,
                "ttl_seconds": 300,
            },
        )
        assert proposal.status_code == 201
        assert proposal.json()["status"] == "REVIEW_REQUIRED"
        assert "strategy_deployment_unqualified" in proposal.json()["blockers"]
        halted = browser.post(
            "/api/paper/deployments/" + proposal.json()["deployment_id"] + "/halt", headers=headers
        )
        assert halted.status_code == 200 and halted.json()["status"] == "HALTED"
        assert (
            browser.get("/api/paper/allocations").json()["plans"][0]["sleeves"][0]["reserved_usd"]
            == "0"
        )
        assert not list(tmp_path.rglob("mandate.json"))
        assert not list(tmp_path.rglob("*.terminal.json"))
        assert browser.post("/api/paper/deployments/../halt", headers=headers).status_code in {
            404,
            409,
        }


def test_post_requests_are_bounded_and_ambiguous_json_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
    with client(tmp_path) as browser:
        headers = {"Origin": "http://127.0.0.1", "Content-Type": "application/json"}
        assert (
            browser.post("/api/paper/allocations", headers=headers, content=" " * 8193).status_code
            == 413
        )
        assert (
            browser.post(
                "/api/paper/allocations",
                headers=headers,
                content='{"idempotency_key":"a","idempotency_key":"b","account_alias":null}',
            ).status_code
            == 422
        )
        assert (
            browser.post(
                "/api/paper/deployments", headers=headers, content='{"intended_capital_usd":NaN}'
            ).status_code
            == 422
        )
        assert not (tmp_path / "allocation-plan.json").exists()


def test_disk_failures_are_sanitized_without_private_exception_text(tmp_path, monkeypatch):
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")

    def failed(*args, **kwargs):
        raise OSError("private credential or path MUST NOT ESCAPE")

    monkeypatch.setattr(PaperWorkspace, "create_allocation", failed)
    with client(tmp_path) as browser:
        response = browser.post(
            "/api/paper/allocations",
            headers={"Origin": "http://127.0.0.1"},
            json={"idempotency_key": "allocation", "account_alias": None},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "paper_workspace_unavailable"
        assert "private" not in response.text


def test_blocked_allocation_mutation_does_not_stall_read_only_requests(tmp_path, monkeypatch):
    import threading
    import time

    import anyio
    import httpx

    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
    started, release = threading.Event(), threading.Event()
    original = PaperWorkspace.create_allocation

    def blocked(self, *args, **kwargs):
        started.set()
        release.wait(2)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PaperWorkspace, "create_allocation", blocked)
    app = FastAPI()
    attach(app, PaperWorkspace(tmp_path))

    async def exercise():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as browser:

            async def mutation():
                response = await browser.post(
                    "/api/paper/allocations",
                    headers={"Origin": "http://127.0.0.1"},
                    json={"idempotency_key": "allocation", "account_alias": None},
                )
                assert response.status_code == 201

            try:
                beginning = time.monotonic()
                async with anyio.create_task_group() as group:
                    group.start_soon(mutation)
                    assert await anyio.to_thread.run_sync(started.wait, 1)
                    response = await browser.get("/api/paper/accounts")
                    assert response.status_code == 200
                    assert time.monotonic() - beginning < 1
                    release.set()
            finally:
                release.set()

    anyio.run(exercise)


def test_setup_prioritizes_existing_ibkr_and_keeps_snaptrade_alternate(tmp_path):
    with client(tmp_path) as browser:
        setup = browser.get("/api/paper/setup").json()
        assert setup["preferred_qualification_provider"] == "ibkr"
        assert setup["qualification_providers"] == ["ibkr", "snaptrade"]
        assert setup["execution_providers"] == ["snaptrade"]
        assert setup["checklist"][0]["step"] == "ibkr_first"
