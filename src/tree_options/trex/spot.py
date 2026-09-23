"""Where the exit machine's underlying price comes from (touch-exit spot, E0).

The touch exit fires when the underlying trades at or below the long
strike, so it is only as good as its spot. Two sources, in order:

1. **IBKR**: the ticker's ``last``, else its bid/ask mid, and only when
   the ticker itself updated within IBKR_MAX_AGE_S (``ticker.time``, the
   receipt time). Never ``close``: that is the PRIOR session's close, which
   can fake a touch or hide one. Frozen market data (types 2 and 4, the
   last value from when the market closed) is refused; delayed data (type
   3) is accepted and reports its true age (receipt age + 15 min). The
   paper account delivers no equity quotes today, so this is usually empty.
2. **Polygon** stock snapshot (Starter plan, 15 min delayed intraday):
   the latest minute bar's close, dated by ``min.t`` (the bar's START, so
   the age is conservative by up to a minute), else the day's close dated
   by ``updated``. Accepted while at most POLYGON_MAX_AGE_S old.

The Polygon lookup never stalls the exit loop: one attempt per symbol per
POLYGON_TTL_S, POLYGON_TIMEOUT_S per request, TICK_BUDGET_S per tick for
all symbols together, never a response cache (a cached quote is exactly
the stale spot this module exists to refuse). Any failure costs that
symbol that tick; its last good bar keeps serving while young enough.

A symbol with no accepted reading is simply absent: the engine makes no
touch decision without a spot (engine.decide), and the monitor reports it
as blind (``monitor.json`` ``spot_blind``) for the exit watchdog.

Ages are epoch-second differences, never date arithmetic (the repo's
time/ ban). The API key is read by path at the first fetch and never
logged: failures are logged by exception class only.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

from tree_options.trex.clock import ET

if TYPE_CHECKING:
    from tree_options.data.massive_client import Transport

log = logging.getLogger("trex.spot")

IBKR_MAX_AGE_S = 120.0
IBKR_DELAYED_LAG_S = 15 * 60.0  # market data type 3 runs ~15 min behind
POLYGON_MAX_AGE_S = 20 * 60.0
POLYGON_TTL_S = 60.0
POLYGON_TIMEOUT_S = 3.0
TICK_BUDGET_S = 6.0
MIN_FETCH_S = 0.25  # less budget than this left: skip, don't start a doomed request
FUTURE_SKEW_S = 60.0  # a timestamp further ahead than this is nonsense

_FROZEN_TYPES = frozenset({2, 4})
_DELAYED_TYPE = 3
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}"

Source = Literal["ibkr", "polygon"]
PolygonFetch = Callable[[str, float], Mapping[str, Any]]  # (symbol, timeout_s) -> body


@dataclass(frozen=True)
class SpotReading:
    """One accepted underlying price and how old its data is."""

    px: Decimal
    source: Source
    as_of: datetime  # aware: when the data describes the market
    age_s: float  # seconds between as_of and the tick that read it

    def to_json(self) -> dict[str, Any]:
        return {
            "px": str(self.px),
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "age_s": round(self.age_s, 1),
        }


def _price(value: Any) -> Decimal | None:
    """A positive finite price, exactly: Decimal/int/str as given, float via
    its repr (ib_async hands floats; loads_exact never does). bool is not
    a number here."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = str(value)
    if not isinstance(value, (Decimal, int, str)):
        return None
    try:
        px = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return px if px.is_finite() and px > 0 else None


def _epoch(value: Any, scale: float) -> float | None:
    """A positive epoch timestamp in ``scale`` units, as seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, float)):
        return None
    seconds = float(value) / scale
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def ibkr_reading(ticker: Any, now: datetime) -> SpotReading | None:
    """The IBKR spot, or None unless its ticker updated within
    IBKR_MAX_AGE_S. ``last`` first, then the bid/ask mid; never ``close``."""
    if ticker is None:
        return None
    stamp = getattr(ticker, "time", None)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        return None
    data_type = getattr(ticker, "marketDataType", 1)
    if data_type in _FROZEN_TYPES:
        return None
    receipt_age = now.timestamp() - stamp.timestamp()
    if receipt_age > IBKR_MAX_AGE_S or receipt_age < -FUTURE_SKEW_S:
        return None
    px = _price(getattr(ticker, "last", None))
    if px is None:
        bid = _price(getattr(ticker, "bid", None))
        ask = _price(getattr(ticker, "ask", None))
        if bid is None or ask is None or ask < bid:
            return None
        px = (bid + ask) / 2
    age = max(receipt_age, 0.0)
    if data_type == _DELAYED_TYPE:
        age += IBKR_DELAYED_LAG_S
    return SpotReading(px, "ibkr", stamp.astimezone(ET), age)


def _polygon_parts(body: Mapping[str, Any], symbol: str) -> tuple[Decimal, float] | None:
    """(price, data epoch seconds) from a snapshot body, or None."""
    ticker = body.get("ticker")
    if not isinstance(ticker, Mapping) or ticker.get("ticker") != symbol:
        return None
    bar = ticker.get("min")
    if isinstance(bar, Mapping):
        px, at = _price(bar.get("c")), _epoch(bar.get("t"), 1_000)
        if px is not None and at is not None:
            return px, at
    day = ticker.get("day")
    if isinstance(day, Mapping):
        px, at = _price(day.get("c")), _epoch(ticker.get("updated"), 1_000_000_000)
        if px is not None and at is not None:
            return px, at
    return None


def _aged(px: Decimal, at: float, now: datetime) -> SpotReading | None:
    age = now.timestamp() - at
    if age > POLYGON_MAX_AGE_S or age < -FUTURE_SKEW_S:
        return None
    return SpotReading(px, "polygon", datetime.fromtimestamp(at, ET), max(age, 0.0))


def polygon_reading(body: Mapping[str, Any], symbol: str, now: datetime) -> SpotReading | None:
    """The Polygon spot from one snapshot body, or None when unusable or
    older than POLYGON_MAX_AGE_S."""
    parts = _polygon_parts(body, symbol)
    return _aged(*parts, now) if parts is not None else None


class SpotResolver:
    """Accepted spots per symbol for one monitor tick (IBKR, else Polygon).

    ``fetch=None`` means IBKR only (the entry runner, which never needs a
    spot). ``monotonic`` is injectable so the TTL and budget are testable.
    """

    def __init__(
        self,
        fetch: PolygonFetch | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._mono = monotonic
        # symbol -> (monotonic time of the last attempt, last good (px, epoch))
        self._polygon: dict[str, tuple[float, tuple[Decimal, float] | None]] = {}
        self._last_error: dict[str, str] = {}

    def resolve(
        self,
        symbols: Iterable[str],
        ibkr: Mapping[str, SpotReading | None],
        now: datetime,
    ) -> dict[str, SpotReading]:
        out: dict[str, SpotReading] = {}
        started = self._mono()
        for symbol in dict.fromkeys(symbols):
            reading = ibkr.get(symbol)
            if reading is None:
                reading = self._polygon_spot(symbol, now, started)
            if reading is not None:
                out[symbol] = reading
        return out

    def _polygon_spot(self, symbol: str, now: datetime, started: float) -> SpotReading | None:
        if self._fetch is None:
            return None
        entry = self._polygon.get(symbol)
        attempted = entry[0] if entry is not None else None
        good = entry[1] if entry is not None else None
        mono = self._mono()
        remaining = TICK_BUDGET_S - (mono - started)
        due = attempted is None or mono - attempted >= POLYGON_TTL_S
        if due and remaining >= MIN_FETCH_S:
            fresh = self._attempt(symbol, min(POLYGON_TIMEOUT_S, remaining))
            good = fresh or good  # a failed refresh keeps the last good bar
            self._polygon[symbol] = (mono, good)
        return _aged(*good, now) if good is not None else None

    def _attempt(self, symbol: str, timeout: float) -> tuple[Decimal, float] | None:
        assert self._fetch is not None
        try:
            parts = _polygon_parts(self._fetch(symbol, timeout), symbol)
        except Exception as exc:  # isolated per symbol: never the exit loop
            error = type(exc).__name__
            if self._last_error.get(symbol) != error:
                log.warning("polygon spot for %s failed: %s", symbol, error)
            self._last_error[symbol] = error
            return None
        if parts is None:
            error = "unusable snapshot"
            if self._last_error.get(symbol) != error:
                log.warning("polygon spot for %s: %s", symbol, error)
            self._last_error[symbol] = error
            return None
        if self._last_error.pop(symbol, None) is not None:
            log.info("polygon spot for %s recovered", symbol)
        return parts


def polygon_fetcher(
    *,
    api_key: Callable[[], str] | None = None,
    transport: Transport | None = None,
) -> PolygonFetch:
    """The live Polygon snapshot fetch: uncached, one attempt, no spacing.

    The key is loaded at the first call (``POLYGON_API_KEY`` or the 0600
    key file, via massive_client) and stays inside the client; every
    massive_client error is key-redacted by construction.
    """
    from tree_options.data import massive_client as mc

    load = api_key or mc.load_api_key
    client: mc.MassiveClient | None = None

    def fetch(symbol: str, timeout: float) -> Mapping[str, Any]:
        nonlocal client
        if not _SYMBOL.match(symbol):
            raise ValueError("not a stock symbol")
        if client is None:
            client = mc.MassiveClient(
                api_key=load(),
                transport=transport or mc.urllib_transport,
                cache_dir=None,  # never a cached quote
                governor=mc.RateGovernor(None),  # paid plan; spacing would stall the tick
                backoff=mc.BackoffPolicy(max_attempts=1),  # the next tick is the retry
                timeout=timeout,
            )
        client.timeout = timeout
        return client.get_json(SNAPSHOT_PATH.format(symbol=symbol), use_cache=False)

    return fetch
