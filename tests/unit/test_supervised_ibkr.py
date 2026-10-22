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

from tests.unit.trex_fakes import (
    FakeStatus,
    FakeTrade,
    SupervisedGateway,
    Wave2Gateway,
    equity_effect,
)
from tree_options.execution import ExecutionLifecycle, ExecutionState, OrderIntent, SubmitAttempt
from tree_options.time.sessions import shift_instant
from tree_options.trex.contracts import (
    EQUITY_EFFECT_SCHEMA,
    Blocker,
    effect_v1_bytes,
)
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
    EQUITY_EFFECT_SCHEMA as ADAPTER_EQUITY_SCHEMA,
)
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    SupervisedEffect,
    decode_effect,
    effect_bytes,
    equity_order,
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


# a provider message that can carry account details: a blocker lands in
# requests/<iid>.result.json, so it must carry the error CLASS only
SECRET = "acct DU1234567 provider detail"


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
def test_preflight_blocker_carries_the_error_class_only(monkeypatch, error):
    ib, gw = _session()

    def prepare(structures: Any) -> None:
        raise error(SECRET)

    monkeypatch.setattr(ib, "prepare", prepare)
    blockers = _broker(ib).preflight(effect_bytes(_effect()))
    assert blockers == [f"order_unbuildable:{error.__name__}"]
    assert SECRET not in json.dumps(blockers)
    assert gw.trades == []


def test_preflight_above_the_cap_names_the_class_not_the_message():
    ib, _ = _session()
    blockers = _broker(ib).preflight(effect_bytes(_effect(limit=Decimal("1.50"))))
    assert blockers == ["order_unbuildable:ValueError"], "never the cap text"


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
def test_preflight_propagates_an_error_raised_once_the_session_is_gone(error):
    """A lost gateway is not an unbuildable order: the error propagates so
    the desk loop records gateway_lost and exits 6 at once."""
    ib, gw = _session()
    gw.drop("qualifyContracts", error(SECRET), disconnect=True)
    with pytest.raises(error):
        _broker(ib).preflight(effect_bytes(_effect()))
    assert gw.trades == []


def test_submit_unbuildable_detail_carries_the_error_class_only(monkeypatch):
    """The detail lands in the outbox's terminal receipt and the request's
    result.json: the class only, as for preflight."""
    ib, gw = _session()

    def prepare(structures: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(ib, "prepare", prepare)
    outcome = _broker(ib).submit(_attempt(), effect_bytes(_effect()))
    assert isinstance(outcome, Uncertain)
    assert (outcome.reason, outcome.detail) == ("not_sent:order_unbuildable", "RuntimeError")
    assert gw.trades == []


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


# ============================================================ Wave 2 equities
#
# The fixture equity effect (tests/contracts/fixtures/equity_effect.json):
# BUY 2 SPY (conId 756733) at 671.50, DAY, account DU0000001, tag
# trex:sup:eq-spy-001. Oracles are literal wire values, IBKR codes and the
# frozen Blocker vocabulary.

EQ_ACCOUNT = "DU0000001"
SPY_CON = 756733
QQQ_CON = 320227571
EQ_INTENT = "eq-spy-001"
EQ_REF = "trex:sup:eq-spy-001"
EQ_NOTIONAL_USD = "1343.00"  # 2 x 671.50


def _eq_session() -> tuple[IbkrTrex, Wave2Gateway]:
    gw = Wave2Gateway()
    ib = IbkrTrex(client_id=SUPERVISED_CLIENT_ID)
    ib._ib = gw
    return ib, gw


def _eq_broker(ib: IbkrTrex, gw: Wave2Gateway, **kwargs: Any) -> IbkrSupervisedBroker:
    return IbkrSupervisedBroker(ib, clock=gw.clock, **kwargs)


def _eq_payload(**changes: Any) -> bytes:
    return effect_v1_bytes(equity_effect(**changes))


def _eq_attempt(gw: Wave2Gateway, intent_id: str = EQ_INTENT) -> SubmitAttempt:
    return SubmitAttempt(
        record_id=f"sup-send-{intent_id}",
        intent_id=intent_id,
        send_attempt_at=gw.clock.utc(),
        source="supervised",
        source_sequence_id=f"permit-{intent_id}",
    )


def _quoted(gw: Wave2Gateway, symbol: str = "SPY", bid: int = 67148, ask: int = 67152) -> None:
    """The first NBBO tick arrives during the adapter's settle sleep."""
    gw.script_ticks(symbol, [(bid, ask)])


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def test_adapter_constants_match_the_frozen_contracts() -> None:
    assert ADAPTER_EQUITY_SCHEMA == EQUITY_EFFECT_SCHEMA == "supervised-equity-effect/1"


# ------------------------------------------------------------ equity preflight


def test_equity_preflight_passes_with_a_realtime_quote_and_settled_cash() -> None:
    ib, gw = _eq_session()
    _quoted(gw)
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == []
    assert gw.trades == [], "preflight must never place"
    assert [s.conId for s in gw.subscribed] == [SPY_CON]


@pytest.mark.parametrize("kind", ["delayed", "frozen"])
def test_a_non_realtime_quote_blocks_the_entry(kind: str) -> None:
    ib, gw = _eq_session()
    gw.set_market_data_type("SPY", kind)
    _quoted(gw)
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == [Blocker.EQUITY_QUOTE_NOT_REALTIME]


def test_no_quote_tick_blocks_the_entry() -> None:
    ib, gw = _eq_session()
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == ["equity_quote_incomplete"]


def test_a_symbol_off_the_allowlist_is_refused_before_any_broker_call() -> None:
    ib, gw = _eq_session()
    gw.add_stock("QQQ", QQQ_CON)
    _quoted(gw, "QQQ", 48010, 48014)
    payload = _eq_payload(symbol="QQQ", con_id=QQQ_CON)
    assert _eq_broker(ib, gw).preflight(payload) == [Blocker.EQUITY_SYMBOL_NOT_QUALIFIED]
    assert gw.qualify_calls == [] and gw.subscribed == []
    widened = _eq_broker(ib, gw, equity_allowlist=frozenset({"SPY", "QQQ"}))
    assert widened.preflight(payload) == []


def test_an_unqualifiable_allowlisted_symbol_is_not_qualified() -> None:
    ib, gw = _eq_session()
    broker = _eq_broker(ib, gw, equity_allowlist=frozenset({"ZZZZ"}))
    blockers = broker.preflight(_eq_payload(symbol="ZZZZ", con_id=4242))
    assert blockers == [Blocker.EQUITY_SYMBOL_NOT_QUALIFIED]
    assert gw.subscribed == []


def test_an_effect_con_id_other_than_the_qualified_one_is_refused() -> None:
    ib, gw = _eq_session()
    _quoted(gw)
    assert _eq_broker(ib, gw).preflight(_eq_payload(con_id=SPY_CON + 1)) == [
        "equity_con_id_mismatch"
    ]
    assert gw.subscribed == []


@pytest.mark.parametrize(
    ("settled", "blockers"),
    [
        (EQ_NOTIONAL_USD, []),
        ("1342.99", [Blocker.INSUFFICIENT_CASH]),
        ("0.00", [Blocker.INSUFFICIENT_CASH]),
        ("-100.00", [Blocker.INSUFFICIENT_CASH]),
    ],
)
def test_cash_only_settled_cash_must_cover_the_notional(settled: str, blockers: list[str]) -> None:
    ib, gw = _eq_session()
    _quoted(gw)
    gw.account_values["SettledCash"] = settled
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == blockers


def test_working_stock_buys_commit_settled_cash() -> None:
    import ib_async

    ib, gw = _eq_session()
    _quoted(gw)
    gw.account_values["SettledCash"] = EQ_NOTIONAL_USD
    contract = ib_async.Stock("SPY", "SMART", "USD", conId=SPY_CON)
    resting = ib_async.LimitOrder(
        "BUY", 1, 1.0, tif="DAY", account=EQ_ACCOUNT, orderRef="trex:sup:eq-spy-000"
    )
    gw.placeOrder(contract, resting)
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == [Blocker.INSUFFICIENT_CASH]


def test_an_unbounded_or_unreadable_cash_commitment_blocks_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ib, gw = _eq_session()
    _quoted(gw)
    gw.trades.append(
        FakeTrade(
            contract=SimpleNamespace(secType="STK", conId=SPY_CON),
            order=SimpleNamespace(action="BUY", totalQuantity=1.0, lmtPrice=0.0, account=""),
            orderStatus=FakeStatus(),
        )
    )
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == ["cash_commitment_unbounded"]

    def unreadable() -> list[Any]:
        raise TimeoutError(EQ_SECRET)

    monkeypatch.setattr(gw, "openTrades", unreadable)
    blockers = _eq_broker(ib, gw).preflight(_eq_payload())
    assert blockers == ["broker_view_unreadable:open_orders"]
    assert EQ_SECRET not in json.dumps(blockers)


def test_a_quote_and_a_cash_blocker_are_both_reported() -> None:
    ib, gw = _eq_session()
    gw.set_market_data_type("SPY", "delayed")
    _quoted(gw)
    gw.account_values["SettledCash"] = "1.00"
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == [
        Blocker.EQUITY_QUOTE_NOT_REALTIME,
        Blocker.INSUFFICIENT_CASH,
    ]


@pytest.mark.parametrize(
    ("total", "blockers"),
    [
        (EQ_NOTIONAL_USD, []),
        ("1342.99", [Blocker.INSUFFICIENT_CASH]),
        ("-100.00", [Blocker.INSUFFICIENT_CASH]),
    ],
)
def test_preflight_cash_falls_back_to_total_cash_value(total: str, blockers: list[str]) -> None:
    """2026-10-21: the margin-type paper account carries no SettledCash; the
    preflight reads TotalCashValue by the same rule as the books."""
    ib, gw = _eq_session()
    _quoted(gw)
    del gw.account_values["SettledCash"]
    gw.account_values["TotalCashValue"] = total
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == blockers


def test_preflight_settled_cash_wins_over_total_cash_value() -> None:
    ib, gw = _eq_session()
    _quoted(gw)
    gw.account_values["SettledCash"] = "1342.99"
    gw.account_values["TotalCashValue"] = "1000000.00"
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == [Blocker.INSUFFICIENT_CASH]


EQ_SECRET = "acct DU0000001 provider detail"


def test_unreadable_settled_cash_blocks_with_the_view_name_only() -> None:
    ib, gw = _eq_session()
    _quoted(gw)
    del gw.account_values["SettledCash"]
    del gw.account_values["TotalCashValue"]  # neither cash tag: unreadable
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == ["broker_view_unreadable:account_values"]
    gw.account_values["SettledCash"] = "junk"  # junk: never replaced
    gw.account_values["TotalCashValue"] = EQ_NOTIONAL_USD
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == ["broker_view_unreadable:account_values"]
    gw.account_view_error = TimeoutError(EQ_SECRET)
    blockers = _eq_broker(ib, gw).preflight(_eq_payload())
    assert blockers == ["broker_view_unreadable:account_values"]
    assert EQ_SECRET not in json.dumps(blockers)


def test_a_cash_read_that_loses_the_gateway_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    ib, gw = _eq_session()
    _quoted(gw)

    def lost(*_: Any) -> list[Any]:
        gw.is_connected = False
        raise ConnectionError(EQ_SECRET)

    monkeypatch.setattr(gw, "accountValues", lost)
    with pytest.raises(ConnectionError):
        _eq_broker(ib, gw).preflight(_eq_payload())


def test_a_qualification_error_names_the_class_or_propagates_once_disconnected() -> None:
    ib, gw = _eq_session()
    gw.drop("qualifyContracts", RuntimeError(EQ_SECRET), disconnect=False)
    blockers = _eq_broker(ib, gw).preflight(_eq_payload())
    assert blockers == ["order_unbuildable:RuntimeError"]
    assert EQ_SECRET not in json.dumps(blockers)
    lost_ib, lost_gw = _eq_session()
    lost_gw.drop("qualifyContracts", RuntimeError(EQ_SECRET), disconnect=True)
    with pytest.raises(RuntimeError):
        _eq_broker(lost_ib, lost_gw).preflight(_eq_payload())
    assert lost_gw.trades == []


def test_equity_preflight_keeps_the_paper_session_guards() -> None:
    ib, gw = _eq_session()
    gw.accounts = ["DU9999999"]
    assert _eq_broker(ib, gw).preflight(_eq_payload()) == ["account_not_managed_by_session"]
    assert gw.qualify_calls == []


def test_non_canonical_or_off_contract_equity_payloads_are_invalid() -> None:
    ib, gw = _eq_session()
    broker = _eq_broker(ib, gw)
    document = json.loads(_eq_payload())
    pretty = json.dumps(document, indent=2).encode()
    assert broker.preflight(pretty) == ["effect_invalid:ValueError"]
    for change in ({"tif": "GTC"}, {"side": "SELL"}, {"account_id": "U7654321"}, {"rogue": 1}):
        payload = _canonical(document | change)
        assert broker.preflight(payload) == ["effect_invalid:ValidationError"], change
        outcome = broker.submit(_eq_attempt(gw), payload)
        assert isinstance(outcome, Uncertain)
        assert (outcome.reason, outcome.detail) == ("not_sent:effect_invalid", "ValidationError")
    assert gw.qualify_calls == [] and gw.trades == []


# --------------------------------------------------------------- equity submit


def test_equity_submit_places_the_stk_order_built_from_the_effect_bytes() -> None:
    ib, gw = _eq_session()
    broker = _eq_broker(ib, gw)
    outcome = broker.submit(_eq_attempt(gw), _eq_payload())
    assert isinstance(outcome, Acknowledged)
    (trade,) = gw.trades
    contract, order = trade.contract, trade.order
    assert (contract.secType, contract.conId, contract.symbol) == ("STK", SPY_CON, "SPY")
    assert (contract.exchange, contract.currency) == ("SMART", "USD")
    assert (order.action, order.totalQuantity, order.orderType, order.lmtPrice) == (
        "BUY",
        2,
        "LMT",
        671.5,
    )
    assert (order.tif, order.outsideRth, order.transmit) == ("DAY", False, True)
    assert (order.account, order.orderRef) == (EQ_ACCOUNT, EQ_REF)
    assert gw.wire_violations == []
    assert outcome.acknowledgement.broker_order_id == str(order.orderId)
    assert broker.lookup(EQ_INTENT) == Submitted(str(order.orderId))


def test_quantity_and_limit_come_from_the_bytes() -> None:
    ib, gw = _eq_session()
    payload = _eq_payload(quantity=3, limit_cents=67149)
    assert isinstance(_eq_broker(ib, gw).submit(_eq_attempt(gw), payload), Acknowledged)
    (trade,) = gw.trades
    assert (trade.order.totalQuantity, trade.order.lmtPrice) == (3, 671.49)


@pytest.mark.parametrize(
    ("setup", "payload_changes", "reason", "detail"),
    [
        (None, {"con_id": SPY_CON + 1}, "not_sent:order_unbuildable", "StockConIdMismatch"),
        (
            None,
            {"symbol": "QQQ", "con_id": QQQ_CON},
            "not_sent:equity_symbol_not_qualified",
            None,
        ),
        (
            lambda gw: gw.completed.append(
                SimpleNamespace(order=SimpleNamespace(orderRef=EQ_REF, orderId=41, permId=9001))
            ),
            {},
            "not_sent:tag_already_at_broker",
            EQ_REF,
        ),
        (
            lambda gw: setattr(gw, "views_error", ConnectionError("down")),
            {},
            "not_sent:duplicate_check_unreadable",
            None,
        ),
        (lambda gw: setattr(gw, "accounts", ["DU9999999"]), {}, "not_sent:session", None),
    ],
    ids=["con-id", "allowlist", "duplicate", "views", "session"],
)
def test_equity_submit_refusals_never_touch_the_wire(
    setup: Any, payload_changes: dict[str, Any], reason: str, detail: str | None
) -> None:
    ib, gw = _eq_session()
    gw.add_stock("QQQ", QQQ_CON)
    if setup is not None:
        setup(gw)
    outcome = _eq_broker(ib, gw).submit(_eq_attempt(gw), _eq_payload(**payload_changes))
    assert isinstance(outcome, Uncertain)
    assert outcome.reason == reason
    if detail is not None:
        assert outcome.detail == detail
    assert gw.trades == []


def test_equity_submit_unqualified_and_provider_errors_carry_the_class_only() -> None:
    ib, gw = _eq_session()
    broker = _eq_broker(ib, gw, equity_allowlist=frozenset({"ZZZZ", "SPY"}))
    outcome = broker.submit(_eq_attempt(gw), _eq_payload(symbol="ZZZZ", con_id=4242))
    assert isinstance(outcome, Uncertain)
    assert (outcome.reason, outcome.detail) == ("not_sent:order_unbuildable", "StockNotQualified")
    gw.drop("qualifyContracts", RuntimeError(EQ_SECRET), disconnect=False)
    outcome = broker.submit(_eq_attempt(gw), _eq_payload())
    assert isinstance(outcome, Uncertain)
    assert (outcome.reason, outcome.detail) == ("not_sent:order_unbuildable", "RuntimeError")
    assert gw.trades == []


def test_equity_submit_refuses_an_effect_for_another_intent() -> None:
    ib, gw = _eq_session()
    outcome = _eq_broker(ib, gw).submit(_eq_attempt(gw, "eq-spy-002"), _eq_payload())
    assert isinstance(outcome, Uncertain)
    assert outcome.reason == "not_sent:intent_mismatch"
    assert gw.trades == []


@pytest.mark.parametrize(
    "change",
    [
        {"side": "SELL"},
        {"quantity": 0},
        {"quantity": True},
        {"limit_cents": 0},
        {"tif": "GTC"},
        {"outside_rth": True},
        {"exchange": "ISLAND"},
        {"currency": "EUR"},
        {"account_id": "U7654321"},
        {"order_ref": "trex:sup:eq-spy-999"},
        {"con_id": 0},
    ],
)
def test_equity_order_refuses_an_effect_that_bypassed_validation(change: dict[str, Any]) -> None:
    pytest.importorskip("ib_async")
    forged = equity_effect().model_copy(update=change)
    with pytest.raises(ValueError):
        equity_order(forged)


def test_option_payloads_keep_the_option_path() -> None:
    """An option effect never reaches the stock wire; a payload claiming
    the equity schema never reaches the option decoder."""
    ib, gw = _session()
    gw.quote(100, 2.00, 2.20)
    gw.quote(95, 1.10, 1.25)
    broker = _broker(ib)
    assert broker.preflight(effect_bytes(_effect())) == []
    assert isinstance(broker.submit(_attempt(), effect_bytes(_effect())), Acknowledged)
    assert [t.contract.secType for t in gw.trades] == ["BAG"]
    option_doc = json.loads(effect_bytes(_effect()))
    claimed = _canonical(option_doc | {"schema": EQUITY_EFFECT_SCHEMA})
    assert broker.preflight(claimed) == ["effect_invalid:ValidationError"]
