"""Quote schemas (§6.4): raw reality vs tradable quote.

A raw QuoteEvent MAY be crossed — reality is crossed sometimes, and silently
dropping such quotes at ingestion would be a silent exclusion. The fail-closed
boundary is the fill: `as_tradable` is the only door a fill may consume, and
it rejects crossed, zero-size, stale, and non-tradable-condition quotes.

G3 amendment (protocol 0.2.0): a second, structurally separate quote kind —
`VwapQuoteEvent`, the daily-aggregate bar. It is NOT a QuoteEvent with
optional sides: making bid/ask optional would conditionalize every two-sided
guard (crossed/locked/tick/size) and blur the one shape fills have consumed
since M0. A daily bar carries no two-sided market, so it gets its own event,
its own tradable door (`as_tradable_vwap`), and its own fill semantics
(session VWAP as the executable benchmark, participation-capped by observed
volume) — see guards.fills. The two kinds never coerce into each other.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import Field, model_validator

from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime

TRADABLE_CONDITIONS = frozenset({"regular", "opening", "closing", "regular_trading"})
DEFAULT_MAX_QUOTE_AGE_SECONDS = 900


class QuoteEvent(StrictModel):
    contract_id: IdStr
    exchange_timestamp: UTCDatetime
    received_timestamp: UTCDatetime
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(ge=0)
    bid_size: int = Field(ge=0)
    ask_size: int = Field(ge=0)
    quote_condition: IdStr
    source: IdStr

    @model_validator(mode="after")
    def _checks(self) -> QuoteEvent:
        if self.exchange_timestamp > self.received_timestamp:
            raise ValueError("exchange_timestamp must be <= received_timestamp")
        return self


class CrossedQuoteError(RuntimeError):
    pass


class LockedQuoteError(RuntimeError):
    """bid == ask: a locked market is not executable under the frozen protocol."""


class NonpositiveQuoteError(RuntimeError):
    """Zero (or negative) side price — no economically meaningful executable."""


class NonTickQuoteError(RuntimeError):
    """A price not on the 0.01 tick grid — truncating it would fabricate a
    better-than-quoted executable, so execution refuses sub-tick quotes."""


def assert_on_tick(price: Decimal, side: str) -> None:
    cents = price * 100
    if cents != cents.to_integral_value():
        raise NonTickQuoteError(f"{side} {price} is not on the 0.01 tick grid")


class ZeroSizeQuoteError(RuntimeError):
    pass


class StaleQuoteError(RuntimeError):
    pass


class NonTradableConditionError(RuntimeError):
    pass


class TradableQuote(StrictModel):
    """The only quote representation a fill may consume."""

    quote: QuoteEvent

    @model_validator(mode="after")
    def _checks(self) -> TradableQuote:
        # Structural backstop; the graded failure classes are raised by
        # `as_tradable` before construction so callers see the specific type.
        q = self.quote
        if not (Decimal("0") < q.bid <= q.ask):
            raise ValueError(f"crossed or non-positive quote: bid={q.bid} ask={q.ask}")
        if q.bid_size < 1 or q.ask_size < 1:
            raise ValueError(f"zero-size quote: bid_size={q.bid_size} ask_size={q.ask_size}")
        if q.quote_condition not in TRADABLE_CONDITIONS:
            raise ValueError(f"condition {q.quote_condition!r} not tradable")
        return self

    @property
    def bid(self) -> Decimal:
        return self.quote.bid

    @property
    def ask(self) -> Decimal:
        return self.quote.ask

    @property
    def bid_size(self) -> int:
        return self.quote.bid_size

    @property
    def ask_size(self) -> int:
        return self.quote.ask_size


def as_tradable(
    q: QuoteEvent,
    *,
    execution_at,
    max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    reject_locked: bool = False,
) -> TradableQuote:
    """Validate a raw quote into a TradableQuote, or fail closed.

    Staleness: the quote must not be received AFTER the execution instant, and
    its age at the execution instant must not exceed max_quote_age_seconds.
    A locked market (bid == ask) is rejected when the protocol demands it.
    """
    if q.received_timestamp > execution_at:
        raise StaleQuoteError(
            f"quote received {q.received_timestamp} after execution {execution_at}"
        )
    age = (execution_at - q.received_timestamp).total_seconds()
    if age > max_quote_age_seconds:
        raise StaleQuoteError(f"quote age {age:.0f}s exceeds {max_quote_age_seconds}s")
    if q.bid <= 0 or q.ask <= 0:
        raise NonpositiveQuoteError(f"non-positive quote side: bid={q.bid} ask={q.ask}")
    assert_on_tick(q.bid, "bid")
    assert_on_tick(q.ask, "ask")
    if q.bid > q.ask:
        raise CrossedQuoteError(f"crossed quote: bid={q.bid} > ask={q.ask}")
    if reject_locked and q.bid == q.ask:
        raise LockedQuoteError(f"locked quote: bid == ask == {q.bid}")
    if q.bid_size < 1 or q.ask_size < 1:
        raise ZeroSizeQuoteError(f"zero-size quote: bid_size={q.bid_size} ask_size={q.ask_size}")
    if q.quote_condition not in TRADABLE_CONDITIONS:
        raise NonTradableConditionError(f"condition {q.quote_condition!r} not tradable")
    return TradableQuote(quote=q)


def select_quote(quotes, execution_at) -> QuoteEvent:
    """Latest quote received at or before execution_at (fail closed if none).

    Monotone in time by construction: a later execution instant can never
    reach back for an earlier (potentially better) quote. Returns the RAW
    event; tradability grading stays with as_tradable so crossed/locked/
    stale quotes raise their graded error classes, not constructor errors.
    """
    eligible = [q for q in quotes if q.received_timestamp <= execution_at]
    if not eligible:
        raise StaleQuoteError(f"no quote received at or before {execution_at}")
    return max(eligible, key=lambda q: (q.received_timestamp, q.exchange_timestamp))


# ---- G3: the vwap quote kind (protocol 0.2.0) --------------------------------


class VwapQuoteEvent(StrictModel):
    """A daily-aggregate bar as a quote: one session, one volume-weighted
    average price. NOT a QuoteEvent with optional sides — the two kinds are
    structurally separate so no two-sided guard ever runs conditionally.

    `exchange_timestamp` is the close of `session` (the bar summarizes the
    whole session, so the close is the earliest instant it can describe).
    `received_timestamp` is the PUBLICATION instant — the moment the bar
    became observable. On the massive free lane this is the T+1 receipt
    wall, so a vwap fill can never land inside the bar's own session on
    that lane; the engine enforces publication and nothing looser.

    `vwap` may be sub-tick (e.g. 1.2375): it is a benchmark, not a quoted
    two-sided price, so there is no on-tick constructor gate — rounding to
    the fill tick happens at the fill, conservatively per side.
    """

    contract_id: IdStr
    session: date
    exchange_timestamp: UTCDatetime
    received_timestamp: UTCDatetime
    vwap: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    quote_condition: IdStr
    source: IdStr

    @model_validator(mode="after")
    def _checks(self) -> VwapQuoteEvent:
        if self.exchange_timestamp > self.received_timestamp:
            raise ValueError("exchange_timestamp must be <= received_timestamp")
        return self


class ZeroVolumeVwapError(RuntimeError):
    """A zero-volume session produced no VWAP executions to participate in —
    the day is unfillable, never filled at a fabricated price."""


class VwapTradableQuote(StrictModel):
    """The only vwap-quote representation a fill may consume."""

    quote: VwapQuoteEvent

    @model_validator(mode="after")
    def _checks(self) -> VwapTradableQuote:
        q = self.quote
        if q.volume < 1:
            raise ValueError(f"zero-volume bar: {q.contract_id} {q.session}")
        if q.quote_condition not in TRADABLE_CONDITIONS:
            raise ValueError(f"condition {q.quote_condition!r} not tradable")
        return self

    @property
    def vwap(self) -> Decimal:
        return self.quote.vwap

    @property
    def volume(self) -> int:
        return self.quote.volume


def as_tradable_vwap(q: VwapQuoteEvent, *, execution_at) -> VwapTradableQuote:
    """Validate a vwap bar into a VwapTradableQuote, or fail closed.

    Publication gate only: the bar must have been received at or before the
    execution instant. There is deliberately NO max_quote_age_seconds rule
    here — a 900-second tick makes sense for a live two-sided book and is
    meaningless for a daily summary whose validity is its session identity,
    not its recency in seconds. The lookahead protection is the publication
    instant itself (on the massive lane the T+1 receipt wall).
    """
    if q.received_timestamp > execution_at:
        raise StaleQuoteError(
            f"vwap bar received {q.received_timestamp} after execution {execution_at}"
            f" (unpublished: {q.contract_id} session {q.session})"
        )
    if q.volume < 1:
        raise ZeroVolumeVwapError(
            f"zero-volume session {q.session} for {q.contract_id}: unfillable"
        )
    if q.quote_condition not in TRADABLE_CONDITIONS:
        raise NonTradableConditionError(f"condition {q.quote_condition!r} not tradable")
    return VwapTradableQuote(quote=q)


def conservative_tick(price: Decimal, side: str) -> Decimal:
    """Round a benchmark price to the 0.01 fill tick, worsening the taker:
    a BUY rounds UP, a SELL rounds DOWN. Quantization can only hurt —
    the same conservative direction as fraction_to_midpoint's tick rule,
    applied to the sub-tick VWAP instead of a half-tick midpoint."""
    cents = price * 100
    if side == "buy":
        return (cents.to_integral_value(rounding="ROUND_CEILING") / 100).quantize(Decimal("0.01"))
    if side == "sell":
        return (cents.to_integral_value(rounding="ROUND_FLOOR") / 100).quantize(Decimal("0.01"))
    raise ValueError(f"side must be buy or sell, got {side!r}")
