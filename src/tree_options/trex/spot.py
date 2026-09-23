"""Where the exit machine's underlying price comes from (touch-exit spot, E0).

The touch exit fires when the underlying trades at or below the long
strike, so it is only as good as its spot. The spot is the Polygon stock
snapshot (Starter plan, 15 min delayed intraday): the latest minute bar's
close, dated by ``min.t`` (the bar's START, so the age is conservative by
up to a minute), else the day's close dated by ``updated``. A reading is
accepted only when that timestamp falls inside the CURRENT session's
regular hours (calendar open <= t < close, early closes respected: the
snapshot includes premarket and after-hours prints, and a 09:14 bar below
the strike must not fire a 09:30 exit) and is at most POLYGON_MAX_AGE_S old.

IBKR stock prices are not a source (see IbkrTrex.snapshot): ``ticker.time``
moves on every bid/ask/size tick, so it can't date ``last``; ``close`` is
the prior session's; and the paper account has no equity quotes anyway.

Fetching never blocks the exit loop. SpotFeed.readings() only dispatches
due fetches to background workers and returns what has already arrived:
at most one request in flight per symbol and MAX_INFLIGHT overall (the
longest-waiting symbols first, so none starves), one attempt per symbol
per POLYGON_TTL_S, and each request bounded by a TOTAL deadline
(deadline_transport; a socket timeout alone bounds each read, not the
request). Never a response cache: a cached quote is exactly the stale spot
this module exists to refuse. A failure costs that symbol, and its last
good bar keeps serving while it passes the checks above.

The delayed feed's first regular-session bar arrives about FEED_LAG_S after
the open, so blindness only counts inside touch_window() (open + lag to the
calendar close). A symbol with no accepted reading is simply absent: the
engine makes no touch decision without a spot (engine.decide).

Ages are epoch-second differences, never date arithmetic (the repo's time/
ban). The API key is read by path at the first fetch and never logged:
failures are logged by exception class only.
"""

from __future__ import annotations

import functools
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

from tree_options.trex.clock import ET, session_calendar

if TYPE_CHECKING:
    from tree_options.data.massive_client import HttpResponse, Transport

log = logging.getLogger("trex.spot")

POLYGON_MAX_AGE_S = 20 * 60.0
POLYGON_TTL_S = 60.0
POLYGON_TIMEOUT_S = 3.0  # per blocking socket operation
REQUEST_DEADLINE_S = 10.0  # per request, total
MAX_BODY_BYTES = 256 * 1024
MAX_INFLIGHT = 8
FEED_LAG_S = 16 * 60.0  # 15 min delay + the 1-minute bar: first regular bar ~open+16m
FUTURE_SKEW_S = 60.0  # a timestamp further ahead than this is nonsense

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}"

Source = Literal["polygon"]
PolygonFetch = Callable[[str, float], Mapping[str, Any]]  # (symbol, timeout_s) -> body
Spawn = Callable[[Callable[[], None]], None]


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


def session_bounds(d: date) -> tuple[float, float] | None:
    """(open, close) epoch seconds of session ``d`` per the trex calendar
    (early closes respected), or None when ``d`` is not a session."""
    cal = session_calendar()
    if not cal.is_session(d):
        return None
    return cal.session_open(d).timestamp(), cal.session_close(d).timestamp()


def touch_window(now: datetime) -> tuple[float, float] | None:
    """When blindness counts today: from the moment the delayed feed can
    first deliver a regular-session bar (open + FEED_LAG_S) to the close."""
    bounds = session_bounds(now.astimezone(ET).date())
    return (bounds[0] + FEED_LAG_S, bounds[1]) if bounds is not None else None


def in_touch_window(now: datetime) -> bool:
    window = touch_window(now)
    return window is not None and window[0] <= now.timestamp() <= window[1]


def _price(value: Any) -> Decimal | None:
    """A positive finite price, exactly (loads_exact hands Decimal/int).
    bool is not a number here."""
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


def accept(px: Decimal, at: float, now: datetime) -> SpotReading | None:
    """A reading the touch exit may act on at ``now``: dated inside the
    current session's regular hours and at most POLYGON_MAX_AGE_S old."""
    bounds = session_bounds(now.astimezone(ET).date())
    if bounds is None or not bounds[0] <= at < bounds[1]:
        return None
    age = now.timestamp() - at
    if age > POLYGON_MAX_AGE_S or age < -FUTURE_SKEW_S:
        return None
    return SpotReading(px, "polygon", datetime.fromtimestamp(at, ET), max(age, 0.0))


def polygon_reading(body: Mapping[str, Any], symbol: str, now: datetime) -> SpotReading | None:
    """The accepted spot from one snapshot body, or None."""
    parts = _polygon_parts(body, symbol)
    return accept(*parts, now) if parts is not None else None


def _daemon_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="trex-spot", daemon=True).start()


class SpotFeed:
    """Background Polygon spots; the exit loop only reads what has arrived.

    ``fetch=None`` means no spots at all. ``spawn`` and ``monotonic`` are
    injectable so tests control when workers run and how time passes.
    """

    def __init__(
        self,
        fetch: PolygonFetch | None,
        *,
        spawn: Spawn = _daemon_thread,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._spawn = spawn
        self._mono = monotonic
        self._lock = threading.Lock()
        self._latest: dict[str, tuple[Decimal, float]] = {}  # last good (px, epoch)
        self._attempted: dict[str, float] = {}  # monotonic time of the last dispatch
        self._inflight: set[str] = set()
        self._last_error: dict[str, str] = {}

    def readings(self, symbols: Iterable[str], now: datetime) -> dict[str, SpotReading]:
        """Non-blocking: dispatch due fetches, return accepted readings."""
        wanted = list(dict.fromkeys(symbols))
        if self._fetch is None:
            return {}
        self._dispatch(wanted)
        with self._lock:
            latest = {s: self._latest[s] for s in wanted if s in self._latest}
        out: dict[str, SpotReading] = {}
        for symbol, (px, at) in latest.items():
            reading = accept(px, at, now)
            if reading is not None:
                out[symbol] = reading
        return out

    def _dispatch(self, wanted: list[str]) -> None:
        mono = self._mono()
        with self._lock:
            due = [
                s
                for s in wanted
                if s not in self._inflight
                and (s not in self._attempted or mono - self._attempted[s] >= POLYGON_TTL_S)
            ]
            # never-tried first, then the longest-waiting: nobody starves
            due.sort(key=lambda s: self._attempted.get(s, -math.inf))
            start = due[: max(MAX_INFLIGHT - len(self._inflight), 0)]
            for symbol in start:
                self._inflight.add(symbol)
                self._attempted[symbol] = mono
        for symbol in start:
            self._spawn(functools.partial(self._run, symbol))

    def _run(self, symbol: str) -> None:
        assert self._fetch is not None
        parts: tuple[Decimal, float] | None = None
        error: str | None = "interrupted"
        try:
            parts = _polygon_parts(self._fetch(symbol, POLYGON_TIMEOUT_S), symbol)
            error = None if parts is not None else "unusable snapshot"
        except Exception as exc:  # isolated per symbol: never the exit loop
            error = type(exc).__name__
        finally:
            with self._lock:
                self._inflight.discard(symbol)
                if parts is not None:
                    self._latest[symbol] = parts  # a failure keeps the last good bar
                previous = self._last_error.get(symbol)
                if error is None:
                    self._last_error.pop(symbol, None)
                else:
                    self._last_error[symbol] = error
        if error is not None and previous != error:
            log.warning("polygon spot for %s failed: %s", symbol, error)
        elif error is None and previous is not None:
            log.info("polygon spot for %s recovered", symbol)


def deadline_transport(
    deadline_s: float = REQUEST_DEADLINE_S,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    monotonic: Callable[[], float] = time.monotonic,
) -> Transport:
    """massive_client's GET with a TOTAL deadline and a body-size cap: the
    socket timeout bounds each blocking read, so a response trickling a
    byte before each timeout could otherwise hold a worker indefinitely.
    Errors never carry the URL (massive_client redacts again anyway)."""
    from tree_options.data import massive_client as mc

    def transport(url: str, *, timeout: float) -> HttpResponse:
        started = monotonic()
        request = urllib.request.Request(
            url, headers={"User-Agent": mc.USER_AGENT, "Accept": "application/json"}
        )
        try:
            response = opener(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return mc.HttpResponse(status=int(exc.code), body=b"")  # body unread: bounded
        except urllib.error.URLError as exc:
            raise mc.MassiveTransportError(
                f"transport failure: {type(exc.reason).__name__}"
            ) from None
        with response:
            chunks: list[bytes] = []
            size = 0
            while True:
                if monotonic() - started > deadline_s:
                    raise mc.MassiveTransportError(f"request deadline of {deadline_s:g}s exceeded")
                chunk = response.read(16384)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise mc.MassiveTransportError("response body too large")
                chunks.append(chunk)
            headers = {k.lower(): v for k, v in response.headers.items()}
            return mc.HttpResponse(
                status=int(response.status), body=b"".join(chunks), headers=headers
            )

    return transport


def polygon_fetcher(
    *,
    api_key: Callable[[], str] | None = None,
    transport: Transport | None = None,
) -> PolygonFetch:
    """The live Polygon snapshot fetch: uncached, one attempt, no spacing,
    total-deadline transport. Called from SpotFeed workers only.

    The key is loaded at the first call (``POLYGON_API_KEY`` or the 0600
    key file, via massive_client) and stays inside the client; every
    massive_client error is key-redacted by construction.
    """
    from tree_options.data import massive_client as mc

    load = api_key or mc.load_api_key
    lock = threading.Lock()
    client: mc.MassiveClient | None = None

    def fetch(symbol: str, timeout: float) -> Mapping[str, Any]:
        nonlocal client
        if not _SYMBOL.match(symbol):
            raise ValueError("not a stock symbol")
        with lock:
            if client is None:
                client = mc.MassiveClient(
                    api_key=load(),
                    transport=transport or deadline_transport(),
                    cache_dir=None,  # never a cached quote
                    governor=mc.RateGovernor(None),  # paid plan; spacing only delays
                    backoff=mc.BackoffPolicy(max_attempts=1),  # the TTL is the retry
                    timeout=timeout,
                )
            live = client
        # one client shared by the workers: every worker passes the same
        # timeout, so this write is idempotent (stats counters are advisory)
        live.timeout = timeout
        return live.get_json(SNAPSHOT_PATH.format(symbol=symbol), use_cache=False)

    return fetch
