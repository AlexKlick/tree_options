"""Quote schemas (§6.4): raw reality vs tradable quote.

A raw QuoteEvent MAY be crossed — reality is crossed sometimes, and silently
dropping such quotes at ingestion would be a silent exclusion. The fail-closed
boundary is the fill: `as_tradable` is the only door a fill may consume, and
it rejects crossed, zero-size, stale, and non-tradable-condition quotes.
"""

from __future__ import annotations

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
) -> TradableQuote:
    """Validate a raw quote into a TradableQuote, or fail closed.

    Staleness: the quote must not be received AFTER the execution instant, and
    its age at the execution instant must not exceed max_quote_age_seconds.
    """
    if q.received_timestamp > execution_at:
        raise StaleQuoteError(
            f"quote received {q.received_timestamp} after execution {execution_at}"
        )
    age = (execution_at - q.received_timestamp).total_seconds()
    if age > max_quote_age_seconds:
        raise StaleQuoteError(f"quote age {age:.0f}s exceeds {max_quote_age_seconds}s")
    if not (Decimal("0") < q.bid <= q.ask):
        raise CrossedQuoteError(f"crossed or non-positive quote: bid={q.bid} ask={q.ask}")
    if q.bid_size < 1 or q.ask_size < 1:
        raise ZeroSizeQuoteError(f"zero-size quote: bid_size={q.bid_size} ask_size={q.ask_size}")
    if q.quote_condition not in TRADABLE_CONDITIONS:
        raise NonTradableConditionError(f"condition {q.quote_condition!r} not tradable")
    return TradableQuote(quote=q)
