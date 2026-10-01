"""Fresh exact equity NBBO semantics above MassiveClient transport.

Official provider contract: https://massive.com/docs/rest/stocks/trades-quotes/last-quote
`t` is SIP receive time; `y` is exchange quote generation time, both Unix ns.
Neither is local HTTP receive time. Raw integer ns survive the datetime projection.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from tree_options.time.nanoseconds import from_nanoseconds, utc_nanoseconds


class QuoteClient(Protocol):
    def get_json(
        self, path: str, params: Mapping[str, Any] | None = None, *, use_cache: bool = True
    ) -> Mapping[str, Any]: ...


class MassiveQuoteError(ValueError):
    """Source facts cannot establish a current executable NBBO."""


@dataclass(frozen=True)
class EquityNBBOQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    sip_received_ns: int
    exchange_event_ns: int
    sip_received_at: datetime
    exchange_event_at: datetime
    request_id: str
    sequence_number: int


def _integer(raw: Any, minimum: int = 1) -> int:
    if type(raw) is not int or raw < minimum:
        raise MassiveQuoteError("invalid integer quote fact")
    return raw


def _price(raw: Any) -> Decimal:
    if not isinstance(raw, (Decimal, int)) or isinstance(raw, bool):
        raise MassiveQuoteError("quote prices require exact provider decimals")
    value = Decimal(raw)
    if not value.is_finite() or value <= 0:
        raise MassiveQuoteError("invalid quote price")
    return value


def fresh_equity_quote(
    client: QuoteClient,
    symbol: str,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> EquityNBBOQuote:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
        raise MassiveQuoteError("unsupported equity symbol")
    # The existing client owns authentication, rate spacing and bounded retries.
    # Every canary preflight must observe the wire rather than a cached quote.
    body = client.get_json(f"/v2/last/nbbo/{symbol}", use_cache=False)
    at = now if now is not None else (clock or (lambda: datetime.now(UTC)))()
    try:
        now_ns = utc_nanoseconds(at)
    except ValueError:
        raise MassiveQuoteError("aware quote clock required") from None
    if body.get("status") != "OK":
        raise MassiveQuoteError("real-time quote status required")
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", request_id):
        raise MassiveQuoteError("provider request identity required")
    row = body.get("results")
    if not isinstance(row, Mapping) or row.get("T") != symbol:
        raise MassiveQuoteError("quote ticker mismatch or missing results")
    bid, ask = _price(row.get("p")), _price(row.get("P"))
    if bid > ask:
        raise MassiveQuoteError("crossed NBBO refused")
    sip, exchange = _integer(row.get("t")), _integer(row.get("y"))
    if (
        exchange > sip
        or not 0 <= now_ns - sip <= 15000000000
        or not 0 <= now_ns - exchange <= 15000000000
    ):
        raise MassiveQuoteError("future stale or contradictory quote timestamps")
    try:
        sip_at, exchange_at = from_nanoseconds(sip), from_nanoseconds(exchange)
    except (ValueError, OverflowError, OSError):
        raise MassiveQuoteError("quote time cannot be represented") from None
    return EquityNBBOQuote(
        symbol,
        bid,
        ask,
        _integer(row.get("s")),
        _integer(row.get("S")),
        sip,
        exchange,
        sip_at,
        exchange_at,
        request_id,
        _integer(row.get("q"), 0),
    )
