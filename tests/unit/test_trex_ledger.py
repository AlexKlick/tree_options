"""Books lane: the append-only ``trex-ledger-event/1`` ledger and its fold.

Oracles: the frozen contract fixtures (``ledger_event_fill.json``,
``holding.json``) and hand arithmetic in integer micros.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import tree_options.trex.contracts as c
from tests.unit.trex_books_support import (
    ACCOUNT,
    ENTRY,
    EXIT,
    SPY,
    at,
    buy,
    execution,
    gateway_executions,
    sell,
)
from tests.unit.trex_fakes import (
    FillStep,
    Wave2Gateway,
    contract_fixture,
    entry_proposal,
    ledger_event,
)
from tree_options.trex.fills import CommissionReport, fold_executions
from tree_options.trex.ledger import (
    HOLDINGS_SCHEMA,
    MAX_HOLDINGS_BYTES,
    OUTSIDE,
    AttributionIndex,
    HoldingsDocument,
    HoldingsUnavailable,
    LedgerConflict,
    append_events,
    append_executions,
    append_ledger,
    attribution_for_proposal,
    cash_adjustment_event,
    fill_events,
    fold_ledger,
    holdings_document,
    load_holdings,
    read_executions,
    read_ledger,
    symbols_from_executions,
    write_holdings,
)

FIRST = "0000e0d5.6576d6a5.01.01"
RECORDED = at("2026-10-22T14:03:06+00:00")
SPY_ATTR = c.Attribution(
    intent_id="eq-spy-001",
    strategy_version="equity-pilot/1",
    program_id="prog-equity-pilot",
    sleeve_ref="sleeve-equity-01",
)
CANARY_ATTR = c.Attribution(
    intent_id="canary-001",
    strategy_version="operational-canary/1",
    program_id="prog-canary",
    sleeve_ref="unassigned",
)
OUTSIDE_DOC = {
    "intent_id": None,
    "strategy_version": None,
    "program_id": None,
    "sleeve_ref": "unassigned",
}


def option_proposal() -> c.EntryProposal:
    return c.EntryProposal.model_validate(contract_fixture("proposal_option.json"))


def specs() -> list[Any]:
    return [
        c.parse_desk_spec(contract_fixture("desk_spec_v2_equity.json")),
        c.parse_desk_spec(contract_fixture("desk_spec_v2_option.json")),
    ]


INDEX = AttributionIndex.build(proposals=[entry_proposal(), option_proposal()], specs=specs())


def book(executions: list[c.BrokerExecution], **kw: Any) -> tuple[c.LedgerEvent, ...]:
    commissions = kw.pop("commissions", ())
    kw.setdefault("recorded_at", RECORDED)
    return fill_events(fold_executions(executions, commissions), INDEX, **kw)


def ev(
    exec_id: str,
    qty: int,
    price_micros: int,
    *,
    when: str = "2026-10-22T14:03:05+00:00",
    kind: str = "fill",
    supersedes: str | None = None,
    attribution: dict[str, Any] | None = None,
    con_id: int = SPY,
    sec_type: str = "STK",
    multiplier: int = 1,
    fee: int = 0,
) -> c.LedgerEvent:
    """A ledger row built from the fixture fill (independent of the module)."""
    cash = -fee if kind == "commission" else -qty * price_micros * multiplier
    return ledger_event(
        event_id=f"{kind}:{exec_id}",
        kind=kind,
        exec_id=exec_id,
        base_exec_id=c.base_exec_id_of(exec_id),
        quantity_delta=0 if kind == "commission" else qty,
        cash_delta_micros=cash,
        at=when,
        supersedes_event_id=supersedes,
        con_id=con_id,
        sec_type=sec_type,
        attribution=attribution or {},
    )


# -------------------------------------------------------------- attribution


def test_attribution_comes_from_the_proposal() -> None:
    assert attribution_for_proposal(entry_proposal()) == SPY_ATTR
    assert attribution_for_proposal(option_proposal()) == CANARY_ATTR


@pytest.mark.parametrize(
    ("order_ref", "expected"),
    [
        (ENTRY, SPY_ATTR),
        (EXIT, SPY_ATTR),  # equity: the structure id IS the intent id
        ("trex:sup:canary-001", CANARY_ATTR),
        ("trex:desk:canary-001", CANARY_ATTR),  # through the option spec
        ("", OUTSIDE),
        ("manual-tws-order", OUTSIDE),
        ("trex:plan-7:legacy-1", OUTSIDE),  # another book's tag is not ours
        ("trex:sup:ghost", None),
        ("trex:desk:ghost", None),
    ],
)
def test_order_refs_resolve(order_ref: str, expected: c.Attribution | None) -> None:
    assert INDEX.resolve(order_ref) == expected


def test_desk_tags_resolve_through_the_spec_structure_id() -> None:
    option = contract_fixture("desk_spec_v2_option.json")
    option["structure"]["id"] = "canary-001-s"
    index = AttributionIndex.build(proposals=[option_proposal()], specs=[c.parse_desk_spec(option)])
    assert index.resolve("trex:desk:canary-001-s") == CANARY_ATTR
    # no spec: an equity exit still resolves (its structure id is its intent id)
    bare = AttributionIndex.build(proposals=[entry_proposal()])
    assert bare.resolve(EXIT) == SPY_ATTR


def test_outside_attribution_shape() -> None:
    assert OUTSIDE == c.Attribution(intent_id=None, strategy_version=None, program_id=None)


# ---------------------------------------------------------- broker facts -> events


def test_fixture_execution_books_the_fixture_fill_and_its_commission() -> None:
    fill, commission = book([execution()])
    assert c.model_document(fill) == contract_fixture("ledger_event_fill.json")
    assert commission.event_id == f"commission:{FIRST}"
    assert (commission.kind, commission.quantity_delta, commission.cash_delta_micros) == (
        "commission",
        0,
        -1_000_000,
    )
    assert commission.attribution == SPY_ATTR
    assert (commission.exec_id, commission.base_exec_id, commission.con_id) == (
        FIRST,
        c.base_exec_id_of(FIRST),
        SPY,
    )
    assert commission.at == fill.at == at("2026-10-22T14:03:05+00:00")


def test_no_commission_event_until_the_report_arrives() -> None:
    [fill] = book([execution(commission_micros=None)])
    assert fill.kind == "fill"
    later = book(
        [execution(commission_micros=None)],
        commissions=[CommissionReport(FIRST, 1_000_000)],
        existing=[fill],
    )
    assert [e.event_id for e in later] == [f"commission:{FIRST}"]
    # until it arrives the fold says its figures are provisional, never zero-fee
    assert fold_ledger([fill]).pending_commissions == (FIRST,)
    assert not fold_ledger([fill]).complete
    assert fold_ledger([fill, *later]).complete


def test_a_booked_chain_keeps_its_attribution_when_the_index_changes() -> None:
    first = book([execution(commission_micros=None)])
    v2 = execution(exec_id="0000e0d5.6576d6a5.01.02", quantity=1)
    # after a restart the proposal is no longer indexed: the tag would not resolve
    later = fill_events(
        fold_executions([execution(), v2]),
        AttributionIndex(),
        recorded_at=RECORDED,
        existing=first,
    )
    assert {e.kind for e in later} == {"correction", "commission"}
    assert all(e.attribution == SPY_ATTR and e.detail == "" for e in later)
    assert fold_ledger([*first, *later]).positions(ACCOUNT) == {SPY: 1}


def test_a_correction_books_the_adjustment_and_names_what_it_supersedes() -> None:
    v2 = execution(
        exec_id="0000e0d5.6576d6a5.01.02",
        quantity=1,
        price_micros=671_400_000,
        commission_micros=0,
        time="2026-10-22T14:05:00Z",
    )
    events = book([execution(), v2])
    kinds = [(e.event_id, e.kind) for e in events]
    assert kinds == [
        (f"fill:{FIRST}", "fill"),
        ("correction:0000e0d5.6576d6a5.01.02", "correction"),
        (f"commission:{FIRST}", "commission"),
        ("commission:0000e0d5.6576d6a5.01.02", "commission"),
    ]
    correction = events[1]
    assert correction.supersedes_event_id == f"fill:{FIRST}"
    assert correction.quantity_delta == 1 - 2
    assert correction.cash_delta_micros == -671_400_000 - (-2 * 671_480_000)
    fold = fold_ledger(events)
    assert fold.positions(ACCOUNT) == {SPY: 1}
    assert fold.cash(ACCOUNT) == -671_400_000 - 1_000_000


def test_corrections_chain_and_late_older_reports_never_double_book() -> None:
    v1 = execution()
    v2 = execution(exec_id="0000e0d5.6576d6a5.01.02", quantity=1, commission_micros=0)
    v3 = execution(exec_id="0000e0d5.6576d6a5.01.03", quantity=3, commission_micros=0)
    # the recorder first saw only .02 (it booked it as the fill) ...
    first = book([v2])
    assert first[0].event_id == "fill:0000e0d5.6576d6a5.01.02"
    # ... then .01 turned up late and .03 corrected again
    later = book([v1, v2, v3], existing=first)
    assert not [e for e in later if e.kind == "fill"]
    [correction] = [e for e in later if e.kind == "correction"]
    assert correction.event_id == "correction:0000e0d5.6576d6a5.01.03"
    assert correction.supersedes_event_id == "fill:0000e0d5.6576d6a5.01.02"
    assert correction.quantity_delta == 3 - 1
    ledger = append_events(first, later).events
    assert fold_ledger(ledger).positions(ACCOUNT) == {SPY: 3}
    # a full stateless rebuild of the same facts lands on the same position
    assert fold_ledger(book([v1, v2, v3])).positions(ACCOUNT) == {SPY: 3}


def test_combo_executions_are_not_booked() -> None:
    bag = execution(
        exec_id="0000e0d5.70000000.01.01",
        sec_type="BAG",
        con_id=28812380,
        multiplier=100,
        order_ref="trex:sup:canary-001",
        perm_id=77,
    )
    assert book([bag]) == ()


def test_unresolved_trex_tags_book_as_outside_and_are_listed() -> None:
    ghost = execution(order_ref="trex:sup:ghost")
    fill, commission = book([ghost])
    assert fill.attribution == OUTSIDE == commission.attribution
    assert fill.detail == "unattributed:trex:sup:ghost"
    assert fold_ledger([fill, commission]).unattributed == (commission.event_id, fill.event_id)


def _v1_spec(intent_id: str, *, entry_order_ref: str | None = None) -> Any:
    """A ``trex-desk-spec/1`` the v1 inbox registers (no Wave 2 proposal)."""
    from tree_options.trex.desk_spec import DeskSpec
    from tree_options.trex.plan import LegStructure

    option = c.parse_desk_spec(contract_fixture("desk_spec_v2_option.json"))
    structure = LegStructure.model_validate(
        {**option.structure.model_dump(mode="json"), "id": intent_id}
    )
    return DeskSpec(
        intent_id=intent_id,
        account_id=ACCOUNT,
        structure=structure,
        entry_order_ref=entry_order_ref or f"trex:sup:{intent_id}",
        registered_at=at("2026-10-22T14:00:00+00:00"),
    )


def test_a_v1_spec_without_a_proposal_attributes_its_fills_to_the_desk() -> None:
    """W2c item 5: the v1 inbox registers a desk spec but writes no Wave 2
    proposal, so its fills (``trex:sup:<iid>`` entries, ``trex:desk:<sid>``
    exits) used to book OUTSIDE ``unattributed:<ref>``. A known v1 option
    spec whose entry tag is ``trex:sup:<its intent>`` now owns them under the
    fixed desk-v1 attribution; a proposal still wins for its own intent, and
    a tag no spec or proposal explains stays unattributed."""
    from tree_options.trex.ledger import V1_DESK_PROGRAM, V1_DESK_STRATEGY

    proposal = entry_proposal()
    v1 = _v1_spec("v1-canary-001")
    foreign = _v1_spec("v1-foreign", entry_order_ref="trex:sup:someone-else")
    index = AttributionIndex.build(proposals=[proposal], specs=[v1, foreign])
    owned = index.resolve("trex:sup:v1-canary-001")
    assert owned is not None and owned != OUTSIDE
    assert (owned.intent_id, owned.program_id, owned.strategy_version) == (
        "v1-canary-001",
        V1_DESK_PROGRAM,
        V1_DESK_STRATEGY,
    )
    assert index.resolve("trex:desk:v1-canary-001") == owned, "its desk exits too"
    assert index.resolve("trex:sup:v1-foreign") is None, "a spec not tagged as its intent"
    assert index.resolve("trex:sup:ghost") is None
    pid = proposal.effect.intent_id
    assert index.resolve(f"trex:sup:{pid}") == attribution_for_proposal(proposal)
    # a proposal for the same intent as a spec: the proposal's attribution
    both = AttributionIndex.build(proposals=[proposal], specs=[_v1_spec(pid)])
    assert both.resolve(f"trex:sup:{pid}") == attribution_for_proposal(proposal)
    fill, commission = fill_events(
        fold_executions([execution(order_ref="trex:sup:v1-canary-001")]),
        index,
        recorded_at=RECORDED,
    )
    assert fill.attribution == owned == commission.attribution
    assert fill.detail == "" and fold_ledger([fill, commission]).unattributed == ()


def test_outside_executions_are_booked_as_outside() -> None:
    fill, _ = book([execution(order_ref="")])
    assert fill.attribution == OUTSIDE
    assert fill.order_ref is None
    assert fill.detail == ""


def test_option_legs_book_with_the_multiplier() -> None:
    leg = execution(
        exec_id="0000e0d5.70000001.01.01",
        sec_type="OPT",
        con_id=700001,
        multiplier=100,
        price_micros=3_000_000,
        quantity=1,
        order_ref="trex:sup:canary-001",
    )
    fill, _ = book([leg])
    assert (fill.sec_type, fill.quantity_delta, fill.cash_delta_micros) == ("OPT", 1, -300_000_000)
    assert fill.attribution == CANARY_ATTR


def test_cash_adjustment_event_shape() -> None:
    event = cash_adjustment_event(
        account_id=ACCOUNT,
        source_id="interest-2026-10",
        at=at("2026-10-22T20:00:00+00:00"),
        recorded_at=RECORDED,
        cash_delta_micros=1_230_000,
        detail="credit interest",
    )
    assert (event.event_id, event.kind, event.quantity_delta) == (
        "cash:interest-2026-10",
        "cash_adjustment",
        0,
    )
    assert event.attribution == OUTSIDE
    assert fold_ledger([event]).cash(ACCOUNT) == 1_230_000


# ------------------------------------------------------------------- append


def test_replaying_a_broker_fact_is_a_no_op() -> None:
    events = book([execution()])
    replay = book([execution()], recorded_at=at("2026-10-22T15:00:00+00:00"))
    result = append_events(events, replay)
    assert result.appended == ()
    assert result.duplicates == tuple(e.event_id for e in events)
    assert result.events == events
    within = append_events((), [*events, *events])
    assert within.events == events


def test_a_changed_fact_under_the_same_id_refuses() -> None:
    events = book([execution()])
    changed = ledger_event(cash_delta_micros=-1)
    with pytest.raises(LedgerConflict, match=f"fill:{FIRST}"):
        append_events(events, [changed])


def test_a_second_fill_for_one_base_exec_id_refuses() -> None:
    first = book([execution(exec_id="0000e0d5.6576d6a5.01.02")])
    stray = book([execution()])
    with pytest.raises(LedgerConflict, match=re.escape("0000e0d5.6576d6a5.01")):
        append_events(first, stray)


# --------------------------------------------------------------------- fold


def test_fold_of_the_fixture_fill_is_the_fixture_holding() -> None:
    fold = fold_ledger([ledger_event()])
    symbols = symbols_from_executions([execution()])
    [holding] = fold.holdings(ACCOUNT, symbols)
    assert c.model_document(holding) == contract_fixture("holding.json")
    assert fold.touched(ACCOUNT) == frozenset({SPY})


def test_a_round_trip_is_flat_touched_and_realized() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep(price_micros=671_480_000)])
    sell(gw, [FillStep(price_micros=672_000_000)], price=672.0)
    events = book(gateway_executions(gw))
    fold = fold_ledger(events)
    assert fold.positions(ACCOUNT) == {SPY: 0}
    assert fold.holdings(ACCOUNT, {SPY: "SPY"}) == ()
    assert fold.touched(ACCOUNT) == frozenset({SPY})
    assert fold.cash(ACCOUNT) == -2 * 671_480_000 + 2 * 672_000_000 - 2_000_000
    totals = fold.totals_by("intent_id", ACCOUNT)
    # realized net of both commissions (IBKR's convention): 1.04 gained, 2.00 paid
    assert totals["eq-spy-001"].realized_micros == 2 * (672_000_000 - 671_480_000) - 2_000_000
    assert totals["eq-spy-001"].fees_micros == 2_000_000
    assert totals["eq-spy-001"].open_quantity == {}
    day = date(2026, 10, 22)
    assert fold.day_realized_loss_micros(ACCOUNT, day) == 2_000_000 - 1_040_000


def test_an_entry_commission_is_realized_only_when_the_lot_closes() -> None:
    events = [
        ev("a.01.01", 2, 100_000_000, when="2026-10-21T14:00:00+00:00"),
        ev("a.01.01", 0, 0, kind="commission", fee=1_000_000, when="2026-10-21T14:00:00+00:00"),
    ]
    fold = fold_ledger(events)
    assert fold.day_realized_loss_micros(ACCOUNT, date(2026, 10, 21)) == 0
    closed = fold_ledger(
        [*events, ev("b.01.01", -1, 100_000_000, when="2026-10-22T14:00:00+00:00")]
    )
    # half the lot closes flat the next day: half the entry fee is realized then
    assert closed.day_realized_loss_micros(ACCOUNT, date(2026, 10, 21)) == 0
    assert closed.day_realized_loss_micros(ACCOUNT, date(2026, 10, 22)) == 500_000


def test_average_cost_on_a_partial_close() -> None:
    events = [
        ev("a.01.01", 2, 100_000_000, when="2026-10-22T14:00:00+00:00"),
        ev("b.01.01", 1, 130_000_000, when="2026-10-22T14:10:00+00:00"),
        ev("c.01.01", -1, 120_000_000, when="2026-10-22T14:20:00+00:00"),
    ]
    fold = fold_ledger(events)
    [holding] = fold.holdings(ACCOUNT, {SPY: "SPY"})
    assert holding.quantity == 2
    assert holding.cost_basis_micros == 220_000_000  # 2 left at the 110 average
    assert fold.totals_by("intent_id", ACCOUNT)["eq-spy-001"].realized_micros == 10_000_000


def test_lots_split_by_attribution_and_sum_to_the_holding() -> None:
    other = {"intent_id": "eq-spy-002", "sleeve_ref": "sleeve-equity-02"}
    events = [
        ev("a.01.01", 2, 100_000_000),
        ev("b.01.01", 1, 101_000_000, attribution=other),
        ev("c.01.01", 3, 99_000_000, attribution=OUTSIDE_DOC),
    ]
    fold = fold_ledger(events)
    [holding] = fold.holdings(ACCOUNT, {SPY: "SPY"})
    assert holding.quantity == 6
    assert holding.cost_basis_micros == 200_000_000 + 101_000_000 + 297_000_000
    assert sorted((lot.attribution.intent_id or "", lot.quantity) for lot in holding.lots) == [
        ("", 3),
        ("eq-spy-001", 2),
        ("eq-spy-002", 1),
    ]
    programs = fold.totals_by("program_id", ACCOUNT)
    assert programs["prog-equity-pilot"].open_quantity == {SPY: 3}
    assert programs[None].open_quantity == {SPY: 3}
    sleeves = fold.totals_by("sleeve_ref", ACCOUNT)
    assert set(sleeves) == {"sleeve-equity-01", "sleeve-equity-02", "unassigned"}


def test_day_loss_counts_today_per_attribution_without_netting_across() -> None:
    other = {"intent_id": "eq-spy-002"}
    events = [
        # yesterday: intent 1 bought 2 @ 100 and sold 1 @ 90 (a loss, not today's)
        ev("a.01.01", 2, 100_000_000, when="2026-10-21T14:00:00+00:00"),
        ev("b.01.01", -1, 90_000_000, when="2026-10-21T15:00:00+00:00"),
        # today: intent 1 sells its last share @ 95 (-5); intent 2 wins +20
        ev("c.01.01", -1, 95_000_000, when="2026-10-22T14:00:00+00:00"),
        ev("d.01.01", 1, 100_000_000, when="2026-10-22T13:31:00+00:00", attribution=other),
        ev("e.01.01", -1, 120_000_000, when="2026-10-22T15:00:00+00:00", attribution=other),
        ev("c.01.01", 0, 0, kind="commission", fee=250_000, when="2026-10-22T14:00:00+00:00"),
    ]
    fold = fold_ledger(events)
    # a commission with no booked fill is an expense when reported; 23:30 ET
    # on 10-21 is still 10-21: the session day is the ET date
    late = ev("f.01.01", 0, 0, kind="commission", fee=1, when="2026-10-22T03:30:00+00:00")
    assert fold.day_realized_loss_micros(ACCOUNT, date(2026, 10, 22)) == 5_000_000 + 250_000
    assert fold.day_realized_loss_micros(ACCOUNT, date(2026, 10, 21)) == 10_000_000
    later = fold_ledger([*events, late])
    assert later.day_realized_loss_micros(ACCOUNT, date(2026, 10, 21)) == 10_000_001


def test_a_correction_of_an_unknown_event_refuses() -> None:
    orphan = ev("a.01.02", -1, 100_000_000, kind="correction", supersedes="fill:a.01.01")
    with pytest.raises(LedgerConflict, match=re.escape("fill:a.01.01")):
        fold_ledger([orphan])


def test_short_stock_is_refused_as_a_holding_but_still_reconcilable() -> None:
    fold = fold_ledger([ev("a.01.01", -1, 100_000_000)])
    assert fold.positions(ACCOUNT) == {SPY: -1}
    with pytest.raises(LedgerConflict, match="long-only"):
        fold.holdings(ACCOUNT, {SPY: "SPY"})


def test_holdings_need_a_symbol() -> None:
    with pytest.raises(LedgerConflict, match=str(SPY)):
        fold_ledger([ledger_event()]).holdings(ACCOUNT, {})


def _universe() -> list[c.LedgerEvent]:
    other = {"intent_id": "eq-spy-002"}
    canary = c.model_document(CANARY_ATTR)
    rec = c.Reconciliation(account_id=ACCOUNT, status="clean", as_of=RECORDED)
    sha = c.sha256_hex(c.model_bytes(rec))
    marker = ledger_event(
        event_id=f"reconciliation:{sha}",
        kind="reconciliation",
        exec_id=None,
        base_exec_id=None,
        perm_id=None,
        quantity_delta=0,
        cash_delta_micros=0,
        ref_sha256=sha,
        attribution=OUTSIDE_DOC,
    )
    return [
        ev("a.01.01", 2, 100_000_000, when="2026-10-22T14:00:00+00:00"),
        ev("a.01.02", -1, 100_000_000, kind="correction", supersedes="fill:a.01.01"),
        ev("a.01.01", 0, 0, kind="commission", fee=1_000_000),
        ev("b.01.01", 2, 104_000_000, when="2026-10-22T14:05:00+00:00"),
        ev("c.01.01", -2, 110_000_000, when="2026-10-22T14:30:00+00:00"),
        ev("d.01.01", 1, 99_000_000, when="2026-10-22T14:01:00+00:00", attribution=other),
        ev("e.01.01", 4, 98_000_000, attribution=OUTSIDE_DOC),
        ev(
            "f.01.01",
            1,
            3_000_000,
            con_id=700001,
            sec_type="OPT",
            multiplier=100,
            attribution=canary,
        ),
        ev(
            "g.01.01",
            -1,
            1_900_000,
            con_id=700002,
            sec_type="OPT",
            multiplier=100,
            attribution=canary,
        ),
        cash_adjustment_event(
            account_id=ACCOUNT,
            source_id="fee-rebate-1",
            at=RECORDED,
            recorded_at=RECORDED,
            cash_delta_micros=5,
        ),
        marker,
    ]


@settings(max_examples=60, deadline=None)
@given(st.permutations(_universe()))
def test_fold_is_independent_of_event_order(events: list[c.LedgerEvent]) -> None:
    expected = fold_ledger(_universe())
    fold = fold_ledger(events)
    assert fold == expected
    symbols = {SPY: "SPY", 700001: "SPY", 700002: "SPY"}
    assert fold.holdings(ACCOUNT, symbols) == expected.holdings(ACCOUNT, symbols)
    assert fold.day_realized_loss_micros(ACCOUNT, date(2026, 10, 22)) == (
        expected.day_realized_loss_micros(ACCOUNT, date(2026, 10, 22))
    )


def test_universe_figures() -> None:
    fold = fold_ledger(_universe())
    # intent 1: 1 @ 100 (corrected), then 2 @ 104, sold 2 @ 110 at the 102.67 average
    assert fold.positions(ACCOUNT) == {SPY: 1 + 1 + 4, 700001: 1, 700002: -1}
    intent = fold.totals_by("intent_id", ACCOUNT)["eq-spy-001"]
    assert intent.open_quantity == {SPY: 1}
    # 2 of 3 shares close: 2/3 of the cost basis and of the entry fee release
    assert intent.realized_micros == (
        2 * 110_000_000 - (308_000_000 * 2 // 3) - (1_000_000 * 2 // 3)
    )
    assert intent.fees_micros == 1_000_000


# -------------------------------------------------------------- persistence


def test_ledger_file_appends_once_and_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "events.jsonl"
    events = book([execution()])
    assert append_ledger(path, events).appended == events
    again = append_ledger(path, book([execution()], recorded_at=at("2026-10-22T16:00:00+00:00")))
    assert again.appended == ()
    lines = path.read_bytes().splitlines()
    assert len(lines) == 2
    assert lines[0] == c.model_bytes(events[0])
    assert read_ledger(path) == events
    assert read_ledger(tmp_path / "absent.jsonl") == ()


def test_a_conflicting_append_leaves_the_file_untouched(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    append_ledger(path, book([execution()]))
    before = path.read_bytes()
    with pytest.raises(LedgerConflict):
        append_ledger(path, [ledger_event(cash_delta_micros=-1)])
    assert path.read_bytes() == before


def test_a_torn_ledger_line_refuses(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    append_ledger(path, book([execution()]))
    with path.open("ab") as fh:
        fh.write(b'{"event_id": "fill:x')
    with pytest.raises(LedgerConflict, match="line 3"):
        read_ledger(path)


def test_executions_file_keeps_each_distinct_report_once(tmp_path: Path) -> None:
    path = tmp_path / "executions.jsonl"
    early = execution(commission_micros=None)
    assert append_executions(path, [early, early]) == 1
    assert append_executions(path, [early, execution()]) == 1
    reports = read_executions(path)
    assert reports == (early, execution())
    assert fold_executions(reports) == fold_executions([execution()])


def test_holdings_projection_is_written_atomically(tmp_path: Path) -> None:
    run_dir = c.Wave2RunDir(tmp_path)
    fold = fold_ledger(book([execution()]))
    path = write_holdings(run_dir, fold, account_id=ACCOUNT, symbols={SPY: "SPY"}, as_of=RECORDED)
    assert path == run_dir.holdings()
    doc = json.loads(path.read_bytes())
    assert doc["schema"] == HOLDINGS_SCHEMA
    assert doc["account_id"] == ACCOUNT
    assert doc["as_of"] == "2026-10-22T14:03:06Z"
    assert doc["cash_micros"] == -1_342_960_000 - 1_000_000
    assert [c.Holding.model_validate(h) for h in doc["holdings"]] == list(
        fold.holdings(ACCOUNT, {SPY: "SPY"})
    )
    assert sorted(p.name for p in path.parent.iterdir()) == ["holdings.json"]


# ----------------------------------------- trex-holdings/1: the canonical read


def test_holdings_round_trip_through_load_holdings(tmp_path: Path) -> None:
    run_dir = c.Wave2RunDir(tmp_path)
    fold = fold_ledger(book([execution()]))
    path = write_holdings(run_dir, fold, account_id=ACCOUNT, symbols={SPY: "SPY"}, as_of=RECORDED)
    loaded = load_holdings(path)
    assert isinstance(loaded, HoldingsDocument)
    # oracles: the frozen holding fixture and hand arithmetic (fill + its fee)
    assert loaded.schema_version == HOLDINGS_SCHEMA == "trex-holdings/1"
    assert (loaded.account_id, loaded.as_of) == (ACCOUNT, RECORDED)
    assert loaded.cash_micros == -2 * 671_480_000 - 1_000_000
    assert loaded.holdings == (c.Holding.model_validate(contract_fixture("holding.json")),)
    # what the writer wrote is exactly the canonical bytes of what reads back
    assert path.read_bytes() == c.model_bytes(loaded)
    assert loaded == holdings_document(
        fold, account_id=ACCOUNT, symbols={SPY: "SPY"}, as_of=RECORDED
    )


def test_holdings_document_keeps_every_attribution_lot(tmp_path: Path) -> None:
    outside = execution(exec_id="0000e0d5.6576d6a7.01.01", order_ref="", perm_id=55, quantity=1)
    fold = fold_ledger(book([execution(), outside]))
    path = write_holdings(
        c.Wave2RunDir(tmp_path), fold, account_id=ACCOUNT, symbols={SPY: "SPY"}, as_of=RECORDED
    )
    [holding] = load_holdings(path).holdings
    assert holding.quantity == 3
    assert {lot.attribution: lot.quantity for lot in holding.lots} == {SPY_ATTR: 2, OUTSIDE: 1}


def _holdings_doc(**changes: Any) -> dict[str, Any]:
    holding = contract_fixture("holding.json")
    doc: dict[str, Any] = {
        "schema": "trex-holdings/1",
        "account_id": ACCOUNT,
        "as_of": "2026-10-22T14:03:06Z",
        "cash_micros": -1_343_960_000,
        "holdings": [holding],
    }
    doc.update(changes)
    return doc


def _foreign_holding() -> dict[str, Any]:
    return {**contract_fixture("holding.json"), "account_id": "DU0000002"}


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(b'{"schema": "trex-hold', "unreadable", id="torn-json"),
        pytest.param(b"\xff\xfe", "unreadable", id="not-utf8"),
        pytest.param(json.dumps(_holdings_doc()["holdings"]).encode(), "invalid", id="bare-list"),
        pytest.param(
            json.dumps(_holdings_doc(schema="trex-holdings/2")).encode(),
            "invalid",
            id="other-schema",
        ),
        pytest.param(
            json.dumps(_holdings_doc(holdings=[_foreign_holding()])).encode(),
            "invalid",
            id="another-accounts-holding",
        ),
        pytest.param(
            json.dumps(_holdings_doc(holdings=[_holdings_doc()["holdings"][0]] * 2)).encode(),
            "invalid",
            id="two-rows-one-contract",
        ),
        pytest.param(
            json.dumps(_holdings_doc(cash_micros=1.5)).encode(), "invalid", id="fractional-cash"
        ),
        pytest.param(json.dumps(_holdings_doc(extra="x")).encode(), "invalid", id="unknown-field"),
    ],
)
def test_load_holdings_refuses_what_is_not_a_holdings_document(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    path = tmp_path / "holdings.json"
    path.write_bytes(content)
    with pytest.raises(HoldingsUnavailable) as caught:
        load_holdings(path)
    assert caught.value.reason == reason


def test_the_valid_hand_written_document_loads(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"
    path.write_text(json.dumps(_holdings_doc()))
    assert load_holdings(path).holdings[0].quantity == 2


def test_load_holdings_fails_closed_on_the_file_itself(tmp_path: Path) -> None:
    with pytest.raises(HoldingsUnavailable) as caught:
        load_holdings(tmp_path / "absent.json")
    assert caught.value.reason == "missing"

    real = tmp_path / "real.json"
    real.write_text(json.dumps(_holdings_doc()))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(HoldingsUnavailable) as caught:
        load_holdings(link)  # a symlink is never followed
    assert caught.value.reason == "unreadable"

    with pytest.raises(HoldingsUnavailable) as caught:
        load_holdings(tmp_path)  # a directory
    assert caught.value.reason == "unreadable"

    big = tmp_path / "big.json"
    body = json.dumps(_holdings_doc()).encode()
    big.write_bytes(body + b" " * (MAX_HOLDINGS_BYTES + 1 - len(body)))
    with pytest.raises(HoldingsUnavailable) as caught:
        load_holdings(big)
    assert caught.value.reason == "unreadable"
    big.write_bytes(body + b" " * (MAX_HOLDINGS_BYTES - len(body)))
    assert load_holdings(big).account_id == ACCOUNT  # exactly the cap still loads
