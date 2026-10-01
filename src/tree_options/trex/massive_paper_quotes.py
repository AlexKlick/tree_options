"""Explicit server-side fresh Massive quote source for governed paper canaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tree_options.data.massive_client import client_from_environment
from tree_options.data.massive_quotes import QuoteClient, fresh_equity_quote
from tree_options.trex.snaptrade_runtime import CanaryQuote


def canary_quote_source(
    client: QuoteClient, *, clock: Callable[[], datetime] | None = None
) -> Callable[[str], CanaryQuote]:
    def observe(symbol: str) -> CanaryQuote:
        quote = fresh_equity_quote(client, symbol, clock=clock)
        source = f"massive-last-nbbo/{quote.request_id}/{quote.symbol}/{quote.sequence_number}/{quote.sip_received_ns}/{quote.exchange_event_ns}"
        return CanaryQuote(quote.symbol, quote.bid, quote.ask, quote.sip_received_at, source)

    return observe


def quote_source_from_environment(
    *, clock: Callable[[], datetime] | None = None
) -> Callable[[str], CanaryQuote]:
    """No credential or provider access happens until the operator opts in.

    Existing Massive key custody remains server-side; access entitlement and
    actual fresh quotes are external qualification requirements, not inferred.
    """
    return canary_quote_source(client_from_environment(), clock=clock)
