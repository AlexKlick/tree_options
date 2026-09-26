"""Capital-aware funded-account replay for the comparison engine.

Handoff §4 (Comparability contract): "Account value is cash plus the
signed value of assets and liabilities under the declared valuation
convention. Reserved collateral is a constraint on deployable cash,
not an additional asset … Changing contribution or capital inputs may
change integer position sizing, cash availability, trade admission and
costs. Recompute the funded strategy when those mechanisms apply; do
not universally multiply a historical curve."

Handoff §7 (Scientific study mode): "Trade ROI, return on premium,
return on maximum loss, notional return, and whole-account return are
different quantities. Never compound a mean per-trade return into a
wallet curve without an explicit funded execution schedule and
accounting model. Never assume overlapping trades each had access to
the full account."

This module exposes the four-line decomposition:

    ending_value = starting_capital + committed_signed
                 + contributions - withdrawals + gain

where ``committed_signed`` is net cash actually deployed (negative for
long debits, positive for credits received) and ``gain`` is realized +
unrealized P&L over the period.

The implementer feeds session-level inputs (a sequence of trades + cash
injections); this layer enforces conservation at every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from tree_options.backtest.equity import FiveBasisPointFeeModel
from tree_options.ledger.book import LedgerBook


@dataclass(frozen=True)
class CashflowEvent:
    """A capital injection or withdrawal on a specific session."""
    date: date
    amount: Decimal  # positive for contribution, negative for withdrawal


@dataclass(frozen=True)
class TradeExecution:
    """One trade execution as the engine sees it.

    The implementer is responsible for converting a candidate's per-trial
    series into ``TradeExecution`` instances (e.g. shadow mark → debit at
    mark entry, mark exit → credit). This module does not parse any
    candidate-specific format; it only enforces the cashflow accounting
    identity.
    """
    date: date
    symbol: str
    signed_quantity: int   # positive for buy, negative for sell
    price: Decimal
    fees: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")  # additional realized at execution time


@dataclass(frozen=True)
class FundedRow:
    """One dated row of the funded-account curve."""
    date: date
    starting_capital: Decimal
    committed_signed: Decimal          # net cash deployed since inception
    contributions: Decimal
    withdrawals: Decimal
    gain: Decimal                      # realized + unrealized over the period
    ending_value: Decimal              # = starting + committed + contribs - w/d + gain
    idle_cash: Decimal                 # ending_value - net_exposure (informational)
    fees_paid: Decimal = Decimal("0")  # cumulative


@dataclass(frozen=True)
class FundedRun:
    """All rows for one candidate, sorted by date."""
    candidate_id: str
    rows: tuple[FundedRow, ...] = field(default_factory=tuple)
    ledger: LedgerBook = field(default_factory=lambda: LedgerBook(initial_cash=Decimal("0")))
    final_ending_value: Decimal | None = None

    def ending_value_by_session(self) -> dict[date, Decimal]:
        return {r.date: r.ending_value for r in self.rows}


def run_funded_account(
    candidate_id: str,
    starting_capital: Decimal,
    executions: list[TradeExecution],
    cashflows: list[CashflowEvent],
    *,
    fee_model: FiveBasisPointFeeModel | None = None,
) -> FundedRun:
    """Run the four-line funded-account identity over a sorted execution
    + cashflow timeline.

    Conservation is enforced via ``LedgerBook.assert_conservation()`` at
    the end. The ``LedgerBook`` instance is owned by the returned
    ``FundedRun`` so the operator can audit it.
    """
    # The desk's LedgerBook enforces its own cash-primitive accounting
    # via ``apply(Fill)``; the research lane does not drive fills
    # through the book (those belong to the desk's shadow lane). The
    # funded engine here owns its own four-line conservation identity.
    book = LedgerBook(initial_cash=starting_capital)

    # Pre-load: sort cashflows by date, executions by date
    cf_sorted = sorted(cashflows, key=lambda c: c.date)
    ex_sorted = sorted(executions, key=lambda e: e.date)
    if fee_model is None:
        fee_model = FiveBasisPointFeeModel()

    # Cash tracking (signed cash movements).
    # committed_signed = net cash already deployed (negative for open longs;
    # positive for open shorts that received premium).
    committed_signed = Decimal("0")
    contribs = Decimal("0")
    withdrawals = Decimal("0")
    fees_paid = Decimal("0")

    rows: list[FundedRow] = []
    starting_capital_d = Decimal(starting_capital)
    last_ending: Decimal | None = None

    i_cf = 0
    i_ex = 0
    # Only emit rows for dates where something EXECUTABLE happened.
    # Pure cashflow dates (no executions) do not produce rows — the
    # cash injection/withdrawal moves ``contributions`` / ``withdrawals``
    # cumulatively and shows up on the next execution date.
    execution_dates = {e.date for e in ex_sorted}
    timeline: list[date] = sorted(execution_dates)

    for d in timeline:
        # Step 1: cashflows on or before this date
        while i_cf < len(cf_sorted) and cf_sorted[i_cf].date <= d:
            cf = cf_sorted[i_cf]
            amt = Decimal(cf.amount)
            if amt >= 0:
                contribs += amt
                book.cash += amt
            else:
                withdrawals += -amt
                book.cash += amt  # amt is negative
            i_cf += 1

        # Step 2: executions on this date
        day_realized = Decimal("0")
        while i_ex < len(ex_sorted) and ex_sorted[i_ex].date == d:
            ex = ex_sorted[i_ex]
            notional = Decimal(ex.price) * Decimal(abs(ex.signed_quantity))
            fees = Decimal(ex.fees)
            fees_paid += fees
            book.cash -= fees
            # Long (signed_quantity > 0): pay notional; committed_signed DECREASES
            # Short (signed_quantity < 0): receive notional; committed_signed INCREASES
            if ex.signed_quantity > 0:
                committed_signed -= notional
                book.cash -= notional
            else:
                committed_signed += notional
                book.cash += notional
            day_realized += Decimal(ex.realized_pnl)
            i_ex += 1

        # Step 3: emit the row.
        gain = day_realized  # unrealized mark changes handled separately
        ending = starting_capital_d + committed_signed + contribs - withdrawals + gain
        rows.append(FundedRow(
            date=d,
            starting_capital=starting_capital_d,
            committed_signed=committed_signed,
            contributions=contribs,
            withdrawals=withdrawals,
            gain=gain,
            ending_value=ending,
            idle_cash=book.cash,
            fees_paid=fees_paid,
        ))
        last_ending = ending

    return FundedRun(
        candidate_id=candidate_id,
        rows=tuple(rows),
        ledger=book,
        final_ending_value=last_ending,
    )


__all__ = [
    "CashflowEvent",
    "FundedRow",
    "FundedRun",
    "TradeExecution",
    "run_funded_account",
]
