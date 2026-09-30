"""Actual SDK completion futures with fake callbacks; no gateway connection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from ib_async import IB, Contract, Order, OrderState

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.time.sessions import shift_instant
from tree_options.trex.account_ownership import AccountOwnership
from tree_options.trex.desk_runtime import DeskPaths, DeskRuntime
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.ibkr_paper_qualification import persist_receipt, qualify_read_only
from tree_options.trex.supervised import SupervisedPaths, SupervisedRefused
from tree_options.trex.supervised_desk import SupervisedDesk
from tree_options.trex.supervised_ibkr import IbkrSupervisedBroker

ACCOUNT = "DU1234567"
ALIAS = "ibkr-paper-primary"
NOW = datetime(2026, 9, 30, 16, tzinfo=UTC)


@pytest.fixture
def owned(tmp_path, monkeypatch):
    policy = asyncio.get_event_loop_policy()
    prior_loop = getattr(getattr(policy, "_local", None), "_loop", None)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    wire = IB()
    wire.wrapper.accounts = [ACCOUNT]
    wire.wrapper.clientId = 83
    monkeypatch.setattr(wire.client, "isReady", lambda: True)
    state = SimpleNamespace(
        now=NOW,
        reads=[],
        canceled=[],
        no_callbacks=False,
        other_request=False,
        step=0,
        money="1000",
        statuses="Submitted",
        total=1,
        filled=0,
        remaining=1,
    )
    monkeypatch.setattr(wire.client, "getReqId", lambda: 991)

    def summary(req_id, group, tags):
        state.reads.append("balances")
        if not state.no_callbacks:
            for tag in ("NetLiquidation", "TotalCashValue", "BuyingPower"):
                wire.wrapper.accountSummary(
                    req_id if not state.other_request else 990, ACCOUNT, tag, state.money, "USD"
                )
        wire.wrapper.accountSummaryEnd(req_id)

    def positions():
        state.reads.append("positions")
        wire.wrapper.position(ACCOUNT, Contract(conId=123, symbol="SPY", secType="STK"), 2.5, 150)
        wire.wrapper.positionEnd()

    def orders():
        state.reads.append("orders")
        wire.wrapper.openOrder(
            42,
            Contract(conId=123, symbol="SPY", secType="STK"),
            Order(account=ACCOUNT, orderId=42, clientId=83, totalQuantity=state.total),
            OrderState(status=state.statuses),
        )
        trade = wire.wrapper.trades[(83, 42)]
        trade.orderStatus.filled = state.filled
        trade.orderStatus.remaining = state.remaining
        wire.wrapper.openOrderEnd()

    monkeypatch.setattr(wire.client, "reqAccountSummary", summary)
    monkeypatch.setattr(wire.client, "cancelAccountSummary", lambda req: state.canceled.append(req))
    monkeypatch.setattr(wire.client, "reqPositions", positions)
    monkeypatch.setattr(wire.client, "reqAllOpenOrders", orders)

    def completed(api_only):
        assert api_only is False
        state.reads.append("completed_orders")
        wire.wrapper.completedOrdersEnd()

    monkeypatch.setattr(wire.client, "reqCompletedOrders", completed)
    run = wire.run

    def bounded_run(future, timeout):
        assert timeout == 5
        state.now = shift_instant(state.now, state.step)
        return run(future, timeout=timeout)

    monkeypatch.setattr(wire, "run", bounded_run)
    for name in ("placeOrder", "cancelOrder", "connect", "disconnect"):
        monkeypatch.setattr(wire, name, lambda *a, **k: pytest.fail("economic/session mutation"))
    ib = IbkrTrex(client_id=83)
    ib._ib = wire

    def clock():
        return state.now

    sup = SupervisedPaths(tmp_path / "supervised")
    runtime = DeskRuntime(ib, DeskPaths(tmp_path / "desk"), supervised=sup, clock=clock)
    runtime.acquire()
    fence = AccountOwnership(tmp_path / "owners", ALIAS, epoch="desk83-qualification")
    fence.acquire()
    desk = SupervisedDesk(
        ib, runtime, IbkrSupervisedBroker(ib), supervised=sup, owner_epoch=fence.epoch, clock=clock
    )
    desk.write_owner()
    fixture = SimpleNamespace(
        desk=desk, fence=fence, state=state, wire=wire, output=tmp_path / "qualification-output"
    )
    yield fixture
    fence.close()
    runtime.release()
    loop.close()
    asyncio.set_event_loop(prior_loop)


def qualify(owned, **kwargs):
    return qualify_read_only(
        owned.desk,
        owned.fence,
        account_id=ACCOUNT,
        account_alias=ALIAS,
        output=kwargs.pop("output", owned.output),
        **kwargs,
    )


def test_same_owned_sdk_session_qualifies_without_orders_or_new_owner(owned):
    receipt = qualify(owned)
    assert receipt["verdict"] == "QUALIFIED", receipt
    assert receipt["paper_verified"] is True and receipt["ownership_verified_at_assessment"] is True
    assert receipt["equity_execution_ready"] is False
    assert receipt["orders_authorized"] is False and receipt["exact_economics"] is False
    assert receipt["live_money"] is False
    assert owned.fence.held and owned.desk.runtime._lock_handle is not None
    assert owned.state.reads == ["balances", "positions", "orders", "completed_orders"]
    assert owned.state.canceled == [991]
    assert all(row["broker_event_at"] is None for row in receipt["observations"])
    assert receipt["provider_account_sha256"] == hashlib.sha256(ACCOUNT.encode()).hexdigest()
    assert ACCOUNT not in json.dumps(receipt)
    assert "accountSummary" not in owned.wire.wrapper.__dict__


def test_foreign_subscription_callbacks_cannot_qualify_missing_own_request(owned):
    owned.state.other_request = True
    receipt = qualify(owned)
    assert receipt["verdict"] == "BLOCKED"
    assert receipt["findings"] == ["account_balance_missing"]
    assert owned.state.canceled == [991]
    assert "accountSummary" not in owned.wire.wrapper.__dict__


def test_cached_summary_is_not_current_request_evidence(owned):
    for tag in ("NetLiquidation", "TotalCashValue", "BuyingPower"):
        owned.wire.wrapper.accountSummary(900, ACCOUNT, tag, "1000", "USD")
    owned.state.no_callbacks = True
    receipt = qualify(owned)
    assert receipt["verdict"] == "BLOCKED" and receipt["findings"] == ["account_balance_missing"]


@pytest.mark.parametrize("money", ["NaN", "Infinity", "malformed"])
def test_bad_actual_money_callbacks_fail_closed(owned, money):
    owned.state.money = money
    assert qualify(owned)["verdict"] == "BLOCKED"


@pytest.mark.parametrize("guard", ["lease", "epoch", "lock", "port", "client", "managed"])
def test_owner_and_paper_guards_block_before_read_requests(owned, guard):
    if guard == "lease":
        owned.fence.close()
    elif guard == "epoch":
        owned.desk.owner_epoch = "different-epoch"
    elif guard == "lock":
        owned.desk.runtime.release()
    elif guard == "port":
        owned.desk.ib.port = 4001
    elif guard == "client":
        owned.desk.ib.client_id = 84
    else:
        owned.wire.wrapper.accounts = ["U1234567"]
    receipt = qualify(owned)
    assert receipt["verdict"] == "BLOCKED" and owned.state.reads == []


def test_stale_observations_refuse_even_when_requests_return(owned):
    owned.state.step = 20
    receipt = qualify(owned)
    assert receipt["verdict"] == "BLOCKED"
    assert receipt["findings"] == ["observation_stale_or_future"]


def test_assessment_is_final_completion_and_expiry_uses_oldest_read(owned):
    owned.state.step = 1
    receipt = qualify(owned)
    assert receipt["verdict"] == "QUALIFIED"
    assert datetime.fromisoformat(receipt["assessed_at"]) == owned.state.now
    assert datetime.fromisoformat(receipt["expires_at"]) == shift_instant(NOW, 31)


@pytest.mark.parametrize("field,value", [("statuses", "UNKNOWN"), ("remaining", 2)])
def test_ambiguous_order_facts_refuse(owned, field, value):
    setattr(owned.state, field, value)
    assert qualify(owned)["verdict"] == "BLOCKED"


def test_pending_owner_request_is_not_overwritten(owned):
    original = owned.wire.wrapper.startReq("positions")
    receipt = qualify(owned)
    assert receipt["findings"] == ["owner_read_request_in_progress"]
    assert owned.wire.wrapper._futures["positions"] is original
    assert owned.state.reads == []
    owned.wire.wrapper._endReq("positions")


def test_prior_working_orders_missing_from_snapshot_hold(owned):
    contract = Contract(conId=123, symbol="SPY", secType="STK")
    owned.wire.wrapper.openOrder(
        41,
        contract,
        Order(account=ACCOUNT, orderId=41, clientId=83),
        OrderState(status="Submitted"),
    )
    receipt = qualify(owned)
    assert receipt["findings"] == ["previously_known_order_absent"]
    assert (83, 41) in owned.wire.wrapper.trades


@pytest.mark.parametrize("where", ["desk-child", "supervised-child", "fence-child", "ancestor"])
def test_output_cannot_overlap_live_owner_roots(owned, where):
    output = {
        "desk-child": owned.desk.paths.root / "qualification",
        "supervised-child": owned.desk.supervised.root / "qualification",
        "fence-child": owned.fence.root / "qualification",
        "ancestor": owned.output.parent,
    }[where]
    with pytest.raises(SupervisedRefused, match="overlaps_owner_state"):
        qualify(owned, output=output)
    assert owned.state.reads == []


def test_historical_receipt_is_immutable_and_collision_preserves_latest(owned):
    receipt = qualify(owned)
    content = canonical_bytes(receipt)
    historical = (
        owned.output / "qualification" / "ibkr" / (hashlib.sha256(content).hexdigest() + ".json")
    )
    inode = historical.stat().st_ino
    persist_receipt(owned.output, receipt)
    assert historical.stat().st_ino == inode
    historical.write_text("conflicting historical bytes")
    latest = (owned.output / "read-only-qualification.json").read_bytes()
    with pytest.raises(SupervisedRefused, match="identity_collision"):
        persist_receipt(owned.output, receipt)
    assert (owned.output / "read-only-qualification.json").read_bytes() == latest


def test_alias_cannot_qualify_different_managed_account_under_same_fence(owned):
    owned.wire.wrapper.accounts = [ACCOUNT, "DU7654321"]
    receipt = qualify(owned)
    assert receipt["verdict"] == "BLOCKED"
    assert "qualification_account_alias_ambiguous" in receipt["findings"]
    assert owned.state.reads == []


def test_owner_hook_uses_held_alias_lease_and_same_session(owned):
    from tree_options.trex.supervised_desk import qualify_owned_account

    receipt = qualify_owned_account(
        owned.desk, [owned.fence], account_id=ACCOUNT, account_alias=ALIAS, output=owned.output
    )
    assert receipt["verdict"] == "QUALIFIED"
    assert owned.fence.held


@pytest.mark.parametrize(
    "flags",
    [
        ["--qualification-account", ACCOUNT],
        ["--qualification-receipt", "/tmp/unused-qualification"],
    ],
)
def test_incomplete_owner_hook_flags_refuse_before_session_constructor(monkeypatch, flags):
    from tree_options.trex import supervised_desk

    monkeypatch.setattr(
        supervised_desk, "IbkrTrex", lambda **kwargs: pytest.fail("must not construct session")
    )
    with pytest.raises(SystemExit) as error:
        supervised_desk.main(["run", *flags])
    assert error.value.code == 2


def test_timeout_restores_callback_cancels_only_new_read_and_keeps_owners(owned, monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("credential-bearing provider detail must stay private")

    monkeypatch.setattr(owned.wire, "run", timeout)
    receipt = qualify(owned)
    assert receipt["verdict"] == "BLOCKED"
    assert receipt["findings"] == ["owner_observation_unavailable"]
    assert "credential-bearing" not in json.dumps(receipt)
    assert "accountSummary" not in owned.wire.wrapper.__dict__
    assert owned.state.canceled == [991]
    assert owned.fence.held and owned.desk.runtime._lock_handle is not None


def test_actual_owner_receipt_projects_as_qualified_read_only_account(owned):
    from tree_options.trex.paper_workspace import PaperWorkspace

    receipt = qualify(owned)
    assert receipt["verdict"] == "QUALIFIED"
    catalog = owned.output.parent / "private-paper-catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "provider": "ibkr",
                        "account_alias": ALIAS,
                        "account_id": ACCOUNT,
                        "state_root": str(owned.output),
                    }
                ]
            }
        )
    )
    catalog.chmod(0o600)
    workspace = PaperWorkspace(
        owned.output.parent / "workspace", catalog=catalog, clock=lambda: owned.state.now
    )
    projection = workspace.accounts()
    row = projection["accounts"][0]
    assert row["qualification_status"] == "QUALIFIED_AT_ASSESSMENT", row
    assert row["tradeable"] is False and row["equity_execution_ready"] is False
    assert row["owner_held"] is False
    assert row["blockers"] == ["ibkr_equity_execution_unimplemented"]
    public = json.dumps(projection)
    assert ACCOUNT not in public and str(owned.output) not in public
    assert str(owned.desk.paths.root) not in public


@pytest.mark.parametrize("public_alias", [ACCOUNT, ACCOUNT.lower()])
def test_raw_account_identifier_cannot_be_persisted_as_public_alias(owned, public_alias):
    with pytest.raises(SupervisedRefused, match="requires_private_label"):
        qualify_read_only(
            owned.desk, owned.fence, account_id=ACCOUNT, account_alias=public_alias, output=owned.output
        )
    assert not owned.output.exists() and owned.state.reads == []


@pytest.mark.parametrize("operation", ["positions", "orders"])
def test_missing_account_identity_in_rows_refuses(owned, monkeypatch, operation):
    if operation == "positions":

        def malformed():
            owned.wire.wrapper.position(
                "", Contract(conId=123, symbol="SPY", secType="STK"), 2, 150
            )
            owned.wire.wrapper.positionEnd()

        monkeypatch.setattr(owned.wire.client, "reqPositions", malformed)
    else:

        def malformed():
            owned.wire.wrapper.openOrder(
                42,
                Contract(conId=123, symbol="SPY", secType="STK"),
                Order(account="", orderId=42, clientId=83, totalQuantity=1),
                OrderState(status="Submitted"),
            )
            owned.wire.wrapper.openOrderEnd()

        monkeypatch.setattr(owned.wire.client, "reqAllOpenOrders", malformed)
    receipt = qualify(owned)
    assert receipt["verdict"] == "BLOCKED"
    assert receipt["findings"] == ["provider_row_account_unavailable"]
