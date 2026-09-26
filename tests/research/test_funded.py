"""Funded-account accounting oracles (RL1-01 correction).

Every expectation below is HAND-CALCULATED from the economic identity

    NAV = cash + Σ quantity * mark          (account value, not cash)
    investment_gain = NAV - opening - contributions + withdrawals

— never from the implementation's own expression. The 2026-09-25 audit
reproduced the previous engine labeling a cash balance as wealth
($9,950 "ending value" while holding $50 of inventory), double-counting
realized P&L after sale proceeds ($10,520 vs $10,510), dropping
cash-only dates and post-trade flows, excluding fees, and silently
financing a $200 purchase from a $100 account. These tests pin the
correct economics.

The returned LedgerBook must independently conserve: its
``assert_conservation()`` replays the fill stream from primitive fields
— passing it is part of the contract, not a courtesy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tree_options.backtest.equity import FiveBasisPointFeeModel
from tree_options.research.comparison.funded import (
    CashflowEvent,
    MarkObservation,
    TradeExecution,
    run_funded_account,
)

D = Decimal


def _run(start: str, calendar, executions=(), marks=(), cashflows=(), **kw):
    return run_funded_account(
        candidate_id="test",
        starting_capital=D(start),
        calendar=calendar,
        executions=list(executions),
        marks=list(marks),
        cashflows=list(cashflows),
        **kw,
    )


# -- 1. NAV is cash plus inventory ------------------------------------------


def test_buy_and_hold_at_unchanged_mark_preserves_nav() -> None:
    """Start $10,000; buy 10 @ $5; mark still $5. Cash 9,950 +
    inventory 10*$5 = $50 ⇒ NAV $10,000. The old engine reported
    $9,950 (cash labeled as wealth)."""
    cal = (date(2024, 1, 2), date(2024, 1, 3))
    run = _run(
        "10000", cal,
        executions=[TradeExecution(date(2024, 1, 2), "SPY", 10, D("5.00"))],
        marks=[MarkObservation(date(2024, 1, 2), "SPY", D("5.00"))],
    )
    assert run.refusal_reason is None
    assert len(run.rows) == 2  # no-trade second session still has a row
    for r in run.rows:
        assert r.cash == D("9950.00")
        assert r.inventory == (("SPY", 10),)
        assert r.marked_value == D("50.00")
        assert r.nav == D("10000.00")
        assert r.investment_gain == D("0.00")
    assert run.final_nav == D("10000.00")


def test_roundtrip_and_contribution_do_not_double_count_profit() -> None:
    """Buy 10 @ $5 (Jan 2), contribute $500 (Jan 15), sell 10 @ $6
    (Jun 1).

    Hand math: cash after buy = 9,950; after contribution = 10,450;
    after sale = 10,510. Inventory 0. Final NAV $10,510 (the old engine
    said $10,520 by adding a separately supplied realized figure on top
    of sale proceeds). Mid-window NAV (holding 10 @ $5) = 10,500.
    investment_gain = 10,510 - 10,000 - 500 = $10, matching FIFO
    realized (proceeds 60 - cost 50)."""
    cal = (date(2024, 1, 2), date(2024, 1, 15), date(2024, 6, 1))
    run = _run(
        "10000", cal,
        executions=[
            TradeExecution(date(2024, 1, 2), "SPY", 10, D("5.00")),
            TradeExecution(date(2024, 6, 1), "SPY", -10, D("6.00")),
        ],
        marks=[
            MarkObservation(date(2024, 1, 2), "SPY", D("5.00")),
            MarkObservation(date(2024, 6, 1), "SPY", D("6.00")),
        ],
        cashflows=[CashflowEvent(date(2024, 1, 15), D("500"))],
    )
    assert run.refusal_reason is None
    r_mid = run.rows[1]
    assert r_mid.cash == D("10450.00")
    assert r_mid.marked_value == D("50.00")
    assert r_mid.nav == D("10500.00")
    final = run.rows[2]
    assert final.inventory == ()
    assert final.nav == D("10510.00")
    assert final.contributions_cum == D("500.00")
    assert final.investment_gain == D("10.00")
    assert run.ledger.realized_pnl("SPY") == D("10.00")


def test_explicit_fees_reduce_account_value_exactly_once() -> None:
    """Same flat round trip, $1 fee per execution, no external realized
    figure: 10,000 - 50 - 1 + 60 - 1 = $10,008; cumulative fees $2;
    gain = realized (10) - fees (2) = $8."""
    cal = (date(2024, 1, 2), date(2024, 6, 1))
    run = _run(
        "10000", cal,
        executions=[
            TradeExecution(date(2024, 1, 2), "SPY", 10, D("5.00"), fees=D("1.00")),
            TradeExecution(date(2024, 6, 1), "SPY", -10, D("6.00"), fees=D("1.00")),
        ],
        marks=[
            MarkObservation(date(2024, 1, 2), "SPY", D("5.00")),
            MarkObservation(date(2024, 6, 1), "SPY", D("6.00")),
        ],
    )
    assert run.refusal_reason is None
    final = run.rows[-1]
    assert final.nav == D("10008.00")
    assert final.fees_cum == D("2.00")
    assert final.investment_gain == D("8.00")
    assert run.total_fees == D("2.00")


def test_cashflow_only_history_is_not_empty() -> None:
    """A $500 contribution with no trades: rows on both sessions, final
    NAV $10,500 (zero-yield cash policy). The old engine emitted no
    rows and a null final value."""
    cal = (date(2024, 1, 2), date(2024, 1, 3))
    run = _run(
        "10000", cal,
        cashflows=[CashflowEvent(date(2024, 1, 3), D("500"))],
    )
    assert run.refusal_reason is None
    assert len(run.rows) == 2
    assert run.rows[0].nav == D("10000.00")
    assert run.rows[1].contributions_cum == D("500.00")
    assert run.final_nav == D("10500.00")
    assert run.rows[1].investment_gain == D("0.00")


def test_withdrawal_after_last_trade_is_counted() -> None:
    """Flat round trip ends Jan value $10,000; a $500 withdrawal on the
    session AFTER the final trade leaves $9,500. The old engine dropped
    the flow and kept $10,000."""
    cal = (date(2024, 1, 2), date(2024, 2, 1), date(2024, 2, 2))
    run = _run(
        "10000", cal,
        executions=[
            TradeExecution(date(2024, 1, 2), "SPY", 10, D("5.00")),
            TradeExecution(date(2024, 2, 1), "SPY", -10, D("5.00")),
        ],
        marks=[
            MarkObservation(date(2024, 1, 2), "SPY", D("5.00")),
            MarkObservation(date(2024, 2, 1), "SPY", D("5.00")),
        ],
        cashflows=[CashflowEvent(date(2024, 2, 2), D("-500"))],
    )
    assert run.refusal_reason is None
    assert run.final_nav == D("9500.00")
    assert run.rows[-1].withdrawals_cum == D("500.00")
    # the withdrawal is not a loss: flat account + zero gain - 500 out
    assert run.rows[-1].investment_gain == D("0.00")


def test_flat_zero_pnl_roundtrip_control() -> None:
    cal = (date(2024, 1, 2), date(2024, 6, 1))
    run = _run(
        "10000", cal,
        executions=[
            TradeExecution(date(2024, 1, 2), "SPY", 10, D("5.00")),
            TradeExecution(date(2024, 6, 1), "SPY", -10, D("5.00")),
        ],
        marks=[
            MarkObservation(date(2024, 1, 2), "SPY", D("5.00")),
            MarkObservation(date(2024, 6, 1), "SPY", D("5.00")),
        ],
    )
    assert run.final_nav == D("10000.00")
    assert run.ledger.realized_pnl("SPY") == D("0.00")


# -- 2. no implicit borrowing -------------------------------------------------


def test_insufficient_capital_refuses_instead_of_borrowing() -> None:
    """$100 account, buy 2 @ $100 needs $200: the run refuses with a
    reason; no rows, no fills, no negative cash anywhere."""
    cal = (date(2024, 1, 2),)
    run = _run(
        "100", cal,
        executions=[TradeExecution(date(2024, 1, 2), "SPY", 2, D("100.00"))],
        marks=[MarkObservation(date(2024, 1, 2), "SPY", D("100.00"))],
    )
    assert run.refusal_reason == "research.funded.insufficient_cash"
    assert run.rows == ()
    assert run.final_nav is None
    assert run.ledger.entries == ()  # nothing was applied


def test_contribution_counts_toward_buying_power() -> None:
    """$60 start; a $50 contribution lands before a $100 purchase: the
    buy is affordable on day 3 (account cash 10 + inventory 100)."""
    cal = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    run = _run(
        "60", cal,
        executions=[TradeExecution(date(2024, 1, 4), "SPY", 1, D("100.00"))],
        marks=[MarkObservation(date(2024, 1, 4), "SPY", D("100.00"))],
        cashflows=[CashflowEvent(date(2024, 1, 3), D("50"))],
    )
    assert run.refusal_reason is None
    assert run.rows[-1].cash == D("10.00")
    assert run.rows[-1].nav == D("110.00")


def test_withdrawal_below_zero_refuses() -> None:
    cal = (date(2024, 1, 2), date(2024, 1, 3))
    run = _run(
        "100", cal,
        cashflows=[CashflowEvent(date(2024, 1, 3), D("-500"))],
    )
    assert run.refusal_reason == "research.funded.insufficient_cash_for_flow"


def test_sell_more_than_held_refuses() -> None:
    cal = (date(2024, 1, 2), date(2024, 1, 3))
    run = _run(
        "10000", cal,
        executions=[TradeExecution(date(2024, 1, 3), "SPY", -5, D("5.00"))],
        marks=[MarkObservation(date(2024, 1, 3), "SPY", D("5.00"))],
    )
    assert run.refusal_reason == "research.funded.position_underflow"
    assert run.rows == ()


def test_execution_off_calendar_refuses() -> None:
    """The calendar is declared, not derived from executions: a fill on
    a non-session date is a refused input, never an invented session."""
    cal = (date(2024, 1, 2),)
    run = _run(
        "10000", cal,
        executions=[TradeExecution(date(2024, 1, 7), "SPY", 1, D("5.00"))],  # Sunday
        marks=[MarkObservation(date(2024, 1, 7), "SPY", D("5.00"))],
    )
    assert run.refusal_reason == "research.funded.execution_off_calendar"


# -- 3. marks, gaps, no-trade sessions ----------------------------------------


def test_missing_mark_is_a_gap_not_a_zero() -> None:
    """Inventory held before ANY mark is observed (the execution price
    is a trade, not a valuation): NAV is None with the missing symbols
    named — never a zero return and never cash-as-NAV. Once a mark
    arrives the series resumes; a later session with no NEW mark
    carries the last observed one (standard close-carry convention)."""
    cal = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))
    run = _run(
        "10000", cal,
        executions=[TradeExecution(date(2024, 1, 2), "SPY", 10, D("5.00"))],
        marks=[
            MarkObservation(date(2024, 1, 4), "SPY", D("7.00")),
            # nothing on Jan 5 — the Jan 4 close carries forward
        ],
    )
    for gap_idx in (0, 1):
        gap = run.rows[gap_idx]
        assert gap.nav is None
        assert gap.marked_value is None
        assert gap.missing_mark_symbols == ("SPY",)
        assert gap.investment_gain is None
    assert run.rows[2].nav == D("10020.00")  # 9,950 cash + 10 * $7
    assert run.rows[3].nav == D("10020.00")  # carried mark
    assert run.nav_by_session()[date(2024, 1, 3)] is None


def test_no_trade_sessions_carry_cash_and_inventory_forward() -> None:
    """A no-trade day is not a missing-data day: NAV repeats off the
    carried mark until a new observation arrives."""
    cal = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    run = _run(
        "10000", cal,
        executions=[TradeExecution(date(2024, 1, 2), "SPY", 4, D("25.00"))],
        marks=[
            MarkObservation(date(2024, 1, 2), "SPY", D("25.00")),
            MarkObservation(date(2024, 1, 4), "SPY", D("30.00")),
        ],
    )
    assert run.rows[0].nav == D("10000.00")
    assert run.rows[1].nav == D("10000.00")  # carried mark
    assert run.rows[2].nav == D("10020.00")  # 9,900 + 4*30


def test_overlapping_positions_marked_independently() -> None:
    """A and B held together: NAV = cash + qA*markA + qB*markB; selling
    A leaves B marked alone."""
    cal = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    run = _run(
        "10000", cal,
        executions=[
            TradeExecution(date(2024, 1, 2), "A", 10, D("10.00")),
            TradeExecution(date(2024, 1, 3), "B", 5, D("20.00")),
            TradeExecution(date(2024, 1, 4), "A", -10, D("11.00")),
        ],
        marks=[
            MarkObservation(date(2024, 1, 2), "A", D("10.00")),
            MarkObservation(date(2024, 1, 3), "B", D("20.00")),
            MarkObservation(date(2024, 1, 4), "A", D("11.00")),
        ],
    )
    assert run.rows[0].nav == D("10000.00")  # 9,900 + 10*10
    assert run.rows[1].nav == D("10000.00")  # 9,800 + 10*10 + 5*20
    final = run.rows[2]
    # 9,800 + 110 sale proceeds = 9,910 cash + 5*20 inventory
    assert final.cash == D("9910.00")
    assert final.inventory == (("B", 5),)
    assert final.nav == D("10010.00")
    assert final.investment_gain == D("10.00")
    assert run.ledger.realized_pnl("A") == D("10.00")


# -- 4. fees from the declared model ------------------------------------------


def test_fee_model_fees_applied_when_declared() -> None:
    """A declared fee model prices executions that carry no explicit
    fee: 1 * $200 at 5 bps ⇒ $0.10 exactly. With NO model declared the
    same execution pays nothing — fee policy comes from the resolved
    plan, never invented by the engine."""
    cal = (date(2024, 1, 2),)
    executions = [TradeExecution(date(2024, 1, 2), "SPY", 1, D("200.00"))]
    marks = [MarkObservation(date(2024, 1, 2), "SPY", D("200.00"))]
    run = _run("10000", cal, executions=executions, marks=marks,
               fee_model=FiveBasisPointFeeModel())
    assert run.refusal_reason is None
    assert run.rows[0].fees_cum == D("0.10")
    assert run.rows[0].cash == D("9799.90")
    assert run.rows[0].nav == D("9999.90")

    unpriced = _run("10000", cal, executions=executions, marks=marks)
    assert unpriced.rows[0].fees_cum == D("0.00")
    assert unpriced.rows[0].nav == D("10000.00")


# -- 5. the ledger's independent oracle ---------------------------------------


def test_returned_ledger_passes_assert_conservation() -> None:
    """The book the run returns must satisfy its own independent replay
    (cash/fees/positions/realized/entries from primitive fill fields).
    The old engine mutated ``book.cash`` directly and never invoked the
    check — invoking it failed with CASH_MISMATCH."""
    cal = (date(2024, 1, 2), date(2024, 1, 15), date(2024, 6, 1))
    run = _run(
        "10000", cal,
        executions=[
            TradeExecution(date(2024, 1, 2), "SPY", 10, D("5.00"), fees=D("1.00")),
            TradeExecution(date(2024, 6, 1), "SPY", -10, D("6.00"), fees=D("1.00")),
        ],
        marks=[
            MarkObservation(date(2024, 1, 2), "SPY", D("5.00")),
            MarkObservation(date(2024, 6, 1), "SPY", D("6.00")),
        ],
        cashflows=[CashflowEvent(date(2024, 1, 15), D("500"))],
    )
    assert run.refusal_reason is None
    run.ledger.assert_conservation()  # must not raise
    # flat-book identity: trading cash == initial + Σ realized_gross - fees
    # (contributions live outside the trading book)
    assert run.ledger.cash == D("10008.00")
    assert run.ledger.total_fees == D("2.00")
    # account-level identity: NAV = opening + flows + (realized - fees)
    assert run.final_nav == D("10000.00") + D("500.00") + D("10.00") - D("2.00")


def test_flow_between_sessions_applies_on_the_next_session() -> None:
    """A flow dated on a non-session day (Friday holiday gap) applies on
    the next declared session, not never."""
    cal = (date(2024, 1, 2), date(2024, 1, 8))
    run = _run(
        "10000", cal,
        cashflows=[CashflowEvent(date(2024, 1, 5), D("500"))],
    )
    assert run.rows[0].contributions_cum == D("0.00")
    assert run.rows[1].contributions_cum == D("500.00")
    assert run.final_nav == D("10500.00")


def test_empty_calendar_produces_no_rows_without_error() -> None:
    run = _run("10000", ())
    assert run.rows == ()
    assert run.final_nav is None
    assert run.refusal_reason is None


def test_rows_are_strictly_dated_and_complete() -> None:
    """Every declared session emits exactly one row, in order (an active
    run — here a single flow — covers sessions with nothing else on
    them; a run with NO observations at all correctly emits none)."""
    cal = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))
    run = _run("10000", cal, cashflows=[CashflowEvent(date(2024, 1, 2), D("1"))])
    assert [r.date for r in run.rows] == list(cal)
    empty = _run("10000", cal)
    assert empty.rows == ()


@pytest.mark.parametrize("start,qty,price", [
    ("100", 2, "100.00"),   # exactly $200 needed vs $100 held
    ("149.99", 1, "150.00"),
    ("10000", 3000, "5.00"),  # $15,000 vs $10,000
])
def test_various_infeasible_buys_refuse(start: str, qty: int, price: str) -> None:
    cal = (date(2024, 1, 2),)
    run = _run(
        start, cal,
        executions=[TradeExecution(date(2024, 1, 2), "SPY", qty, D(price))],
        marks=[MarkObservation(date(2024, 1, 2), "SPY", D(price))],
    )
    assert run.refusal_reason == "research.funded.insufficient_cash"
    assert all(r.cash >= 0 for r in run.rows)  # never any implicit loan


def test_timestamps_are_utc_and_fill_ids_unique() -> None:
    """Fill construction detail: the book demands non-decreasing UTC
    execution timestamps and unique fill ids — the engine's generated
    fills must satisfy both across same-session executions."""
    cal = (date(2024, 1, 2), date(2024, 1, 3))
    run = _run(
        "100000", cal,
        executions=[
            TradeExecution(date(2024, 1, 2), "SPY", 1, D("100.00")),
            TradeExecution(date(2024, 1, 2), "SPY", 1, D("100.00")),
            TradeExecution(date(2024, 1, 3), "SPY", -2, D("100.00")),
        ],
        marks=[MarkObservation(date(2024, 1, 3), "SPY", D("100.00"))],
    )
    assert run.refusal_reason is None
    fills = [e for e in run.ledger.entries]
    assert len(fills) == 6  # two entries per fill
    ts = datetime(2024, 1, 2, tzinfo=UTC)
    assert all(e.ts >= ts for e in fills)
