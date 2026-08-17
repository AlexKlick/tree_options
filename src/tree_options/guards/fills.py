"""FillEngine (INV-09/10/11): the only component allowed to mint a Fill.

Ordered fail-closed checks — the first failure wins, every failure carries a
distinct code, nothing is silently skipped:

  1. SESSION_NOT_IN_CALENDAR   execution_session must be a calendar session
  2. CONTRACT_MISMATCH         order / quote / contract must name one contract
  3. CONTRACT_NOT_LISTED       contract.exists_on(execution_session) (INV-09)
  4. CONTRACT_EXPIRED          trading after expiration
  5. SAME_SESSION_EXECUTION    D5 both levels: execution_at > decision_at AND
                               ordinal(execution) > ordinal(decision)
  6. EXECUTION_INSTANT_MISMATCH the instant must lie inside the labeled session
  7. quote reality             as_tradable(): crossed / zero-size / stale /
                               non-tradable condition (graded error classes)
  8. INVALID_IMPROVEMENT_FRACTION
  9. side rule                 buy at the ask, sell at the bid; improvement
                               moves toward the midpoint, never past it
 10. UNMARKETABLE_LIMIT        a limit the touch cannot satisfy
 11. partial fill              quantity capped at the opposite side's size
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from tree_options.ledger.fees import FeeModel, PerContractFeeModel
from tree_options.schemas.common import PRICE_TICK
from tree_options.schemas.market import QuoteEvent, as_tradable
from tree_options.schemas.options import OptionContract
from tree_options.schemas.trading import Fill, Order
from tree_options.time.calendar import SessionCalendar

ALLOWED_IMPROVEMENT_FRACTIONS = (Decimal("0"), Decimal("0.25"), Decimal("0.50"))


class FillRejection(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class PriceOutsideBounds(FillRejection):
    def __init__(self, detail: str) -> None:
        super().__init__("PRICE_OUTSIDE_BOUNDS", detail)


def _improved_price(tq, side: str, fraction: Decimal) -> Decimal:
    """Touch price moved toward the midpoint by fraction/2 of the spread.

    Conservative tick rounding: buy prices round UP, sell prices round DOWN,
    so quantization can never improve the price past the midpoint.
    """
    bid, ask = tq.bid, tq.ask
    if side == "buy":
        exact = ask - (fraction / 2) * (ask - bid)
        price = exact.quantize(PRICE_TICK, rounding=ROUND_CEILING)
        mid = (bid + ask) / 2
        if price > ask or price < mid:
            raise PriceOutsideBounds(
                f"buy price {price} outside [{mid}, {ask}] (bid={bid} ask={ask})"
            )
    else:
        exact = bid + (fraction / 2) * (ask - bid)
        price = exact.quantize(PRICE_TICK, rounding=ROUND_FLOOR)
        mid = (bid + ask) / 2
        if price < bid or price > mid:
            raise PriceOutsideBounds(
                f"sell price {price} outside [{bid}, {mid}] (bid={bid} ask={ask})"
            )
    return price


class FillEngine:
    def __init__(
        self,
        calendar: SessionCalendar,
        *,
        fee_model: FeeModel | None = None,
        max_quote_age_seconds: int = 900,
    ) -> None:
        self.calendar = calendar
        self.fee_model: FeeModel = fee_model or PerContractFeeModel()
        self.max_quote_age_seconds = max_quote_age_seconds

    def execute(
        self,
        order: Order,
        quote: QuoteEvent,
        contract: OptionContract,
        *,
        execution_session: date,
        execution_at: datetime,
        improvement_fraction: Decimal = Decimal("0"),
    ) -> Fill:
        if not self.calendar.is_session(execution_session):
            raise FillRejection(
                "SESSION_NOT_IN_CALENDAR", f"{execution_session} is not a session"
            )
        if quote.contract_id != order.contract_id or contract.contract_id != order.contract_id:
            raise FillRejection(
                "CONTRACT_MISMATCH",
                f"order {order.contract_id} / quote {quote.contract_id} / "
                f"contract {contract.contract_id} disagree",
            )
        if not contract.exists_on(execution_session):
            raise FillRejection(
                "CONTRACT_NOT_LISTED",
                f"{contract.contract_id} not listed on {execution_session} "
                f"(listing {contract.listing_start}..{contract.listing_end})",
            )
        if contract.expired_on(execution_session):
            raise FillRejection(
                "CONTRACT_EXPIRED",
                f"{contract.contract_id} expired {contract.expiration} "
                f"before {execution_session}",
            )

        # D5 same-close rule, both levels: the instant must strictly follow the
        # decision instant AND the session ordinal must strictly follow the
        # decision session. 16:00:01 on the decision session passes the first
        # and dies here on the second; the next session's open passes the
        # second only.
        exec_ord = self.calendar.ordinal(execution_session)
        decision_ord = self.calendar.ordinal(order.decision_session)
        if not (execution_at > order.decision_at and exec_ord > decision_ord):
            raise FillRejection(
                "SAME_SESSION_EXECUTION",
                f"execution {execution_at}/{execution_session} not strictly after "
                f"decision {order.decision_at}/{order.decision_session}",
            )
        if not self.calendar.contains_instant(execution_session, execution_at):
            raise FillRejection(
                "EXECUTION_INSTANT_MISMATCH",
                f"{execution_at} does not lie inside session {execution_session}",
            )

        # Quote reality: graded failures from as_tradable (crossed, zero-size,
        # stale, non-tradable condition).
        tq = as_tradable(
            quote, execution_at=execution_at, max_quote_age_seconds=self.max_quote_age_seconds
        )

        if improvement_fraction not in ALLOWED_IMPROVEMENT_FRACTIONS:
            raise FillRejection(
                "INVALID_IMPROVEMENT_FRACTION",
                f"{improvement_fraction} not in {ALLOWED_IMPROVEMENT_FRACTIONS}",
            )

        price = _improved_price(tq, order.side, improvement_fraction)

        if order.order_type == "limit" and order.limit_price is not None:
            if order.side == "buy" and order.limit_price < price:
                raise FillRejection(
                    "UNMARKETABLE_LIMIT", f"buy limit {order.limit_price} < fill {price}"
                )
            if order.side == "sell" and order.limit_price > price:
                raise FillRejection(
                    "UNMARKETABLE_LIMIT", f"sell limit {order.limit_price} > fill {price}"
                )

        side_size = tq.ask_size if order.side == "buy" else tq.bid_size
        quantity = min(order.quantity, side_size)
        if quantity < 1:
            raise FillRejection("NO_LIQUIDITY", "opposite side size is zero")

        fees = self.fee_model.order_fees(quantity)

        return Fill(
            fill_id=f"{order.order_id}-F1",
            order_id=order.order_id,
            contract_id=order.contract_id,
            side=order.side,
            quantity=quantity,
            price=price,
            fees=fees,
            execution_at=execution_at,
            execution_session=execution_session,
            price_improvement_fraction=improvement_fraction,
        )
