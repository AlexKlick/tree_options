"""CBOE delayed full option chain: fetch and parse, calls and puts.

Source: ``https://cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json``
(keyless; indices take a leading underscore, ``_SPX``; our universe is
equities and ETFs).

Probed 2026-09-23 15:13 ET (KO, one request, read-only):

* the payload is an OVERNIGHT end-of-day snapshot. Top-level keys are
  ``data``, ``symbol`` and ``timestamp``. ``timestamp`` ("2026-09-23
  03:49:33") is a naive UTC wall stamp: the object's HTTP Last-Modified
  was 03:49:36 GMT. During the 09-23 session it still carried the 09-22
  close state (underlying ``last_trade_time`` "2026-09-22T15:59:59",
  ET-naive; modal option last trade date 09-22);
* ``data`` holds the underlying quote (current_price, bid, ask, sizes,
  open/high/low/close, prev_day_close, volume, iv30, last_trade_time)
  and ``options``: rows with ``option`` (OCC), bid/ask (+ float sizes),
  iv, delta, gamma, theta, vega, rho, theo, open_interest, volume,
  last_trade_price and an ET-naive last_trade_time (null if never
  traded). KO: 1038 rows, 925 with bid > 0.

So the recorder runs after the evening publication (or the next morning)
and stamps the snapshot with the session it describes.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tree_options.trex.clock import ET
from tree_options.trex.discovery.market import MOZILLA_UA, Transport, parse_occ

CHAIN_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
SOURCE = "cboe-delayed"
MAX_BYTES = 32_000_000  # SPY's full chain is ~5.5 MB; bigger is refused, never truncated
TIMEOUT_S = 30.0
INDEX_SYMBOLS = frozenset({"SPX", "XSP", "NDX", "RUT", "MRUT", "VIX", "DJX", "OEX", "XND"})

# Columnar layout of one recorded chain (store.py); every list has n entries.
COLUMNS: tuple[str, ...] = (
    "occ",
    "exp",
    "right",
    "strike",
    "bid",
    "ask",
    "bid_size",
    "ask_size",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "theo",
    "oi",
    "volume",
    "last",
    "last_time",
)
# source row key -> column (the OCC-derived columns are filled separately)
_ROW_FIELDS: tuple[tuple[str, str], ...] = (
    ("bid", "bid"),
    ("ask", "ask"),
    ("iv", "iv"),
    ("delta", "delta"),
    ("gamma", "gamma"),
    ("theta", "theta"),
    ("vega", "vega"),
    ("rho", "rho"),
    ("theo", "theo"),
    ("last_trade_price", "last"),
)
_ROW_COUNTS: tuple[tuple[str, str], ...] = (
    ("bid_size", "bid_size"),
    ("ask_size", "ask_size"),
    ("open_interest", "oi"),
    ("volume", "volume"),
)
UNDERLYING_NUMBERS = (
    "current_price",
    "bid",
    "ask",
    "open",
    "high",
    "low",
    "close",
    "prev_day_close",
    "iv30",
)
UNDERLYING_COUNTS = ("bid_size", "ask_size", "volume")


class ChainFetchError(RuntimeError):
    """Transport-level failure (status, size)."""


class ChainParseError(ValueError):
    """The body is not a usable chain payload for the requested symbol."""


@dataclass(frozen=True)
class ParsedChain:
    underlying: str
    source_as_of: datetime  # aware UTC
    underlying_quote: dict[str, Any]
    columns: dict[str, list[Any]]
    n: int
    n_skipped: int  # rows without a parseable (or with a duplicate) OCC symbol


def cboe_symbol(sym: str) -> str:
    return f"_{sym}" if sym in INDEX_SYMBOLS else sym


def chain_url(sym: str) -> str:
    return CHAIN_URL.format(sym=cboe_symbol(sym))


def urllib_transport(url: str, *, timeout: float = TIMEOUT_S) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": MOZILLA_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


def fetch_raw(sym: str, transport: Transport) -> bytes:
    status, body = transport(chain_url(sym), timeout=TIMEOUT_S)
    if status != 200:
        raise ChainFetchError(f"{sym}: HTTP {status}")
    if len(body) > MAX_BYTES:
        raise ChainFetchError(f"{sym}: body over {MAX_BYTES} bytes")
    return body


def _num(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _count(value: Any) -> int | float | None:
    n = _num(value)
    if isinstance(n, float) and n.is_integer():
        return int(n)
    return n


def et_iso(raw: Any) -> str | None:
    """CBOE's ET-naive trade stamps ("2026-09-22T15:59:59") as aware ISO."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    return ts.astimezone(ET).isoformat()


def _source_as_of(raw: Any, sym: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ChainParseError(f"{sym}: payload has no timestamp")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ChainParseError(f"{sym}: unparseable timestamp {raw!r}") from exc
    # the naive stamp is UTC (pinned against HTTP Last-Modified, see above)
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def parse_chain(raw: bytes, sym: str) -> ParsedChain:
    """Every row (calls and puts) into sorted columns; rows whose OCC does
    not parse are counted, not guessed."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ChainParseError(f"{sym}: body is not JSON ({type(exc).__name__})") from exc
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        raise ChainParseError(f"{sym}: payload has no data object")
    got = doc.get("symbol") or data.get("symbol")
    if got != cboe_symbol(sym):
        raise ChainParseError(f"{sym}: payload is for {got!r}")
    as_of = _source_as_of(doc.get("timestamp"), sym)
    options = data.get("options")
    if not isinstance(options, list):
        raise ChainParseError(f"{sym}: payload has no options list")

    parsed_rows = []
    seen: set[str] = set()
    skipped = 0
    for row in options:
        occ = row.get("option") if isinstance(row, dict) else None
        if not isinstance(occ, str) or occ in seen:
            skipped += 1
            continue
        try:
            expiry, right, strike = parse_occ(occ)
        except ValueError:
            skipped += 1
            continue
        seen.add(occ)
        parsed_rows.append((expiry, right, strike, occ, row))
    parsed_rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))

    columns: dict[str, list[Any]] = {c: [] for c in COLUMNS}
    for expiry, right, strike, occ, row in parsed_rows:
        columns["occ"].append(occ)
        columns["exp"].append(expiry.isoformat())
        columns["right"].append(right)
        # a JSON number whose shortest repr round-trips the OCC decimal (587.5)
        columns["strike"].append(float(strike))
        for src, col in _ROW_FIELDS:
            columns[col].append(_num(row.get(src)))
        for src, col in _ROW_COUNTS:
            columns[col].append(_count(row.get(src)))
        columns["last_time"].append(et_iso(row.get("last_trade_time")))

    quote: dict[str, Any] = {k: _num(data.get(k)) for k in UNDERLYING_NUMBERS}
    quote.update({k: _count(data.get(k)) for k in UNDERLYING_COUNTS})
    quote["last_trade_time"] = et_iso(data.get("last_trade_time"))
    return ParsedChain(
        underlying=sym,
        source_as_of=as_of,
        underlying_quote=quote,
        columns=columns,
        n=len(parsed_rows),
        n_skipped=skipped,
    )
