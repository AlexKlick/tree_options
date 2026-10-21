"""Books lane: IBKR execution reports folded into per-order fill state.

Oracles: the frozen contract fixtures and hand arithmetic over the scripted
fake fills (quantities, micros prices and commissions are literals here).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.unit.trex_books_support import (
    ACCOUNT,
    ENTRY,
    SPY,
    as_broker_execution,
    buy,
    execution,
    gateway_executions,
    place,
    run,
    sell,
)
from tests.unit.trex_fakes import FillStep, Wave2Gateway
from tree_options.trex.fills import (
    CommissionReport,
    FillConflict,
    fold_executions,
    version_key,
)

FIRST = "0000e0d5.6576d6a5.01.01"
SECOND = "0000e0d5.6576d6a6.01.01"


def test_one_full_fill_with_its_commission_is_complete() -> None:
    book = fold_executions([execution()])
    [order] = book.orders
    assert (order.perm_id, order.order_ref, order.account_id) == (1234567890, ENTRY, ACCOUNT)
    [fill] = order.fills
    assert (fill.base_exec_id, fill.exec_id, fill.superseded_exec_ids) == (
        "0000e0d5.6576d6a5.01",
        FIRST,
        (),
    )
    assert fill.signed_quantity == 2
    assert fill.cash_micros == -2 * 671_480_000
    assert order.status == "complete" and order.complete
    assert order.commission_micros == 1_000_000
    assert order.missing_commissions == ()
    assert order.quantity_by_con_id == {SPY: 2}
    assert order.filled_quantity == 2
    assert order.net_cash_micros == -1_342_960_000
    assert order.progress(2) == "filled"
    assert book.complete


def test_a_missing_commission_is_incomplete_never_zero_fee() -> None:
    book = fold_executions([execution(commission_micros=None)])
    [order] = book.orders
    assert order.status == "incomplete"
    assert not order.complete and not book.complete
    assert order.commission_micros is None
    assert order.missing_commissions == (FIRST,)
    # a reported zero commission is a real fact and completes the lifecycle
    zero = fold_executions([execution(commission_micros=0)])
    assert zero.orders[0].commission_micros == 0
    assert zero.orders[0].status == "complete"


def test_late_commission_report_joins_by_exec_id() -> None:
    late = CommissionReport(exec_id=FIRST, commission_micros=1_000_000)
    joined = fold_executions([execution(commission_micros=None)], [late])
    assert joined.orders[0].commission_micros == 1_000_000
    assert joined.orders[0].status == "complete"
    # the same report re-delivered as an updated execution row joins the same way
    rereported = fold_executions([execution(commission_micros=None), execution()])
    assert rereported == fold_executions([execution()])
    # a report for an exec id never seen stays visible, never dropped
    orphan = CommissionReport(exec_id="0000e0d5.ffffffff.01.01", commission_micros=5)
    book = fold_executions([execution()], [orphan])
    assert book.unmatched_commissions == (orphan,)


def test_gateway_partial_fills_fold_per_order() -> None:
    gw = Wave2Gateway()
    gw.commission_lag = None  # hold every report until delivered
    gw.script_fills(ENTRY, [FillStep(1, commission_micros=350_000)])
    trade = place(gw)
    run(gw)
    book = fold_executions(gateway_executions(gw))
    [order] = book.orders
    assert order.perm_id == trade.order.permId
    assert order.filled_quantity == 1
    assert order.progress(2) == "partial"
    assert order.status == "incomplete"
    assert order.missing_commissions == (FIRST,)
    gw.script_fills(ENTRY, [FillStep(price_micros=671_480_000, commission_micros=650_000)])
    run(gw, 1)
    gw.deliver_commissions()
    book = fold_executions(gateway_executions(gw))
    [order] = book.orders
    assert [f.exec_id for f in order.fills] == [FIRST, SECOND]
    assert order.filled_quantity == 2
    assert order.progress(2) == "filled"
    assert order.commission_micros == 1_000_000
    assert order.net_cash_micros == -(671_500_000 + 671_480_000)
    assert order.status == "complete"


def test_unfilled_and_overfilled_progress() -> None:
    book = fold_executions([execution(quantity=2)])
    order = book.orders[0]
    assert order.progress(3) == "partial"
    with pytest.raises(FillConflict, match="overfilled"):
        order.progress(1)
    with pytest.raises(ValueError):
        order.progress(0)


def test_gateway_correction_replaces_the_earlier_report() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    fix = gw.correct_execution(FIRST, quantity=1, price_micros=671_480_000)
    run(gw, 1)  # the correction's own (zero) commission report
    book = fold_executions(gateway_executions(gw))
    [order] = book.orders
    [fill] = order.fills
    assert fill.exec_id == fix.execution.execId == "0000e0d5.6576d6a5.01.02"
    assert fill.superseded_exec_ids == (FIRST,)
    assert (fill.signed_quantity, fill.cash_micros) == (1, -671_480_000)
    assert order.filled_quantity == 1
    # both reports carry a commission (1.00 then 0.00): summed, complete
    assert order.commission_micros == 1_000_000
    assert order.status == "complete"


def test_a_correction_awaiting_its_commission_is_incomplete() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    gw.correct_execution(FIRST, price_micros=671_490_000)
    book = fold_executions(gateway_executions(gw))
    assert book.orders[0].missing_commissions == ("0000e0d5.6576d6a5.01.02",)
    assert book.orders[0].commission_micros is None


def test_corrections_order_by_version_not_arrival() -> None:
    v1 = execution()
    v2 = execution(exec_id="0000e0d5.6576d6a5.01.02", quantity=1, commission_micros=0)
    v3 = execution(exec_id="0000e0d5.6576d6a5.01.03", price_micros=671_400_000, commission_micros=0)
    forward = fold_executions([v1, v2, v3])
    assert fold_executions([v3, v1, v2]) == forward
    [fill] = forward.orders[0].fills
    assert fill.exec_id == v3.exec_id
    assert fill.superseded_exec_ids == (v1.exec_id, v2.exec_id)
    assert fill.signed_quantity == 2  # v3 re-reports 2 at the new price
    assert fill.cash_micros == -2 * 671_400_000


def test_version_key_orders_ibkr_suffixes() -> None:
    ids = ["x.01.09", "x.01.0a", "x.01.10", "x.01.01", "x.01.02"]
    assert sorted(ids, key=version_key) == ["x.01.01", "x.01.02", "x.01.09", "x.01.0a", "x.01.10"]
    assert version_key("nodot") < version_key("nodotz")


def test_a_replayed_report_is_a_no_op() -> None:
    once = fold_executions([execution()])
    assert fold_executions([execution(), execution()]) == once


@pytest.mark.parametrize(
    "change",
    [
        {"price_micros": 671_490_000},
        {"quantity": 3},
        {"commission_micros": 2_000_000},
        {"side": "SLD"},
    ],
)
def test_conflicting_reports_under_one_exec_id_refuse(change: dict[str, Any]) -> None:
    with pytest.raises(FillConflict, match=FIRST):
        fold_executions([execution(), execution(**change)])


def test_a_conflicting_late_commission_refuses() -> None:
    with pytest.raises(FillConflict, match="commission"):
        fold_executions([execution()], [CommissionReport(FIRST, 999)])


@pytest.mark.parametrize(
    "change", [{"perm_id": 42}, {"con_id": 1}, {"account_id": "DU0000002"}, {"side": "SLD"}]
)
def test_a_correction_of_another_order_refuses(change: dict[str, Any]) -> None:
    other = execution(exec_id="0000e0d5.6576d6a5.01.02", **change)
    with pytest.raises(FillConflict, match=re.escape("0000e0d5.6576d6a5.01")):
        fold_executions([execution(), other])


def test_entry_and_exit_are_separate_orders() -> None:
    gw = Wave2Gateway()
    entry = buy(gw, [FillStep()])
    exit_trade = sell(gw, [FillStep(price_micros=672_000_000)], price=672.0)
    book = fold_executions(gateway_executions(gw))
    assert [o.perm_id for o in book.orders] == sorted([entry.order.permId, exit_trade.order.permId])
    exit_order = book.order(exit_trade.order.permId)
    assert exit_order is not None
    assert exit_order.quantity_by_con_id == {SPY: -2}
    assert exit_order.net_cash_micros == 2 * 672_000_000
    assert book.for_order_ref(ENTRY) == (book.order(entry.order.permId),)
    assert [f.signed_quantity for f in book.fills()] == [2, -2]


def test_combo_executions_are_evidence_only() -> None:
    bag = execution(
        exec_id="0000e0d5.70000000.01.01",
        sec_type="BAG",
        con_id=28812380,
        multiplier=100,
        price_micros=1_100_000,
        quantity=1,
        perm_id=77,
        order_ref="trex:sup:canary-001",
        commission_micros=None,
    )
    long_leg = execution(
        exec_id="0000e0d5.70000001.01.01",
        sec_type="OPT",
        con_id=700001,
        multiplier=100,
        price_micros=3_000_000,
        quantity=1,
        perm_id=77,
        order_ref="trex:sup:canary-001",
        commission_micros=650_000,
    )
    short_leg = execution(
        exec_id="0000e0d5.70000002.01.01",
        sec_type="OPT",
        con_id=700002,
        multiplier=100,
        side="SLD",
        price_micros=1_900_000,
        quantity=1,
        perm_id=77,
        order_ref="trex:sup:canary-001",
        commission_micros=650_000,
    )
    book = fold_executions([long_leg, bag, short_leg])
    [order] = book.orders
    assert [f.exec_id for f in order.fills] == [long_leg.exec_id, short_leg.exec_id]
    assert [f.exec_id for f in order.combo_fills] == [bag.exec_id]
    assert order.quantity_by_con_id == {700001: 1, 700002: -1}
    assert order.filled_quantity == 1  # one package, from the combo report
    assert order.net_cash_micros == -(3_000_000 - 1_900_000) * 100
    # the combo report's commission is not a leg commission: still complete
    assert order.status == "complete"
    assert order.commission_micros == 1_300_000
    legs_only = fold_executions([long_leg, short_leg]).orders[0]
    assert legs_only.filled_quantity == 1  # a package = every leg filled


def _events_universe() -> list[Any]:
    reports = [
        execution(commission_micros=None),
        execution(),
        execution(exec_id="0000e0d5.6576d6a5.01.02", quantity=1, commission_micros=0),
        execution(exec_id="0000e0d5.6576d6a6.01.01", quantity=1, commission_micros=None),
        execution(
            exec_id="0000e0d5.6576d6a7.01.01",
            perm_id=1234567891,
            side="SLD",
            order_ref="trex:desk:eq-spy-001",
            commission_micros=250_000,
        ),
    ]
    late = [CommissionReport("0000e0d5.6576d6a6.01.01", 400_000)]
    return [*reports, *late]


@settings(max_examples=60, deadline=None)
@given(st.permutations(_events_universe()))
def test_fold_is_independent_of_arrival_order(items: list[Any]) -> None:
    executions = [i for i in items if not isinstance(i, CommissionReport)]
    commissions = [i for i in items if isinstance(i, CommissionReport)]
    universe = _events_universe()
    expected = fold_executions(universe[:-1], universe[-1:])
    assert fold_executions(executions, commissions) == expected


def test_gateway_fixture_fill_maps_to_the_contract_fixture() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep(price_micros=671_480_000)])
    [report] = [as_broker_execution(f) for f in gw.reqExecutions()]
    assert report.exec_id == FIRST
    assert report.commission_micros == 1_000_000
