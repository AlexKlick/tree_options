"""NBBO fixture builders (handoff §7 fixture 6: crossed + stale quotes).

Every builder targets a single contract at one instant so guard tests can
swap variants one field at a time. Fresh quotes are two-sided and inside the
900s staleness budget relative to the given execution instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tree_options.schemas.market import QuoteEvent


def fresh_quote(
    *,
    contract_id: str = "OPT-C-2024-06-21-50",
    bid: str = "1.00",
    ask: str = "1.10",
    bid_size: int = 10,
    ask_size: int = 12,
    execution_at: datetime,
    received_offset_seconds: int = 5,
    condition: str = "regular",
) -> QuoteEvent:
    received = execution_at - timedelta(seconds=received_offset_seconds)
    return QuoteEvent(
        contract_id=contract_id,
        exchange_timestamp=received - timedelta(seconds=1),
        received_timestamp=received,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=bid_size,
        ask_size=ask_size,
        quote_condition=condition,
        source="nbbo-fixture",
    )


def crossed_quote(execution_at: datetime, **kwargs) -> QuoteEvent:
    return fresh_quote(bid="1.20", ask="1.10", execution_at=execution_at, **kwargs)


def zero_size_quote(execution_at: datetime, **kwargs) -> QuoteEvent:
    return fresh_quote(ask_size=0, execution_at=execution_at, **kwargs)


def stale_quote(execution_at: datetime, **kwargs) -> QuoteEvent:
    """Quote received AFTER the execution instant (impossible ordering)."""
    q = fresh_quote(execution_at=execution_at, **kwargs)
    return q.model_copy(
        update={
            "received_timestamp": execution_at + timedelta(seconds=30),
            "exchange_timestamp": execution_at + timedelta(seconds=31),
        }
    )


def over_age_quote(execution_at: datetime, **kwargs) -> QuoteEvent:
    return fresh_quote(received_offset_seconds=1200, execution_at=execution_at, **kwargs)


def non_tradable_quote(execution_at: datetime, **kwargs) -> QuoteEvent:
    return fresh_quote(condition="closed_auction", execution_at=execution_at, **kwargs)


def execution_instant(session_open: datetime, minutes_in: int = 60) -> datetime:
    """A normal intraday execution instant: session open + minutes."""
    return (session_open + timedelta(minutes=minutes_in)).astimezone(UTC)


def locked_quote(execution_at: datetime, price: str = "1.05", **kwargs) -> QuoteEvent:
    """Locked market: bid == ask (fixture §5.3)."""
    return fresh_quote(bid=price, ask=price, execution_at=execution_at, **kwargs)
