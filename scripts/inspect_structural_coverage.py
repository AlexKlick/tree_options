#!/usr/bin/env python3
"""Structural coverage inspector for the Massive (Polygon) free-tier lane (WS-D2).

The free tier is entitled to the point-in-time contract master
(`/v3/reference/options/contracts?as_of=...`) and daily option bars
(`/v2/aggs/ticker/{optionTicker}/range/1/day/...`) and to NOTHING ELSE:
`/v3/snapshot/options/{underlying}` answers HTTP 200 with a
`{"status": "NOT_AUTHORIZED"}` body. So this lane carries NO bid/ask, NO
greeks and NO open interest, and it therefore CANNOT feed the M3 candidate
filter (abs_delta + open interest + spread) or the backtest's INV-11
executable prices. What it CAN measure is STRUCTURE, and that is all this
inspector reports — the `capability` block states the blockage in the
artifact itself so no downstream reader mistakes silence for zero.

Measured, per underlying and pooled, over a sequence of per-(underlying,
as_of) contract masters plus optional daily bars:

- universe size per as_of, ladder depth (distinct strikes) and grid width
  (max/min strike, plus spot units ONLY when `--spot-json` supplies a spot
  proxy — otherwise NOT_EVALUABLE, never a guessed spot);
- distinct expiries per as_of and a tenor histogram in calendar days,
  bucketed around the protocol's `option_candidate_defaults.dte_min/dte_max`
  (30/60), with the quarterly (third-Friday-of-quarter-month) and LEAPS
  (>365d) tails called out;
- exercise_style distribution; shares_per_contract distribution with an
  explicit NON_STANDARD list (!= 100, i.e. an adjusted / non-standard
  deliverable) and, across as_ofs, an ADJUSTMENT TIMELINE — every contract
  whose shares_per_contract changes and every root that appears or
  disappears. Contracts that vanish while still un-expired are surfaced
  separately as active delistings (the same adjusted-chain risk, seen from
  the other side);
- contract lifecycle: first_seen/last_seen per ticker across the as_of
  series plus births/deaths per as_of (deaths split into expiries and
  still-active disappearances);
- root stability: the OCC root parsed out of each ticker vs its
  `underlying_ticker`, counted per as_of. The M4-A shakedown found SPX/SPXW
  colliding on one (underlying, expiry, strike, C/P) cell, so any
  multi-root underlying is surfaced loudly;
- from bars: sessions observed, volume totals + nearest-rank distribution,
  and the IMPLIED SESSION CALENDAR (sorted distinct bar dates) with a gap
  list. Weekend gaps are expected and counted; WEEKDAY gaps are candidate
  market holidays OR simply uncovered sessions — the capture span is the
  only evidence this lane has, and the report says exactly that rather than
  asserting a holiday calendar.

Fail-loud discipline (house pattern, `scripts/inspect_options_coverage.py`):
every vendor field is read through `_field`, so a missing or null field
raises instead of degrading a metric; a `NOT_AUTHORIZED` body raises even
though it arrives at HTTP 200; a page chain whose last page still carries
`next_url` is reported as an INCOMPLETE capture rather than silently
under-counting the universe; duplicate tickers, duplicate (underlying,
as_of) masters, non-monotonic bar timestamps, an OCC ticker that disagrees
with its own `strike_price`/`expiration_date`/`contract_type`, and a daily
bar whose timestamp is not ET midnight all refuse.

Exactness: payloads are decoded with `json.loads(..., parse_float=Decimal)`,
so every vendor number keeps its RAW DECIMAL TEXT — `587.5` becomes
`Decimal("587.5")` built from the token, never from a float. A float that
reaches `_dec` (i.e. a caller decoded the payload without that hook) is
refused rather than coerced. Decimals are emitted to JSON as exponent-free
STRINGS so strikes round-trip exactly (this deliberately differs from the
Cboe coverage inspector, whose JSON floats carry ratios, not ladders).

Time: weekday/weekend logic runs through the sanctioned `time/` helpers
(`is_friday` probes plus `minus_calendar_days` with a negative offset ==
plus k days), mirroring `tree_options.data.cboe_eod._is_weekend`; session
dates come from `America/New_York` via `time.sessions.SESSION_TIMEZONE`.

No network, no API key: this tool reads CAPTURED payloads only. WS-D1's
client (`tree_options.data.massive_options`) lands concurrently and is
never imported — it is probed at RUNTIME purely to record provenance.

CLI:
    inspect_structural_coverage.py --contracts-json <path|dir>
        [--bars-json <path|dir>] [--spot-json <path>] --out-json <path>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

from tree_options.time.expiries import (
    is_friday,
    is_third_friday_of_quarter_month,
    minus_calendar_days,
)
from tree_options.time.sessions import SESSION_TIMEZONE

REPORT_VERSION = "m4-structural/1"
TIER = "massive-free/options-basic"
ADAPTER_MODULE = "tree_options.data.massive_options"

# research_protocol.yaml :: option_candidate_defaults (the only protocol
# numbers this lane can actually evaluate).
DTE_MIN = 30
DTE_MAX = 60
MIN_SAME_DAY_VOLUME = 100
STANDARD_SHARES_PER_CONTRACT = Decimal("100")

LEAPS_DAYS = 365
OCC_STRIKE_SCALE = Decimal(1000)
RATIO_PLACES = Decimal("0.000001")  # derived strike/spot quotients only
OCC_PATTERN = re.compile(r"^O:([A-Z][A-Z0-9]{0,5})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
OK_STATUSES = frozenset({"OK", "DELAYED"})
CONTRACT_TYPE_TO_OCC = {"call": "C", "put": "P"}
MISSING_BUCKET = "(missing)"

# Load-bearing: every metric below reads them, so absence refuses.
REQUIRED_CONTRACT_KEYS = (
    "ticker",
    "underlying_ticker",
    "expiration_date",
    "strike_price",
    "contract_type",
    "exercise_style",
    "shares_per_contract",
)
# Descriptive: recorded as distributions with an explicit "(missing)" bucket
# rather than refused, so a vendor that drops them still yields a report.
DESCRIPTIVE_CONTRACT_KEYS = ("cfi", "primary_exchange")
KNOWN_CONTRACT_KEYS = frozenset(REQUIRED_CONTRACT_KEYS + DESCRIPTIVE_CONTRACT_KEYS)

TENOR_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("dte_0_7", 0, 7),
    (f"dte_8_{DTE_MIN - 1}", 8, DTE_MIN - 1),
    (f"dte_{DTE_MIN}_{DTE_MAX}", DTE_MIN, DTE_MAX),  # THE protocol candidate band
    (f"dte_{DTE_MAX + 1}_180", DTE_MAX + 1, 180),
    (f"dte_181_{LEAPS_DAYS}", 181, LEAPS_DAYS),
    (f"dte_gt_{LEAPS_DAYS}", LEAPS_DAYS + 1, None),
)
BAND_BUCKET = f"dte_{DTE_MIN}_{DTE_MAX}"

WEEKDAY_GAP_NOTE = (
    "a weekday with no bar inside the observed span: a CANDIDATE market holiday "
    "OR an uncovered session — this lane's only evidence is the capture itself, "
    "so the report never asserts a holiday calendar"
)
SPOT_UNITS_NOTE = (
    "no spot proxy supplied (--spot-json): strike grid width in spot units is "
    "NOT_EVALUABLE — the free tier carries no underlying quote, and a guessed "
    "spot would be a fabricated denominator"
)
NO_QUOTES_NOTE = (
    "the free tier carries no bid/ask (quotes ride the NOT_AUTHORIZED snapshot endpoint)"
)
NO_GREEKS_NOTE = (
    "the free tier carries no greeks (delta rides the NOT_AUTHORIZED snapshot endpoint)"
)
NO_OI_NOTE = (
    "the free tier carries no open interest (it rides the NOT_AUTHORIZED snapshot endpoint)"
)
NO_EARNINGS_NOTE = (
    "no earnings-event source exists in this lane (same NOT_EVALUABLE discipline as M4-A)"
)
NO_UNDERLYING_DV_NOTE = (
    "underlying 20d median dollar volume needs the equity aggregates pull, which is "
    "a different endpoint than this option-contract/option-bar lane"
)

CAPABILITY: dict[str, Any] = {
    "tier": TIER,
    "vendor": "Massive (formerly Polygon; api.polygon.io endpoints)",
    "rate_limit": "5 requests/minute on the free tier (HTTP 429 beyond it)",
    "endpoints_entitled": [
        "/v3/reference/options/contracts?underlying_ticker=X&as_of=YYYY-MM-DD (PIT contract master)",
        "/v2/aggs/ticker/{optionTicker}/range/1/day/{from}/{to} (daily OHLCV bars)",
    ],
    "endpoints_not_entitled": [
        "/v3/snapshot/options/{underlying} -> HTTP 200 with body status=NOT_AUTHORIZED",
    ],
    "available": [
        "contract universe per as_of",
        "strike ladder depth and grid width",
        "expiry set and calendar-day tenor histogram",
        "exercise_style distribution",
        "shares_per_contract distribution + non-standard deliverable list",
        "contract lifecycle (first/last seen, births/deaths)",
        "root stability per underlying",
        "daily bar volume",
        "implied session calendar from bar dates",
    ],
    "unavailable": {
        "bid_ask_quotes": NO_QUOTES_NOTE,
        "greeks_delta": NO_GREEKS_NOTE,
        "open_interest": NO_OI_NOTE,
    },
    "protocol_filter_inputs": {
        "dte_min/dte_max": "SATISFIABLE (expiration_date - as_of, calendar days)",
        "standard_deliverable_only": "SATISFIABLE (shares_per_contract == 100)",
        "min_same_day_volume": "SATISFIABLE from daily bar volume (no intraday detail)",
        "abs_delta_min/abs_delta_max": "BLOCKED — " + NO_GREEKS_NOTE,
        "min_open_interest": "BLOCKED — " + NO_OI_NOTE,
        "max_spread_fraction_of_midpoint": "BLOCKED — " + NO_QUOTES_NOTE,
        "min_underlying_20d_median_dollar_volume": "BLOCKED — " + NO_UNDERLYING_DV_NOTE,
        "exclude_earnings_spanning_hold": "BLOCKED — " + NO_EARNINGS_NOTE,
    },
    "blocked_lanes": [
        "M3 candidate filter (needs abs_delta + open_interest + spread fraction)",
        "backtest INV-11 executable prices (needs bid/ask at the decision instant)",
    ],
}


class StructuralCoverageError(ValueError):
    """Every refusal in this module — a metric never degrades to None."""


# ---- vendor payload decoding (raw decimal text, fail-loud) -------------------


def decode_payload(text: str, *, source: str) -> dict[str, Any]:
    """`json.loads` with `parse_float=Decimal`: every vendor number keeps its
    RAW token text, so `587.5` is `Decimal("587.5")` and never a float."""
    try:
        payload = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise StructuralCoverageError(f"{source}: not valid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise StructuralCoverageError(
            f"{source}: top-level JSON is {type(payload).__name__}, not an object"
        )
    return payload


def _field(payload: Mapping[str, Any], key: str, where: str) -> Any:
    """Loud field access: missing or null refuses — no metric degrades."""
    if key not in payload:
        raise StructuralCoverageError(
            f"{where}: missing required field {key!r} — refusing to degrade the metric"
        )
    value = payload[key]
    if value is None:
        raise StructuralCoverageError(
            f"{where}: field {key!r} is null — refusing to degrade the metric"
        )
    return value


def _text(payload: Mapping[str, Any], key: str, where: str) -> str:
    value = _field(payload, key, where)
    if not isinstance(value, str) or not value:
        raise StructuralCoverageError(
            f"{where}: field {key!r} is not a non-empty string ({value!r})"
        )
    return value


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return None
    return value


def _dec(value: Any, where: str) -> Decimal:
    """int/Decimal/str -> Decimal. A float refuses: it means the caller decoded
    without `parse_float=Decimal` and the exact vendor text is already lost."""
    if isinstance(value, bool):
        raise StructuralCoverageError(f"{where}: boolean {value!r} is not a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise StructuralCoverageError(f"{where}: {value!r} is not a decimal") from exc
    if isinstance(value, float):
        raise StructuralCoverageError(
            f"{where}: float {value!r} — decode with decode_payload() "
            "(parse_float=Decimal); refusing to coerce a float into a price"
        )
    raise StructuralCoverageError(f"{where}: unusable numeric {value!r}")


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StructuralCoverageError(f"{where}: {value!r} is not an integer")
    return value


def _as_date(value: Any, where: str) -> date:
    if not isinstance(value, str):
        raise StructuralCoverageError(f"{where}: {value!r} is not an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise StructuralCoverageError(f"{where}: {value!r} is not an ISO date") from exc


def require_ok_status(payload: Mapping[str, Any], *, where: str) -> None:
    """The not-entitled answer arrives at HTTP 200 with a status FIELD, so the
    body is the only place the refusal can be detected."""
    status = payload.get("status")
    if status is None:
        return
    if status == "NOT_AUTHORIZED":
        message = payload.get("message") or "(no message)"
        raise StructuralCoverageError(
            f"{where}: vendor answered NOT_AUTHORIZED at HTTP 200 — {message}; "
            f"this tier ({TIER}) is not entitled to that endpoint and the body carries no data"
        )
    if status not in OK_STATUSES:
        raise StructuralCoverageError(f"{where}: vendor status {status!r} is not OK")


def _plain(value: Decimal) -> str:
    """Exponent-free decimal text ('100', never '1E+2')."""
    return format(value.normalize(), "f")


def _is_weekend(d: date) -> bool:
    """Sat/Sun via Friday probes — the sanctioned `time/` helpers only, mirroring
    `tree_options.data.cboe_eod._is_weekend` (no weekday()/timedelta here)."""
    return is_friday(minus_calendar_days(d, 1)) or is_friday(minus_calendar_days(d, 2))


def _between(a: date, b: date) -> Iterator[date]:
    """The calendar days strictly between a and b (a < b)."""
    for k in range(1, (b - a).days):
        yield minus_calendar_days(a, -k)  # -k == plus k days


# ---- OCC ticker ---------------------------------------------------------------


@dataclass(frozen=True)
class OccTicker:
    ticker: str
    root: str
    expiration: date
    call_put: str
    strike: Decimal


def parse_occ_ticker(ticker: str, *, where: str = "ticker") -> OccTicker:
    """`O:SPY250314C00560000` -> root SPY, 2025-03-14, C, strike 560.

    Strike comes from the 8-digit field divided by 1000 in exact Decimal
    arithmetic; the 2-digit year is 20YY (the vendor's OCC-21 encoding).
    """
    match = OCC_PATTERN.match(ticker)
    if match is None:
        raise StructuralCoverageError(
            f"{where}: {ticker!r} is not an OCC option ticker "
            "(expected O:<root><yymmdd><C|P><8-digit strike x1000>)"
        )
    root, yy, mm, dd, call_put, strike_digits = match.groups()
    try:
        expiration = date(2000 + int(yy), int(mm), int(dd))
    except ValueError as exc:
        raise StructuralCoverageError(f"{where}: {ticker!r} encodes an impossible date") from exc
    strike = Decimal(int(strike_digits)) / OCC_STRIKE_SCALE
    return OccTicker(
        ticker=ticker, root=root, expiration=expiration, call_put=call_put, strike=strike
    )


# ---- captured shapes ----------------------------------------------------------


@dataclass(frozen=True)
class VendorContract:
    ticker: str
    underlying_ticker: str
    root: str
    contract_type: str
    exercise_style: str
    expiration: date
    strike: Decimal
    shares_per_contract: Decimal
    cfi: str | None
    primary_exchange: str | None


@dataclass(frozen=True)
class ContractMaster:
    underlying: str
    as_of: date
    source: str
    contracts: tuple[VendorContract, ...]
    pages: int
    capture_complete: bool
    unknown_result_keys: tuple[str, ...]


@dataclass(frozen=True)
class Bar:
    session: date
    epoch_ms: int
    volume: Decimal
    vwap: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    transactions: int | None


@dataclass(frozen=True)
class BarSeries:
    ticker: str
    root: str
    source: str
    adjusted: bool | None
    bars: tuple[Bar, ...]


def parse_contract(result: Mapping[str, Any], *, where: str) -> VendorContract:
    """One `/v3/reference/options/contracts` result row.

    The OCC ticker is cross-checked against `expiration_date`,
    `strike_price` and `contract_type`: a disagreement is vendor corruption
    (or a ticker this parser does not understand) and refuses rather than
    silently picking one side.
    """
    ticker = _text(result, "ticker", where)
    occ = parse_occ_ticker(ticker, where=where)
    expiration = _as_date(_field(result, "expiration_date", where), f"{where}.expiration_date")
    strike = _dec(_field(result, "strike_price", where), f"{where}.strike_price")
    contract_type = _text(result, "contract_type", where)
    if contract_type not in CONTRACT_TYPE_TO_OCC:
        raise StructuralCoverageError(
            f"{where}: contract_type {contract_type!r} is neither 'call' nor 'put'"
        )
    if occ.expiration != expiration:
        raise StructuralCoverageError(
            f"{where}: ticker {ticker!r} encodes expiry {occ.expiration} but "
            f"expiration_date says {expiration}"
        )
    if occ.strike != strike:
        raise StructuralCoverageError(
            f"{where}: ticker {ticker!r} encodes strike {_plain(occ.strike)} but "
            f"strike_price says {_plain(strike)}"
        )
    if occ.call_put != CONTRACT_TYPE_TO_OCC[contract_type]:
        raise StructuralCoverageError(
            f"{where}: ticker {ticker!r} encodes {occ.call_put} but "
            f"contract_type says {contract_type!r}"
        )
    return VendorContract(
        ticker=ticker,
        underlying_ticker=_text(result, "underlying_ticker", where),
        root=occ.root,
        contract_type=contract_type,
        exercise_style=_text(result, "exercise_style", where),
        expiration=expiration,
        strike=strike,
        shares_per_contract=_dec(
            _field(result, "shares_per_contract", where), f"{where}.shares_per_contract"
        ),
        cfi=_optional_text(result, "cfi"),
        primary_exchange=_optional_text(result, "primary_exchange"),
    )


def _pages_of(payload: Mapping[str, Any], *, source: str) -> tuple[list[Mapping[str, Any]], bool]:
    """The vendor response(s) inside a capture, plus completeness.

    A capture is either one vendor response or an envelope with a `pages`
    list of them. `next_url` on any page but the last means the chain is
    broken (refuse); on the LAST page it means the capture stopped early —
    reported as an incomplete capture, never silently under-counted.
    """
    raw_pages = payload.get("pages")
    if raw_pages is None:
        pages: list[Mapping[str, Any]] = [payload]
    elif isinstance(raw_pages, list) and raw_pages:
        for index, page in enumerate(raw_pages):
            if not isinstance(page, dict):
                raise StructuralCoverageError(f"{source}: pages[{index}] is not an object")
        pages = list(raw_pages)
    else:
        raise StructuralCoverageError(f"{source}: 'pages' must be a non-empty list of responses")
    for index, page in enumerate(pages[:-1]):
        if not page.get("next_url"):
            raise StructuralCoverageError(
                f"{source}: pages[{index}] has no next_url but is not the last page — broken chain"
            )
    return pages, not pages[-1].get("next_url")


def parse_contract_master(
    payload: Mapping[str, Any], *, source: str, as_of: date | None = None
) -> ContractMaster:
    """One captured contract master == one (underlying, as_of) pair.

    The vendor body does NOT carry `as_of` (it is a request parameter), so
    the capture envelope must supply it — either an `as_of` key or an
    ISO date in the filename. Neither present refuses.
    """
    pages, capture_complete = _pages_of(payload, source=source)
    envelope_as_of = payload.get("as_of")
    if envelope_as_of is not None:
        as_of = _as_date(envelope_as_of, f"{source}.as_of")
    if as_of is None:
        raise StructuralCoverageError(
            f"{source}: cannot determine as_of — add an 'as_of' key to the capture "
            "envelope or put an ISO date in the filename (e.g. SPY_2025-03-07.json)"
        )

    contracts: list[VendorContract] = []
    unknown: set[str] = set()
    seen: set[str] = set()
    for page_index, page in enumerate(pages):
        require_ok_status(page, where=f"{source} page {page_index}")
        results = page.get("results")
        if not isinstance(results, list):
            raise StructuralCoverageError(
                f"{source} page {page_index}: 'results' is missing or not a list"
            )
        for row_index, result in enumerate(results):
            where = f"{source} page {page_index} result {row_index}"
            if not isinstance(result, dict):
                raise StructuralCoverageError(f"{where}: not an object")
            unknown |= set(result) - KNOWN_CONTRACT_KEYS
            contract = parse_contract(result, where=where)
            if contract.ticker in seen:
                raise StructuralCoverageError(
                    f"{where}: duplicate ticker {contract.ticker!r} in one PIT master"
                )
            seen.add(contract.ticker)
            contracts.append(contract)

    if not contracts:
        raise StructuralCoverageError(
            f"{source}: contract master is empty — refusing (an empty universe is a "
            "capture failure, not a structural fact)"
        )
    underlyings = {c.underlying_ticker for c in contracts}
    declared = payload.get("underlying_ticker")
    if isinstance(declared, str) and declared:
        underlyings.add(declared)
    if len(underlyings) != 1:
        raise StructuralCoverageError(
            f"{source}: {sorted(underlyings)} underlyings in one master — this inspector "
            "consumes ONE master per (underlying, as_of)"
        )
    for contract in contracts:
        if contract.expiration < as_of:
            raise StructuralCoverageError(
                f"{source}: {contract.ticker} expired {contract.expiration} before as_of "
                f"{as_of} — a PIT master cannot list an already-expired contract"
            )
    return ContractMaster(
        underlying=underlyings.pop(),
        as_of=as_of,
        source=source,
        contracts=tuple(sorted(contracts, key=lambda c: c.ticker)),
        pages=len(pages),
        capture_complete=capture_complete,
        unknown_result_keys=tuple(sorted(unknown)),
    )


def bar_session(epoch_ms: int, *, where: str) -> date:
    """The session date of a daily bar timestamp (ms epoch UTC of ET midnight).

    Anything other than 00:00 America/New_York means the aggregation window
    changed and the derived session date would be a guess — refuse.
    """
    if epoch_ms % 1000 != 0:
        raise StructuralCoverageError(f"{where}: bar t={epoch_ms} is not a whole second")
    local = datetime.fromtimestamp(epoch_ms // 1000, tz=UTC).astimezone(SESSION_TIMEZONE)
    if (local.hour, local.minute, local.second) != (0, 0, 0):
        raise StructuralCoverageError(
            f"{where}: bar t={epoch_ms} is {local:%H:%M:%S} America/New_York, not the "
            "session's ET midnight — the daily aggregation window is not what this lane assumes"
        )
    return local.date()


def parse_bar_series(
    payload: Mapping[str, Any], *, source: str, ticker: str | None = None
) -> BarSeries:
    """One `/v2/aggs/ticker/{optionTicker}/range/1/day/...` capture."""
    require_ok_status(payload, where=source)
    declared = payload.get("ticker")
    if isinstance(declared, str) and declared:
        ticker = declared
    if ticker is None:
        raise StructuralCoverageError(
            f"{source}: cannot determine the option ticker — the vendor body carries "
            "'ticker'; supply it in the capture envelope"
        )
    occ = parse_occ_ticker(ticker, where=f"{source}.ticker")
    results = payload.get("results")
    if not isinstance(results, list):
        raise StructuralCoverageError(f"{source}: 'results' is missing or not a list")
    declared_count = payload.get("resultsCount")
    if declared_count is not None and _int(declared_count, f"{source}.resultsCount") != len(
        results
    ):
        raise StructuralCoverageError(
            f"{source}: resultsCount={declared_count} but {len(results)} results — truncated capture"
        )
    bars: list[Bar] = []
    previous_ms: int | None = None
    for index, row in enumerate(results):
        where = f"{source} bar {index}"
        if not isinstance(row, dict):
            raise StructuralCoverageError(f"{where}: not an object")
        epoch_ms = _int(_field(row, "t", where), f"{where}.t")
        if previous_ms is not None and epoch_ms <= previous_ms:
            raise StructuralCoverageError(
                f"{where}: t={epoch_ms} is not strictly after the previous bar ({previous_ms})"
            )
        previous_ms = epoch_ms
        bars.append(
            Bar(
                session=bar_session(epoch_ms, where=where),
                epoch_ms=epoch_ms,
                volume=_dec(_field(row, "v", where), f"{where}.v"),
                vwap=_optional_dec(row, "vw", where),
                open=_optional_dec(row, "o", where),
                high=_optional_dec(row, "h", where),
                low=_optional_dec(row, "l", where),
                close=_optional_dec(row, "c", where),
                transactions=None if row.get("n") is None else _int(row["n"], f"{where}.n"),
            )
        )
    if not bars:
        raise StructuralCoverageError(
            f"{source}: bar series is empty — refusing (no silent empty coverage)"
        )
    adjusted = payload.get("adjusted")
    return BarSeries(
        ticker=ticker,
        root=occ.root,
        source=source,
        adjusted=adjusted if isinstance(adjusted, bool) else None,
        bars=tuple(bars),
    )


def _optional_dec(row: Mapping[str, Any], key: str, where: str) -> Decimal | None:
    value = row.get(key)
    return None if value is None else _dec(value, f"{where}.{key}")


# ---- loaders ------------------------------------------------------------------


def _json_files(path: Path, *, what: str) -> tuple[Path, ...]:
    if path.is_dir():
        files = tuple(sorted(p for p in path.glob("*.json") if p.is_file()))
        if not files:
            raise StructuralCoverageError(f"{what}: no *.json files in {path}")
        return files
    if path.is_file():
        return (path,)
    raise StructuralCoverageError(f"{what}: not a file or directory: {path}")


def _as_of_from_name(path: Path) -> date | None:
    matches = ISO_DATE_PATTERN.findall(path.stem)
    if not matches:
        return None
    if len(set(matches)) != 1:
        raise StructuralCoverageError(
            f"{path.name}: {sorted(set(matches))} ISO dates in the filename — ambiguous as_of"
        )
    return date.fromisoformat(matches[0])


def load_contract_masters(path: Path) -> tuple[ContractMaster, ...]:
    """Load one capture file or every *.json in a directory (sorted)."""
    masters = [
        parse_contract_master(
            decode_payload(file.read_text(encoding="utf-8"), source=file.name),
            source=file.name,
            as_of=_as_of_from_name(file),
        )
        for file in _json_files(path, what="--contracts-json")
    ]
    return tuple(sorted(masters, key=lambda m: (m.underlying, m.as_of, m.source)))


def load_bar_series(path: Path) -> tuple[BarSeries, ...]:
    series = [
        parse_bar_series(
            decode_payload(file.read_text(encoding="utf-8"), source=file.name), source=file.name
        )
        for file in _json_files(path, what="--bars-json")
    ]
    return tuple(sorted(series, key=lambda s: (s.ticker, s.source)))


def load_spot_proxy(path: Path) -> dict[str, dict[date, Decimal]]:
    """`{"SPY": {"2025-03-07": "560.12"}}` or `{"SPY": "560.12"}` (all as_ofs).

    A spot proxy is DECLARED INPUT, never derived: this tier has no
    underlying quote, and the report says NOT_EVALUABLE without one.
    """
    payload = decode_payload(path.read_text(encoding="utf-8"), source=path.name)
    proxy: dict[str, dict[date, Decimal]] = {}
    for underlying, value in payload.items():
        where = f"{path.name}[{underlying!r}]"
        if isinstance(value, dict):
            proxy[underlying] = {
                _as_date(as_of, where): _dec(spot, f"{where}[{as_of!r}]")
                for as_of, spot in value.items()
            }
        else:
            proxy[underlying] = {}
            proxy[underlying][date.min] = _dec(value, where)
    for underlying, spots in proxy.items():
        for as_of, spot in spots.items():
            if spot <= 0:
                raise StructuralCoverageError(
                    f"{path.name}: spot proxy for {underlying} {as_of} is {_plain(spot)} <= 0"
                )
    return proxy


# ---- report shapes ------------------------------------------------------------


@dataclass(frozen=True)
class StrikeGrid:
    depth: int
    min_strike: Decimal
    max_strike: Decimal
    span: Decimal
    spot: Decimal | None
    min_in_spot_units: Decimal | None
    max_in_spot_units: Decimal | None
    span_in_spot_units: Decimal | None


@dataclass(frozen=True)
class TenorHistogram:
    buckets: tuple[tuple[str, int], ...]
    nearest_days: int
    farthest_days: int
    band_expiries: int
    quarterly_expiries: int
    leaps_expiries: int


@dataclass(frozen=True)
class AsOfSlice:
    underlying: str
    as_of: date
    source: str
    universe_size: int
    capture_complete: bool
    strikes: StrikeGrid
    distinct_expiries: int
    expiries: tuple[date, ...]
    tenors: TenorHistogram
    contract_type_counts: tuple[tuple[str, int], ...]
    exercise_style_counts: tuple[tuple[str, int], ...]
    shares_per_contract_counts: tuple[tuple[str, int], ...]
    non_standard: tuple[str, ...]
    cfi_counts: tuple[tuple[str, int], ...]
    primary_exchange_counts: tuple[tuple[str, int], ...]
    root_counts: tuple[tuple[str, int], ...]
    multi_root: bool
    births: int | None
    deaths: int | None
    born: tuple[str, ...]
    died_expired: tuple[str, ...]
    died_active: tuple[str, ...]


@dataclass(frozen=True)
class LifecycleEntry:
    ticker: str
    first_seen: date
    last_seen: date
    as_of_count: int


@dataclass(frozen=True)
class Lifecycle:
    as_ofs: int
    distinct_contracts: int
    full_span: int
    entries: tuple[LifecycleEntry, ...]


@dataclass(frozen=True)
class AdjustmentEvent:
    kind: str
    underlying: str
    as_of: date
    previous_as_of: date
    ticker: str | None
    root: str | None
    before: str | None
    after: str | None


@dataclass(frozen=True)
class VolumeStats:
    bars: int
    total: Decimal
    minimum: Decimal | None
    median: Decimal | None
    p90: Decimal | None
    maximum: Decimal | None
    zero_volume_bars: int
    bars_at_or_above_min: int


@dataclass(frozen=True)
class SessionCalendar:
    sessions: tuple[date, ...]
    first: date | None
    last: date | None
    calendar_days_spanned: int
    weekday_gaps: tuple[date, ...]
    weekend_days_skipped: int


@dataclass(frozen=True)
class BarCoverage:
    series: int
    tickers: tuple[str, ...]
    volume: VolumeStats
    calendar: SessionCalendar


@dataclass(frozen=True)
class UnderlyingStructure:
    underlying: str
    as_ofs: tuple[date, ...]
    slices: tuple[AsOfSlice, ...]
    roots: tuple[str, ...]
    multi_root: bool
    lifecycle: Lifecycle
    adjustments: tuple[AdjustmentEvent, ...]
    active_delistings: tuple[tuple[date, str], ...]
    exercise_style_counts: tuple[tuple[str, int], ...]
    shares_per_contract_counts: tuple[tuple[str, int], ...]
    non_standard_rows: int
    contract_rows: int
    bars: BarCoverage | None


@dataclass(frozen=True)
class StructuralReport:
    report_version: str
    tier: str
    adapter_status: str
    contract_sources: tuple[str, ...]
    bar_sources: tuple[str, ...]
    incomplete_captures: tuple[str, ...]
    unknown_result_keys: tuple[str, ...]
    spot_proxy_supplied: bool
    underlyings: tuple[UnderlyingStructure, ...]
    masters: int
    as_ofs: tuple[date, ...]
    contract_rows: int
    distinct_contracts: int
    contract_type_counts: tuple[tuple[str, int], ...]
    exercise_style_counts: tuple[tuple[str, int], ...]
    shares_per_contract_counts: tuple[tuple[str, int], ...]
    non_standard_rows: int
    multi_root_underlyings: tuple[str, ...]
    adjustment_events: int
    active_delistings: int
    bars: BarCoverage | None
    unmatched_bar_tickers: tuple[str, ...]


# ---- statistics helpers -------------------------------------------------------


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _p90(values: Sequence[Decimal]) -> Decimal | None:
    """Nearest-rank 90th percentile: the ceil(0.90*n)-th smallest value."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    rank = (9 * n + 9) // 10  # == ceil(0.9 * n) in exact integer arithmetic
    return ordered[max(0, min(rank - 1, n - 1))]


def _counts(values: Iterable[str]) -> tuple[tuple[str, int], ...]:
    tally: dict[str, int] = {}
    for value in values:
        tally[value] = tally.get(value, 0) + 1
    return tuple(sorted(tally.items()))


def _decimal_counts(values: Iterable[Decimal]) -> tuple[tuple[str, int], ...]:
    tally: dict[str, int] = {}
    for value in values:
        key = _plain(value)
        tally[key] = tally.get(key, 0) + 1
    return tuple(sorted(tally.items(), key=lambda item: Decimal(item[0])))


def _merge_counts(
    parts: Iterable[tuple[tuple[str, int], ...]], *, numeric: bool = False
) -> tuple[tuple[str, int], ...]:
    tally: dict[str, int] = {}
    for part in parts:
        for key, count in part:
            tally[key] = tally.get(key, 0) + count
    if numeric:
        return tuple(sorted(tally.items(), key=lambda item: Decimal(item[0])))
    return tuple(sorted(tally.items()))


# ---- engine -------------------------------------------------------------------


def _strike_grid(contracts: Sequence[VendorContract], spot: Decimal | None) -> StrikeGrid:
    strikes = sorted({c.strike for c in contracts})
    low, high = strikes[0], strikes[-1]
    span = high - low
    return StrikeGrid(
        depth=len(strikes),
        min_strike=low,
        max_strike=high,
        span=span,
        spot=spot,
        min_in_spot_units=None if spot is None else low / spot,
        max_in_spot_units=None if spot is None else high / spot,
        span_in_spot_units=None if spot is None else span / spot,
    )


def _tenors(contracts: Sequence[VendorContract], as_of: date) -> TenorHistogram:
    # Calendar-day DTE, the house idiom for option tenors
    # (src/tree_options/options/strategy.py, candidates/filters.py).
    dtes = [(c.expiration - as_of).days for c in contracts]
    buckets: list[tuple[str, int]] = []
    for name, low, high in TENOR_BUCKETS:
        buckets.append(
            (name, sum(1 for dte in dtes if dte >= low and (high is None or dte <= high)))
        )
    if sum(count for _name, count in buckets) != len(dtes):
        raise StructuralCoverageError(  # pragma: no cover - buckets partition [0, inf)
            f"{as_of}: tenor buckets do not partition the universe"
        )
    expiries = sorted({c.expiration for c in contracts})
    return TenorHistogram(
        buckets=tuple(buckets),
        nearest_days=min(dtes),
        farthest_days=max(dtes),
        band_expiries=sum(1 for e in expiries if DTE_MIN <= (e - as_of).days <= DTE_MAX),
        quarterly_expiries=sum(1 for e in expiries if is_third_friday_of_quarter_month(e)),
        leaps_expiries=sum(1 for e in expiries if (e - as_of).days > LEAPS_DAYS),
    )


def _describe(values: Iterable[str | None]) -> tuple[tuple[str, int], ...]:
    return _counts(MISSING_BUCKET if v is None else v for v in values)


def _slice(
    master: ContractMaster,
    previous: ContractMaster | None,
    spot: Decimal | None,
) -> AsOfSlice:
    contracts = master.contracts
    by_ticker = {c.ticker: c for c in contracts}
    roots = _counts(c.root for c in contracts)
    if previous is None:
        births: int | None = None
        deaths: int | None = None
        born: tuple[str, ...] = ()
        died_expired: tuple[str, ...] = ()
        died_active: tuple[str, ...] = ()
    else:
        prior = {c.ticker: c for c in previous.contracts}
        born = tuple(sorted(set(by_ticker) - set(prior)))
        gone = sorted(set(prior) - set(by_ticker))
        died_expired = tuple(t for t in gone if prior[t].expiration <= master.as_of)
        died_active = tuple(t for t in gone if prior[t].expiration > master.as_of)
        births = len(born)
        deaths = len(gone)
    return AsOfSlice(
        underlying=master.underlying,
        as_of=master.as_of,
        source=master.source,
        universe_size=len(contracts),
        capture_complete=master.capture_complete,
        strikes=_strike_grid(contracts, spot),
        distinct_expiries=len({c.expiration for c in contracts}),
        expiries=tuple(sorted({c.expiration for c in contracts})),
        tenors=_tenors(contracts, master.as_of),
        contract_type_counts=_counts(c.contract_type for c in contracts),
        exercise_style_counts=_counts(c.exercise_style for c in contracts),
        shares_per_contract_counts=_decimal_counts(c.shares_per_contract for c in contracts),
        non_standard=tuple(
            sorted(
                c.ticker for c in contracts if c.shares_per_contract != STANDARD_SHARES_PER_CONTRACT
            )
        ),
        cfi_counts=_describe(c.cfi for c in contracts),
        primary_exchange_counts=_describe(c.primary_exchange for c in contracts),
        root_counts=roots,
        multi_root=len(roots) > 1,
        births=births,
        deaths=deaths,
        born=born,
        died_expired=died_expired,
        died_active=died_active,
    )


def _adjustments(masters: Sequence[ContractMaster]) -> tuple[AdjustmentEvent, ...]:
    """shares_per_contract restatements + roots appearing/disappearing.

    This IS the adjusted-chain risk the M3 plan flagged: a deliverable that
    changes under a stable ticker, or a root that shows up / vanishes
    between two point-in-time masters.
    """
    events: list[AdjustmentEvent] = []
    for previous, current in pairwise(masters):
        prior = {c.ticker: c for c in previous.contracts}
        now = {c.ticker: c for c in current.contracts}
        for ticker in sorted(set(prior) & set(now)):
            before, after = prior[ticker].shares_per_contract, now[ticker].shares_per_contract
            if before != after:
                events.append(
                    AdjustmentEvent(
                        kind="shares_per_contract_change",
                        underlying=current.underlying,
                        as_of=current.as_of,
                        previous_as_of=previous.as_of,
                        ticker=ticker,
                        root=now[ticker].root,
                        before=_plain(before),
                        after=_plain(after),
                    )
                )
        prior_roots = {c.root for c in previous.contracts}
        now_roots = {c.root for c in current.contracts}
        for root in sorted(now_roots - prior_roots):
            events.append(
                AdjustmentEvent(
                    kind="root_appeared",
                    underlying=current.underlying,
                    as_of=current.as_of,
                    previous_as_of=previous.as_of,
                    ticker=None,
                    root=root,
                    before=None,
                    after=root,
                )
            )
        for root in sorted(prior_roots - now_roots):
            events.append(
                AdjustmentEvent(
                    kind="root_disappeared",
                    underlying=current.underlying,
                    as_of=current.as_of,
                    previous_as_of=previous.as_of,
                    ticker=None,
                    root=root,
                    before=root,
                    after=None,
                )
            )
    return tuple(events)


def _lifecycle(masters: Sequence[ContractMaster]) -> Lifecycle:
    first: dict[str, date] = {}
    last: dict[str, date] = {}
    count: dict[str, int] = {}
    for master in masters:
        for contract in master.contracts:
            first.setdefault(contract.ticker, master.as_of)
            last[contract.ticker] = master.as_of
            count[contract.ticker] = count.get(contract.ticker, 0) + 1
    entries = tuple(
        LifecycleEntry(
            ticker=ticker,
            first_seen=first[ticker],
            last_seen=last[ticker],
            as_of_count=count[ticker],
        )
        for ticker in sorted(first)
    )
    return Lifecycle(
        as_ofs=len(masters),
        distinct_contracts=len(entries),
        full_span=sum(1 for e in entries if e.as_of_count == len(masters)),
        entries=entries,
    )


def _volume_stats(bars: Sequence[Bar]) -> VolumeStats:
    volumes = [bar.volume for bar in bars]
    return VolumeStats(
        bars=len(volumes),
        total=sum(volumes, Decimal(0)),
        minimum=min(volumes) if volumes else None,
        median=_median(volumes),
        p90=_p90(volumes),
        maximum=max(volumes) if volumes else None,
        zero_volume_bars=sum(1 for v in volumes if v == 0),
        bars_at_or_above_min=sum(1 for v in volumes if v >= MIN_SAME_DAY_VOLUME),
    )


def _session_calendar(bars: Iterable[Bar]) -> SessionCalendar:
    """The implied session calendar: sorted distinct bar dates plus the gaps.

    Weekend days inside the span are expected and counted; weekday gaps are
    candidate market holidays OR uncovered sessions (see WEEKDAY_GAP_NOTE).
    """
    sessions = tuple(sorted({bar.session for bar in bars}))
    if not sessions:
        return SessionCalendar(
            sessions=(),
            first=None,
            last=None,
            calendar_days_spanned=0,
            weekday_gaps=(),
            weekend_days_skipped=0,
        )
    weekday_gaps: list[date] = []
    weekend = 0
    for earlier, later in pairwise(sessions):
        for day in _between(earlier, later):
            if _is_weekend(day):
                weekend += 1
            else:
                weekday_gaps.append(day)
    return SessionCalendar(
        sessions=sessions,
        first=sessions[0],
        last=sessions[-1],
        calendar_days_spanned=(sessions[-1] - sessions[0]).days + 1,
        weekday_gaps=tuple(weekday_gaps),
        weekend_days_skipped=weekend,
    )


def _bar_coverage(series: Sequence[BarSeries]) -> BarCoverage | None:
    if not series:
        return None
    bars = [bar for s in series for bar in s.bars]
    return BarCoverage(
        series=len(series),
        tickers=tuple(sorted(s.ticker for s in series)),
        volume=_volume_stats(bars),
        calendar=_session_calendar(bars),
    )


def _spot_for(
    spot_proxy: Mapping[str, Mapping[date, Decimal]] | None, underlying: str, as_of: date
) -> Decimal | None:
    if spot_proxy is None:
        return None
    per_underlying = spot_proxy.get(underlying)
    if not per_underlying:
        return None
    if as_of in per_underlying:
        return per_underlying[as_of]
    return per_underlying.get(date.min)


def _attribute_bars(
    series: Sequence[BarSeries], masters: Sequence[ContractMaster]
) -> tuple[dict[str, list[BarSeries]], tuple[str, ...]]:
    """Bars -> underlying by exact ticker first, then by the underlying's ROOT
    set (so SPXW bars land on SPX). Unmatched series are excluded from every
    metric and named in the report — counted, never silently dropped."""
    ticker_owner: dict[str, str] = {}
    root_owner: dict[str, set[str]] = {}
    for master in masters:
        for contract in master.contracts:
            ticker_owner[contract.ticker] = master.underlying
            root_owner.setdefault(contract.root, set()).add(master.underlying)
    matched: dict[str, list[BarSeries]] = {}
    unmatched: list[str] = []
    for one in series:
        owner = ticker_owner.get(one.ticker)
        if owner is None:
            owners = root_owner.get(one.root, set())
            owner = owners.pop() if len(owners) == 1 else None
        if owner is None:
            unmatched.append(one.ticker)
            continue
        matched.setdefault(owner, []).append(one)
    return matched, tuple(sorted(unmatched))


def build_structural_report(
    masters: Sequence[ContractMaster],
    *,
    bars: Sequence[BarSeries] = (),
    spot_proxy: Mapping[str, Mapping[date, Decimal]] | None = None,
    adapter_status: str = "NOT_PROBED",
) -> StructuralReport:
    """Structural coverage over a sequence of per-(underlying, as_of) masters."""
    if not masters:
        raise StructuralCoverageError("no contract masters supplied — nothing to inspect")
    by_underlying: dict[str, list[ContractMaster]] = {}
    for master in masters:
        by_underlying.setdefault(master.underlying, []).append(master)
    for underlying, group in by_underlying.items():
        group.sort(key=lambda m: m.as_of)
        seen: set[date] = set()
        for master in group:
            if master.as_of in seen:
                raise StructuralCoverageError(
                    f"{underlying}: two masters for as_of {master.as_of} "
                    f"({master.source}) — one master per (underlying, as_of)"
                )
            seen.add(master.as_of)

    matched_bars, unmatched = _attribute_bars(bars, masters)

    structures: list[UnderlyingStructure] = []
    for underlying, group in sorted(by_underlying.items()):
        slices = tuple(
            _slice(
                master,
                group[index - 1] if index else None,
                _spot_for(spot_proxy, underlying, master.as_of),
            )
            for index, master in enumerate(group)
        )
        roots = tuple(sorted({c.root for m in group for c in m.contracts}))
        structures.append(
            UnderlyingStructure(
                underlying=underlying,
                as_ofs=tuple(m.as_of for m in group),
                slices=slices,
                roots=roots,
                multi_root=len(roots) > 1,
                lifecycle=_lifecycle(group),
                adjustments=_adjustments(group),
                active_delistings=tuple(
                    (s.as_of, ticker) for s in slices for ticker in s.died_active
                ),
                exercise_style_counts=_merge_counts(s.exercise_style_counts for s in slices),
                shares_per_contract_counts=_merge_counts(
                    (s.shares_per_contract_counts for s in slices), numeric=True
                ),
                non_standard_rows=sum(len(s.non_standard) for s in slices),
                contract_rows=sum(s.universe_size for s in slices),
                bars=_bar_coverage(matched_bars.get(underlying, [])),
            )
        )

    all_bars = [one for group in matched_bars.values() for one in group]
    return StructuralReport(
        report_version=REPORT_VERSION,
        tier=TIER,
        adapter_status=adapter_status,
        contract_sources=tuple(sorted(m.source for m in masters)),
        bar_sources=tuple(sorted(one.source for one in bars)),
        incomplete_captures=tuple(sorted(m.source for m in masters if not m.capture_complete)),
        unknown_result_keys=tuple(sorted({k for m in masters for k in m.unknown_result_keys})),
        spot_proxy_supplied=spot_proxy is not None,
        underlyings=tuple(structures),
        masters=len(masters),
        as_ofs=tuple(sorted({m.as_of for m in masters})),
        contract_rows=sum(s.contract_rows for s in structures),
        distinct_contracts=len({c.ticker for m in masters for c in m.contracts}),
        contract_type_counts=_merge_counts(
            s.contract_type_counts for structure in structures for s in structure.slices
        ),
        exercise_style_counts=_merge_counts(s.exercise_style_counts for s in structures),
        shares_per_contract_counts=_merge_counts(
            (s.shares_per_contract_counts for s in structures), numeric=True
        ),
        non_standard_rows=sum(s.non_standard_rows for s in structures),
        multi_root_underlyings=tuple(s.underlying for s in structures if s.multi_root),
        adjustment_events=sum(len(s.adjustments) for s in structures),
        active_delistings=sum(len(s.active_delistings) for s in structures),
        bars=_bar_coverage(all_bars),
        unmatched_bar_tickers=unmatched,
    )


# ---- machine JSON -------------------------------------------------------------


def _d(value: Decimal | None) -> str | None:
    """Decimals travel as exponent-free STRINGS: a strike ladder must
    round-trip exactly, and float() would not."""
    return None if value is None else _plain(value)


def _ratio(value: Decimal | None) -> str | None:
    """DERIVED ratios (strike / spot proxy) are quantized to 6 places for the
    artifact. Prices, strikes and volumes are never quantized — they carry the
    vendor's exact text; only a quotient invented here gets rounded."""
    if value is None:
        return None
    return _plain(value.quantize(RATIO_PLACES, rounding=ROUND_HALF_EVEN))


def _pairs(counts: Sequence[tuple[str, int]]) -> dict[str, int]:
    return dict(counts)


def _strike_json(grid: StrikeGrid) -> dict[str, Any]:
    return {
        "ladder_depth": grid.depth,
        "min_strike": _d(grid.min_strike),
        "max_strike": _d(grid.max_strike),
        "span": _d(grid.span),
        "spot_proxy": _d(grid.spot),
        "min_in_spot_units": _ratio(grid.min_in_spot_units),
        "max_in_spot_units": _ratio(grid.max_in_spot_units),
        "span_in_spot_units": _ratio(grid.span_in_spot_units),
        "spot_units_status": "EVALUATED" if grid.spot is not None else "NOT_EVALUABLE",
    }


def _tenor_json(tenors: TenorHistogram) -> dict[str, Any]:
    return {
        "histogram": _pairs(tenors.buckets),
        "band_bucket": BAND_BUCKET,
        "nearest_days": tenors.nearest_days,
        "farthest_days": tenors.farthest_days,
        "expiries_in_band": tenors.band_expiries,
        "quarterly_expiries": tenors.quarterly_expiries,
        "leaps_expiries": tenors.leaps_expiries,
    }


def _slice_json(item: AsOfSlice) -> dict[str, Any]:
    return {
        "as_of": item.as_of.isoformat(),
        "source": item.source,
        "universe_size": item.universe_size,
        "capture_complete": item.capture_complete,
        "strikes": _strike_json(item.strikes),
        "distinct_expiries": item.distinct_expiries,
        "expiries": [e.isoformat() for e in item.expiries],
        "tenors": _tenor_json(item.tenors),
        "contract_type": _pairs(item.contract_type_counts),
        "exercise_style": _pairs(item.exercise_style_counts),
        "shares_per_contract": _pairs(item.shares_per_contract_counts),
        "non_standard": list(item.non_standard),
        "cfi": _pairs(item.cfi_counts),
        "primary_exchange": _pairs(item.primary_exchange_counts),
        "roots": _pairs(item.root_counts),
        "multi_root": item.multi_root,
        "births": item.births,
        "deaths": item.deaths,
        "born": list(item.born),
        "died_expired": list(item.died_expired),
        "died_active": list(item.died_active),
    }


def _lifecycle_json(lifecycle: Lifecycle) -> dict[str, Any]:
    return {
        "as_ofs": lifecycle.as_ofs,
        "distinct_contracts": lifecycle.distinct_contracts,
        "full_span": lifecycle.full_span,
        "contracts": {
            entry.ticker: {
                "first_seen": entry.first_seen.isoformat(),
                "last_seen": entry.last_seen.isoformat(),
                "as_of_count": entry.as_of_count,
            }
            for entry in lifecycle.entries
        },
    }


def _adjustment_json(event: AdjustmentEvent) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "underlying": event.underlying,
        "as_of": event.as_of.isoformat(),
        "previous_as_of": event.previous_as_of.isoformat(),
        "ticker": event.ticker,
        "root": event.root,
        "before": event.before,
        "after": event.after,
    }


def _bars_json(coverage: BarCoverage | None) -> dict[str, Any] | None:
    if coverage is None:
        return None
    volume, calendar = coverage.volume, coverage.calendar
    return {
        "series": coverage.series,
        "tickers": list(coverage.tickers),
        "volume": {
            "bars": volume.bars,
            "total": _d(volume.total),
            "min": _d(volume.minimum),
            "median": _d(volume.median),
            "p90": _d(volume.p90),
            "max": _d(volume.maximum),
            "zero_volume_bars": volume.zero_volume_bars,
            "bars_at_or_above_min_same_day_volume": volume.bars_at_or_above_min,
            "min_same_day_volume": MIN_SAME_DAY_VOLUME,
        },
        "implied_session_calendar": {
            "sessions_observed": len(calendar.sessions),
            "first": None if calendar.first is None else calendar.first.isoformat(),
            "last": None if calendar.last is None else calendar.last.isoformat(),
            "calendar_days_spanned": calendar.calendar_days_spanned,
            "sessions": [d.isoformat() for d in calendar.sessions],
            "weekday_gaps": [d.isoformat() for d in calendar.weekday_gaps],
            "weekend_days_skipped": calendar.weekend_days_skipped,
            "weekday_gap_note": WEEKDAY_GAP_NOTE,
        },
    }


def _not_evaluable(report: StructuralReport) -> dict[str, Any]:
    spot_supplied = report.spot_proxy_supplied
    return {
        "strike_grid_in_spot_units": {
            "status": "EVALUATED" if spot_supplied else "NOT_EVALUABLE",
            "value": None,
            "note": "spot proxy supplied via --spot-json" if spot_supplied else SPOT_UNITS_NOTE,
        },
        "abs_delta": {"status": "NOT_EVALUABLE", "value": None, "note": NO_GREEKS_NOTE},
        "open_interest": {"status": "NOT_EVALUABLE", "value": None, "note": NO_OI_NOTE},
        "spread_fraction_of_midpoint": {
            "status": "NOT_EVALUABLE",
            "value": None,
            "note": NO_QUOTES_NOTE,
        },
        "underlying_20d_median_dollar_volume": {
            "status": "NOT_EVALUABLE",
            "value": None,
            "note": NO_UNDERLYING_DV_NOTE,
        },
        "spans_earnings": {"status": "NOT_EVALUABLE", "value": None, "note": NO_EARNINGS_NOTE},
    }


def report_to_json(report: StructuralReport) -> dict[str, Any]:
    return {
        "report_version": report.report_version,
        "capability": CAPABILITY,
        "sources": {
            "contract_masters": list(report.contract_sources),
            "bars": list(report.bar_sources),
            "incomplete_captures": list(report.incomplete_captures),
            "unknown_result_keys": list(report.unknown_result_keys),
            "spot_proxy_supplied": report.spot_proxy_supplied,
            "adapter": {"module": ADAPTER_MODULE, "status": report.adapter_status},
        },
        "aggregate": {
            "tier": report.tier,
            "underlyings": len(report.underlyings),
            "masters": report.masters,
            "as_ofs": [d.isoformat() for d in report.as_ofs],
            "contract_rows": report.contract_rows,
            "distinct_contracts": report.distinct_contracts,
            "contract_type": _pairs(report.contract_type_counts),
            "exercise_style": _pairs(report.exercise_style_counts),
            "shares_per_contract": _pairs(report.shares_per_contract_counts),
            "non_standard_rows": report.non_standard_rows,
            "multi_root_underlyings": list(report.multi_root_underlyings),
            "adjustment_events": report.adjustment_events,
            "active_delistings": report.active_delistings,
            "bars": _bars_json(report.bars),
            "unmatched_bar_tickers": list(report.unmatched_bar_tickers),
        },
        "per_underlying": {
            structure.underlying: {
                "as_ofs": [d.isoformat() for d in structure.as_ofs],
                "contract_rows": structure.contract_rows,
                "roots": list(structure.roots),
                "multi_root": structure.multi_root,
                "exercise_style": _pairs(structure.exercise_style_counts),
                "shares_per_contract": _pairs(structure.shares_per_contract_counts),
                "non_standard_rows": structure.non_standard_rows,
                "lifecycle": _lifecycle_json(structure.lifecycle),
                "adjustment_timeline": [_adjustment_json(e) for e in structure.adjustments],
                "active_delistings": [
                    {"as_of": as_of.isoformat(), "ticker": ticker}
                    for as_of, ticker in structure.active_delistings
                ],
                "as_of_slices": [_slice_json(s) for s in structure.slices],
                "bars": _bars_json(structure.bars),
            }
            for structure in report.underlyings
        },
        "not_evaluable": _not_evaluable(report),
    }


# ---- human markdown -----------------------------------------------------------


def _counts_text(counts: Sequence[tuple[str, int]]) -> str:
    return " ".join(f"{key}={count}" for key, count in counts) or "(none)"


def _slice_rows(structure: UnderlyingStructure) -> list[str]:
    rows = []
    for item in structure.slices:
        grid = item.strikes
        spot_units = _ratio(grid.span_in_spot_units) or "NOT_EVALUABLE"
        rows.append(
            f"| {item.as_of} | {item.universe_size} | {grid.depth}"
            f" | {_plain(grid.min_strike)} | {_plain(grid.max_strike)} | {spot_units}"
            f" | {item.distinct_expiries} | {item.tenors.nearest_days}"
            f" | {dict(item.tenors.buckets)[BAND_BUCKET]} | {item.tenors.band_expiries}"
            f" | {len(item.root_counts)} | {'yes' if item.multi_root else 'no'}"
            f" | {'-' if item.births is None else item.births}"
            f" | {'-' if item.deaths is None else item.deaths}"
            f" | {'yes' if item.capture_complete else 'NO'} |"
        )
    return rows


def _bars_markdown(title: str, coverage: BarCoverage | None) -> list[str]:
    if coverage is None:
        return [
            f"## {title}",
            "",
            "- no bars supplied (--bars-json) — session calendar NOT_EVALUABLE",
        ]
    volume, calendar = coverage.volume, coverage.calendar
    lines = [
        f"## {title}",
        "",
        f"- series={coverage.series} bars={volume.bars} volume total={_plain(volume.total)}"
        f" min={_d(volume.minimum)} median={_d(volume.median)} p90={_d(volume.p90)}"
        f" max={_d(volume.maximum)}",
        f"- zero-volume bars={volume.zero_volume_bars};"
        f" bars with v>={MIN_SAME_DAY_VOLUME}: {volume.bars_at_or_above_min}",
        f"- implied session calendar: {len(calendar.sessions)} sessions"
        f" {calendar.first}..{calendar.last} over {calendar.calendar_days_spanned} calendar days"
        f" (weekend days skipped: {calendar.weekend_days_skipped})",
    ]
    if calendar.weekday_gaps:
        lines.append(
            f"- WEEKDAY GAPS ({len(calendar.weekday_gaps)}): "
            + ", ".join(d.isoformat() for d in calendar.weekday_gaps)
        )
        lines.append(f"  - {WEEKDAY_GAP_NOTE}")
    else:
        lines.append("- weekday gaps: none inside the observed span")
    return lines


def render_markdown(report: StructuralReport) -> str:
    lines = [
        f"# Massive free-tier structural coverage — {report.report_version}",
        "",
        f"- tier: `{report.tier}`; adapter `{ADAPTER_MODULE}`: {report.adapter_status}",
        f"- scope: underlyings={len(report.underlyings)} masters={report.masters}"
        f" as_ofs={len(report.as_ofs)} contract-rows={report.contract_rows}"
        f" distinct-contracts={report.distinct_contracts}",
        f"- pooled exercise_style: {_counts_text(report.exercise_style_counts)}",
        f"- pooled shares_per_contract: {_counts_text(report.shares_per_contract_counts)}"
        f" (non-standard rows: {report.non_standard_rows})",
        f"- adjustment events: {report.adjustment_events};"
        f" active delistings: {report.active_delistings}",
    ]
    if report.multi_root_underlyings:
        lines.append(
            "- MULTI-ROOT UNDERLYINGS (M4-A found SPX/SPXW collisions): "
            + ", ".join(report.multi_root_underlyings)
        )
    else:
        lines.append("- multi-root underlyings: none")
    if report.incomplete_captures:
        lines.append(
            f"- INCOMPLETE CAPTURES ({len(report.incomplete_captures)}): "
            + ", ".join(report.incomplete_captures)
            + " — the last page still carried next_url, so the universe is UNDER-COUNTED"
        )
    else:
        lines.append("- captures: every page chain terminated (no dangling next_url)")
    if report.unknown_result_keys:
        lines.append(
            "- SCHEMA DRIFT: unexpected result keys " + ", ".join(report.unknown_result_keys)
        )
    if report.unmatched_bar_tickers:
        lines.append(
            "- bar series excluded (no contract-master owner): "
            + ", ".join(report.unmatched_bar_tickers)
        )
    lines.extend(["", "## Capability (this tier)", ""])
    lines.append("- available: " + "; ".join(CAPABILITY["available"]))
    for name, note in CAPABILITY["unavailable"].items():
        lines.append(f"- UNAVAILABLE `{name}`: {note}")
    for name, note in CAPABILITY["protocol_filter_inputs"].items():
        lines.append(f"- filter input `{name}`: {note}")
    for lane in CAPABILITY["blocked_lanes"]:
        lines.append(f"- BLOCKED LANE: {lane}")

    for structure in report.underlyings:
        lines.extend(
            [
                "",
                f"## {structure.underlying}",
                "",
                f"- roots: {', '.join(structure.roots)}"
                f" ({'MULTI-ROOT' if structure.multi_root else 'single root'})",
                f"- lifecycle: {structure.lifecycle.distinct_contracts} distinct contracts over"
                f" {structure.lifecycle.as_ofs} as_ofs; full-span={structure.lifecycle.full_span}",
                "",
                "| as_of | universe | strikes | min K | max K | span/spot | expiries | nearest DTE"
                f" | {BAND_BUCKET} | band expiries | roots | multi-root | births | deaths | capture |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        lines.extend(_slice_rows(structure))
        lines.append("")
        if structure.adjustments:
            lines.append("### Adjustment timeline")
            lines.append("")
            for event in structure.adjustments:
                subject = event.ticker or f"root {event.root}"
                change = f"{event.before or '(absent)'} -> {event.after or '(absent)'}"
                lines.append(
                    f"- {event.previous_as_of} -> {event.as_of} {event.kind}: {subject} {change}"
                )
        else:
            lines.append("### Adjustment timeline: no events across the as_of series")
        lines.append("")
        if structure.active_delistings:
            lines.append(
                "- ACTIVE DELISTINGS (contract vanished while un-expired): "
                + ", ".join(f"{as_of} {ticker}" for as_of, ticker in structure.active_delistings)
            )
        for item in structure.slices:
            if item.non_standard:
                lines.append(
                    f"- {item.as_of} NON_STANDARD deliverable "
                    f"(shares_per_contract != {_plain(STANDARD_SHARES_PER_CONTRACT)}): "
                    + ", ".join(item.non_standard)
                )
            if item.tenors.band_expiries == 0:
                lines.append(
                    f"- {item.as_of} NO expiry inside the protocol band"
                    f" {DTE_MIN}-{DTE_MAX} DTE — the candidate tenor rule has nothing to select"
                )
        lines.append("")
        lines.extend(_bars_markdown(f"{structure.underlying} — bars", structure.bars))

    lines.extend(["", *_bars_markdown("Aggregate bars", report.bars)])
    lines.extend(["", "## NOT_EVALUABLE inputs", ""])
    for name, block in _not_evaluable(report).items():
        lines.append(f"- {name}: {block['status']} — {block['note']}")
    lines.append("")
    return "\n".join(lines)


# ---- CLI ----------------------------------------------------------------------


def adapter_status(module_name: str = ADAPTER_MODULE) -> str:
    """Probe WS-D1's client for PROVENANCE ONLY (house pattern:
    `scripts/inspect_options_coverage.py::_load_adapter`).

    This inspector consumes captured payloads, so it never imports the
    client and never depends on it landing; the report just records whether
    it was there.
    """
    import importlib
    import importlib.util

    module = sys.modules.get(module_name)
    if module is None:
        try:
            if importlib.util.find_spec(module_name) is None:
                return "ABSENT"
            module = importlib.import_module(module_name)
        except Exception:  # provenance must never break the report
            return "UNIMPORTABLE"
    for name in ("MASSIVE_SCHEMA_VERSION", "MASSIVE_PROVIDER", "__version__"):
        version = getattr(module, name, None)
        if isinstance(version, str) and version:
            return f"PRESENT ({version})"
    return "PRESENT"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Massive free-tier structural coverage inspector")
    parser.add_argument(
        "--contracts-json",
        type=Path,
        required=True,
        help="captured contract master (file) or a directory of them (*.json, sorted)",
    )
    parser.add_argument(
        "--bars-json",
        type=Path,
        default=None,
        help="captured daily-bar response (file) or a directory of them",
    )
    parser.add_argument(
        "--spot-json",
        type=Path,
        default=None,
        help=(
            "declared spot proxy {underlying: {as_of: price}} — without it the strike "
            "grid in spot units is NOT_EVALUABLE (this tier has no underlying quote)"
        ),
    )
    parser.add_argument("--out-json", type=Path, required=True, help="machine JSON output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    masters = load_contract_masters(args.contracts_json)
    bars = load_bar_series(args.bars_json) if args.bars_json is not None else ()
    spot = load_spot_proxy(args.spot_json) if args.spot_json is not None else None
    report = build_structural_report(
        masters, bars=bars, spot_proxy=spot, adapter_status=adapter_status()
    )
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report_to_json(report), indent=2) + "\n", encoding="utf-8")
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StructuralCoverageError as error:  # loud, but not a traceback wall
        raise SystemExit(f"REFUSED: {error}") from error
