from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tree_options.execution.records import OrderIntent
from tree_options.execution.snaptrade_provider import (
    EquityPaperEffect,
    PaperAccountBinding,
    ProviderObservation,
)
from tree_options.time.sessions import shift_instant
from tree_options.trex.account_ownership import AccountOwnership
from tree_options.trex.snaptrade_runtime import CanaryQuote, SnapTradePaperRuntime
from tree_options.trex.supervised import (
    SupervisedPaths,
    SupervisedRefused,
    grant_mandate,
    project_intent,
)

NOW = datetime(2026, 9, 29, 20, tzinfo=UTC)


class FakeProvider:
    def __init__(self, timeout=False):
        self.binding = PaperAccountBinding(
            alias="alpaca-paper-canary",
            account_id="account-1",
            brokerage_slug="ALPACA-PAPER",
            paper_confirmed_by="operator",
            credential_sha256="a" * 64,
        )
        self.timeout = timeout
        self.calls = 0
        self.clock = lambda: NOW
        self.rows = []

    def account_snapshot(self):
        obs = ProviderObservation([], NOW, "req", "a" * 64)
        return SimpleNamespace(
            binding=self.binding,
            fresh_at=lambda now: True,
            details=obs,
            balances=ProviderObservation(
                [{"currency": {"code": "USD"}, "cash": "1000"}], NOW, "b", "b" * 64
            ),
            positions=obs,
            orders=ProviderObservation(self.rows, NOW, "o", "c" * 64),
            holdings_at=NOW,
        )

    def _submit(self, **kwargs):
        self.calls += 1
        row = {
            "status": "ACCEPTED",
            "brokerage_order_id": "b1",
            "client_order_id": kwargs["client_order_id"],
            "total_quantity": kwargs["quantity"],
            "filled_quantity": 0,
            "time_updated": NOW.isoformat(),
            "time_placed": NOW.isoformat(),
        }
        self.rows.append(row)
        if self.timeout:
            raise TimeoutError
        return ProviderObservation(row, NOW, "submit-1", "d" * 64)

    def order_readback(self, order_id):
        return ProviderObservation(self.rows[0], NOW, "lookup-1", "e" * 64)


def setup(tmp_path, provider):
    paths = SupervisedPaths(tmp_path / "state")
    runtime = SnapTradePaperRuntime(
        provider,
        paths,
        ownership_root=tmp_path / "owners",
        clock=lambda: NOW,
        quote_source=lambda symbol: CanaryQuote(
            symbol, Decimal("49"), Decimal("50"), NOW, "quote-fixture"
        ),
    )
    runtime.start()
    grant_mandate(
        paths,
        now=NOW,
        account_id=provider.binding.alias,
        owner_epoch=runtime.owner.epoch,
        strategy_version="operational-canary/1",
        profile_digest="a" * 64,
        max_orders=1,
        ttl_seconds=600,
        granted_by="fixture-operator",
        environment="broker_paper",
        max_gross_notional_usd=Decimal("100"),
    )
    effect = EquityPaperEffect(
        intent_id="intent-canary",
        account_alias=provider.binding.alias,
        owner_epoch=runtime.owner.epoch,
        strategy_version="operational-canary/1",
        symbol="AAPL",
        quantity=1,
        limit=Decimal("50"),
    )
    order = OrderIntent(
        intent_id=effect.intent_id,
        contract_id=effect.symbol,
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=1,
        order_type="LIMIT",
        limit_price=effect.limit,
        intent_created_at=NOW,
        source=effect.strategy_version,
        source_sequence_id="canary-1",
    )
    return runtime, paths, effect, order


def test_canary_disabled_then_bounded_single_use_and_halt(tmp_path):
    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    with pytest.raises(SupervisedRefused, match="disabled"):
        runtime.canary(order, effect, operator_approved=False)
    assert provider.calls == 0
    result = runtime.canary(order, effect, operator_approved=True)
    assert result["outcome"] == "acknowledged"
    assert provider.calls == 1
    with pytest.raises(SupervisedRefused):
        runtime.canary(order, effect, operator_approved=True)
    runtime.halt()
    assert paths.mandate_revoked().exists()
    runtime.close()


def test_timeout_no_retry_restart_readback_fences_effect(tmp_path):
    provider = FakeProvider(timeout=True)
    runtime, paths, effect, order = setup(tmp_path, provider)
    result = runtime.canary(order, effect, operator_approved=True)
    assert result["outcome"] == "uncertain" and provider.calls == 1
    assert any(
        r.record_type == "TIMEOUT_OBSERVED" for r in project_intent(paths, order.intent_id).records
    )
    runtime.close()
    restarted = SnapTradePaperRuntime(
        provider, paths, ownership_root=tmp_path / "owners", clock=lambda: shift_instant(NOW, 121)
    )
    restarted.start()
    assert provider.calls == 1
    assert not restarted.ready
    assert restarted.projection()["executions"][0]["exact_economics"] is False
    restarted.close()


def test_account_alias_cannot_have_two_owners(tmp_path):
    first = AccountOwnership(tmp_path, "same-account")
    first.acquire()
    second = AccountOwnership(tmp_path, "same-account")
    with pytest.raises(SupervisedRefused, match="owned"):
        second.acquire()
    first.close()
    second.acquire()
    assert first.epoch != second.epoch
    second.close()


def test_canary_exposure_quote_and_binding_refusals(tmp_path):
    provider = FakeProvider()
    runtime, _paths, effect, order = setup(tmp_path, provider)
    with pytest.raises(SupervisedRefused, match="notional"):
        runtime.canary(
            order, effect.model_copy(update={"limit": Decimal("101")}), operator_approved=True
        )
    runtime.quote_source = lambda symbol: CanaryQuote(
        symbol, Decimal("49"), Decimal("50"), shift_instant(NOW, -100), "stale"
    )
    with pytest.raises(SupervisedRefused, match="quote"):
        runtime.canary(order, effect, operator_approved=True)
    assert provider.calls == 0
    runtime.close()


def test_order_id_cannot_be_reused_across_economic_intents(tmp_path):
    provider = FakeProvider()
    runtime, _paths, _effect, order = setup(tmp_path, provider)
    runtime.bind_broker_order("broker-one", order.intent_id)
    runtime.bind_broker_order("broker-one", order.intent_id)
    with pytest.raises(SupervisedRefused, match="broker_order_identity"):
        runtime.bind_broker_order("broker-one", "other-intent")
    runtime.close()


def test_one_intent_cannot_bind_changed_broker_identity(tmp_path):
    import json

    provider = FakeProvider()
    runtime, paths, _effect, order = setup(tmp_path, provider)
    try:
        runtime.bind_broker_order("observed-first", order.intent_id)
        runtime.bind_broker_order("observed-first", order.intent_id)
        with pytest.raises(SupervisedRefused, match="broker_order_identity_changed"):
            runtime.bind_broker_order("observed-replacement", order.intent_id)
        assert json.loads((paths.root / "broker-order-identities.json").read_text()) == {
            "observed-first": order.intent_id
        }
    finally:
        runtime.close()


def test_unrepresentable_ack_preserves_observed_identity_and_fences_changed_readback(tmp_path):
    import json

    from tree_options.execution.records import BrokerAcknowledgement
    from tree_options.trex.supervised import LookupUnknown

    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    original_submit = provider._submit

    def missing_ack_time(**kwargs):
        observed = original_submit(**kwargs)
        observed.body.pop("time_placed")
        return observed

    provider._submit = missing_ack_time
    try:
        result = runtime.canary(order, effect, operator_approved=True)
        assert result["outcome"] == "uncertain"
        assert provider.calls == 1
        assert not any(
            isinstance(record, BrokerAcknowledgement)
            for record in project_intent(paths, order.intent_id).records
        )
        assert json.loads((paths.root / "broker-order-identities.json").read_text()) == {
            "b1": order.intent_id
        }
    finally:
        runtime.close()

    # The provider retains the intent correlation but changes the previously
    # observed identity. There is no modeled replacement authorizing that change.
    provider.rows[0]["brokerage_order_id"] = "b2"
    restarted = SnapTradePaperRuntime(
        provider,
        paths,
        ownership_root=tmp_path / "owners",
        clock=lambda: shift_instant(NOW, 121),
    )
    try:
        restarted.start()
        lookup = restarted.lookup(order.intent_id)
        assert isinstance(lookup, LookupUnknown)
        assert lookup.reason == "broker_order_identity_changed"
        assert not restarted.ready
        assert provider.calls == 1
        assert json.loads((paths.root / "broker-order-identities.json").read_text()) == {
            "b1": order.intent_id
        }
        assert not paths.reconciled(order.intent_id).exists()
        assert not any(
            isinstance(record, BrokerAcknowledgement)
            for record in project_intent(paths, order.intent_id).records
        )
    finally:
        restarted.close()


def test_missing_fees_cannot_be_exact_zero():
    from pydantic import ValidationError

    from tree_options.execution.records import CompleteFill

    with pytest.raises(ValidationError):
        CompleteFill(
            record_id="f",
            intent_id="i",
            broker_order_id="b",
            fill_quantity=1,
            cumulative_quantity=1,
            unit_price=Decimal("50"),
            exchange_event_at=NOW,
            locally_received_at=NOW,
            source="fixture",
            source_sequence_id="f",
            broker_sequence_id="f",
        )


def test_default_owner_root_and_close_projection(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("TREX_ACCOUNT_OWNERS", str(tmp_path / "owners"))
    runtime = SnapTradePaperRuntime(
        FakeProvider(), SupervisedPaths(tmp_path / "state"), clock=lambda: NOW
    )
    runtime.start()
    runtime.publish()
    assert json.loads((runtime.paths.root / "projection.json").read_text())["ready"] is True
    runtime.close()
    projection = json.loads((runtime.paths.root / "projection.json").read_text())
    assert projection["ready"] is False and projection["owner_held"] is False


def test_unknown_lookup_never_inherits_ready_from_terminal_journal(tmp_path):
    provider = FakeProvider()
    runtime, _paths, effect, order = setup(tmp_path, provider)
    original_submit = provider._submit

    def rejected(**kwargs):
        observed = original_submit(**kwargs)
        observed.body["status"] = "REJECTED"
        return observed

    provider._submit = rejected
    result = runtime.canary(order, effect, operator_approved=True)
    assert result["outcome"] == "uncertain"
    assert not any(
        r.record_type == "ORDER_REJECT"
        for r in project_intent(runtime.paths, order.intent_id).records
    )
    runtime.clock = lambda: shift_instant(NOW, 121)
    runtime.refresh()
    assert runtime.ready
    provider.order_readback = lambda _: (_ for _ in ()).throw(TimeoutError())
    runtime.refresh()
    assert not runtime.ready
    runtime.close()


def test_identical_readback_recovery_uses_first_receipt_timestamp(tmp_path):
    provider = FakeProvider(timeout=True)
    runtime, paths, effect, order = setup(tmp_path, provider)
    runtime.canary(order, effect, operator_approved=True)
    runtime.close()
    ticks = iter([shift_instant(NOW, i) for i in range(5, 500)])
    original = provider.order_readback
    provider.order_readback = lambda order_id: ProviderObservation(
        original(order_id).body, next(ticks), "lookup", "e" * 64
    )
    restarted = SnapTradePaperRuntime(
        provider, paths, ownership_root=tmp_path / "owners", clock=lambda: shift_instant(NOW, 121)
    )
    restarted.start()
    import json

    assert (
        json.loads(paths.reconciled(order.intent_id).read_text())["verdict"]
        == "confirmed_submitted"
    )
    assert provider.calls == 1
    restarted.close()


@pytest.mark.parametrize(
    "failure,record_type",
    [("disconnect", "DISCONNECT_OBSERVED"), ("invalid_facts", "UNCERTAINTY_OBSERVED")],
)
def test_transport_and_unrepresentable_facts_keep_their_meaning(tmp_path, failure, record_type):
    from tree_options.execution.snaptrade_provider import ProviderDisconnected

    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    original = provider._submit

    def fail(**kwargs):
        observed = original(**kwargs)
        if failure == "disconnect":
            raise ProviderDisconnected("fixture disconnect")
        observed.body["filled_quantity"] = "0.5"
        return observed

    provider._submit = fail
    assert runtime.canary(order, effect, operator_approved=True)["outcome"] == "uncertain"
    records = project_intent(paths, order.intent_id).records
    assert any(r.record_type == record_type for r in records)
    assert not any(r.record_type == "TIMEOUT_OBSERVED" for r in records)
    assert provider.calls == 1
    runtime.close()


def test_campaign_research_to_broker_readback_is_persisted_and_projected(tmp_path):
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tree_options.research.quant import (
        FrozenUniverse,
        QuantSnapshot,
        campaign_proposal,
        digest,
        register_version,
        run_strategy,
    )
    from tree_options.research.runstate.store import open_runstate_store
    from tree_options.strategy_lab.contracts import Observation
    from tree_options.trex_web.quant_view import attach

    workspace = tmp_path / "research"
    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    snapshot = QuantSnapshot(
        FrozenUniverse(NOW.date(), ("AAPL",), "fixture-universe", "a" * 64),
        NOW,
        (
            Observation(
                entity_id="AAPL",
                source="fixture",
                source_id="close-one",
                event_at=NOW,
                available_at=NOW,
                values={"close": Decimal("50")},
            ),
        ),
    )
    with open_runstate_store(workspace) as store:
        candidate = register_version(
            store,
            "equal_weight_us_equities",
            code_sha="a" * 40,
            lock_sha="b" * 64,
            parameters={"top_n": 1},
        )
        control = register_version(
            store,
            "equal_weight_us_equities",
            code_sha="a" * 40,
            lock_sha="b" * 64,
            parameters={"top_n": 2},
        )
        a = run_strategy(store, candidate, snapshot)
        b = run_strategy(store, control, snapshot)
        campaign = campaign_proposal(store, a["run_id"], b["run_id"])
        runtime.canary(
            order,
            effect,
            operator_approved=True,
            research_store=store,
            campaign_id=digest(campaign),
        )
    runtime.close()
    app = FastAPI()
    attach(app, workspace=workspace, execution_state=paths.root)
    data = TestClient(app).get("/api/research/quant").json()
    link = next(row for row in data["campaigns"] if row["schema"] == "quant-execution-link/1")
    assert link["research_run"] == a["run_id"]
    assert link["intent_id"] == order.intent_id
    edges = data["execution"]["provenance"]
    assert {
        "proposal",
        "authorization",
        "reservation",
        "permit",
        "execution",
        "reconciliation",
        "evidence",
    } <= {row["operation"] for row in edges}
    assert json.loads(paths.terminal(order.intent_id).read_text())["permit_id"] in json.dumps(edges)
    assert data["execution"]["ready"] is False
    assert data["execution"]["executions"][0]["exact_economics"] is False


def test_restart_durable_claim_before_journal_is_reconciled_without_submit(tmp_path):
    import hashlib
    import json

    from tree_options.execution.snaptrade_adapter import stable_client_order_id
    from tree_options.trex.supervised import SupervisedIntent

    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    claim = SupervisedIntent(
        intent=order,
        package_intent_sha256=hashlib.sha256(effect.payload()).hexdigest(),
        created_at=NOW,
        send_deadline=shift_instant(NOW, 30),
    ).model_dump(mode="json")
    claim.update(claimed_at=NOW.isoformat(), permit_id="claimed-permit")
    paths.outbox_dir().mkdir(parents=True, exist_ok=True)
    paths.sending(order.intent_id).write_text(json.dumps(claim))
    provider.rows = [
        {
            "status": "ACCEPTED",
            "brokerage_order_id": "b1",
            "client_order_id": stable_client_order_id(order.intent_id),
            "total_quantity": 1,
            "filled_quantity": 0,
            "time_updated": NOW.isoformat(),
        }
    ]
    runtime.close()
    restarted = SnapTradePaperRuntime(
        provider, paths, ownership_root=tmp_path / "owners", clock=lambda: shift_instant(NOW, 121)
    )
    restarted.start()
    assert provider.calls == 0 and not restarted.ready
    assert any(
        r.record_type == "UNCERTAINTY_OBSERVED"
        for r in project_intent(paths, order.intent_id).records
    )
    assert paths.terminal(order.intent_id).exists()
    restarted.close()


def test_preflight_uses_time_after_reads_and_expired_permit_never_submits(tmp_path):
    provider = FakeProvider()
    runtime, _paths, effect, order = setup(tmp_path, provider)
    current = [NOW]
    runtime.clock = lambda: current[0]
    original = provider.account_snapshot

    def advancing_account():
        current[0] = shift_instant(current[0], 10)
        account = original()
        at = current[0]
        account.fresh_at = lambda now: 0 <= (now - at).total_seconds() <= 30
        return account

    provider.account_snapshot = advancing_account
    runtime.quote_source = lambda symbol: CanaryQuote(
        symbol, Decimal("49"), Decimal("50"), current[0], "current-quote"
    )
    result = runtime.canary(order, effect, operator_approved=True)
    assert result["outcome"] == "uncertain"
    assert provider.calls == 0
    runtime.close()


@pytest.mark.parametrize(
    "surface,body,reason",
    [
        ("balances", [None], "account_balance_shape_invalid"),
        ("balances", [{"currency": None, "cash": "1000"}], "account_balance_shape_invalid"),
        ("balances", [{"currency": "USD", "cash": "1000"}], "account_balance_shape_invalid"),
        ("balances", [{"currency": {"code": "USD"}, "cash": None}], "account_cash_invalid"),
        ("balances", [{"currency": {"code": "USD"}, "cash": "bad"}], "account_cash_invalid"),
        ("balances", [{"currency": {"code": "USD"}, "cash": "NaN"}], "account_cash_invalid"),
        ("balances", [{"currency": {"code": "USD"}, "cash": "Infinity"}], "account_cash_invalid"),
        ("orders", [None], "account_order_shape_invalid"),
        ("orders", [{"status": None}], "account_order_shape_invalid"),
    ],
)
def test_malformed_account_preflight_is_explicitly_refused(tmp_path, surface, body, reason):
    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    original_snapshot = provider.account_snapshot

    def malformed_snapshot():
        account = original_snapshot()
        setattr(account, surface, ProviderObservation(body, NOW, "malformed", "a" * 64))
        return account

    provider.account_snapshot = malformed_snapshot
    try:
        with pytest.raises(SupervisedRefused, match=reason):
            runtime.canary(order, effect, operator_approved=True)
        assert provider.calls == 0
        assert not paths.terminal(order.intent_id).exists()
    finally:
        runtime.close()


def test_preflight_refusal_after_permit_consumption_keeps_its_meaning(tmp_path):
    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    validate = runtime.validate_effect
    calls = 0

    def refused_at_final_boundary(payload, intent, permit=None):
        nonlocal calls
        calls += 1
        if calls == 3:
            assert permit is not None and paths.permit_consumed(permit.permit_id).exists()
            raise SupervisedRefused("risk_changed")
        return validate(payload, intent, permit)

    runtime.validate_effect = refused_at_final_boundary
    try:
        result = runtime.canary(order, effect, operator_approved=True)
        assert result["outcome"] == "uncertain"
        assert result["reason"] == "preflight_refused"
        assert "no provider effect" in result["detail"]
        assert provider.calls == 0
        record_types = {r.record_type for r in project_intent(paths, order.intent_id).records}
        assert "UNCERTAINTY_OBSERVED" in record_types
        assert "TIMEOUT_OBSERVED" not in record_types
        assert "DISCONNECT_OBSERVED" not in record_types
        assert paths.permit_consumed(result["permit_id"]).exists()
    finally:
        runtime.close()


def test_identity_refusal_after_submit_does_not_claim_no_provider_effect(tmp_path):
    provider = FakeProvider()
    runtime, _paths, effect, order = setup(tmp_path, provider)
    try:
        runtime.bind_broker_order("b1", "other-intent")
        result = runtime.canary(order, effect, operator_approved=True)
        assert result["outcome"] == "uncertain"
        assert result["reason"] == "provider_facts_unrepresentable"
        assert "no provider effect" not in result["detail"]
        assert provider.calls == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "guard",
    ["intent_not_preflighted", "consumed_permit_absent", "consumed_permit_binding_mismatch"],
)
def test_local_submit_guards_are_unclassified_without_provider_effect(tmp_path, guard):
    from tree_options.execution.records import SubmitAttempt
    from tree_options.trex.supervised import EffectPermit

    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    attempt = SubmitAttempt(
        record_id="local-attempt",
        intent_id=order.intent_id,
        send_attempt_at=NOW,
        source="fixture",
        source_sequence_id="local-permit",
    )
    if guard != "intent_not_preflighted":
        runtime.validate_effect(effect.payload(), order)
    if guard == "consumed_permit_binding_mismatch":
        permit = EffectPermit(
            permit_id="local-permit",
            mandate_id="local-mandate",
            intent_id="different-intent",
            package_intent_sha256="a" * 64,
            effect_sha256="b" * 64,
            account_id=provider.binding.alias,
            owner_epoch=runtime.owner.epoch,
            strategy_version=effect.strategy_version,
            screening_sha256="c" * 64,
            issued_at=NOW,
            expires_at=shift_instant(NOW, 15),
        )
        paths.permit_consumed(permit.permit_id).write_text(permit.model_dump_json())
    try:
        outcome = runtime.submit(attempt, effect.payload())
        assert outcome.reason == guard
        assert outcome.observation == "unclassified"
        assert "no provider effect" in outcome.detail
        assert provider.calls == 0
    finally:
        runtime.close()
