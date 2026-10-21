"""Books lane: ledger holdings vs broker positions (``trex-reconciliation/1``).

Oracles: the frozen ``reconciliation_required.json`` fixture, the contract's
own mismatch-kind table, and the ``Wave2Gateway`` position faults.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import tree_options.trex.contracts as c
from tests.unit.trex_books_support import (
    ACCOUNT,
    SPY,
    at,
    buy,
    execution,
    gateway_executions,
    portfolio_item,
    sell,
)
from tests.unit.trex_fakes import FillStep, Wave2Gateway, contract_fixture, entry_proposal
from tree_options.trex.fills import fold_executions
from tree_options.trex.ledger import AttributionIndex, fill_events, fold_ledger
from tree_options.trex.reconcile import (
    outside_positions,
    parse_broker_rows,
    read_reconciliation,
    reconcile,
    reconcile_ledger,
    reconciliation_event,
    reconciliation_sha256,
    write_reconciliation,
)

AS_OF = at("2026-10-22T14:04:00+00:00")
QQQ = 320227571
INDEX = AttributionIndex.build(
    proposals=[entry_proposal()],
    specs=[c.parse_desk_spec(contract_fixture("desk_spec_v2_equity.json"))],
)


def row(con_id: int = SPY, position: int = 2, **kw: Any) -> c.BrokerPortfolioItem:
    return portfolio_item(con_id=con_id, position=position, **kw)


def raw_rows(gw: Wave2Gateway) -> list[dict[str, Any]]:
    """The fake's portfolio as raw documents (fractional / foreign kept as is)."""
    out = []
    for item in gw.portfolio():
        qty = item.position
        whole = isinstance(qty, float) and qty.is_integer()
        out.append(
            {
                "account_id": item.account,
                "con_id": item.contract.conId,
                "sec_type": item.contract.secType,
                "symbol": item.contract.symbol,
                "position": int(qty) if whole else qty,
                "avg_cost_micros": round(item.averageCost * 1_000_000),
                "market_price_micros": round(item.marketPrice * 1_000_000),
                "as_of": AS_OF,
            }
        )
    return out


def test_fixture_quantity_mismatch() -> None:
    rec = reconcile(
        account_id=ACCOUNT, as_of=AS_OF, local_positions={SPY: 2}, broker_rows=[row(position=3)]
    )
    assert c.model_document(rec) == contract_fixture("reconciliation_required.json")


def test_clean_when_every_touched_contract_agrees() -> None:
    rec = reconcile(
        account_id=ACCOUNT,
        as_of=AS_OF,
        local_positions={SPY: 2, 700001: 1, 700002: -1, 555: 0},
        broker_rows=[
            row(),
            row(700001, 1, sec_type="OPT", symbol="SPY"),
            row(700002, -1, sec_type="OPT", symbol="SPY"),
            row(555, 0),  # a closed contract may linger as a 0 row
        ],
    )
    assert (rec.status, rec.mismatches) == ("clean", ())
    assert c.reconciliation_blockers(rec, AS_OF) == ()
    in_120 = at("2026-10-22T14:06:00+00:00")
    in_121 = at("2026-10-22T14:06:01+00:00")
    assert c.reconciliation_blockers(rec, in_120) == ()
    assert c.reconciliation_blockers(rec, in_121) == (c.Blocker.RECONCILIATION_REQUIRED,)


@pytest.mark.parametrize(
    ("local", "broker", "kind"),
    [
        (0, 3, "unexplained"),
        (2, None, "missing"),
        (2, 0, "missing"),
        (2, 3, "quantity_mismatch"),
        (-1, 1, "quantity_mismatch"),
    ],
)
def test_mismatch_kinds(local: int, broker: int | None, kind: str) -> None:
    rows = [] if broker is None else [row(position=broker)]
    rec = reconcile(account_id=ACCOUNT, as_of=AS_OF, local_positions={SPY: local}, broker_rows=rows)
    assert rec.status == "reconciliation_required"
    expected = c.PositionMismatch.model_validate(
        {"con_id": SPY, "local_qty": local, "broker_qty": broker or 0, "kind": kind}
    )
    assert rec.mismatches == (expected,)
    assert c.reconciliation_blockers(rec, AS_OF) == (c.Blocker.RECONCILIATION_REQUIRED,)


def test_mismatches_are_listed_per_con_id_in_order() -> None:
    rec = reconcile(
        account_id=ACCOUNT,
        as_of=AS_OF,
        local_positions={900: 1, 100: 1, 500: 0},
        broker_rows=[row(500, 2), row(100, 3)],
    )
    assert [(m.con_id, m.kind) for m in rec.mismatches] == [
        (100, "quantity_mismatch"),
        (500, "unexplained"),
        (900, "missing"),
    ]


def test_outside_positions_are_reported_not_mismatched() -> None:
    rows = [row(), row(QQQ, 5, symbol="QQQ"), row(712345678, -1, sec_type="OPT", symbol="NVDA")]
    rec = reconcile(account_id=ACCOUNT, as_of=AS_OF, local_positions={SPY: 2}, broker_rows=rows)
    assert rec.status == "clean"
    assert outside_positions(account_id=ACCOUNT, touched={SPY}, broker_rows=rows) == (
        c.OutsidePosition(con_id=QQQ, sec_type="STK", symbol="QQQ", quantity=5),
        c.OutsidePosition(con_id=712345678, sec_type="OPT", symbol="NVDA", quantity=-1),
    )
    # a flat outside row is no position at all
    assert outside_positions(account_id=ACCOUNT, touched=set(), broker_rows=[row(QQQ, 0)]) == ()


def test_unreadable_broker_view_is_unavailable() -> None:
    rec = reconcile(account_id=ACCOUNT, as_of=AS_OF, local_positions={SPY: 2}, broker_rows=None)
    assert (rec.status, rec.mismatches) == ("unavailable", ())
    assert c.reconciliation_blockers(rec, AS_OF) == (c.Blocker.RECONCILIATION_REQUIRED,)


def test_other_accounts_rows_are_ignored() -> None:
    rows = [row(), row(position=7, account_id="DU0000002")]
    rec = reconcile(account_id=ACCOUNT, as_of=AS_OF, local_positions={SPY: 2}, broker_rows=rows)
    assert rec.status == "clean"


def test_contradictory_rows_for_one_contract_are_unavailable() -> None:
    rows = [row(position=2), row(position=3)]
    rec = reconcile(account_id=ACCOUNT, as_of=AS_OF, local_positions={SPY: 2}, broker_rows=rows)
    assert rec.status == "unavailable"
    same_twice = reconcile(
        account_id=ACCOUNT, as_of=AS_OF, local_positions={SPY: 2}, broker_rows=[row(), row()]
    )
    assert same_twice.status == "clean"


@pytest.mark.parametrize(
    "fault",
    [
        {"position": 1.5},
        {"position": Decimal("2.5")},
        {"sec_type": "FUT"},
        {"position": "2"},
    ],
)
def test_an_unrepresentable_row_makes_the_view_unreadable(fault: dict[str, Any]) -> None:
    good = c.model_document(row())
    bad = {**c.model_document(row(QQQ, 5, symbol="QQQ")), **fault}
    assert parse_broker_rows([good, bad]) is None
    assert parse_broker_rows([good]) == (row(),)
    assert parse_broker_rows(None) is None


# --------------------------------------------------------- against the fake


def ledger_after(gw: Wave2Gateway) -> Any:
    events = fill_events(
        fold_executions(gateway_executions(gw)), INDEX, recorded_at=at("2026-10-22T14:00:00+00:00")
    )
    return fold_ledger(events)


def gateway_reconciliation(gw: Wave2Gateway) -> c.Reconciliation:
    try:
        rows = parse_broker_rows(raw_rows(gw))
    except OSError:
        rows = None
    return reconcile_ledger(ledger_after(gw), account_id=ACCOUNT, as_of=AS_OF, broker_rows=rows)


def test_gateway_clean_after_a_fill() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    assert gateway_reconciliation(gw).status == "clean"


@pytest.mark.parametrize("zero_row", [False, True])
def test_gateway_missing(zero_row: bool) -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    gw.hide_position(SPY, zero_row=zero_row)
    [mismatch] = gateway_reconciliation(gw).mismatches
    assert (mismatch.kind, mismatch.local_qty, mismatch.broker_qty) == ("missing", 2, 0)


def test_gateway_quantity_mismatch() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    gw.override_position(SPY, 1.0)
    [mismatch] = gateway_reconciliation(gw).mismatches
    assert (mismatch.kind, mismatch.local_qty, mismatch.broker_qty) == ("quantity_mismatch", 2, 1)


def test_gateway_round_trip_reconciles_flat_against_the_zero_row() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    sell(gw, [FillStep()])
    [zero] = parse_broker_rows(raw_rows(gw)) or ()
    assert zero.position == 0
    assert gateway_reconciliation(gw).status == "clean"


def test_gateway_unexplained_row_on_a_touched_flat_contract() -> None:
    fold = fold_ledger(
        fill_events(
            fold_executions(
                [
                    execution(),
                    execution(
                        exec_id="0000e0d5.6576d6a7.01.01",
                        side="SLD",
                        perm_id=1234567891,
                        order_ref="trex:desk:eq-spy-001",
                    ),
                ]
            ),
            INDEX,
            recorded_at=AS_OF,
        )
    )
    gw = Wave2Gateway()
    gw.add_broker_position("SPY", 3, avg_cost_micros=670_000_000)
    rec = reconcile_ledger(
        fold, account_id=ACCOUNT, as_of=AS_OF, broker_rows=parse_broker_rows(raw_rows(gw))
    )
    assert rec.mismatches == (
        c.PositionMismatch(con_id=SPY, local_qty=0, broker_qty=3, kind="unexplained"),
    )


@pytest.mark.parametrize(
    "fault",
    [
        lambda gw: gw.add_broker_position("SPY", Decimal("0.5")),
        lambda gw: gw.add_broker_position("ES", 1, con_id=495512563, sec_type="FUT"),
        lambda gw: setattr(gw, "account_view_error", ConnectionError("view gone")),
    ],
    ids=["fractional", "unknown_sec_type", "unreadable"],
)
def test_gateway_faults_are_unavailable(fault: Any) -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    fault(gw)
    assert gateway_reconciliation(gw).status == "unavailable"


def test_gateway_outside_position_stays_clean() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    gw.add_broker_position("QQQ", 5, con_id=QQQ)
    rows = parse_broker_rows(raw_rows(gw))
    assert rows is not None
    assert gateway_reconciliation(gw).status == "clean"
    assert outside_positions(account_id=ACCOUNT, touched={SPY}, broker_rows=rows) == (
        c.OutsidePosition(con_id=QQQ, sec_type="STK", symbol="QQQ", quantity=5),
    )


# --------------------------------------------------- ledger marker and files


def test_reconciliation_marker_event_binds_the_document_hash() -> None:
    rec = c.Reconciliation.model_validate(contract_fixture("reconciliation_required.json"))
    sha = reconciliation_sha256(rec)
    assert sha == hashlib.sha256(c.model_bytes(rec)).hexdigest()
    event = reconciliation_event(rec, recorded_at=at("2026-10-22T14:04:01+00:00"))
    assert (event.event_id, event.kind, event.ref_sha256) == (
        f"reconciliation:{sha}",
        "reconciliation",
        sha,
    )
    assert (event.quantity_delta, event.cash_delta_micros, event.at) == (0, 0, rec.as_of)
    assert event.account_id == ACCOUNT
    assert event.detail == "reconciliation_required"


def test_reconciliation_file_round_trip(tmp_path: Path) -> None:
    run_dir = c.Wave2RunDir(tmp_path)
    assert read_reconciliation(run_dir) is None
    rec = c.Reconciliation.model_validate(contract_fixture("reconciliation_required.json"))
    path = write_reconciliation(run_dir, rec)
    assert path == run_dir.reconciliation()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reconciliation_sha256(rec)
    assert read_reconciliation(run_dir) == rec
    assert sorted(p.name for p in path.parent.iterdir()) == ["reconciliation.json"]
    path.write_text("{not json")
    assert read_reconciliation(run_dir) is None
