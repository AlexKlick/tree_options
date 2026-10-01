"""IBKR adapter for the supervised port: wire content, paper binding, lookup.

Drives the REAL ``IbkrTrex`` (contract building, BAG orders, order bounds)
over the shared ``FakeGateway`` extended with the views the adapter reads
(managed accounts, all-client open orders, completed orders, executions)
and a scripted order-status walk. Oracles are the wire fields IBKR would
receive and the supervised records, never adapter internals.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.trex_fakes import SupervisedGateway
from tree_options.execution import ExecutionLifecycle, ExecutionState, OrderIntent, SubmitAttempt
from tree_options.time.sessions import shift_instant
from tree_options.trex.ibkr import GATEWAY_LIVE_PORT, GATEWAY_PAPER_PORT, IbkrTrex
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised import (
    Acknowledged,
    LookupUnknown,
    NotSubmitted,
    Refused,
    Submitted,
    Uncertain,
)
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    SupervisedEffect,
    decode_effect,
    effect_bytes,
    supervised_order_ref,
)

pytest.importorskip("ib_async")

T0 = datetime(2026, 9, 28, 14, 30, tzinfo=UTC)
ACCOUNT = "DU1234567"
F = "20261016"
CON = {
    ("OPT", "SPY", F, 100.0, "P"): 100,
    ("OPT", "SPY", F, 95.0, "P"): 95,
    ("STK", "SPY", "", 0.0, ""): 1,
}
DEBIT_PUT = LegStructure(
    id="dv",
    underlying="SPY",
    kind="debit_vertical",
    legs=[
        {"right": "P", "action": "BUY", "strike": "100", "expiry": date(2026, 10, 16)},
        {"right": "P", "action": "SELL", "strike": "95", "expiry": date(2026, 10, 16)},
    ],
    quantity=2,
    entry_date=date(2026, 9, 28),
    exit_deadline=date(2026, 10, 9),
    limit="1.00",
    exits={"touch": False, "breach": False},
)


def _session(
    port: int = GATEWAY_PAPER_PORT, client_id: int = SUPERVISED_CLIENT_ID
) -> tuple[IbkrTrex, SupervisedGateway]:
    gw = SupervisedGateway(CON, ACCOUNT)
    ib = IbkrTrex(port=port, client_id=client_id)
    ib._ib = gw
    return ib, gw


def _effect(intent_id: str = "sup-001", **overrides: Any) -> SupervisedEffect:
    fields: dict[str, Any] = {
        "intent_id": intent_id,
        "account_id": ACCOUNT,
        "structure": DEBIT_PUT,
        "side": "BUY",
        "quantity": 1,
        "limit": Decimal("0.90"),
        "order_ref": supervised_order_ref(intent_id),
    }
    fields.update(overrides)
    return SupervisedEffect(**fields)


def _attempt(intent_id: str = "sup-001") -> SubmitAttempt:
    return SubmitAttempt(
        record_id=f"sup-send-{intent_id}",
        intent_id=intent_id,
        send_attempt_at=T0,
        source="supervised",
        source_sequence_id=f"permit-{intent_id}",
    )


def _broker(ib: IbkrTrex, **kwargs: Any) -> IbkrSupervisedBroker:
    return IbkrSupervisedBroker(ib, clock=lambda: shift_instant(T0, 2), **kwargs)


# ---------------------------------------------------------------- effect


def test_effect_roundtrips_through_canonical_bytes():
    effect = _effect()
    payload = effect_bytes(effect)
    assert decode_effect(payload) == effect
    assert json.loads(payload)["order_ref"] == "trex:sup:sup-001"


def test_non_canonical_payload_refused():
    pretty = json.dumps(json.loads(effect_bytes(_effect())), indent=2).encode()
    with pytest.raises(ValueError, match="canonical"):
        decode_effect(pretty)


def test_unhashed_extra_field_refused():
    document = json.loads(effect_bytes(_effect()))
    document["tif"] = "GTC"
    with pytest.raises(ValueError):
        decode_effect(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"order_ref": "trex:dv"}, "supervised tag"),
        ({"account_id": "U7654321"}, "paper accounts"),
        ({"side": "SELL"}, "opens packages only"),
        ({"quantity": 3}, "exceeds"),
        ({"structure": DEBIT_PUT.model_copy(update={"id": "sup:x"})}, "collide"),
    ],
)
def test_effect_binding_rules(overrides, message):
    with pytest.raises(ValueError, match=message):
        _effect(**overrides)


# ----------------------------------------------------------- paper guard


def test_paper_session_has_no_blockers():
    ib, _ = _session()
    assert _broker(ib).paper_blockers(ACCOUNT) == []


@pytest.mark.parametrize(
    "setup, blocker",
    [
        (lambda gw: setattr(gw, "is_connected", False), "gateway_disconnected"),
        (lambda gw: setattr(gw, "accounts", ["DU9999999"]), "account_not_managed_by_session"),
    ],
)
def test_paper_blockers_from_session_state(setup, blocker):
    ib, gw = _session()
    setup(gw)
    assert blocker in _broker(ib).paper_blockers(ACCOUNT)


def test_live_port_and_foreign_client_are_blocked():
    ib, _ = _session(port=GATEWAY_LIVE_PORT, client_id=77)
    blockers = _broker(ib).paper_blockers(ACCOUNT)
    assert "not_paper_gateway_port" in blockers
    assert "wrong_client_id" in blockers


def test_preflight_passes_with_a_two_sided_package_quote():
    ib, gw = _session()
    broker = _broker(ib)
    payload = effect_bytes(_effect())
    assert broker.preflight(payload) == ["package_quote_incomplete"]
    gw.quote(100, 2.00, 2.20)
    gw.quote(95, 1.10, 1.25)
    assert broker.preflight(payload) == []
    assert gw.trades == [], "preflight must never place"


def test_preflight_refuses_limit_above_the_cap():
    ib, _ = _session()
    blockers = _broker(ib).preflight(effect_bytes(_effect(limit=Decimal("1.50"))))
    assert blockers and blockers[0].startswith("order_unbuildable:")


# ---------------------------------------------------------------- submit


def test_submit_places_the_permitted_order_with_tag_and_account():
    ib, gw = _session()
    outcome = _broker(ib).submit(_attempt(), effect_bytes(_effect()))
    assert isinstance(outcome, Acknowledged)
    (trade,) = gw.trades
    assert trade.contract.secType == "BAG"
    assert trade.order.orderRef == "trex:sup:sup-001"
    assert trade.order.account == ACCOUNT
    assert (trade.order.action, trade.order.totalQuantity, trade.order.lmtPrice) == ("BUY", 1, 0.90)
    assert trade.order.tif == "DAY"
    assert outcome.acknowledgement.broker_order_id == str(trade.order.orderId)
    # The acknowledgement folds cleanly into the execution lifecycle.
    intent = OrderIntent(
        intent_id="sup-001",
        contract_id="BAG:dv",
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=1,
        order_type="LIMIT",
        limit_price=Decimal("0.90"),
        execution_style="package",
        package_id="dv",
        intent_created_at=T0,
        source="supervised",
        source_sequence_id="seq-sup-001",
    )
    lifecycle = ExecutionLifecycle.start(intent).apply(_attempt()).apply(outcome.acknowledgement)
    assert lifecycle.state == ExecutionState.ACKNOWLEDGED


def test_submit_waits_through_pending_states():
    ib, gw = _session()
    gw.status_script = ["PendingSubmit", "PendingSubmit", "PreSubmitted"]
    outcome = _broker(ib).submit(_attempt(), effect_bytes(_effect()))
    assert isinstance(outcome, Acknowledged)
    assert gw.slept.count(0.25) == 2


def test_submit_reports_a_broker_rejection():
    ib, gw = _session()
    gw.status_script = ["PendingSubmit", "Inactive"]
    gw.reject_code = 201
    outcome = _broker(ib).submit(_attempt(), effect_bytes(_effect()))
    assert isinstance(outcome, Refused)
    assert outcome.reject.reason_code == "IB201"


def test_submit_ack_timeout_is_uncertain_after_bounded_polls():
    ib, gw = _session()
    gw.status_script = ["PendingSubmit"]
    outcome = _broker(ib, ack_timeout_s=1.0, poll_s=0.25).submit(
        _attempt(), effect_bytes(_effect())
    )
    assert isinstance(outcome, Uncertain)
    assert outcome.reason == "ack_timeout"
    assert gw.slept.count(0.25) == math.ceil(1.0 / 0.25)
    assert len(gw.trades) == 1, "one order on the wire, never a retry"


@pytest.mark.parametrize(
    "setup, reason",
    [
        (
            lambda gw: gw.completed.append(
                SimpleNamespace(
                    order=SimpleNamespace(orderRef="trex:sup:sup-001", orderId=41, permId=9001)
                )
            ),
            "not_sent:tag_already_at_broker",
        ),
        (
            lambda gw: setattr(gw, "views_error", ConnectionError("down")),
            "not_sent:duplicate_check_unreadable",
        ),
        (lambda gw: setattr(gw, "accounts", ["DU9999999"]), "not_sent:session"),
    ],
)
def test_submit_refusals_never_touch_the_wire(setup, reason):
    ib, gw = _session()
    setup(gw)
    outcome = _broker(ib).submit(_attempt(), effect_bytes(_effect()))
    assert isinstance(outcome, Uncertain)
    assert outcome.reason == reason
    assert gw.trades == []


def test_submit_refuses_an_effect_for_another_intent():
    ib, gw = _session()
    outcome = _broker(ib).submit(_attempt("sup-001"), effect_bytes(_effect("sup-002")))
    assert isinstance(outcome, Uncertain)
    assert outcome.reason == "not_sent:intent_mismatch"
    assert gw.trades == []


# ---------------------------------------------------------------- lookup


def test_lookup_not_submitted_only_with_every_view_read():
    ib, gw = _session()
    broker = _broker(ib)
    assert isinstance(broker.lookup("sup-001"), NotSubmitted)
    gw.views_error = TimeoutError("slow")
    assert isinstance(broker.lookup("sup-001"), LookupUnknown)
    gw.views_error = None
    gw.is_connected = False
    assert broker.lookup("sup-001") == LookupUnknown("gateway_disconnected")


def test_lookup_finds_our_own_submitted_order():
    ib, gw = _session()
    broker = _broker(ib)
    broker.submit(_attempt(), effect_bytes(_effect()))
    verdict = broker.lookup("sup-001")
    assert verdict == Submitted(str(gw.trades[0].order.orderId))


def test_lookup_dedupes_one_order_seen_in_several_views():
    ib, gw = _session()
    ref = supervised_order_ref("sup-001")
    gw.foreign_open.append(
        SimpleNamespace(order=SimpleNamespace(orderRef=ref, orderId=41, permId=9001))
    )
    gw.executions.append(
        SimpleNamespace(execution=SimpleNamespace(orderRef=ref, orderId=0, permId=9001))
    )
    assert _broker(ib).lookup("sup-001") == Submitted("41")


def test_lookup_refuses_to_pick_between_two_orders():
    ib, gw = _session()
    ref = supervised_order_ref("sup-001")
    for perm in (9001, 9002):
        gw.completed.append(
            SimpleNamespace(order=SimpleNamespace(orderRef=ref, orderId=perm - 8960, permId=perm))
        )
    verdict = _broker(ib).lookup("sup-001")
    assert isinstance(verdict, LookupUnknown)
    assert verdict.reason.startswith("multiple_orders_for_tag")


# ------------------------------------------------- legacy non-adoption


def test_monitor_side_trade_attribution_never_adopts_a_supervised_order():
    """The monitor's tag parser reads trex:sup:<id> as structure 'sup:<id>',
    which no monitor book prepares, even when the monitor prepared the very
    same package."""
    ours, gw = _session()
    _broker(ours).submit(_attempt(), effect_bytes(_effect()))
    monitor = IbkrTrex(client_id=77)
    monitor._ib = gw
    monitor.prepare([DEBIT_PUT])
    (trade,) = gw.trades
    assert monitor.structure_for_trade(trade) is None
