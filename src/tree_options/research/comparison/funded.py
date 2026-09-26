"""Capital-aware funded-account replay for the comparison engine.

Handoff §4 (Comparability contract): "Account value is cash plus the
signed value of assets and liabilities under the declared valuation
convention. Reserved collateral is a constraint on deployable cash, not
an additional asset … Changing contribution or capital inputs may
change integer position sizing, cash availability, trade admission and
costs."

Handoff §7 (Scientific study mode): "Trade ROI, return on premium,
return on maximum loss, notional return, and whole-account return are
different quantities. Never compound a mean per-trade return into a
wallet curve without an explicit funded execution schedule and
accounting model."

Corrected per the 2026-09-25 external audit (RL1-01). The identity this
module enforces on every declared session:

    account_cash = trading_cash + contributions - withdrawals
    NAV          = account_cash + Σ held_quantity * last_observed_mark
    investment_gain = NAV - starting_capital - contributions + withdrawals

Realized P&L is DERIVED from FIFO lots via the desk's ``LedgerBook``
(``apply``/``assert_conservation``) — never supplied as an input to be
added on top of sale proceeds. Fees are applied exactly once, through
the declared fee model unless an execution carries an explicit
pass-through fee. The book's independent replay oracle
(``assert_conservation``) validates the trading stream; external
cashflows live outside the trading book and are reconciled at the
account level, so the book's conservation check stays pure.

No implicit borrowing: a buy the account cannot cover, a withdrawal
beyond account cash, a sell beyond holdings, or an execution off the
declared calendar REFUSES the run with a machine-readable reason — a
refused run emits no rows and applies no fills, never a financed or
partially invented curve.

Missing marks are gaps: a session holding inventory without any
observed mark carries ``nav=None`` with the missing symbols named —
never a zero return and never a cash balance relabeled as wealth.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError

from tree_options.backtest.equity import FiveBasisPointFeeModel
from tree_options.ledger.book import LedgerBook, LedgerViolation
from tree_options.schemas.trading import Fill

#: Refusal reason codes (wire-visible; the SPA renders these as the
#: honest blocker instead of an invented series).
REFUSAL_INSUFFICIENT_CASH = "research.funded.insufficient_cash"
REFUSAL_INSUFFICIENT_CASH_FOR_FLOW = "research.funded.insufficient_cash_for_flow"
REFUSAL_POSITION_UNDERFLOW = "research.funded.position_underflow"
REFUSAL_OFF_CALENDAR = "research.funded.execution_off_calendar"
REFUSAL_INVALID_EXECUTION = "research.funded.invalid_execution"


@dataclass(frozen=True)
class CashflowEvent:
    """A capital injection or withdrawal on a value date.

    Positive amount = contribution, negative = withdrawal. The date may
    fall between sessions; the flow applies on the next declared
    session (a flow before the first session applies on the first
    session, one after the last session on the last session).
    """
    date: date
    amount: Decimal  # positive for contribution, negative for withdrawal


@dataclass(frozen=True)
class TradeExecution:
    """One trade execution as the engine sees it.

    There is NO realized-P&L input: realized profit is derived from the
    FIFO lot walk in ``LedgerBook``. Adding a supplied realized figure
    to cash proceeds was exactly the double-count the audit removed.

    ``fees=None`` means "price it with the declared fee model"; an
    explicit fee is a pass-through observation and is applied as-is.
    """
    date: date
    symbol: str
    signed_quantity: int   # positive for buy, negative for sell
    price: Decimal
    fees: Decimal | None = None


@dataclass(frozen=True)
class MarkObservation:
    """A price observation for a symbol, carried forward until superseded."""
    date: date
    symbol: str
    price: Decimal


@dataclass(frozen=True)
class FundedRow:
    """One dated row of the funded-account curve, on a declared session."""
    date: date
    cash: Decimal                        # account cash (trading + flows)
    inventory: tuple[tuple[str, int], ...]  # symbols with nonzero holdings
    marked_value: Decimal | None         # None when a held symbol is unmarked
    nav: Decimal | None                  # cash + marked inventory; None = gap
    contributions_cum: Decimal
    withdrawals_cum: Decimal
    fees_cum: Decimal
    realized_pnl_cum: Decimal            # gross, from FIFO lots (informational)
    investment_gain: Decimal | None      # NAV - opening - contribs + w/d
    missing_mark_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class FundedRun:
    """All rows for one candidate over the declared calendar.

    A refused run (``refusal_reason`` set) has no rows: a misleading
    partial curve is worse than an honest blocker.
    """
    candidate_id: str
    rows: tuple[FundedRow, ...] = field(default_factory=tuple)
    ledger: LedgerBook = field(default_factory=lambda: LedgerBook(initial_cash=Decimal("0")))
    final_nav: Decimal | None = None
    total_fees: Decimal = Decimal("0")
    refusal_reason: str | None = None
    refusal_detail: str | None = None

    def nav_by_session(self) -> dict[date, Decimal | None]:
        """NAV per session; ``None`` marks a missing-mark gap."""
        return {r.date: r.nav for r in self.rows}

    def ending_value_by_session(self) -> dict[date, Decimal]:
        """Observed (non-gap) NAVs only — the plottable series."""
        return {r.date: r.nav for r in self.rows if r.nav is not None}


def _utc_execution_at(day: date, seq: int) -> datetime:
    """Non-decreasing UTC timestamps for same-session executions: one
    sequence number of separation keeps the book's ordering guard
    satisfied without inventing intraday times that mean anything. The
    offset is whole seconds past a 21:00 base (may roll into the next
    day for pathological same-session fill counts) — ordering, not
    wall time, is the contract."""
    from tree_options.time.sessions import shift_instant
    base = datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC)
    return shift_instant(base, seq)


def _refusal(run_kwargs: dict, code: str, detail: str) -> FundedRun:
    return FundedRun(
        candidate_id=run_kwargs["candidate_id"],
        ledger=run_kwargs["book"],
        refusal_reason=code,
        refusal_detail=detail,
        total_fees=run_kwargs["book"].total_fees,
    )


def run_funded_account(
    candidate_id: str,
    starting_capital: Decimal,
    *,
    calendar: Sequence[date],
    executions: Sequence[TradeExecution],
    marks: Sequence[MarkObservation],
    cashflows: Sequence[CashflowEvent],
    fee_model: FiveBasisPointFeeModel | None = None,
) -> FundedRun:
    """Replay ``executions``/``cashflows`` over the DECLARED ``calendar``
    session list, marking inventory from ``marks``.

    Conservation is enforced by construction and re-proven at the end by
    ``LedgerBook.assert_conservation()`` — the desk's independent
    primitive-field replay oracle. The returned ``FundedRun.ledger`` is
    the actual audited book, available for operator inspection.
    """
    book = LedgerBook(initial_cash=Decimal(starting_capital))
    ctx = {"candidate_id": candidate_id, "book": book}
    # Fee POLICY belongs to the resolved comparison plan (the spec's
    # cost model), not to the engine: with no model supplied, an
    # execution without an explicit fee pays none — the engine never
    # invents costs the caller did not declare.
    fee_policy = fee_model

    sessions = tuple(sorted(set(calendar)))
    session_set = set(sessions)
    if not sessions:
        return FundedRun(candidate_id=candidate_id, ledger=book)

    # No executions, no marks, no flows ⇒ no observations. Emitting a
    # flat cash curve for every declared session would fabricate a
    # "series" the evidence never expressed (RL §11 missingness: an
    # empty adapter is "no evidence", not "evidence of a flat account").
    if not executions and not marks and not cashflows:
        return FundedRun(candidate_id=candidate_id, ledger=book)

    # An execution on a date the declared calendar does not contain is a
    # refused input — never an invented session.
    for ex in executions:
        if ex.date not in session_set:
            return _refusal(ctx, REFUSAL_OFF_CALENDAR,
                            f"execution {ex.symbol} on {ex.date.isoformat()}")

    # Group events by the session they apply on. Flows and marks dated
    # between sessions clamp forward to the next declared session.
    def _clamp_to_session(d: date) -> date:
        if d <= sessions[0]:
            return sessions[0]
        if d >= sessions[-1]:
            return sessions[-1]
        return next(s for s in sessions if s >= d)

    flows_by_session: dict[date, list[Decimal]] = {}
    for cf in sorted(cashflows, key=lambda c: c.date):
        flows_by_session.setdefault(_clamp_to_session(cf.date), []).append(
            Decimal(cf.amount))

    marks_by_session: dict[date, list[MarkObservation]] = {}
    for m in sorted(marks, key=lambda m: (m.date, m.symbol)):
        marks_by_session.setdefault(_clamp_to_session(m.date), []).append(m)

    executions_by_session: dict[date, list[TradeExecution]] = {}
    for ex in executions:  # preserve given intraday order
        executions_by_session.setdefault(ex.date, []).append(ex)

    contributions_cum = Decimal("0")
    withdrawals_cum = Decimal("0")
    realized_symbols: set[str] = set()
    touched_symbols: set[str] = set()
    last_mark: dict[str, Decimal] = {}
    rows: list[FundedRow] = []
    fill_seq = 0

    for s in sessions:
        # 1) flows
        for amount in flows_by_session.get(s, ()):
            if amount >= 0:
                contributions_cum += amount
            else:
                withdrawals_cum += -amount
            prospective_cash = book.cash + contributions_cum - withdrawals_cum
            if prospective_cash < 0:
                return _refusal(
                    ctx, REFUSAL_INSUFFICIENT_CASH_FOR_FLOW,
                    f"flow {amount} on {s.isoformat()} would overdraw "
                    f"account cash to {prospective_cash}",
                )

        # 2) executions (in given order)
        for ex in executions_by_session.get(s, ()):
            qty = abs(ex.signed_quantity)
            if qty == 0:
                continue
            touched_symbols.add(ex.symbol)
            notional = (Decimal(ex.price) * qty).quantize(Decimal("0.01"))
            if ex.fees is not None:
                fees = Decimal(ex.fees)
            elif fee_policy is not None:
                fees = fee_policy.order_fees(price=Decimal(ex.price), quantity=qty)
            else:
                fees = Decimal("0")
            side: Literal["buy", "sell"]
            if ex.signed_quantity > 0:
                affordable = book.cash + contributions_cum - withdrawals_cum
                if affordable < notional + fees:
                    return _refusal(
                        ctx, REFUSAL_INSUFFICIENT_CASH,
                        f"buy {qty} {ex.symbol} @ {ex.price} + {fees} fee needs "
                        f"{notional + fees}, account cash {affordable} "
                        f"on {s.isoformat()}",
                    )
                side = "buy"
            else:
                side = "sell"
            fill_seq += 1
            fill = Fill(
                fill_id=f"{candidate_id}-F{fill_seq:06d}",
                order_id=f"{candidate_id}-O{fill_seq:06d}",
                contract_id=ex.symbol,
                side=side,
                quantity=qty,
                price=Decimal(ex.price),
                multiplier=1,
                deliverable_shares_per_contract=Decimal("1"),
                fees=fees,
                execution_at=_utc_execution_at(s, fill_seq % 60),
                execution_session=s,
            )
            try:
                book.apply(fill)
            except LedgerViolation as exc:
                return _refusal(ctx, REFUSAL_POSITION_UNDERFLOW, str(exc))
            except ValidationError as exc:
                return _refusal(ctx, REFUSAL_INVALID_EXECUTION, str(exc))
            if side == "sell":
                realized_symbols.add(ex.symbol)

        # 3) marks (supersede carried observations)
        for m in marks_by_session.get(s, ()):
            last_mark[m.symbol] = Decimal(m.price)

        # 4) value the account (public book surface only)
        held = tuple(
            (sym, q) for sym, q in (
                (sym, book.quantity(sym)) for sym in sorted(touched_symbols)
            ) if q != 0
        )
        missing = tuple(sym for sym, _q in held if sym not in last_mark)
        if any(sym not in last_mark for sym, q in held):
            marked_value: Decimal | None = None
            nav: Decimal | None = None
            gain: Decimal | None = None
        else:
            marked_value = sum(
                (Decimal(last_mark[sym]) * q for sym, q in held), Decimal("0")
            ).quantize(Decimal("0.01"))
            nav = (book.cash + contributions_cum - withdrawals_cum
                   + marked_value).quantize(Decimal("0.01"))
            gain = (nav - Decimal(starting_capital)
                    - contributions_cum + withdrawals_cum).quantize(Decimal("0.01"))

        rows.append(FundedRow(
            date=s,
            cash=(book.cash + contributions_cum - withdrawals_cum).quantize(Decimal("0.01")),
            inventory=held,
            marked_value=marked_value,
            nav=nav,
            contributions_cum=contributions_cum.quantize(Decimal("0.01")),
            withdrawals_cum=withdrawals_cum.quantize(Decimal("0.01")),
            fees_cum=book.total_fees.quantize(Decimal("0.01")),
            realized_pnl_cum=sum(
                (book.realized_pnl(sym) for sym in sorted(realized_symbols)),
                Decimal("0"),
            ).quantize(Decimal("0.01")),
            investment_gain=gain,
            missing_mark_symbols=missing,
        ))

    # The independent oracle gets the final word on the trading stream.
    book.assert_conservation()

    return FundedRun(
        candidate_id=candidate_id,
        rows=tuple(rows),
        ledger=book,
        final_nav=rows[-1].nav if rows else None,
        total_fees=book.total_fees,
        refusal_reason=None,
    )


__all__ = [
    "CashflowEvent",
    "FundedRow",
    "FundedRun",
    "MarkObservation",
    "TradeExecution",
    "run_funded_account",
]
