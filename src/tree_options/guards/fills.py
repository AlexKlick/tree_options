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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

from tree_options.ledger.fees import FeeModel, PerContractFeeModel
from tree_options.protocol.schema import ResearchProtocol
from tree_options.schemas.common import PRICE_TICK
from tree_options.schemas.market import (
    QuoteEvent,
    VwapQuoteEvent,
    as_tradable,
    as_tradable_vwap,
    conservative_tick,
    select_quote,
)
from tree_options.schemas.options import OptionContract
from tree_options.schemas.trading import Fill, Order
from tree_options.time.calendar import SessionCalendar
from tree_options.time.sessions import shift_instant

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
        decision_closes: Mapping[date, datetime] | None = None,
    ) -> None:
        self.calendar = calendar
        self.fee_model: FeeModel = fee_model or PerContractFeeModel()
        self.max_quote_age_seconds = max_quote_age_seconds
        if not (Decimal("0") < fill_size_fraction <= Decimal("1")):
            raise ValueError(f"fill_size_fraction {fill_size_fraction} must be in (0, 1]")
        self.reject_locked_quotes = reject_locked_quotes
        self.fill_size_fraction = fill_size_fraction
        # (0.2.2 declaration 3, owner ruling m4-022-ruling-20260828) The
        # frozen VERIFIED decision closes for the door's DECISION-side
        # comparison on the dual-calendar lane. None (the default) keeps the
        # single-calendar behavior byte-identical: the door reads its own
        # calendar's session_close, exactly as before. When supplied, the
        # decision-side comparison consumes ONLY this map — an unmapped
        # decision session refuses by name (DECISION_CLOSE_NOT_MAPPED),
        # never a fallback to the execution calendar's close for a session
        # the decision grid never verified.
        self.decision_closes = decision_closes
        self._fill_seq = 0
        self._executed_orders: set[str] = set()
        self._orders: dict[str, Order] = {}  # order_id -> the order BOUND at first mint
        self._filled_qty: dict[str, int] = {}
        # G3: cumulative contracts filled against one (contract, bar
        # session) — the participation cap is global to the engine, not
        # per-order (a session's observed volume cannot be minted twice).
        self._vwap_consumed: dict[tuple[str, date], int] = {}

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
        quotes: QuoteEvent | VwapQuoteEvent | Sequence[QuoteEvent | VwapQuoteEvent],
        contract: OptionContract,
        *,
        execution_session: date,
        execution_at: datetime,
        fraction_to_midpoint_f: Decimal = Decimal("0"),
        stress: ExecutionStress | None = None,
        partial_sequence: bool = False,
    ) -> Fill:
        """Mint a Fill or fail closed.

        `quotes` is the quote STREAM visible at the (latency-shifted)
        effective instant; the engine itself selects the latest eligible
        quote — the caller cannot cherry-pick a favorable older print.
        The stream may mix quote KINDS (two-sided and vwap bars); the
        selected quote's kind picks the fill branch (G3, protocol 0.2.0):
        a two-sided quote fills at the fraction-to-midpoint executable, a
        vwap bar fills AT the session VWAP rounded conservatively to the
        tick, participation-capped by the bar's observed volume — a
        zero-volume bar is unfillable, never priced at a fabrication.
        `partial_sequence=True` is the explicit, per-call opt-in that lets
        one order mint a further fill (a deliberate partial-fill chain);
        re-executing an order without it is DUPLICATE_ORDER_EXECUTION, and a
        partial sequence is bounded by the order's REMAINING quantity — the
        cumulative filled amount can never exceed order.quantity (review F12).
        An order_id that already minted a fill is BOUND to that order:
        re-presenting the id under different terms is ORDER_REBOUND (review
        round 3 F12) — the cumulative bound belongs to the order first
        presented, not to whatever terms ride the id later.
        """
        stress = stress or ExecutionStress.zero()
        effective_at = shift_instant(execution_at, stress.latency_seconds)
        quote_stream = (
            [quotes] if isinstance(quotes, (QuoteEvent, VwapQuoteEvent)) else list(quotes)
        )
        if order.order_id in self._executed_orders and not partial_sequence:
            raise FillRejection(
                "DUPLICATE_ORDER_EXECUTION",
                f"order {order.order_id} already minted a fill; a further fill "
                "requires partial_sequence=True",
            )
        bound = self._orders.get(order.order_id)
        if bound is not None and bound != order:
            raise FillRejection(
                "ORDER_REBOUND",
                f"order {order.order_id} already minted a fill under different "
                "terms; an order_id cannot be re-bound to a new order",
            )
        already_filled = self._filled_qty.get(order.order_id, 0)
        if partial_sequence and already_filled >= order.quantity:
            raise FillRejection(
                "PARTIAL_EXCEEDS_ORDER",
                f"order {order.order_id} is already fully filled "
                f"({already_filled}/{order.quantity}); a partial sequence cannot "
                "mint quantity beyond the order",
            )

        if not self.calendar.is_session(execution_session):
            raise FillRejection("SESSION_NOT_IN_CALENDAR", f"{execution_session} is not a session")
        if contract.contract_id != order.contract_id or any(
            q.contract_id != order.contract_id for q in quote_stream
        ):
            raise FillRejection(
                "CONTRACT_MISMATCH",
                f"order {order.contract_id} / contract {contract.contract_id} / "
                f"quotes {[q.contract_id for q in quote_stream]} disagree",
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
        # (022-C, 0.2.2 declaration 3 — fill_door_decision_close:
        # "decision_grid") The DECISION-side close: on the dual-calendar
        # lane the execution calendar's close is NOT the decision instant's
        # authority — an early-close session reads 16:00 there while the
        # verified decision_at carries the grid's 13:00, and the door used
        # to reject exactly the correctly-stamped order (known limitation
        # (a)). With decision_closes supplied, this comparison consumes the
        # FROZEN VERIFIED closes of the decision grid; every EXECUTION-side
        # check below (SAME_SESSION_EXECUTION ordinals,
        # EXECUTION_INSTANT_MISMATCH, contains_instant, the bar stamps)
        # stays on the execution calendar. An unmapped decision session
        # refuses by name — the door never falls back to the execution
        # calendar's close for a session the decision grid never verified.
        if self.decision_closes is not None:
            try:
                calendar_decision_close = self.decision_closes[order.decision_session]
            except KeyError:
                raise FillRejection(
                    "DECISION_CLOSE_NOT_MAPPED",
                    f"decision session {order.decision_session} has no frozen "
                    "verified close in the engine's decision_closes map — the "
                    "decision-side comparison never falls back to the execution "
                    "calendar's close for a session the decision grid never "
                    "verified",
                ) from None
        else:
            calendar_decision_close = self.calendar.session_close(order.decision_session)
        if order.decision_at != calendar_decision_close:
            raise FillRejection(
                "DECISION_INSTANT_NOT_CLOSE",
                f"decision_at {order.decision_at} != session close "
                f"{calendar_decision_close} (early closes are 13:00 ET)",
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

        selected = select_quote(quote_stream, effective_at)
        if isinstance(selected, VwapQuoteEvent):
            # G3 vwap branch: publication-gated bar, no age-in-seconds rule
            # (a daily summary's validity is its session identity).
            vq = as_tradable_vwap(selected, execution_at=effective_at)
            # Session-identity coherence: the bar's exchange stamp IS the
            # close of its own `session` — a label disagreeing with its
            # stamps is a mislabeled input and refuses by name (review P0:
            # otherwise a bar stamped at one session's close but LABELED
            # another session fills as if it were that other session).
            if not self.calendar.is_session(vq.quote.session):
                raise FillRejection(
                    "BAR_SESSION_NOT_IN_CALENDAR",
                    f"bar session {vq.quote.session} for {vq.quote.contract_id}"
                    " is not a calendar session",
                )
            if self.calendar.session_close(vq.quote.session) != vq.quote.exchange_timestamp:
                raise FillRejection(
                    "BAR_SESSION_STAMP_MISMATCH",
                    f"bar session {vq.quote.session} close"
                    f" {self.calendar.session_close(vq.quote.session)} !="
                    f" exchange stamp {vq.quote.exchange_timestamp}"
                    f" ({vq.quote.contract_id})",
                )
            # Recency (review round 2): the bar must be the session
            # IMMEDIATELY before the execution session. An older coherent
            # bar is the last observed reality only because intervening
            # sessions traded nothing — filling at its VWAP would fabricate
            # liquidity those zero-volume sessions deny. Fail closed.
            bar_ordinal = self.calendar.ordinal(vq.quote.session)
            if exec_ord - bar_ordinal != 1:
                raise FillRejection(
                    "BAR_NOT_MOST_RECENT",
                    f"bar session {vq.quote.session} (ordinal {bar_ordinal}) is"
                    f" not the session immediately before execution"
                    f" {execution_session} (ordinal {exec_ord}): an older"
                    " session's VWAP cannot fill — the intervening sessions"
                    " deny its liquidity",
                )
        else:
            tq = as_tradable(
                selected,
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

        if isinstance(selected, VwapQuoteEvent):
            if fraction_to_midpoint_f != Decimal("0"):
                raise FillRejection(
                    "INVALID_FRACTION_TO_MIDPOINT",
                    f"{fraction_to_midpoint_f}: the midpoint fraction is a "
                    "two-sided concept; a vwap fill is AT the benchmark",
                )
            # The sub-tick VWAP rounds to the fill tick AGAINST the taker
            # (buy up, sell down), then stress only worsens — the same
            # monotone direction as the two-sided path.
            price = conservative_tick(vq.vwap, order.side)
            if order.side == "buy":
                price = price + Decimal(stress.slippage_ticks_buy) / 100
            else:
                price = price - Decimal(stress.slippage_ticks_sell) / 100
            if not (Decimal(0) < price):
                raise PriceOutsideBounds(f"stressed price {price} non-positive")
        else:
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

        if isinstance(selected, VwapQuoteEvent):
            # Participation, not displayed size: the cap is the contracts
            # the session actually traded (the bar's own volume), and a
            # zero-volume bar was already refused at as_tradable_vwap.
            # The cap is PER (contract, bar session) and CUMULATIVE across
            # fills (review P0: without the ledger, a partial-sequence — or
            # a second order — could fill twice the session's entire
            # observed volume against the same bar).
            participation_key = (selected.contract_id, selected.session)
            already_participated = self._vwap_consumed.get(participation_key, 0)
            displayed = vq.volume
            capacity = math.floor(self.fill_size_fraction * vq.volume) - already_participated
        else:
            displayed = tq.ask_size if order.side == "buy" else tq.bid_size
            capacity = math.floor(self.fill_size_fraction * displayed)
        remaining = order.quantity - already_filled
        quantity = min(remaining, capacity)
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

        # Construct the Fill FIRST: its validators are the last fail-closed
        # gate (e.g. a poisoned fee model yields fees < 0). Only a Fill that
        # validates may commit engine state — a failed mint must leave the
        # order retryable, not burned (review round 3 P2).
        fill = Fill(
            fill_id=f"{order.order_id}-F{self._fill_seq + 1}",
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
        self._fill_seq += 1
        self._executed_orders.add(order.order_id)
        self._orders[order.order_id] = order
        self._filled_qty[order.order_id] = already_filled + quantity
        if isinstance(selected, VwapQuoteEvent):
            self._vwap_consumed[participation_key] = already_participated + quantity
        return fill
