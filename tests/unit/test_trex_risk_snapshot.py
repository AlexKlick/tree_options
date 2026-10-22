"""Books lane: ``trex-risk-snapshot/1`` from one broker session.

Oracles: the frozen ``risk_snapshot.json`` fixture (reproduced from the
fixture fill, the fixture portfolio row and a worthless outside option) and
hand arithmetic of the contract's open-loss rule in integer cents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tree_options.trex.contracts as c
from tests.unit.trex_books_support import (
    ACCOUNT,
    SPY,
    as_portfolio_item,
    at,
    buy,
    execution,
    gateway_executions,
    portfolio_item,
)
from tests.unit.trex_fakes import FillStep, Wave2Gateway, contract_fixture, entry_proposal
from tree_options.trex.admission import owner_fact_blockers
from tree_options.trex.contracts.desk_spec_v2 import OptionDeskSpecV2
from tree_options.trex.fills import fold_executions
from tree_options.trex.ledger import AttributionIndex, fill_events, fold_ledger
from tree_options.trex.pause import exit_blockers
from tree_options.trex.risk_snapshot import (
    OUTSIDE_UNDEFINED_RISK,
    UndefinedRisk,
    account_cash,
    build_risk_snapshot,
    equity_open_loss_cents,
    option_open_loss_cents,
    read_risk_snapshot,
    settled_cash_cents,
    write_risk_snapshot,
)

AS_OF = at("2026-10-22T14:04:00+00:00")
NVDA_OPT = 712345678
QQQ = 320227571
EQUITY_SPEC = c.parse_desk_spec(contract_fixture("desk_spec_v2_equity.json"))
OPTION_SPEC = c.parse_desk_spec(contract_fixture("desk_spec_v2_option.json"))
SPECS = [EQUITY_SPEC, OPTION_SPEC]


def option_proposal() -> c.EntryProposal:
    return c.EntryProposal.model_validate(contract_fixture("proposal_option.json"))


INDEX = AttributionIndex.build(proposals=[entry_proposal(), option_proposal()], specs=SPECS)


def fold_of(executions: list[c.BrokerExecution]) -> Any:
    events = fill_events(fold_executions(executions), INDEX, recorded_at=AS_OF)
    return fold_ledger(events)


def nvda(**kw: Any) -> c.BrokerPortfolioItem:
    fields: dict[str, Any] = {
        "con_id": NVDA_OPT,
        "sec_type": "OPT",
        "symbol": "NVDA",
        "position": 1,
        "avg_cost_micros": 0,
        "market_price_micros": 0,
        "market_value_cents": 0,
    }
    fields.update(kw)
    return portfolio_item(**fields)


#: the fixture fill (2 SPY bought at 671.48 with a 1.00 commission at
#: 2026-10-22 14:03:05Z) by hand: what it takes out of cash, in cents
FIXTURE_BUY_CENTS = 2 * 67_148 + 100
#: the fixture snapshot's published cash
FIXTURE_CASH = contract_fixture("risk_snapshot.json")["cash_cents"]


def snapshot(**kw: Any) -> Any:
    args: dict[str, Any] = {
        "account_id": ACCOUNT,
        "as_of": AS_OF,
        "fold": fold_of([execution()]),
        "specs": SPECS,
        "broker_rows": [portfolio_item(), nvda()],
        # IBKR's SettledCash does not show the fixture's same-day buy yet
        # (W2c item 8): the published cash is the fixture's
        "settled_cash_cents": FIXTURE_CASH + FIXTURE_BUY_CENTS,
    }
    args.update(kw)
    return build_risk_snapshot(**args)


def test_reproduces_the_fixture_snapshot() -> None:
    build = snapshot()
    assert build.problems == ()
    # plus the additive optional ``cash_source`` (2026-10-21 cash tag fix)
    assert c.model_document(build.snapshot) == {
        **contract_fixture("risk_snapshot.json"),
        "cash_source": "SettledCash",
    }


# ------------------------------------------- W2c item 8: unsettled stock cash


def test_a_same_day_stock_buy_leaves_the_published_cash_until_it_settles() -> None:
    """IBKR's SettledCash shows a stock purchase only once it settles (T+1,
    the next session): until then the snapshot publishes settled cash minus
    the purchase and its commission. From the settlement session on, the
    broker figure includes it and nothing is subtracted (no double count)."""
    broker = 1_000_000
    assert snapshot(settled_cash_cents=broker).snapshot.cash_cents == broker - FIXTURE_BUY_CENTS
    settled = snapshot(as_of=at("2026-10-23T13:31:00+00:00"), settled_cash_cents=broker)
    assert settled.snapshot.cash_cents == broker
    # a negative result is published as such: admission reads it as borrowing
    short = snapshot(settled_cash_cents=FIXTURE_BUY_CENTS - 1).snapshot
    assert short.cash_cents == -1


def test_a_friday_buy_stays_unsettled_over_the_weekend() -> None:
    friday = execution(time="2026-10-23T14:03:05Z")
    fold = fold_of([friday])
    broker = 1_000_000
    for when, expected in (
        ("2026-10-23T19:00:00+00:00", broker - FIXTURE_BUY_CENTS),  # Friday
        ("2026-10-24T15:00:00+00:00", broker - FIXTURE_BUY_CENTS),  # Saturday
        ("2026-10-26T13:31:00+00:00", broker),  # Monday: settled
    ):
        build = snapshot(fold=fold, as_of=at(when), settled_cash_cents=broker)
        assert build.snapshot.cash_cents == expected, when


def test_a_same_day_sale_adds_nothing_but_its_commission_is_taken() -> None:
    """Conservative: a sale's proceeds are unsettled too and never added
    early; its commission is taken at once like a purchase's."""
    yesterday = execution(time="2026-10-21T14:03:05Z")
    today = execution(
        exec_id="0000e0d5.6576d6b0.01.01",
        side="SLD",
        price_micros=672_000_000,
        time="2026-10-22T14:03:30Z",
        order_ref="trex:desk:eq-spy-001",
        perm_id=1234567891,
    )
    build = snapshot(fold=fold_of([yesterday, today]), broker_rows=[nvda()], settled_cash_cents=0)
    assert build.problems == ()
    assert build.snapshot.cash_cents == -100


def test_an_unplaceable_settlement_date_counts_as_unsettled() -> None:
    build = snapshot(
        as_of=at("2026-10-23T13:31:00+00:00"),
        settled_cash_cents=1_000_000,
        settlement_date=lambda trade_date: None,
    )
    assert build.snapshot.cash_cents == 1_000_000 - FIXTURE_BUY_CENTS


# ------------------------------------------------------------ the open-loss rule


def test_equity_open_loss_is_the_gap_reserve_near_the_stop() -> None:
    # 2 x (671.50 - 665.00) = 13.00 < 20% of 1343.00 = 268.60
    assert equity_open_loss_cents(quantity=2, mark_micros=671_500_000, stop_cents=66_500) == 26_860


def test_equity_open_loss_is_the_stop_distance_far_above_the_stop() -> None:
    # 2 x (900.00 - 665.00) = 470.00 > 20% of 1800.00 = 360.00
    assert equity_open_loss_cents(quantity=2, mark_micros=900_000_000, stop_cents=66_500) == 47_000


def test_equity_open_loss_below_the_stop_keeps_the_gap_reserve() -> None:
    assert equity_open_loss_cents(quantity=1, mark_micros=600_000_000, stop_cents=66_500) == 12_000


def test_equity_open_loss_rounds_up_to_the_cent() -> None:
    # 333.333333 - 10.00 = 323.333333 -> 323.34; gap 66.6666666 -> 66.67
    assert equity_open_loss_cents(quantity=1, mark_micros=333_333_333, stop_cents=1_000) == 32_334
    assert (
        equity_open_loss_cents(
            quantity=1, mark_micros=333_333_333, stop_cents=33_000, gap_reserve_bps=2000
        )
        == 6_667
    )
    assert (
        equity_open_loss_cents(
            quantity=3, mark_micros=100_000_000, stop_cents=9_000, gap_reserve_bps=0
        )
        == 3_000
    )


def test_equity_open_loss_matches_the_entry_reservation_at_the_limit() -> None:
    effect = c.SupervisedEquityEffect.model_validate(contract_fixture("equity_effect.json"))
    at_limit = equity_open_loss_cents(
        quantity=effect.quantity,
        mark_micros=effect.limit_cents * 10_000,
        stop_cents=effect.exit_policy.stop_price_cents,
    )
    assert at_limit == c.equity_max_loss_cents(effect)


def test_option_open_loss_is_the_remaining_defined_max_loss() -> None:
    assert isinstance(OPTION_SPEC, OptionDeskSpecV2)
    structure = OPTION_SPEC.structure  # debit vertical capped at 1.20
    assert option_open_loss_cents(structure, 1) == 12_000
    assert option_open_loss_cents(structure, 3) == 36_000
    assert option_open_loss_cents(structure, 0) == 0


def leg(
    exec_id: str,
    con_id: int,
    side: str,
    price_micros: int,
    qty: int = 1,
    order_ref: str = "trex:sup:canary-001",
    perm_id: int = 77,
) -> Any:
    return execution(
        exec_id=exec_id,
        sec_type="OPT",
        con_id=con_id,
        multiplier=100,
        side=side,
        price_micros=price_micros,
        quantity=qty,
        perm_id=perm_id,
        order_ref=order_ref,
        commission_micros=650_000,
    )


LONG_LEG = leg("0000e0d5.70000001.01.01", 700001, "BOT", 3_000_000)
SHORT_LEG = leg("0000e0d5.70000002.01.01", 700002, "SLD", 1_900_000)


def opt_row(con_id: int, position: int, value_cents: int) -> c.BrokerPortfolioItem:
    return portfolio_item(
        con_id=con_id, sec_type="OPT", position=position, market_value_cents=value_cents
    )


def test_an_open_option_structure_reserves_its_defined_max_loss() -> None:
    fold = fold_of([LONG_LEG, SHORT_LEG])
    rows = [opt_row(700001, 1, 29_000), opt_row(700002, -1, -18_500)]
    build = snapshot(fold=fold, broker_rows=rows)
    assert build.problems == ()
    # the short leg is inside a whole defined-risk package: no undefined risk
    assert (build.blockers, build.undefined_risk) == ((), ())
    assert build.snapshot.open_loss_cents == 12_000  # once per structure, not per leg
    assert build.snapshot.equity_gross_cents == 0
    assert build.snapshot.outside_positions == ()


def test_outside_positions_count_broker_priced() -> None:
    rows = [
        portfolio_item(),
        nvda(position=2, market_value_cents=41_000),
        portfolio_item(con_id=QQQ, symbol="QQQ", position=5, market_value_cents=250_000),
    ]
    build = snapshot(broker_rows=rows)
    assert build.problems == ()
    snap = build.snapshot
    assert snap.open_loss_cents == 26_860 + 41_000 + 250_000
    assert snap.equity_gross_cents == 134_300 + 250_000
    assert snap.outside_positions == (
        c.OutsidePosition(con_id=QQQ, sec_type="STK", symbol="QQQ", quantity=5),
        c.OutsidePosition(con_id=NVDA_OPT, sec_type="OPT", symbol="NVDA", quantity=2),
    )


# ------------------------------------------------ undefined risk fails closed


def test_an_outside_short_call_blocks_entries_with_outside_undefined_risk(
    tmp_path: Path,
) -> None:
    # NVDA call written outside the desk: |market value| is no bound on its loss
    rows = [portfolio_item(), nvda(position=-1, market_value_cents=-20_500)]
    build = snapshot(broker_rows=rows)
    assert OUTSIDE_UNDEFINED_RISK == "outside_undefined_risk"
    assert build.blockers == ("outside_undefined_risk",)
    assert build.undefined_risk == (
        UndefinedRisk(
            con_id=NVDA_OPT,
            sec_type="OPT",
            quantity=-1,
            reason="outside_short_option",
            intent_id=None,
        ),
    )
    assert any(
        p.startswith("outside_undefined_risk") and str(NVDA_OPT) in p for p in build.problems
    ), build.problems
    snap = build.snapshot
    assert snap.status == "unavailable"
    assert (snap.open_loss_cents, snap.cash_cents, snap.outside_positions) == (None, None, ())
    assert build.entry_blockers(AS_OF) == ("risk_snapshot_stale", "outside_undefined_risk")
    # the published file fails closed through today's admission; exits never read it
    write_risk_snapshot(c.Wave2RunDir(tmp_path), snap)
    assert "risk_snapshot_stale" in owner_fact_blockers(tmp_path, AS_OF, account_id=ACCOUNT)
    assert exit_blockers(tmp_path) == ()


def test_an_outside_long_option_is_still_priced_at_its_value() -> None:
    build = snapshot(broker_rows=[portfolio_item(), nvda(position=3, market_value_cents=61_500)])
    assert (build.problems, build.blockers, build.undefined_risk) == ((), (), ())
    assert build.snapshot.status == "complete"
    assert build.snapshot.open_loss_cents == 26_860 + 61_500
    assert build.entry_blockers(AS_OF) == ()


def test_an_outside_short_stock_is_undefined_risk() -> None:
    rows = [
        portfolio_item(),
        portfolio_item(con_id=QQQ, symbol="QQQ", position=-5, market_value_cents=-250_000),
    ]
    build = snapshot(broker_rows=rows)
    assert build.blockers == (OUTSIDE_UNDEFINED_RISK,)
    assert build.undefined_risk == (
        UndefinedRisk(
            con_id=QQQ, sec_type="STK", quantity=-5, reason="short_stock", intent_id=None
        ),
    )
    assert build.snapshot.status == "unavailable"


UNTAGGED_SHORT = leg("0000e0d5.70000003.01.01", 700001, "SLD", 3_100_000, order_ref="", perm_id=78)
GHOST_SHORT = leg(
    "0000e0d5.70000009.01.01", 700009, "SLD", 1_000_000, order_ref="trex:sup:ghost", perm_id=79
)


@pytest.mark.parametrize(
    ("executions", "rows", "expected"),
    [
        pytest.param(
            [GHOST_SHORT],
            [opt_row(700009, -1, -10_000)],
            [(700009, -1, None)],
            id="unattributed-trex-tag",
        ),
        pytest.param(
            [LONG_LEG, SHORT_LEG, UNTAGGED_SHORT],
            [opt_row(700001, 0, 0), opt_row(700002, -1, -18_500)],
            [(700001, -1, None)],
            id="outside-lot-in-a-touched-contract",
        ),
    ],
)
def test_outside_or_unattributed_short_option_lots_are_undefined_risk(
    executions: list[c.BrokerExecution], rows: list[c.BrokerPortfolioItem], expected: list[Any]
) -> None:
    build = snapshot(fold=fold_of(executions), broker_rows=rows)
    assert build.blockers == (OUTSIDE_UNDEFINED_RISK,)
    assert build.undefined_risk == tuple(
        UndefinedRisk(
            con_id=con, sec_type="OPT", quantity=qty, reason="short_option_lot", intent_id=intent
        )
        for con, qty, intent in expected
    )
    assert build.snapshot.status == "unavailable"


LONG_LEG_CLOSED = leg(
    "0000e0d5.70000004.01.01",
    700001,
    "SLD",
    3_200_000,
    order_ref="trex:desk:canary-001",
    perm_id=80,
)
LONG_LEG_OVERSOLD = leg(
    "0000e0d5.70000005.01.01",
    700001,
    "SLD",
    3_200_000,
    qty=2,
    order_ref="trex:desk:canary-001",
    perm_id=81,
)


@pytest.mark.parametrize(
    ("executions", "specs", "rows", "expected"),
    [
        pytest.param(
            [LONG_LEG, SHORT_LEG, LONG_LEG_CLOSED],
            SPECS,
            [opt_row(700001, 0, 0), opt_row(700002, -1, -18_500)],
            [(700002, -1)],
            id="short-leg-left-alone",
        ),
        pytest.param(
            [LONG_LEG, SHORT_LEG, LONG_LEG_OVERSOLD],
            SPECS,
            [opt_row(700001, -1, -31_000), opt_row(700002, -1, -18_500)],
            [(700001, -1), (700002, -1)],
            id="both-legs-short",
        ),
        pytest.param(
            [LONG_LEG, SHORT_LEG],
            [EQUITY_SPEC],
            [opt_row(700001, 1, 29_000), opt_row(700002, -1, -18_500)],
            [(700002, -1)],
            id="option-spec-gone",
        ),
    ],
)
def test_our_short_legs_outside_a_whole_defined_risk_package_are_undefined_risk(
    executions: list[c.BrokerExecution],
    specs: list[Any],
    rows: list[c.BrokerPortfolioItem],
    expected: list[tuple[int, int]],
) -> None:
    build = snapshot(fold=fold_of(executions), specs=specs, broker_rows=rows)
    assert build.blockers == (OUTSIDE_UNDEFINED_RISK,)
    assert build.undefined_risk == tuple(
        UndefinedRisk(
            con_id=con,
            sec_type="OPT",
            quantity=qty,
            reason="short_option_lot",
            intent_id="canary-001",
        )
        for con, qty in expected
    )


# ------------------------------- W2c item 5: v1 canary coexistence (no proposal)

V1_ID = "v1-canary-001"


def _v1_spec() -> Any:
    from tree_options.trex.desk_spec import DeskSpec
    from tree_options.trex.plan import LegStructure

    assert isinstance(OPTION_SPEC, OptionDeskSpecV2)
    return DeskSpec(
        intent_id=V1_ID,
        account_id=ACCOUNT,
        structure=LegStructure.model_validate(
            {**OPTION_SPEC.structure.model_dump(mode="json"), "id": V1_ID}
        ),
        entry_order_ref=f"trex:sup:{V1_ID}",
        registered_at=AS_OF,
    )


V1_SPEC = _v1_spec()
V1_SPECS = [*SPECS, V1_SPEC]
V1_INDEX = AttributionIndex.build(proposals=[entry_proposal(), option_proposal()], specs=V1_SPECS)
V1_LONG = leg("0000e0d5.7000000a.01.01", 700021, "BOT", 3_000_000, order_ref=f"trex:sup:{V1_ID}")
V1_SHORT = leg("0000e0d5.7000000b.01.01", 700022, "SLD", 1_900_000, order_ref=f"trex:sup:{V1_ID}")
V1_LONG_CLOSED = leg(
    "0000e0d5.7000000c.01.01", 700021, "SLD", 3_200_000, order_ref=f"trex:desk:{V1_ID}", perm_id=91
)
V1_SHORT_CLOSED = leg(
    "0000e0d5.7000000d.01.01", 700022, "BOT", 1_000_000, order_ref=f"trex:desk:{V1_ID}", perm_id=91
)
V1_ROWS = [opt_row(700021, 1, 29_000), opt_row(700022, -1, -18_500)]


def v1_snapshot(executions: list[c.BrokerExecution], rows: list[Any]) -> Any:
    events = fill_events(fold_executions([execution(), *executions]), V1_INDEX, recorded_at=AS_OF)
    return snapshot(fold=fold_ledger(events), specs=V1_SPECS, broker_rows=[portfolio_item(), *rows])


def test_a_v1_canary_spread_is_a_desk_owned_defined_risk_structure() -> None:
    """W2c item 5: a v1 inbox debit spread (a desk spec, no Wave 2 proposal)
    used to book OUTSIDE and fail the snapshot closed on its short leg,
    blocking every Wave 2 entry. It is ours now, reserved at the desk
    spec's defined max loss (once per structure), and blocks nothing."""
    build = v1_snapshot([V1_LONG, V1_SHORT], V1_ROWS)
    assert (build.problems, build.blockers, build.undefined_risk) == ((), (), ())
    assert build.snapshot.status == "complete"
    defined = option_open_loss_cents(V1_SPEC.structure, 1)
    assert build.snapshot.open_loss_cents == 26_860 + defined
    assert build.entry_blockers(AS_OF) == ()
    # its desk exit closes it under the same attribution: flat, nothing held
    closed = v1_snapshot([V1_LONG, V1_SHORT, V1_LONG_CLOSED, V1_SHORT_CLOSED], [])
    assert (closed.blockers, closed.snapshot.open_loss_cents) == ((), 26_860)


def test_a_broken_v1_package_or_a_genuine_outside_short_still_blocks() -> None:
    # the desk sold the long leg only: the short leg is no defined package
    broken = v1_snapshot(
        [V1_LONG, V1_SHORT, V1_LONG_CLOSED], [opt_row(700021, 0, 0), opt_row(700022, -1, -18_500)]
    )
    assert broken.undefined_risk == (UndefinedRisk(700022, "OPT", -1, "short_option_lot", V1_ID),)
    # beside a whole v1 spread, an outside short call still fails closed
    outside = v1_snapshot(
        [V1_LONG, V1_SHORT], [*V1_ROWS, nvda(position=-1, market_value_cents=-20_500)]
    )
    assert outside.blockers == (OUTSIDE_UNDEFINED_RISK,)
    assert outside.undefined_risk == (UndefinedRisk(NVDA_OPT, "OPT", -1, "outside_short_option"),)
    assert outside.snapshot.status == "unavailable"


def test_a_broker_short_beyond_our_defined_short_leg_is_undefined_risk() -> None:
    fold = fold_of([LONG_LEG, SHORT_LEG])
    rows = [opt_row(700001, 1, 29_000), opt_row(700002, -2, -37_000)]
    build = snapshot(fold=fold, broker_rows=rows)
    assert build.blockers == (OUTSIDE_UNDEFINED_RISK,)
    assert build.undefined_risk == (
        UndefinedRisk(
            con_id=700002,
            sec_type="OPT",
            quantity=-2,
            reason="unexplained_short_option",
            intent_id=None,
        ),
    )


def test_an_outside_stock_without_market_value_is_priced_from_its_mark() -> None:
    rows = [
        portfolio_item(),
        portfolio_item(
            con_id=QQQ,
            symbol="QQQ",
            position=3,
            market_price_micros=500_123_456,
            market_value_cents=None,
        ),
    ]
    build = snapshot(broker_rows=rows)
    # 3 x 500.123456 = 1500.370368 -> 1500.38
    assert build.snapshot.open_loss_cents == 26_860 + 150_038
    assert build.snapshot.equity_gross_cents == 134_300 + 150_038


def test_ledger_lots_without_a_stop_are_priced_at_full_value() -> None:
    build = snapshot(specs=[])  # the SPY lot has no equity spec: no stop known
    assert build.snapshot.open_loss_cents == 134_300


def test_outside_lots_in_a_touched_contract_are_priced_at_full_value() -> None:
    fold = fold_of(
        [
            execution(),
            execution(exec_id="0000e0d5.6576d6a7.01.01", order_ref="", perm_id=55, quantity=1),
        ]
    )
    rows = [portfolio_item(position=3, market_value_cents=201_450)]
    build = snapshot(fold=fold, broker_rows=rows)
    assert build.problems == ()
    # our 2 at the gap reserve of 2 x 671.50, the outside 1 at its value
    assert build.snapshot.open_loss_cents == 26_860 + 67_150


def test_day_realized_loss_comes_from_the_ledger_in_cents() -> None:
    exit_fill = execution(
        exec_id="0000e0d5.6576d6a7.01.01",
        side="SLD",
        perm_id=1234567891,
        order_ref="trex:desk:eq-spy-001",
        price_micros=671_000_000,
        commission_micros=1_000_001,
    )
    fold = fold_of([execution(), exit_fill])
    build = snapshot(fold=fold, broker_rows=[portfolio_item(position=0), nvda()])
    # 2 x (671.00 - 671.48) - 1.00 - 1.000001 = -2.960001 -> 296.0001 cents -> 297
    assert build.snapshot.day_realized_loss_cents == 297
    assert build.snapshot.open_loss_cents == 0
    assert build.snapshot.equity_gross_cents == 0


# ------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"broker_rows": None}, "broker positions unreadable"),
        ({"settled_cash_cents": None}, "settled cash unreadable"),
        ({"broker_rows": [nvda()]}, "no broker mark"),
        (
            {
                "broker_rows": [
                    portfolio_item(market_price_micros=None, market_value_cents=None),
                    nvda(),
                ]
            },
            "no broker mark",
        ),
        ({"broker_rows": [portfolio_item(), nvda(market_value_cents=None)]}, "cannot price"),
    ],
)
def test_unreadable_inputs_make_the_snapshot_unavailable(
    change: dict[str, Any], problem: str
) -> None:
    build = snapshot(**change)
    snap = build.snapshot
    assert snap.status == "unavailable"
    assert (snap.open_loss_cents, snap.cash_cents, snap.outside_positions) == (None, None, ())
    assert any(problem in p for p in build.problems), build.problems
    assert c.risk_snapshot_blockers(snap, AS_OF) == (c.Blocker.RISK_SNAPSHOT_STALE,)


def test_an_inconsistent_ledger_makes_the_snapshot_unavailable() -> None:
    fold = fold_of(
        [execution(exec_id="0000e0d5.6576d6a7.01.01", side="SLD", order_ref="trex:desk:eq-spy-001")]
    )
    build = snapshot(fold=fold, broker_rows=[portfolio_item(position=-2)])
    assert build.snapshot.status == "unavailable"
    assert any("long-only" in p for p in build.problems)


def test_stale_after_120_seconds() -> None:
    snap = snapshot().snapshot
    assert c.risk_snapshot_blockers(snap, at("2026-10-22T14:06:00+00:00")) == ()
    assert c.risk_snapshot_blockers(snap, at("2026-10-22T14:06:01+00:00")) == (
        c.Blocker.RISK_SNAPSHOT_STALE,
    )
    assert c.risk_snapshot_blockers(snap, at("2026-10-22T14:03:59+00:00")) == (
        c.Blocker.RISK_SNAPSHOT_STALE,
    )


# ------------------------------------------------------------ settled cash


def value(tag: str, val: str, currency: str = "USD", account: str = ACCOUNT) -> Any:
    return SimpleNamespace(account=account, tag=tag, value=val, currency=currency, modelCode="")


def test_settled_cash_from_the_gateway_account_values() -> None:
    gw = Wave2Gateway()
    assert settled_cash_cents(gw.accountValues(), account_id=ACCOUNT) == 100_000_000


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([value("SettledCash", "48657.04")], 4_865_704),
        ([value("SettledCash", "-12.5")], -1_250),
        ([value("SettledCash", "10.019")], 1_001),  # sub-cent cash rounds down
        ([value("SettledCash", "-10.011")], -1_002),
        ([value("NetLiquidation", "5.00")], None),
        ([value("SettledCash", "5.00", currency="EUR")], None),
        ([value("SettledCash", "5.00", account="DU0000002")], None),
        ([value("SettledCash", "n/a")], None),
        ([value("SettledCash", "NaN")], None),
        ([value("SettledCash", "5.00"), value("SettledCash", "6.00")], None),
        ([value("SettledCash", "5.00"), value("SettledCash", "5.00")], 500),
    ],
)
def test_settled_cash_parsing(rows: list[Any], expected: int | None) -> None:
    assert settled_cash_cents(rows, account_id=ACCOUNT) == expected


# ------------------------------------- the cash tag (2026-10-21 paper account)


def test_cash_falls_back_to_total_cash_value_without_settled_cash() -> None:
    """The IBKR paper account is margin-type: its accountValues carry no
    SettledCash, only TotalCashValue. That is the cash, and it says so."""
    rows = [value("NetLiquidation", "50000.00"), value("TotalCashValue", "48657.04")]
    cash = account_cash(rows, account_id=ACCOUNT)
    assert cash is not None
    assert (cash.cents, cash.source) == (4_865_704, "TotalCashValue")
    assert settled_cash_cents(rows, account_id=ACCOUNT) == 4_865_704


def test_settled_cash_wins_over_total_cash_value() -> None:
    rows = [value("TotalCashValue", "48657.04"), value("SettledCash", "40000.00")]
    cash = account_cash(rows, account_id=ACCOUNT)
    assert cash is not None
    assert (cash.cents, cash.source) == (4_000_000, "SettledCash")


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # neither tag: unavailable, exactly as before
        ([value("NetLiquidation", "5.00")], None),
        ([], None),
        # USD first, else BASE, for either tag
        ([value("SettledCash", "7.00", currency="BASE")], (700, "SettledCash")),
        (
            [value("SettledCash", "7.00", currency="BASE"), value("SettledCash", "6.00")],
            (600, "SettledCash"),
        ),
        ([value("TotalCashValue", "8.00", currency="BASE")], (800, "TotalCashValue")),
        (
            [value("TotalCashValue", "8.00", currency="BASE"), value("TotalCashValue", "9.00")],
            (900, "TotalCashValue"),
        ),
        ([value("TotalCashValue", "10.019")], (1_001, "TotalCashValue")),  # floored
        # the tag decides first: a BASE SettledCash beats a USD TotalCashValue
        (
            [value("TotalCashValue", "9.00"), value("SettledCash", "7.00", currency="BASE")],
            (700, "SettledCash"),
        ),
        # a SettledCash in neither currency, or another account's, is absent
        (
            [value("SettledCash", "5.00", currency="EUR"), value("TotalCashValue", "9.00")],
            (900, "TotalCashValue"),
        ),
        (
            [value("SettledCash", "5.00", account="DU0000002"), value("TotalCashValue", "9.00")],
            (900, "TotalCashValue"),
        ),
        ([value("TotalCashValue", "9.00", currency="EUR")], None),
        ([value("TotalCashValue", "9.00", account="DU0000002")], None),
        # a present SettledCash that is junk or ambiguous is unreadable: it is
        # never replaced by the other currency or by TotalCashValue
        ([value("SettledCash", "n/a"), value("TotalCashValue", "9.00")], None),
        ([value("SettledCash", "junk"), value("SettledCash", "7.00", currency="BASE")], None),
        (
            [
                value("SettledCash", "5.00"),
                value("SettledCash", "6.00"),
                value("TotalCashValue", "9.00"),
            ],
            None,
        ),
        ([value("TotalCashValue", "NaN")], None),
        ([value("TotalCashValue", "5.00"), value("TotalCashValue", "6.00")], None),
    ],
)
def test_the_cash_tag_and_currency_rule(rows: list[Any], expected: tuple[int, str] | None) -> None:
    cash = account_cash(rows, account_id=ACCOUNT)
    assert (None if cash is None else (cash.cents, cash.source)) == expected


def test_the_snapshot_records_which_tag_its_cash_came_from() -> None:
    settled = snapshot().snapshot
    assert settled.cash_source == "SettledCash"
    total = snapshot(cash_source="TotalCashValue").snapshot
    assert total.cash_source == "TotalCashValue"
    assert c.model_document(total)["cash_source"] == "TotalCashValue"
    # the fixture fold sold nothing: the same conservative figure either way
    assert total.cash_cents == settled.cash_cents == FIXTURE_CASH
    unavailable = snapshot(settled_cash_cents=None, cash_source="TotalCashValue")
    assert unavailable.problems == ("settled cash unreadable",)
    assert unavailable.snapshot.cash_source is None
    assert "cash_source" not in c.model_document(unavailable.snapshot)


def test_total_cash_value_shows_a_same_day_sale_so_its_proceeds_come_back_out() -> None:
    """TotalCashValue books a sale's proceeds on the trade date, settled cash
    only from the settlement session (T+1). Proceeds are never added early
    (W2c), so from a TotalCashValue figure the snapshot takes the unsettled
    proceeds (2 x 672.00) back out beside the sale's commission; from the
    settlement session on nothing is taken."""
    yesterday = execution(time="2026-10-21T14:03:05Z")
    today = execution(
        exec_id="0000e0d5.6576d6b0.01.01",
        side="SLD",
        price_micros=672_000_000,
        time="2026-10-22T14:03:30Z",
        order_ref="trex:desk:eq-spy-001",
        perm_id=1234567891,
    )
    fold = fold_of([yesterday, today])
    kw: dict[str, Any] = {"fold": fold, "broker_rows": [nvda()], "settled_cash_cents": 0}
    total = snapshot(cash_source="TotalCashValue", **kw)
    assert total.problems == ()
    assert total.snapshot.cash_cents == -(100 + 2 * 67_200)
    # the same fold read from SettledCash: only the commission (unchanged)
    assert snapshot(**kw).snapshot.cash_cents == -100
    settled = snapshot(cash_source="TotalCashValue", as_of=at("2026-10-23T13:31:00+00:00"), **kw)
    assert settled.snapshot.cash_cents == 0


# ---------------------------------------------------------- against the fake


def test_gateway_session_snapshot() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    gw.marks[SPY] = 680_000_000
    fold = fold_ledger(
        fill_events(fold_executions(gateway_executions(gw)), INDEX, recorded_at=AS_OF)
    )
    rows = [as_portfolio_item(item, AS_OF) for item in gw.portfolio()]
    build = build_risk_snapshot(
        account_id=ACCOUNT,
        as_of=AS_OF,
        fold=fold,
        specs=SPECS,
        broker_rows=rows,
        settled_cash_cents=settled_cash_cents(gw.accountValues(), account_id=ACCOUNT),
    )
    assert build.problems == ()
    snap = build.snapshot
    # 2 x (680.00 - 665.00) = 30.00 < 20% of 1360.00 = 272.00
    assert (snap.open_loss_cents, snap.equity_gross_cents) == (27_200, 136_000)
    # the fake's SettledCash (1,000,000.00) does not show today's buy of
    # 2 x 671.50 and its 1.00 commission yet (W2c item 8)
    assert snap.cash_cents == 100_000_000 - (2 * 67_150 + 100)
    assert snap.day_realized_loss_cents == 0


# -------------------------------------------------------------- persistence


def test_snapshot_file_round_trip(tmp_path: Path) -> None:
    run_dir = c.Wave2RunDir(tmp_path)
    assert read_risk_snapshot(run_dir) is None
    snap = snapshot().snapshot
    path = write_risk_snapshot(run_dir, snap)
    assert path == run_dir.risk_snapshot()
    assert path.read_bytes() == c.model_bytes(snap)
    assert read_risk_snapshot(run_dir) == snap
    assert sorted(p.name for p in path.parent.iterdir()) == ["risk-snapshot.json"]
    path.write_text(json.dumps({"schema": "trex-risk-snapshot/1"}))
    assert read_risk_snapshot(run_dir) is None


def test_books_modules_import_without_a_broker_library() -> None:
    repo = Path(__file__).resolve().parents[2]
    modules = ", ".join(f"tree_options.trex.{m}" for m in ("fills", "ledger", "reconcile"))
    probe = (
        f"import sys, {modules}, tree_options.trex.risk_snapshot; "
        "sys.exit(1 if 'ib_async' in sys.modules else 0)"
    )
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], cwd=repo, env=env, capture_output=True, timeout=120
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]
