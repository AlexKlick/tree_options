"""The owner's books (``trex.desk_books``) over the Wave 2 gateway fake.

Drives the REAL ``IbkrTrex`` stock views (lane B) into the REAL ledger,
reconciliation and risk snapshot (lane D) through ``DeskBooks``. Oracles:
the frozen fixture effect and proposal (2 SPY at 671.50, program
``prog-equity-pilot``, sleeve ``sleeve-equity-01``), the gateway's scripted
executions and commission reports, and the files at the layout paths.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.trex_fakes import (
    SPY_CON_ID,
    FillStep,
    SessionClock,
    Wave2Gateway,
    entry_proposal,
    equity_effect,
    mandate_v2,
)
from tree_options.trex.contracts import (
    DeskEvent,
    Reconciliation,
    RiskSnapshot,
    desk_spec_v2_for,
    model_bytes,
    proposal_bytes,
)
from tree_options.trex.desk_books import BOOKS_PROBLEM_EVENT, DeskBooks
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.ledger import HoldingsDocument, fold_ledger, read_executions, read_ledger
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised_ibkr import SUPERVISED_CLIENT_ID

pytest.importorskip("ib_async")

ACCOUNT = "DU0000001"


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.clock = SessionClock()
        self.gw = Wave2Gateway(clock=self.clock)
        self.ib = IbkrTrex(client_id=SUPERVISED_CLIENT_ID)
        self.ib._ib = self.gw
        self.root = tmp / "desk"
        self.sup = SupervisedPaths(tmp / "supervised")
        self.sup.prepare()
        self.specs: list[Any] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.books = DeskBooks(
            self.ib,
            self.root,
            self.sup,
            specs=lambda: list(self.specs),
            clock=self.clock,
            event=lambda name, **payload: self.events.append((name, payload)),
        )

    def approve(self, effect: Any = None, *, write_proposal: bool = True) -> Any:
        """What an approval leaves behind: the proposal, the CONSUMED
        mandate naming it, and the registered spec."""
        effect = effect or equity_effect()
        proposal = entry_proposal(effect)
        if write_proposal:
            self.write_proposal(proposal)
        path = self.sup.entry_mandate_consumed(effect.intent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(model_bytes(mandate_v2(proposal)))
        self.specs.append(desk_spec_v2_for(effect, self.clock()))
        return proposal

    def write_proposal(self, proposal: Any) -> None:
        folder = self.root / "proposals"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{proposal.proposal_id}.json").write_bytes(proposal_bytes(proposal))

    def buy(self, effect: Any = None, steps: list[FillStep] | None = None) -> Any:
        """The entry order on the wire (lane B's shape), then its fills."""
        from ib_async import LimitOrder, Stock

        effect = effect or equity_effect()
        self.gw.script_fills(effect.order_ref, steps or [FillStep()])
        stock = Stock(effect.symbol, "SMART", "USD")
        self.gw.qualifyContracts(stock)
        order = LimitOrder(
            "BUY",
            effect.quantity,
            effect.limit_cents / 100,
            tif="DAY",
            outsideRth=False,
            transmit=True,
            orderRef=effect.order_ref,
            account=effect.account_id,
        )
        trade = self.gw.placeOrder(stock, order)
        for _ in steps or [FillStep()]:
            self.gw.sleep(1)
        return trade

    def file(self, name: str) -> Path:
        return self.root / "ledger" / name

    def ledger(self) -> tuple[Any, ...]:
        return read_ledger(self.file("events.jsonl"))

    def reconciliation(self) -> Reconciliation:
        return Reconciliation.model_validate_json(self.file("reconciliation.json").read_bytes())

    def risk(self) -> RiskSnapshot:
        return RiskSnapshot.model_validate_json(self.file("risk-snapshot.json").read_bytes())

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield Rig(tmp_path)


def test_fills_are_booked_once_under_the_approved_proposals_attribution(rig):
    rig.approve()
    rig.buy()
    first = rig.books.step()
    assert (first.events_appended, first.executions_recorded, first.problems) == (1, 1, ())
    (fill,) = [e for e in rig.ledger() if e.kind == "fill"]
    assert (fill.quantity_delta, fill.cash_delta_micros) == (2, -2 * 671_500_000)
    assert (fill.attribution.intent_id, fill.attribution.program_id) == (
        "eq-spy-001",
        "prog-equity-pilot",
    )
    assert fill.attribution.sleeve_ref == "sleeve-equity-01"
    again = rig.books.step()
    assert (again.events_appended, again.executions_recorded) == (0, 0), "replays are no-ops"
    holdings = HoldingsDocument.model_validate_json(rig.file("holdings.json").read_bytes())
    (held,) = holdings.holdings
    assert (held.con_id, held.quantity, held.lots[0].attribution.intent_id) == (
        SPY_CON_ID,
        2,
        "eq-spy-001",
    )
    assert json.loads(rig.file("holdings.json").read_bytes())["schema"] == "trex-holdings/1"


def test_a_late_commission_report_is_booked_when_it_arrives(rig):
    rig.gw.commission_lag = None  # only on deliver_commissions
    rig.approve()
    rig.buy()
    rig.books.step()
    assert [e.kind for e in rig.ledger() if e.kind != "reconciliation"] == ["fill"]
    assert not fold_ledger(rig.ledger()).complete, "a missing fee is pending, never zero"
    rig.gw.deliver_commissions()
    step = rig.books.step()
    assert step.events_appended == 1
    (fee,) = [e for e in rig.ledger() if e.kind == "commission"]
    assert fee.cash_delta_micros == -1_000_000
    assert fold_ledger(rig.ledger()).complete
    reports = read_executions(rig.file("executions.jsonl"))
    assert [r.commission_micros for r in reports] == [None, 1_000_000]


def test_a_corrected_execution_books_its_adjustment_once(rig):
    rig.approve()
    trade = rig.buy()
    rig.books.step()
    (fill,) = trade.fills
    rig.gw.correct_execution(fill.execution.execId, price_micros=671_400_000)
    rig.books.step()
    rig.books.step()
    (correction,) = [e for e in rig.ledger() if e.kind == "correction"]
    assert (correction.quantity_delta, correction.cash_delta_micros) == (0, 2 * 100_000)


def test_the_status_files_follow_their_cadence_and_a_refresh_request(rig):
    t0 = rig.books.step()
    assert t0.published and t0.reconciliation == "clean" and t0.risk_snapshot == "complete"
    first = rig.reconciliation().as_of
    rig.clock.advance(30)
    assert not rig.books.step().published
    assert rig.reconciliation().as_of == first
    rig.clock.advance(30)
    assert rig.books.step().published, "every 60 s"
    second = rig.reconciliation().as_of
    rig.clock.advance(5)
    rig.books.request_refresh()  # the reconnect hook
    assert rig.books.step().published
    assert rig.reconciliation().as_of > second
    assert rig.risk().as_of == rig.reconciliation().as_of


def test_a_quantity_mismatch_requires_reconciliation_with_one_event(rig):
    rig.approve()
    rig.buy()
    assert rig.books.step().reconciliation == "clean"
    rig.gw.override_position(SPY_CON_ID, 3.0)
    assert rig.books.step(force=True).reconciliation == "reconciliation_required"
    (mismatch,) = rig.reconciliation().mismatches
    assert (mismatch.con_id, mismatch.local_qty, mismatch.broker_qty, mismatch.kind) == (
        SPY_CON_ID,
        2,
        3,
        "quantity_mismatch",
    )
    rig.books.step(force=True)
    transitions = [n for n in rig.names() if n.startswith("reconciliation")]
    assert transitions == [
        DeskEvent.RECONCILIATION_CLEAN.value,
        DeskEvent.RECONCILIATION_REQUIRED.value,
    ], "one event per status change"
    markers = [e for e in rig.ledger() if e.kind == "reconciliation"]
    assert [m.detail for m in markers] == ["clean", "reconciliation_required"]
    assert rig.risk().status == "complete", "risk is still priced"


def test_unreadable_broker_views_make_both_status_files_unavailable(rig):
    rig.approve()
    rig.buy()
    rig.books.step()
    rig.gw.account_view_error = RuntimeError("view unreadable")
    step = rig.books.step(force=True)
    assert (step.reconciliation, step.risk_snapshot) == ("unavailable", "unavailable")
    assert any(p.startswith("positions_unavailable") for p in step.problems)
    assert BOOKS_PROBLEM_EVENT in rig.names()
    rig.books.step(force=True)
    problem_events = [p for n, p in rig.events if n == BOOKS_PROBLEM_EVENT]
    codes = [p["problem"] for p in problem_events]
    assert len(codes) == len(set(codes)), "each problem code is recorded once"


def test_an_execution_the_contracts_cannot_carry_fails_closed(rig):
    rig.gw.fill_rows.append(
        SimpleNamespace(
            contract=SimpleNamespace(conId=77, secType="FUT", symbol="ES", multiplier="50"),
            execution=SimpleNamespace(execId="0000e0d5.ffffffff.01.01", acctNumber=ACCOUNT),
            commissionReport=None,
            time=rig.clock.utc(),
        )
    )
    step = rig.books.step()
    assert "execution_sec_type_unsupported:77" in step.problems
    assert step.reconciliation == "unavailable" and step.risk_snapshot == "unavailable"


def test_a_fill_whose_proposal_cannot_be_read_waits_for_it(rig):
    proposal = rig.approve(write_proposal=False)
    rig.buy()
    step = rig.books.step()
    assert "attribution_incomplete" in step.problems
    assert [e for e in rig.ledger() if e.kind == "fill"] == [], "never booked as outside"
    assert step.reconciliation == "unavailable"
    rig.write_proposal(proposal)
    rig.books.step(force=True)
    (fill,) = [e for e in rig.ledger() if e.kind == "fill"]
    assert fill.attribution.intent_id == "eq-spy-001"
    assert rig.reconciliation().status == "clean"


def test_without_one_bound_account_nothing_is_written(rig):
    rig.gw.accounts = ["DU0000001", "DU0000002"]
    step = rig.books.step()
    assert (step.account_id, step.problems) == (None, ("account_unbound",))
    assert not (rig.root / "ledger").exists()
