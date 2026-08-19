"""Minimal long-only equity backtest for M2 machinery validation.

Signals are decision-close facts.  Ordinary executions therefore occur only
at the next session's open.  The top score quintile is held equal-weight with
whole shares, every ordinary side pays the campaign-fixed five basis points,
and all fills flow through the existing exact-Decimal FIFO ``LedgerBook``.

The synthetic vendor emits raw (not split-adjusted) prices.  Ratio actions and
cash dividends are consequently represented as zero-fee conversion fills:
the old lot is sold at its economically equivalent post-action value and the
new whole-share lot is bought at the raw open.  Fractional value remains as
cash in lieu.  This preserves the unchanged ledger and its independent replay
oracle without inventing a free ordinary trade.

Outputs are stamped ``synthetic/v1`` in scope.  They validate machinery only;
they are not real-data or performance claims.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal

from tree_options.data.actions import CorporateActionRecord
from tree_options.data.bars import BarRecord
from tree_options.evaluation.stats import BacktestSummary, backtest_summary
from tree_options.ledger.book import LedgerBook
from tree_options.schemas.common import FEE_TICK, PRICE_TICK
from tree_options.schemas.security import SecurityMasterRecord
from tree_options.schemas.trading import Fill
from tree_options.time.calendar import SessionCalendar

FIVE_BASIS_POINTS = Decimal("0.0005")
DATASET_PROVENANCE = "synthetic/v1"
_RATIO_KINDS = frozenset({"split", "reverse_split", "stock_dividend"})


class EquityBacktestError(RuntimeError):
    """Fail-closed malformed-input or execution error."""


@dataclass(frozen=True)
class BacktestSignal:
    decision_session: date
    security_id: str
    score: float
    label: float | None = None


@dataclass(frozen=True)
class EquityBacktestResult:
    dataset_provenance: str
    summary: BacktestSummary
    sessions: tuple[date, ...]
    turnovers: tuple[float, ...]
    label_hits: tuple[bool, ...]
    fills: tuple[Fill, ...]
    positions: tuple[tuple[str, int], ...]
    terminal_cash: Decimal
    terminal_market_value: Decimal
    terminal_equity: Decimal


class FiveBasisPointFeeModel:
    """The M2 campaign's fixed 5 bps of filled equity notional per side."""

    rate = FIVE_BASIS_POINTS

    def order_fees(self, *, price: Decimal, quantity: int) -> Decimal:
        if price <= 0:
            raise ValueError("price must be positive")
        if quantity < 1:
            raise ValueError("quantity must be >= 1")
        return (price * quantity * self.rate).quantize(FEE_TICK, rounding=ROUND_HALF_UP)

    def affordable_quantity(self, *, budget: Decimal, price: Decimal) -> int:
        if budget <= 0:
            return 0
        estimate = int(
            (budget / (price * (Decimal(1) + self.rate))).to_integral_value(rounding=ROUND_FLOOR)
        )
        while (
            estimate > 0
            and price * estimate + self.order_fees(price=price, quantity=estimate) > budget
        ):
            estimate -= 1
        return estimate


class EquityFillEngine:
    """Mint equity ``Fill`` objects under next-open and fixed-fee rules."""

    def __init__(
        self,
        calendar: SessionCalendar,
        *,
        fee_model: FiveBasisPointFeeModel | None = None,
    ) -> None:
        self.calendar = calendar
        self.fee_model = fee_model or FiveBasisPointFeeModel()
        self._sequence = 0
        self._fills: list[Fill] = []

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    def next_open(
        self,
        *,
        security_id: str,
        side: Literal["buy", "sell"],
        quantity: int,
        decision_session: date,
        bar: BarRecord,
    ) -> Fill:
        expected = self.calendar.nth_after(decision_session, 1)
        if bar.session != expected:
            raise EquityBacktestError(
                f"NOT_NEXT_SESSION_OPEN: decision {decision_session} must execute "
                f"on {expected}, got {bar.session}"
            )
        return self._mint(
            security_id=security_id,
            side=side,
            quantity=quantity,
            price=bar.open,
            execution_session=bar.session,
            execution_at=self.calendar.session_open(bar.session),
            charge_fee=True,
            purpose="rebalance",
        )

    def forced_close(self, *, security_id: str, quantity: int, bar: BarRecord) -> Fill:
        return self._mint(
            security_id=security_id,
            side="sell",
            quantity=quantity,
            price=bar.close,
            execution_session=bar.session,
            execution_at=self.calendar.session_close(bar.session),
            charge_fee=True,
            purpose="delisting",
        )

    def conversion(
        self,
        *,
        security_id: str,
        quantity: int,
        bar: BarRecord,
        ratio_numerator: int,
        ratio_denominator: int,
    ) -> tuple[Fill, ...]:
        """Convert one raw-price lot at an effective-session open.

        Selling the old units at ``open * ratio`` and buying the resulting
        whole shares at ``open`` is cash-neutral except for genuine fractional
        cash in lieu and cent rounding.  Neither leg is an ordinary trade, so
        neither pays fees or contributes turnover.
        """
        if ratio_numerator < 1 or ratio_denominator < 1:
            raise EquityBacktestError("corporate-action ratio must be positive")
        ratio = Decimal(ratio_numerator) / Decimal(ratio_denominator)
        equivalent_price = (bar.open * ratio).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
        old = self._mint(
            security_id=security_id,
            side="sell",
            quantity=quantity,
            price=equivalent_price,
            execution_session=bar.session,
            execution_at=self.calendar.session_open(bar.session),
            charge_fee=False,
            purpose="action-out",
        )
        new_quantity = int((Decimal(quantity) * ratio).to_integral_value(rounding=ROUND_FLOOR))
        if new_quantity == 0:
            return (old,)
        new = self._mint(
            security_id=security_id,
            side="buy",
            quantity=new_quantity,
            price=bar.open,
            execution_session=bar.session,
            execution_at=self.calendar.session_open(bar.session),
            charge_fee=False,
            purpose="action-in",
        )
        return old, new

    def cash_dividend(
        self,
        *,
        security_id: str,
        quantity: int,
        bar: BarRecord,
        cash_amount: Decimal,
    ) -> tuple[Fill, Fill]:
        if cash_amount < 0:
            raise EquityBacktestError("cash dividend cannot be negative")
        old = self._mint(
            security_id=security_id,
            side="sell",
            quantity=quantity,
            price=(bar.open + cash_amount).quantize(PRICE_TICK, rounding=ROUND_HALF_UP),
            execution_session=bar.session,
            execution_at=self.calendar.session_open(bar.session),
            charge_fee=False,
            purpose="dividend-out",
        )
        new = self._mint(
            security_id=security_id,
            side="buy",
            quantity=quantity,
            price=bar.open,
            execution_session=bar.session,
            execution_at=self.calendar.session_open(bar.session),
            charge_fee=False,
            purpose="dividend-in",
        )
        return old, new

    def _mint(
        self,
        *,
        security_id: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: Decimal,
        execution_session: date,
        execution_at: datetime,
        charge_fee: bool,
        purpose: str,
    ) -> Fill:
        if side not in {"buy", "sell"}:
            raise EquityBacktestError(f"invalid side {side!r}")
        if quantity < 1:
            raise EquityBacktestError("fill quantity must be >= 1")
        if price <= 0:
            raise EquityBacktestError("fill price must be positive")
        self._sequence += 1
        fees = (
            self.fee_model.order_fees(price=price, quantity=quantity)
            if charge_fee
            else Decimal("0.00")
        )
        fill = Fill(
            fill_id=f"EQF-{self._sequence:08d}",
            order_id=f"EQO-{purpose}-{self._sequence:08d}",
            contract_id=security_id,
            side=side,
            quantity=quantity,
            price=price,
            multiplier=1,
            deliverable_shares_per_contract=Decimal(1),
            fees=fees,
            execution_at=execution_at,
            execution_session=execution_session,
            fraction_to_midpoint=Decimal(0),
        )
        self._fills.append(fill)
        return fill


def _top_quintile(rows: Sequence[BacktestSignal]) -> tuple[BacktestSignal, ...]:
    if not rows:
        return ()
    count = max(1, math.ceil(len(rows) / 5))
    return tuple(sorted(rows, key=lambda row: (-row.score, row.security_id))[:count])


def _market_value(
    ledger: LedgerBook,
    open_ids: set[str],
    bars: dict[tuple[str, date], BarRecord],
    session: date,
    *,
    at_open: bool,
) -> Decimal:
    value = Decimal(0)
    for security_id in sorted(open_ids):
        bar = bars.get((security_id, session))
        if bar is None:
            raise EquityBacktestError(f"MISSING_HELD_BAR: {security_id} has no bar on {session}")
        price = bar.open if at_open else bar.close
        value += price * ledger.quantity(security_id)
    return value.quantize(FEE_TICK, rounding=ROUND_HALF_UP)


def run_equity_backtest(
    *,
    calendar: SessionCalendar,
    bars: Iterable[BarRecord],
    master: Iterable[SecurityMasterRecord],
    actions: Iterable[CorporateActionRecord],
    signals: Iterable[BacktestSignal],
    initial_cash: Decimal,
    end_session: date | None = None,
) -> EquityBacktestResult:
    """Run the fixed M2 top-quintile strategy over supplied PIT signals."""
    bar_map: dict[tuple[str, date], BarRecord] = {}
    snapshots: set[str] = set()
    for bar in bars:
        bar_key = (bar.security_id, bar.session)
        if bar_key in bar_map:
            raise EquityBacktestError(f"duplicate bar for {bar.security_id}/{bar.session}")
        if bar.source != "synthetic-generator-v1":
            raise EquityBacktestError("M2 equity backtest accepts synthetic/v1 bars only")
        bar_map[bar_key] = bar
        snapshots.add(bar.snapshot_id)
    if len(snapshots) > 1:
        raise EquityBacktestError(f"bars span multiple snapshots: {sorted(snapshots)}")

    master_map: dict[str, SecurityMasterRecord] = {}
    for record in master:
        if record.security_id in master_map:
            raise EquityBacktestError(f"duplicate master record {record.security_id}")
        master_map[record.security_id] = record

    actions_by_session: defaultdict[date, list[CorporateActionRecord]] = defaultdict(list)
    for action in actions:
        if snapshots and action.snapshot_id not in snapshots:
            raise EquityBacktestError(
                f"action snapshot {action.snapshot_id} does not match bars {sorted(snapshots)}"
            )
        actions_by_session[action.effective_session].append(action)

    signals_by_session: defaultdict[date, list[BacktestSignal]] = defaultdict(list)
    seen_signals: set[tuple[date, str]] = set()
    for signal in signals:
        signal_key = (signal.decision_session, signal.security_id)
        if signal_key in seen_signals:
            raise EquityBacktestError(f"duplicate signal for {signal_key}")
        if not math.isfinite(signal.score) or (
            signal.label is not None and not math.isfinite(signal.label)
        ):
            raise EquityBacktestError(f"non-finite signal for {signal_key}")
        if signal.security_id not in master_map:
            raise EquityBacktestError(f"signal security {signal.security_id} missing from master")
        seen_signals.add(signal_key)
        signals_by_session[signal.decision_session].append(signal)
    if not signals_by_session:
        raise EquityBacktestError("at least one signal is required")

    schedule: dict[date, date] = {}
    for registered_decision in signals_by_session:
        execution_session = calendar.nth_after(registered_decision, 1)
        schedule[execution_session] = registered_decision
    first_session = min(schedule)
    terminal_session = end_session or max(schedule)
    if calendar.ordinal(terminal_session) < calendar.ordinal(first_session):
        raise EquityBacktestError("end_session must include the first execution session")
    sessions = tuple(
        session
        for session in calendar.sessions()
        if calendar.ordinal(first_session)
        <= calendar.ordinal(session)
        <= calendar.ordinal(terminal_session)
    )

    ledger = LedgerBook(initial_cash)
    engine = EquityFillEngine(calendar)
    open_ids: set[str] = set()
    previous_equity = ledger.initial_cash
    returns: list[float] = []
    turnovers: list[float] = []
    label_hits: list[bool] = []

    for session in sessions:
        for action in sorted(
            actions_by_session.get(session, []),
            key=lambda item: (item.security_id, item.source_record_id),
        ):
            quantity = ledger.quantity(action.security_id)
            if quantity == 0:
                continue
            action_bar = bar_map.get((action.security_id, session))
            if action_bar is None:
                raise EquityBacktestError(
                    f"MISSING_ACTION_BAR: {action.security_id}/{session}/{action.kind}"
                )
            converted: tuple[Fill, ...] = ()
            if action.kind in _RATIO_KINDS:
                assert action.ratio_numerator is not None
                assert action.ratio_denominator is not None
                converted = engine.conversion(
                    security_id=action.security_id,
                    quantity=quantity,
                    bar=action_bar,
                    ratio_numerator=action.ratio_numerator,
                    ratio_denominator=action.ratio_denominator,
                )
            elif action.kind == "cash_dividend":
                assert action.cash_amount is not None
                converted = engine.cash_dividend(
                    security_id=action.security_id,
                    quantity=quantity,
                    bar=action_bar,
                    cash_amount=action.cash_amount,
                )
            for fill in converted:
                ledger.apply(fill)
            if converted:
                if ledger.quantity(action.security_id) == 0:
                    open_ids.discard(action.security_id)
                else:
                    open_ids.add(action.security_id)

        pretrade_equity = (
            ledger.cash + _market_value(ledger, open_ids, bar_map, session, at_open=True)
        ).quantize(FEE_TICK, rounding=ROUND_HALF_UP)
        if pretrade_equity <= 0:
            raise EquityBacktestError(f"non-positive equity at {session}: {pretrade_equity}")
        traded_notional = Decimal(0)

        scheduled_decision = schedule.get(session)
        if scheduled_decision is not None:
            selected = _top_quintile(signals_by_session[scheduled_decision])
            target_ids = {row.security_id for row in selected}
            target_budget = pretrade_equity / len(selected)
            desired: dict[str, int] = {}
            for row in selected:
                execution_bar = bar_map.get((row.security_id, session))
                if execution_bar is None:
                    raise EquityBacktestError(f"MISSING_EXECUTION_BAR: {row.security_id}/{session}")
                desired[row.security_id] = engine.fee_model.affordable_quantity(
                    budget=target_budget,
                    price=execution_bar.open,
                )
                if row.label is not None:
                    label_hits.append(row.label > 0.0)

            for security_id in sorted(open_ids | target_ids):
                held = ledger.quantity(security_id)
                wanted = desired.get(security_id, 0)
                if held <= wanted:
                    continue
                held_bar = bar_map.get((security_id, session))
                if held_bar is None:
                    raise EquityBacktestError(f"MISSING_EXECUTION_BAR: {security_id}/{session}")
                fill = engine.next_open(
                    security_id=security_id,
                    side="sell",
                    quantity=held - wanted,
                    decision_session=scheduled_decision,
                    bar=held_bar,
                )
                ledger.apply(fill)
                traded_notional += fill.notional()
                if wanted == 0:
                    open_ids.discard(security_id)

            for security_id in sorted(target_ids):
                held = ledger.quantity(security_id)
                wanted = desired[security_id]
                if held >= wanted:
                    continue
                target_bar = bar_map[(security_id, session)]
                affordable = engine.fee_model.affordable_quantity(
                    budget=ledger.cash,
                    price=target_bar.open,
                )
                quantity = min(wanted - held, affordable)
                if quantity == 0:
                    continue
                fill = engine.next_open(
                    security_id=security_id,
                    side="buy",
                    quantity=quantity,
                    decision_session=scheduled_decision,
                    bar=target_bar,
                )
                ledger.apply(fill)
                traded_notional += fill.notional()
                open_ids.add(security_id)

        for security_id in sorted(tuple(open_ids)):
            record = master_map[security_id]
            if record.listing_end != session:
                continue
            delisting_bar = bar_map.get((security_id, session))
            if delisting_bar is None:
                raise EquityBacktestError(f"MISSING_DELISTING_BAR: {security_id}/{session}")
            fill = engine.forced_close(
                security_id=security_id,
                quantity=ledger.quantity(security_id),
                bar=delisting_bar,
            )
            ledger.apply(fill)
            traded_notional += fill.notional()
            open_ids.remove(security_id)

        close_value = _market_value(ledger, open_ids, bar_map, session, at_open=False)
        equity = (ledger.cash + close_value).quantize(FEE_TICK, rounding=ROUND_HALF_UP)
        returns.append(float(equity / previous_equity - Decimal(1)))
        turnovers.append(float(traded_notional / pretrade_equity))
        previous_equity = equity
        ledger.assert_conservation()

    terminal_market_value = _market_value(
        ledger, open_ids, bar_map, terminal_session, at_open=False
    )
    terminal_equity = (ledger.cash + terminal_market_value).quantize(
        FEE_TICK, rounding=ROUND_HALF_UP
    )
    return EquityBacktestResult(
        dataset_provenance=DATASET_PROVENANCE,
        summary=backtest_summary(returns, turnovers, label_hits),
        sessions=sessions,
        turnovers=tuple(turnovers),
        label_hits=tuple(label_hits),
        fills=engine.fills,
        positions=tuple(
            (security_id, ledger.quantity(security_id)) for security_id in sorted(open_ids)
        ),
        terminal_cash=ledger.cash,
        terminal_market_value=terminal_market_value,
        terminal_equity=terminal_equity,
    )
