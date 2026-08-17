"""FillEngine (INV-09/10/11): the only component allowed to mint a Fill.

Ordered fail-closed checks — the first failure wins, every failure carries a
distinct code, nothing is silently skipped:

  1. SESSION_NOT_IN_CALENDAR      execution_session is a calendar session
  2. CONTRACT_MISMATCH            order / quote / contract name one contract
  3. CONTRACT_NOT_LISTED / CONTRACT_EXPIRED     at EXECUTION time (INV-09)
  4. CONTRACT_UNKNOWN_AT_DECISION listed at DECISION time — unknowable
                                  contracts cannot be traded retroactively
  5. DECISION_INSTANT_NOT_CLOSE   decision_at == session_close(decision_session)
  6. SAME_SESSION_EXECUTION       D5 both levels: execution_at > decision_at
                                  AND ordinal(exec) > ordinal(decision)
  7. EXECUTION_INSTANT_MISMATCH   the instant lies inside the labeled session
  8. quote reality                as_tradable(): nonpositive / crossed /
                                  locked / zero-size / stale / condition
  9. NONSTANDARD_DELIVERABLE      M0 trades standard contracts only
 10. INVALID_FRACTION_TO_MIDPOINT
 11. side rule via fraction_to_midpoint(), integer half-tick arithmetic
 12. UNMARKETABLE_LIMIT           a limit the executable cannot satisfy
 13. partial fill                 quantity = floor(fill_size_fraction * size)

Money units: price is dollars per deliverable share; cash impact is
price * quantity * multiplier (snapshotted onto the Fill).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

from tree_options.ledger.fees import FeeModel, PerContractFeeModel
from tree_options.protocol.schema import ResearchProtocol
from tree_options.schemas.common import PRICE_TICK
from tree_options.schemas.market import QuoteEvent, as_tradable
from tree_options.schemas.options import OptionContract
from tree_options.schemas.trading import Fill, Order
from tree_options.time.calendar import SessionCalendar
from tree_options.time.sessions import session_close_instant, shift_instant

ALLOWED_FRACTIONS = (Decimal("0"), Decimal("0.25"), Decimal("0.5"), Decimal("0.75"), Decimal("1"))


class FillRejection(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class PriceOutsideBounds(FillRejection):
    def __init__(self, detail: str) -> None:
        super().__init__("PRICE_OUTSIDE_BOUNDS", detail)


@dataclass(frozen=True)
class ExecutionStress:
    """Adverse execution scenario. Components only WORSEN a fill; composing
    stress can never improve price, fees, or quote recency (audit §4.1).

    slippage_ticks_buy/sell: integer ticks added to (buy) / subtracted from
        (sell) the executable price.
    extra_fee_per_contract: additional dollars per contract.
    latency_seconds: shifts the execution instant later; the quote is then
        selected at the shifted instant — never reached back for an earlier
        (better) quote, and possibly rejected as stale.
    """

    slippage_ticks_buy: int = 0
    slippage_ticks_sell: int = 0
    extra_fee_per_contract: Decimal = Decimal("0")
    latency_seconds: int = 0

    @staticmethod
    def zero() -> ExecutionStress:
        return ExecutionStress()

    def __post_init__(self) -> None:
        if isinstance(self.extra_fee_per_contract, str):
            object.__setattr__(self, "extra_fee_per_contract", Decimal(self.extra_fee_per_contract))
        if self.slippage_ticks_buy < 0 or self.slippage_ticks_sell < 0:
            raise ValueError("stress slippage must be non-negative")
        if self.extra_fee_per_contract < 0:
            raise ValueError("extra_fee_per_contract must be non-negative")
        if self.latency_seconds < 0:
            raise ValueError("latency_seconds must be non-negative")

    @property
    def buy_price_delta_ticks(self) -> int:
        return self.slippage_ticks_buy

    @property
    def sell_price_delta_ticks(self) -> int:
        return -self.slippage_ticks_sell


def _cents(x: Decimal) -> int:
    """Exact integer cents for a 2-decimal price."""
    return int(x * 100)


def fraction_to_midpoint(bid: Decimal, ask: Decimal, side: str, f: Decimal) -> Decimal:
    """Executable price moved toward the midpoint by fraction f of the
    remaining distance: 0.0 = edge, 1.0 = midpoint.

    Pure integer half-tick arithmetic — no repeated Decimal quantization:
    bid/ask are integer cents; the midpoint is an integer number of
    half-ticks; f is a rational; the result rounds CONSERVATIVELY (buy up,
    sell down) to a whole tick, so quantization can only hurt the taker.
    """
    if not (Decimal(0) <= f <= Decimal(1)):
        raise ValueError(f"fraction_to_midpoint {f} not in [0, 1]")
    b, a = _cents(bid), _cents(ask)
    if b <= 0 or a < b:
        raise ValueError(f"invalid quote {bid}/{ask}")
    # Half-tick integers: midpoint in half-ticks is b + a (exact).
    mid_half = b + a
    frac = Fraction(f)  # exact rational from the protocol's decimal strings
    if side == "buy":
        exact = 2 * a + frac * (mid_half - 2 * a)  # half-ticks
        ticks = math.ceil(exact / 2)  # conservative: round the BUY price UP
        drop = max(0, min(a - ticks, a - b))  # never below bid
        price_cents = a - drop
    elif side == "sell":
        exact = 2 * b + frac * (mid_half - 2 * b)
        ticks = math.floor(exact / 2)  # conservative: round the SELL price DOWN
        rise = max(0, min(ticks - b, a - b))  # never above ask
        price_cents = b + rise
    else:
        raise ValueError(f"side must be buy or sell, got {side!r}")
    return (Decimal(price_cents) / 100).quantize(PRICE_TICK)


class _StressedFeeModel:
    """Fee wrapper: base fees + a per-contract surcharge. Only worsens."""

    def __init__(self, base: FeeModel, extra_per_contract: Decimal) -> None:
        self._base = base
        self._extra = extra_per_contract

    def order_fees(self, quantity: int) -> Decimal:
        return (self._base.order_fees(quantity) + self._extra * quantity).quantize(Decimal("0.01"))


class FillEngine:
    def __init__(
        self,
        calendar: SessionCalendar,
        *,
        fee_model: FeeModel | None = None,
        max_quote_age_seconds: int = 900,
        reject_locked_quotes: bool = True,
        fill_size_fraction: Decimal = Decimal("1.0"),
    ) -> None:
        self.calendar = calendar
        self.fee_model: FeeModel = fee_model or PerContractFeeModel()
        self.max_quote_age_seconds = max_quote_age_seconds
        self.reject_locked_quotes = reject_locked_quotes
        self.fill_size_fraction = fill_size_fraction
        self._fill_seq = 0

    @classmethod
    def from_protocol(
        cls, calendar: SessionCalendar, protocol: ResearchProtocol, **overrides
    ) -> FillEngine:
        f = protocol.fills
        kwargs = dict(
            max_quote_age_seconds=f.max_quote_age_seconds,
            reject_locked_quotes=f.reject_locked_quotes,
            fill_size_fraction=f.fill_size_fraction,
        )
        kwargs.update(overrides)
        return cls(calendar, **kwargs)  # type: ignore[arg-type]

    def execute(
        self,
        order: Order,
        quote: QuoteEvent,
        contract: OptionContract,
        *,
        execution_session: date,
        execution_at: datetime,
        fraction_to_midpoint_f: Decimal = Decimal("0"),
        stress: ExecutionStress | None = None,
    ) -> Fill:
        stress = stress or ExecutionStress.zero()
        effective_at = shift_instant(execution_at, stress.latency_seconds)

        if not self.calendar.is_session(execution_session):
            raise FillRejection("SESSION_NOT_IN_CALENDAR", f"{execution_session} is not a session")
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
                f"{contract.contract_id} expired {contract.expiration} before {execution_session}",
            )
        if not contract.exists_on(order.decision_session):
            raise FillRejection(
                "CONTRACT_UNKNOWN_AT_DECISION",
                f"{contract.contract_id} not listed on decision session "
                f"{order.decision_session}: unknowable at decision time",
            )
        if order.decision_at != session_close_instant(order.decision_session):
            raise FillRejection(
                "DECISION_INSTANT_NOT_CLOSE",
                f"decision_at {order.decision_at} != session close "
                f"{session_close_instant(order.decision_session)}",
            )

        exec_ord = self.calendar.ordinal(execution_session)
        decision_ord = self.calendar.ordinal(order.decision_session)
        if not (effective_at > order.decision_at and exec_ord > decision_ord):
            raise FillRejection(
                "SAME_SESSION_EXECUTION",
                f"execution {effective_at}/{execution_session} not strictly after "
                f"decision {order.decision_at}/{order.decision_session}",
            )
        if not self.calendar.contains_instant(execution_session, effective_at):
            raise FillRejection(
                "EXECUTION_INSTANT_MISMATCH",
                f"{effective_at} does not lie inside session {execution_session}",
            )

        tq = as_tradable(
            quote,
            execution_at=effective_at,
            max_quote_age_seconds=self.max_quote_age_seconds,
            reject_locked=self.reject_locked_quotes,
        )

        if not contract.standard_contract_flag:
            raise FillRejection(
                "NONSTANDARD_DELIVERABLE",
                f"{contract.contract_id} has a nonstandard deliverable; M0 trades "
                "standard contracts only",
            )

        if fraction_to_midpoint_f not in ALLOWED_FRACTIONS:
            raise FillRejection(
                "INVALID_FRACTION_TO_MIDPOINT",
                f"{fraction_to_midpoint_f} not in {ALLOWED_FRACTIONS}",
            )

        price = fraction_to_midpoint(tq.bid, tq.ask, order.side, fraction_to_midpoint_f)
        if order.side == "buy":
            price = price + Decimal(stress.slippage_ticks_buy) / 100
        else:
            price = price - Decimal(stress.slippage_ticks_sell) / 100
        if not (Decimal(0) < price):
            raise PriceOutsideBounds(f"stressed price {price} non-positive")
        if order.side == "buy" and price > tq.ask + Decimal(
            stress.slippage_ticks_buy
        ) / 100 + Decimal("0.01"):
            raise PriceOutsideBounds(f"buy price {price} beyond ask+slippage")

        if order.order_type == "limit" and order.limit_price is not None:
            if order.side == "buy" and order.limit_price < price:
                raise FillRejection(
                    "UNMARKETABLE_LIMIT", f"buy limit {order.limit_price} < fill {price}"
                )
            if order.side == "sell" and order.limit_price > price:
                raise FillRejection(
                    "UNMARKETABLE_LIMIT", f"sell limit {order.limit_price} > fill {price}"
                )

        displayed = tq.ask_size if order.side == "buy" else tq.bid_size
        capacity = math.floor(self.fill_size_fraction * displayed)
        quantity = min(order.quantity, capacity)
        if quantity < 1:
            raise FillRejection(
                "NO_LIQUIDITY", f"fill capacity {capacity} from displayed {displayed}"
            )

        fee_model = (
            _StressedFeeModel(self.fee_model, stress.extra_fee_per_contract)
            if stress.extra_fee_per_contract > 0
            else self.fee_model
        )
        fees = fee_model.order_fees(quantity)

        self._fill_seq += 1
        return Fill(
            fill_id=f"{order.order_id}-F{self._fill_seq}",
            order_id=order.order_id,
            contract_id=order.contract_id,
            side=order.side,
            quantity=quantity,
            price=price.quantize(PRICE_TICK),
            multiplier=contract.multiplier,
            deliverable_shares_per_contract=contract.deliverable.shares_per_contract,
            fees=fees,
            execution_at=effective_at,
            execution_session=execution_session,
            fraction_to_midpoint=fraction_to_midpoint_f,
        )
