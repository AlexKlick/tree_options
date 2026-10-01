import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tree_options.trex.paper_workspace import DeploymentRequest, PaperWorkspace, WorkspaceRefused

NOW = datetime(2026, 9, 30, 20, tzinfo=UTC)


def request(**updates):
    return DeploymentRequest.model_validate(
        {
            "idempotency_key": "review-1",
            "account_alias": "alpaca-paper-canary",
            "strategy_version": "operational-canary/1",
            "research_job_id": "research-1",
            "intended_capital_usd": "5000",
            "max_gross_notional_usd": "100",
            "max_orders": 1,
            "ttl_seconds": 300,
            **updates,
        }
    )


def test_no_configuration_is_useful_and_provider_free(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    assert workspace.accounts()["accounts"] == []
    assert workspace.accounts()["setup_required"] is True
    assert workspace.deployments()["live_money"] is False


def test_proposals_durable_idempotent_and_do_not_authorize(tmp_path):
    workspace = PaperWorkspace(tmp_path, clock=lambda: NOW)
    proposal = workspace.propose(request())
    assert proposal["status"] == "REVIEW_REQUIRED"
    assert proposal["execution_status"] == "NOT_AUTHORIZED"
    assert PaperWorkspace(tmp_path).propose(request()) == proposal
    with pytest.raises(WorkspaceRefused, match="idempotency_collision"):
        workspace.propose(request(intended_capital_usd="6000"))
    assert len(workspace.deployments()["deployments"]) == 1
    assert not list(tmp_path.rglob("mandate.json"))


def test_halt_persists_and_cannot_be_reactivated_by_duplicate_request(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    proposal = workspace.propose(request())
    halted = workspace.halt(proposal["deployment_id"])
    assert halted["status"] == "HALTED"
    assert workspace.propose(request())["status"] == "HALTED"
    with pytest.raises(WorkspaceRefused, match="deployment_halted"):
        workspace.stage_canary(
            proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
        )


def test_canary_staging_requires_exact_approval_and_bound_caps(tmp_path):
    workspace = PaperWorkspace(tmp_path, clock=lambda: NOW)
    proposal = workspace.propose(request(strategy_version="operational-canary/1"))
    with pytest.raises(WorkspaceRefused, match="operator_approval_required"):
        workspace.stage_canary(proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"))
    staged = workspace.stage_canary(
        proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
    )
    assert staged["status"] == "STAGED"
    assert (
        workspace.stage_canary(
            proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
        )
        == staged
    )
    with pytest.raises(WorkspaceRefused, match="approval_identity_collision"):
        workspace.stage_canary(
            proposal["deployment_id"], symbol="AAPL", limit=Decimal("49"), operator_approved=True
        )
    bad = workspace.propose(
        request(idempotency_key="bad", max_orders=2, strategy_version="operational-canary/1")
    )
    with pytest.raises(WorkspaceRefused, match="canary_bounds"):
        workspace.stage_canary(
            bad["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
        )


def test_general_research_cannot_be_staged(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    sleeve = workspace.create_allocation("allocation", account_alias=None)["sleeves"][0][
        "sleeve_id"
    ]
    proposal = workspace.propose(request(strategy_version="momentum_12_1/v1", sleeve_id=sleeve))
    with pytest.raises(WorkspaceRefused, match="strategy_deployment_unqualified"):
        workspace.stage_canary(
            proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
        )


def test_private_catalog_projection_never_exposes_paths_provider_or_credentials(tmp_path):
    from tree_options.trex.snaptrade_qualification import initialize_binding

    binding = tmp_path / "secret.json"
    initialize_binding(binding)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_alias": "alpaca-paper-canary",
                        "binding_file": str(binding),
                        "state_root": str(tmp_path / "runtime"),
                    }
                ]
            }
        )
    )
    catalog.chmod(0o600)
    view = PaperWorkspace(tmp_path / "workspace", catalog=catalog).accounts()
    serialized = json.dumps(view)
    assert view["accounts"][0]["configured"] is False
    assert "private_binding_unavailable" in view["accounts"][0]["blockers"]
    assert str(tmp_path) not in serialized and "credentials" not in serialized
    catalog.chmod(0o644)
    assert PaperWorkspace(tmp_path / "workspace", catalog=catalog).accounts()["blockers"] == [
        "private_catalog_unavailable"
    ]


def test_fixed_allocation_plan_is_exact_and_cannot_duplicate_capital(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    plan = workspace.create_allocation("allocation-1", account_alias=None)
    assert len(plan["sleeves"]) == 29
    assert [s["capital_usd"] for s in plan["sleeves"]].count("50000") == 19
    assert [s["capital_usd"] for s in plan["sleeves"]].count("5000") == 10
    assert sum(Decimal(s["capital_usd"]) for s in plan["sleeves"]) == Decimal("1000000")
    assert workspace.create_allocation("allocation-1", account_alias=None) == plan
    with pytest.raises(WorkspaceRefused, match="allocation_plan_already_exists"):
        workspace.create_allocation("allocation-2", account_alias="other")
    assert plan["execution_authorized"] is False


def test_sleeve_reservations_are_atomic_and_safe_halt_releases(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    plan = workspace.create_allocation("allocation-1", account_alias=None)
    sleeve_id = plan["sleeves"][-1]["sleeve_id"]
    first = workspace.propose(request(sleeve_id=sleeve_id, max_gross_notional_usd="4000"))
    assert workspace.allocations()["plans"][0]["sleeves"][-1]["reserved_usd"] == "4000"
    with pytest.raises(WorkspaceRefused, match="sleeve_capital_exhausted"):
        workspace.propose(
            request(idempotency_key="review-2", sleeve_id=sleeve_id, max_gross_notional_usd="2000")
        )
    workspace.halt(first["deployment_id"])
    assert workspace.allocations()["plans"][0]["sleeves"][-1]["reserved_usd"] == "0"
    workspace.propose(
        request(idempotency_key="review-2", sleeve_id=sleeve_id, max_gross_notional_usd="2000")
    )
    with pytest.raises(WorkspaceRefused, match="sleeve_account_mismatch"):
        workspace.propose(
            request(idempotency_key="review-3", account_alias="other", sleeve_id=sleeve_id)
        )


def test_uncertain_effect_halt_keeps_cash_reserved(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    sleeve = workspace.create_allocation("allocation-1", account_alias=None)["sleeves"][0][
        "sleeve_id"
    ]
    proposal = workspace.propose(request(sleeve_id=sleeve))
    path = workspace._path(proposal["deployment_id"])
    record = json.loads(path.read_text())
    record["status"] = "RECOVERY_REQUIRED"
    record["execution_status"] = "CLAIMED_RECONCILIATION_REQUIRED"
    path.write_text(json.dumps(record))
    workspace.halt(proposal["deployment_id"])
    assert workspace.allocations()["plans"][0]["sleeves"][0]["reserved_usd"] == "100"


def runtime_workspace(tmp_path, *, timeout=False, quote=True):
    from tests.unit.test_snaptrade_runtime import FakeProvider
    from tree_options.execution.snaptrade_provider import load_private_binding
    from tree_options.trex.snaptrade_runtime import CanaryQuote, SnapTradePaperRuntime
    from tree_options.trex.supervised import SupervisedPaths

    provider = FakeProvider(timeout=timeout)
    binding = tmp_path / "binding.json"
    binding.write_text(
        json.dumps(
            {
                "binding": {
                    "alias": provider.binding.alias,
                    "account_id": provider.binding.account_id,
                    "brokerage_slug": "ALPACA-PAPER",
                    "paper_confirmed_by": "operator",
                    "environment": "broker_paper",
                    "live_money": False,
                },
                "credentials": {
                    "client_id": "fixture",
                    "consumer_key": "fixture",
                    "user_id": "fixture",
                    "user_secret": "fixture",
                },
            }
        )
    )
    binding.chmod(0o600)
    provider.binding, _ = load_private_binding(binding)
    catalog = tmp_path / "catalog.json"
    state = tmp_path / "runtime"
    catalog.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_alias": provider.binding.alias,
                        "binding_file": str(binding),
                        "state_root": str(state),
                    }
                ]
            }
        )
    )
    catalog.chmod(0o600)
    # Provider test fixture carries its own authoritative fake clock.
    from tests.unit.test_snaptrade_runtime import NOW as runtime_now

    workspace = PaperWorkspace(tmp_path / "workspace", catalog=catalog, clock=lambda: runtime_now)
    runtime = SnapTradePaperRuntime(
        provider,
        SupervisedPaths(state),
        ownership_root=tmp_path / "owners",
        clock=lambda: runtime_now,
        quote_source=(
            lambda symbol: CanaryQuote(
                symbol, Decimal("49"), Decimal("50"), runtime_now, "fixture-authoritative"
            )
        )
        if quote
        else None,
    )
    return workspace, runtime, provider


def test_staged_canary_runs_real_domain_once_and_retains_sleeve_provenance(tmp_path):
    workspace, runtime, provider = runtime_workspace(tmp_path)
    sleeve = workspace.create_allocation("allocation", account_alias=None)["sleeves"][0][
        "sleeve_id"
    ]
    proposal = workspace.propose(request(sleeve_id=sleeve, research_job_id=None))
    workspace.stage_canary(
        proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
    )
    observed = workspace.execute_canary(proposal["deployment_id"], runtime)
    assert observed["status"] == "OBSERVED"
    assert observed["execution_status"] == "ACKNOWLEDGED_RECONCILIATION_REQUIRED"
    assert observed["exact_economics"] is False and provider.calls == 1
    with pytest.raises(WorkspaceRefused, match="reconcile_before_retry"):
        workspace.execute_canary(proposal["deployment_id"], runtime)
    workspace.halt(proposal["deployment_id"])
    assert workspace.allocations()["plans"][0]["sleeves"][0]["reserved_usd"] == "100"
    assert runtime.paths.mandate_revoked().exists()
    facts = [
        json.loads(line)
        for line in (runtime.paths.root / "provenance.jsonl").read_text().splitlines()
    ]
    assert any(
        fact["operation"] == "deployment" and fact["refs"]["sleeve_id"] == sleeve for fact in facts
    )
    assert {
        "proposal",
        "authorization",
        "reservation",
        "permit",
        "execution",
        "reconciliation",
        "evidence",
    } <= {fact["operation"] for fact in facts}


def test_timeout_is_durable_uncertainty_never_auto_resubmitted(tmp_path):
    workspace, runtime, provider = runtime_workspace(tmp_path, timeout=True)
    proposal = workspace.propose(request())
    workspace.stage_canary(
        proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
    )
    observed = workspace.execute_canary(proposal["deployment_id"], runtime)
    assert observed["status"] == "RECOVERY_REQUIRED"
    assert provider.calls == 1
    with pytest.raises(WorkspaceRefused):
        PaperWorkspace(workspace.root, catalog=workspace.catalog).execute_canary(
            proposal["deployment_id"], runtime
        )
    runtime.start()
    assert runtime.ready is False
    runtime.close()
    assert provider.calls == 1


def test_default_cli_runtime_has_no_authoritative_quotes_and_refuses_without_claim(tmp_path):
    workspace, runtime, provider = runtime_workspace(tmp_path, quote=False)
    proposal = workspace.propose(request())
    workspace.stage_canary(
        proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
    )
    with pytest.raises(WorkspaceRefused, match="timestamped_quote_source_unavailable"):
        workspace.execute_canary(proposal["deployment_id"], runtime)
    assert provider.calls == 0
    assert workspace.deployments()["deployments"][0]["status"] == "STAGED"
    assert not runtime.paths.mandate().exists()


def test_sleeve_binding_changes_only_without_active_or_uncertain_reservations(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    plan = workspace.create_allocation("allocation", account_alias=None)
    sleeve = plan["sleeves"][0]["sleeve_id"]
    assert (
        workspace.bind_sleeve(plan["plan_id"], sleeve, "paper-a")["sleeves"][0]["account_alias"]
        == "paper-a"
    )
    proposal = workspace.propose(request(account_alias="paper-a", sleeve_id=sleeve))
    with pytest.raises(WorkspaceRefused, match="active_or_uncertain"):
        workspace.bind_sleeve(plan["plan_id"], sleeve, "paper-b")
    workspace.halt(proposal["deployment_id"])
    assert (
        workspace.bind_sleeve(plan["plan_id"], sleeve, "paper-b")["sleeves"][0]["account_alias"]
        == "paper-b"
    )
    with pytest.raises(WorkspaceRefused, match="allocation_plan_not_found"):
        workspace.bind_sleeve("a" * 64, sleeve, "paper-c")


def test_corrupt_allocation_totals_fail_closed(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    workspace.create_allocation("allocation", account_alias=None)
    path = tmp_path / "allocation-plan.json"
    plan = json.loads(path.read_text())
    plan["sleeves"][0]["capital_usd"] = "50001"
    path.write_text(json.dumps(plan))
    with pytest.raises(WorkspaceRefused, match="allocation_conservation_invalid"):
        workspace.allocations()


def test_monetary_and_strategy_contracts_fail_closed(tmp_path):
    workspace = PaperWorkspace(tmp_path)
    with pytest.raises(ValueError):
        request(max_gross_notional_usd="NaN")
    with pytest.raises(ValueError):
        request(max_gross_notional_usd="5001")
    with pytest.raises(ValueError):
        request(max_orders=True)
    with pytest.raises(WorkspaceRefused, match="sleeve_assignment_required"):
        workspace.propose(request(strategy_version="momentum_12_1/v1"))
    with pytest.raises(WorkspaceRefused, match="research_job_required"):
        workspace.propose(request(strategy_version="momentum_12_1/v1", research_job_id=None))


def test_expired_exact_approval_cannot_claim_or_submit(tmp_path):
    from tree_options.time.sessions import shift_instant

    workspace, runtime, provider = runtime_workspace(tmp_path)
    proposal = workspace.propose(request())
    workspace.stage_canary(
        proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
    )
    workspace.clock = lambda: shift_instant(runtime.clock(), 301)
    with pytest.raises(WorkspaceRefused, match="approval_expired"):
        workspace.execute_canary(proposal["deployment_id"], runtime)
    assert provider.calls == 0 and not runtime.paths.mandate().exists()


def test_wrong_runtime_account_refused_without_effect(tmp_path):
    from tree_options.execution.snaptrade_provider import PaperAccountBinding

    workspace, runtime, provider = runtime_workspace(tmp_path)
    proposal = workspace.propose(request())
    workspace.stage_canary(
        proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
    )
    provider.binding = PaperAccountBinding.model_validate(
        provider.binding.model_dump() | {"alias": "wrong-paper-account"}
    )
    with pytest.raises(WorkspaceRefused, match="approval_account_binding_mismatch"):
        workspace.execute_canary(proposal["deployment_id"], runtime)
    assert provider.calls == 0


def test_halt_remains_immediate_while_workspace_and_effect_locks_held(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from tree_options.trex.supervised import SupervisedRefused, _locked, active_mandate

    workspace, runtime, provider = runtime_workspace(tmp_path)
    proposal = workspace.propose(request())
    with (
        _locked(workspace.paths),
        _locked(runtime.paths),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        halted = pool.submit(workspace.halt, proposal["deployment_id"]).result(timeout=2)
        assert halted["status"] == "HALTED"
        assert halted["mandate_revocation_pending"] is True
        assert (runtime.paths.root / "HALT").exists()
        assert workspace.deployments()["deployments"][0]["status"] == "HALTED"
        with pytest.raises(SupervisedRefused, match="effects_halted"):
            active_mandate(
                runtime.paths,
                now=runtime.clock(),
                account_id=provider.binding.alias,
                owner_epoch=runtime.owner.epoch,
                strategy_version="operational-canary/1",
            )


def test_halt_during_quote_preflight_prevents_submit(tmp_path):
    workspace, runtime, provider = runtime_workspace(tmp_path)
    proposal = workspace.propose(request())
    workspace.stage_canary(
        proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
    )
    original_quote = runtime.quote_source

    def halting_quote(symbol):
        workspace.halt(proposal["deployment_id"])
        return original_quote(symbol)

    runtime.quote_source = halting_quote
    with pytest.raises(Exception, match="effects_halted"):
        workspace.execute_canary(proposal["deployment_id"], runtime)
    assert provider.calls == 0
    assert workspace.deployments()["deployments"][0]["status"] == "HALTED"


def test_claimed_proposal_cannot_be_staged_again(tmp_path):
    workspace, runtime, provider = runtime_workspace(tmp_path, timeout=True)
    proposal = workspace.propose(request())
    workspace.stage_canary(
        proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
    )
    workspace.execute_canary(proposal["deployment_id"], runtime)
    with pytest.raises(WorkspaceRefused, match="reconcile_before_retry"):
        workspace.stage_canary(
            proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
        )
    assert provider.calls == 1


def test_two_concurrent_reservations_cannot_oversubscribe_one_sleeve(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    workspace = PaperWorkspace(tmp_path)
    sleeve = workspace.create_allocation("allocation", account_alias=None)["sleeves"][-1][
        "sleeve_id"
    ]

    def reserve(index):
        try:
            workspace.propose(
                request(
                    idempotency_key=f"proposal-{index}",
                    sleeve_id=sleeve,
                    max_gross_notional_usd="4000",
                )
            )
            return "reserved"
        except WorkspaceRefused:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [1, 2]))
    assert sorted(results) == ["refused", "reserved"]
    assert workspace.allocations()["plans"][0]["sleeves"][-1]["reserved_usd"] == "4000"


def ibkr_catalog(tmp_path):
    output = tmp_path / "ibkr-qualification"
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "provider": "ibkr",
                        "account_alias": "ibkr-paper-primary",
                        "account_id": "DU123456",
                        "state_root": str(output),
                    }
                ]
            }
        )
    )
    catalog.chmod(0o600)
    return PaperWorkspace(tmp_path / "workspace", catalog=catalog, clock=lambda: NOW), output


def test_ibkr_catalog_projects_only_safe_read_only_alias_metadata(tmp_path):
    workspace, output = ibkr_catalog(tmp_path)
    view = workspace.accounts()
    row = view["accounts"][0]
    assert row["provider"] == "ibkr" and row["configured"] is True
    assert row["tradeable"] is False and row["equity_execution_ready"] is False
    assert "ibkr_read_only_qualification_required" in row["blockers"]
    assert str(tmp_path) not in json.dumps(view) and "DU123456" not in json.dumps(view)
    proposal = workspace.propose(request(account_alias="ibkr-paper-primary"))
    assert "ibkr_equity_execution_unimplemented" in proposal["blockers"]
    with pytest.raises(WorkspaceRefused, match="ibkr_equity_execution_unimplemented"):
        workspace.stage_canary(
            proposal["deployment_id"], symbol="AAPL", limit=Decimal("50"), operator_approved=True
        )
    workspace.halt(proposal["deployment_id"])
    assert not output.exists()  # no write into any IBKR owner/receipt directory


def ibkr_receipt(output):
    import hashlib

    from tree_options.time.sessions import shift_instant

    return {
        "schema": "trex.ibkr.read-only-qualification/v1",
        "provider": "ibkr",
        "account_alias": "ibkr-paper-primary",
        "provider_account_sha256": hashlib.sha256(b"DU123456").hexdigest(),
        "state_root": str(output.resolve()),
        "owner_state_root": "/private/owner-state",
        "owner_epoch": "fixture-epoch",
        "assessed_at": NOW.isoformat(),
        "expires_at": shift_instant(NOW, 30).isoformat(),
        "verdict": "QUALIFIED",
        "environment": "BROKER PAPER",
        "paper_verified": True,
        "ownership_verified_at_assessment": True,
        "orders_authorized": False,
        "live_money": False,
        "exact_economics": False,
        "equity_execution_ready": False,
        "findings": [],
        "observations": [
            dict(
                operation=operation,
                captured_at=NOW.isoformat(),
                digest="a" * 64,
                row_count=3 if operation == "balances" else 0,
                broker_event_at=None,
                request_id="fixture-" + operation,
            )
            for operation in ("balances", "positions", "orders", "completed_orders")
        ],
    }


def test_ibkr_qualification_projection_checks_identity_ownership_and_staleness(tmp_path):
    from tree_options.time.sessions import shift_instant

    workspace, output = ibkr_catalog(tmp_path)
    output.mkdir()
    receipt = ibkr_receipt(output)
    path = output / "read-only-qualification.json"
    path.write_text(json.dumps(receipt))
    row = workspace.accounts()["accounts"][0]
    assert row["qualification_status"] == "QUALIFIED_AT_ASSESSMENT"
    assert row["owner_held"] is False
    assert row["tradeable"] is False and row["equity_execution_ready"] is False
    for changed in (
        {"provider_account_sha256": "b" * 64},
        {"paper_verified": False},
        {"ownership_verified_at_assessment": False},
        {"equity_execution_ready": True},
        {"state_root": "/other/state"},
    ):
        path.write_text(json.dumps(receipt | changed))
        assert workspace.accounts()["accounts"][0]["qualification_status"] == "BLOCKED"
    path.write_text(json.dumps(receipt))
    workspace.clock = lambda: shift_instant(NOW, 31)
    assert "account_qualification_stale" in workspace.accounts()["accounts"][0]["blockers"]


def test_unsupported_catalog_provider_fails_closed(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "provider": "live-ibkr",
                        "account_alias": "wrong",
                        "account_id": "U123456",
                        "state_root": str(tmp_path / "state"),
                    }
                ]
            }
        )
    )
    catalog.chmod(0o600)
    assert PaperWorkspace(tmp_path / "workspace", catalog=catalog).accounts()["blockers"] == [
        "private_catalog_unavailable"
    ]


def test_ibkr_receipt_without_complete_account_observations_is_not_qualified(tmp_path):
    workspace, output = ibkr_catalog(tmp_path)
    output.mkdir()
    receipt = ibkr_receipt(output)
    path = output / "read-only-qualification.json"
    for observations in (None, [], receipt["observations"][:-1], [receipt["observations"][0]] * 4):
        altered = dict(receipt)
        if observations is None:
            altered.pop("observations")
        else:
            altered["observations"] = observations
        path.write_text(json.dumps(altered))
        assert workspace.accounts()["accounts"][0]["qualification_status"] == "BLOCKED"


def test_ibkr_observation_timestamp_digest_counts_and_expiry_are_admissible(tmp_path):
    from tree_options.time.sessions import shift_instant

    workspace, output = ibkr_catalog(tmp_path)
    output.mkdir()
    receipt = ibkr_receipt(output)
    path = output / "read-only-qualification.json"
    changes = (
        {"captured_at": shift_instant(NOW, -31).isoformat()},
        {"captured_at": shift_instant(NOW, 1).isoformat()},
        {"captured_at": NOW.replace(tzinfo=None).isoformat()},
        {"captured_at": shift_instant(NOW, -1).isoformat()},
        {"digest": "unverified"},
        {"row_count": -1},
        {"row_count": True},
        {"row_count": 2},
        {"broker_event_at": NOW.isoformat()},
    )
    for change in changes:
        changed = dict(
            receipt,
            observations=[receipt["observations"][0] | change, *receipt["observations"][1:]],
        )
        path.write_text(json.dumps(changed))
        assert workspace.accounts()["accounts"][0]["qualification_status"] == "BLOCKED"
    admissible = dict(
        receipt,
        observations=[
            receipt["observations"][0] | {"captured_at": shift_instant(NOW, -1).isoformat()},
            *receipt["observations"][1:],
        ],
        expires_at=shift_instant(NOW, 29).isoformat(),
    )
    path.write_text(json.dumps(admissible))
    assert workspace.accounts()["accounts"][0]["qualification_status"] == "QUALIFIED_AT_ASSESSMENT"


def test_ibkr_private_account_identity_cannot_be_used_as_public_alias(tmp_path):
    workspace, _output = ibkr_catalog(tmp_path)
    catalog = json.loads(workspace.catalog.read_text())
    for alias in ("DU123456", "du123456"):
        catalog["accounts"][0]["account_alias"] = alias
        workspace.catalog.write_text(json.dumps(catalog))
        projected = workspace.accounts()
        assert projected["accounts"] == []
        assert projected["blockers"] == ["private_catalog_unavailable"]
        assert "DU123456" not in json.dumps(projected)


def test_ibkr_completed_orders_observation_is_required(tmp_path):
    workspace, output = ibkr_catalog(tmp_path)
    output.mkdir()
    receipt = ibkr_receipt(output)
    receipt["observations"] = [
        observation
        for observation in receipt["observations"]
        if observation["operation"] != "completed_orders"
    ]
    (output / "read-only-qualification.json").write_text(json.dumps(receipt))
    assert workspace.accounts()["accounts"][0]["qualification_status"] == "BLOCKED"
