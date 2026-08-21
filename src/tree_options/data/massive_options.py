"""Massive (Polygon) free-tier structural options adapter (M4-A §3 WS-D1).

Raw vendor JSON -> typed STRUCTURAL records. The sibling
`tree_options.data.cboe_eod` maps a quote-bearing product into the M3
option shapes; this module deliberately does NOT, because the free tier
does not carry the fields those shapes require. What it maps instead:

- `/v3/reference/options/contracts?underlying_ticker=&as_of=` — the
  point-in-time contract master as of a date: ticker, expiry, strike,
  call/put, exercise style, shares per contract, listing exchange, CFI.
  That is the CONTRACT UNIVERSE over time: ladders, tenors, lifecycle,
  and the adjusted/non-standard deliverable signal.
- `/v2/aggs/ticker/{optionTicker}/range/1/day/{from}/{to}` — daily OHLCV
  bars per contract: session, open/high/low/close/vwap, volume,
  transaction count, and (implicitly) the session calendar on which that
  contract actually traded.

WHAT THIS SOURCE CANNOT DO (probed 2026-08-21, do not re-litigate): the
free tier carries NO bid/ask, NO greeks, NO open interest —
`/v3/snapshot/options/{underlying}` answers HTTP 200 with
`status: NOT_AUTHORIZED`. The M3 candidate filter needs `abs_delta`,
open interest, and a spread; the backtest needs INV-11 executable
prices. Neither can be sourced here. Rather than let a downstream caller
discover that by getting silently empty or fabricated values,
`MASSIVE_FREE_CAPABILITIES` declares the boundary and
`build_option_candidate_inputs` raises `MassiveCapabilityError` naming
the tier that would be required. Fail closed, loudly, at the seam.

Decimal exactness. Bodies are decoded by `massive_client.loads_exact`,
which parses JSON numbers with `parse_float=Decimal` — the Decimal is
built from the vendor's RAW SOURCE TEXT (`587.5` -> `Decimal("587.5")`),
so no float is ever constructed and no `str(float)` coercion happens.
`_as_decimal` REFUSES a `float` outright: seeing one means the body was
decoded with plain `json.loads`, i.e. exactness was already lost
upstream, and silently accepting it would launder a binary
approximation into a price field.

Epoch -> session. A daily aggregate's `t` is the millisecond epoch (UTC)
of the START of that trading day, anchored to midnight America/New_York
— observed in the golden capture: `1741323600000` is 2025-03-07 00:00
EST (UTC-5) and `1741579200000` is 2025-03-10 00:00 EDT (UTC-4), the two
sides of the 2025-03-09 DST transition. The derivation is therefore
"convert the instant to America/New_York, take the calendar date", which
is DST-correct by construction; deriving the date in UTC coincidentally
agrees for a midnight-ET anchor but breaks the moment the vendor anchors
elsewhere, so it is not used. The conversion is integer-exact
(`divmod(ms, 1000)` — no float seconds) and uses `zoneinfo` via the
sanctioned `time.sessions.SESSION_TIMEZONE`, never date arithmetic.

Row accounting (M1 rule: zero silent drops). Every contract record lands
in exactly one bucket:

    rows_total == rows_mapped + duplicate_rows + foreign_underlying_rows
                  + malformed_rows

`nonstandard_deliverable_rows` and `european_rows` are SUB-counts of
`rows_mapped`: unlike the Cboe lane (whose schema could not describe an
unknown deliverable, so such rows were refused), the structural record
here can state `shares_per_contract` truthfully, so a non-100 contract
is KEPT and FLAGGED — that flag is the corporate-action tell this lane
exists to surface.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from tree_options.data.massive_client import MASSIVE_PROVIDER, MassiveError
from tree_options.time.sessions import SESSION_TIMEZONE

if TYPE_CHECKING:
    from tree_options.data.massive_client import MassiveClient

CONTRACTS_PATH = "/v3/reference/options/contracts"
AGGS_PATH_TEMPLATE = "/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"

# The vendor's page cap on the contracts endpoint.
CONTRACTS_PAGE_LIMIT = 1000

# A standard US equity option delivers 100 shares. Anything else means an
# adjusted / non-standard deliverable (split, merger, special dividend).
STANDARD_SHARES_PER_CONTRACT = 100

ContractType = Literal["call", "put"]
ExerciseStyle = Literal["american", "european"]

CONTRACT_TYPES: frozenset[str] = frozenset({"call", "put"})
EXERCISE_STYLES: frozenset[str] = frozenset({"american", "european"})


class MassiveSchemaError(MassiveError):
    """A vendor record does not match the probed shape — refused, never
    coerced (a guessed field is indistinguishable from fabricated data)."""


class MassiveCapabilityError(MassiveError):
    """Something asked this source for data the tier does not carry."""


# ---- declared capability boundary -----------------------------------------

CAP_CONTRACT_MASTER = "contract_master_pit"
CAP_STRIKE_LADDER = "strike_ladder"
CAP_EXPIRY_TENORS = "expiry_tenors"
CAP_EXERCISE_STYLE = "exercise_style"
CAP_DELIVERABLE = "shares_per_contract"
CAP_CONTRACT_LIFECYCLE = "contract_lifecycle"
CAP_DAILY_OHLCV = "daily_ohlcv"
CAP_DAILY_VOLUME = "daily_volume"
CAP_TRADE_COUNT = "daily_transactions"
CAP_SESSION_CALENDAR = "traded_session_calendar"

CAP_QUOTES = "quote_bid_ask"
CAP_GREEKS = "greeks_delta"
CAP_OPEN_INTEREST = "open_interest"
CAP_SPREAD = "quoted_spread"
CAP_INTRADAY_SNAPSHOT = "intraday_snapshot"
CAP_EXECUTABLE_PRICES = "executable_prices_inv11"


@dataclass(frozen=True)
class SourceCapabilities:
    """What a data source can and cannot answer.

    Downstream code asks `require()` BEFORE building anything, so a
    missing capability is a loud refusal at the seam rather than a silent
    hole discovered in a backtest result."""

    provider: str
    tier: str
    provides: tuple[str, ...]
    withholds: tuple[str, ...]
    upgrade_tier: str
    notes: tuple[str, ...] = ()

    def has(self, capability: str) -> bool:
        return capability in self.provides

    def require(self, *capabilities: str) -> None:
        missing = [c for c in capabilities if c not in self.provides]
        if not missing:
            return
        unknown = [c for c in missing if c not in self.withholds]
        detail = f"{self.provider} ({self.tier}) does not provide {sorted(missing)}"
        if unknown:
            detail += f" (undeclared capabilities: {sorted(unknown)})"
        raise MassiveCapabilityError(f"{detail}; required tier: {self.upgrade_tier}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "tier": self.tier,
            "provides": list(self.provides),
            "withholds": list(self.withholds),
            "upgrade_tier": self.upgrade_tier,
            "notes": list(self.notes),
        }


MASSIVE_FREE_CAPABILITIES = SourceCapabilities(
    provider="massive-polygon",
    tier="free",
    provides=(
        CAP_CONTRACT_MASTER,
        CAP_STRIKE_LADDER,
        CAP_EXPIRY_TENORS,
        CAP_EXERCISE_STYLE,
        CAP_DELIVERABLE,
        CAP_CONTRACT_LIFECYCLE,
        CAP_DAILY_OHLCV,
        CAP_DAILY_VOLUME,
        CAP_TRADE_COUNT,
        CAP_SESSION_CALENDAR,
    ),
    withholds=(
        CAP_QUOTES,
        CAP_GREEKS,
        CAP_OPEN_INTEREST,
        CAP_SPREAD,
        CAP_INTRADAY_SNAPSHOT,
        CAP_EXECUTABLE_PRICES,
    ),
    upgrade_tier="polygon/massive Options Starter or above (options snapshot + quotes entitlement)",
    notes=(
        "probed 2026-08-21: /v3/snapshot/options/{underlying} answers HTTP 200 with"
        " status=NOT_AUTHORIZED on this tier",
        "daily bars are TRADE prints, not quotes: a close is not an executable price"
        " and must never be used as an INV-11 fill",
        "free tier rate limit is 5 requests/minute",
    ),
)

# The inputs the M3 candidate filter needs and this tier cannot supply.
CANDIDATE_INPUT_CAPABILITIES = (CAP_QUOTES, CAP_GREEKS, CAP_OPEN_INTEREST, CAP_SPREAD)


def build_option_candidate_inputs(*_args: object, **_kwargs: object) -> NoReturn:
    """Always raises. The M3 candidate filter needs `abs_delta`, open
    interest and a quoted spread; this tier carries none of them, so
    there is no honest implementation — only a fabricating one."""
    MASSIVE_FREE_CAPABILITIES.require(*CANDIDATE_INPUT_CAPABILITIES)
    raise MassiveCapabilityError(  # pragma: no cover — require() always raises first
        "unreachable: candidate inputs are not derivable from the free tier"
    )


# ---- typed records ----------------------------------------------------------


@dataclass(frozen=True)
class MassiveOptionContract:
    """One row of the point-in-time contract master."""

    ticker: str
    underlying: str
    expiration: date
    strike: Decimal
    contract_type: ContractType
    exercise_style: ExerciseStyle
    shares_per_contract: int
    primary_exchange: str
    cfi: str

    @property
    def is_standard_deliverable(self) -> bool:
        """100 shares. False means adjusted / non-standard: the contract's
        economics are NOT the textbook 100-share deliverable and it must
        not be pooled with standard contracts."""
        return self.shares_per_contract == STANDARD_SHARES_PER_CONTRACT

    @property
    def is_european(self) -> bool:
        return self.exercise_style == "european"


@dataclass(frozen=True)
class MassiveContractStats:
    """Every fetched record lands in exactly one of the first four buckets;
    the last two are sub-counts of `rows_mapped` (see module docstring)."""

    rows_total: int = 0
    rows_mapped: int = 0
    duplicate_rows: int = 0
    foreign_underlying_rows: int = 0
    malformed_rows: int = 0
    nonstandard_deliverable_rows: int = 0
    european_rows: int = 0


@dataclass(frozen=True)
class MassiveContractMaster:
    """The contract universe of one underlying as of one date."""

    underlying: str
    as_of: date
    contracts: tuple[MassiveOptionContract, ...]
    stats: MassiveContractStats
    issues: tuple[str, ...] = ()
    pages_fetched: int = 0
    request_ids: tuple[str, ...] = ()

    def expirations(self) -> tuple[date, ...]:
        return tuple(sorted({c.expiration for c in self.contracts}))

    def ladder_for(self, expiration: date) -> tuple[Decimal, ...]:
        """The strike ladder of one expiry (both sides collapsed)."""
        strikes = {c.strike for c in self.contracts if c.expiration == expiration}
        if not strikes:
            raise ValueError(f"{self.underlying} has no expiry {expiration} as of {self.as_of}")
        return tuple(sorted(strikes))

    def by_ticker(self, ticker: str) -> MassiveOptionContract:
        for contract in self.contracts:
            if contract.ticker == ticker:
                return contract
        raise ValueError(f"unknown contract: {ticker}")

    def nonstandard_deliverables(self) -> tuple[MassiveOptionContract, ...]:
        return tuple(c for c in self.contracts if not c.is_standard_deliverable)

    def european_contracts(self) -> tuple[MassiveOptionContract, ...]:
        return tuple(c for c in self.contracts if c.is_european)


@dataclass(frozen=True)
class MassiveDailyBar:
    """One daily OHLCV aggregate of ONE option contract.

    These are TRADE prints. `close` is the last trade of the session, not
    a quote and not an executable price — see the capability record."""

    option_ticker: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vwap: Decimal
    volume: int
    transactions: int
    epoch_ms: int


# ---- field coercion (exact or refused) --------------------------------------


def _require(record: Mapping[str, Any], key: str, *, what: str) -> Any:
    if key not in record:
        raise MassiveSchemaError(f"{what}: missing field {key!r}")
    return record[key]


def _as_decimal(value: Any, *, what: str) -> Decimal:
    """Decimal from an exactly-decoded number.

    `loads_exact` yields `Decimal` for fractional literals and `int` for
    integral ones; both are exact. A `float` means the body was decoded
    with plain `json.loads` and exactness is already gone — refuse rather
    than launder the approximation. A string is parsed as raw text."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise MassiveSchemaError(f"{what}: bool is not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except ArithmeticError:
            raise MassiveSchemaError(f"{what}: {value!r} is not a decimal") from None
    if isinstance(value, float):
        raise MassiveSchemaError(
            f"{what}: got a float — decode the body with massive_client.loads_exact"
            " (parse_float=Decimal) so prices keep the vendor's exact text"
        )
    raise MassiveSchemaError(f"{what}: {type(value).__name__} is not a number")


def _as_int(value: Any, *, what: str) -> int:
    if isinstance(value, bool):
        raise MassiveSchemaError(f"{what}: bool is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise MassiveSchemaError(f"{what}: {value} is not integral")
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise MassiveSchemaError(f"{what}: {value!r} is not an integer")


def _as_date(value: Any, *, what: str) -> date:
    if not isinstance(value, str):
        raise MassiveSchemaError(f"{what}: {type(value).__name__} is not an ISO date string")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise MassiveSchemaError(f"{what}: {value!r} is not an ISO date ({exc})") from None


def _as_text(value: Any, *, what: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise MassiveSchemaError(f"{what}: {type(value).__name__} is not a string")
    text = value.strip()
    if not text and not allow_empty:
        raise MassiveSchemaError(f"{what}: empty string")
    return text


# ---- epoch -> session -------------------------------------------------------


def instant_of_epoch_ms(epoch_ms: int) -> datetime:
    """The tz-aware UTC instant of a millisecond epoch, integer-exact.

    `divmod` keeps the arithmetic in integers; dividing by 1000.0 would
    route a timestamp through binary floating point."""
    seconds, millis = divmod(int(epoch_ms), 1000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=millis * 1000)


def session_of_epoch_ms(epoch_ms: int) -> date:
    """The trading session a daily bar belongs to.

    The vendor anchors a daily aggregate at 00:00 America/New_York, so the
    session is the ET calendar date of the instant — DST-correct across
    the March/November transitions because `zoneinfo` picks the right UTC
    offset (-05:00 EST, -04:00 EDT) for that instant."""
    return instant_of_epoch_ms(epoch_ms).astimezone(SESSION_TIMEZONE).date()


# ---- contract master --------------------------------------------------------


def parse_option_contract(record: Mapping[str, Any]) -> MassiveOptionContract:
    """One `/v3/reference/options/contracts` result -> a typed record."""
    ticker = _as_text(_require(record, "ticker", what="contract"), what="ticker")
    what = f"contract {ticker}"
    contract_type = _as_text(
        _require(record, "contract_type", what=what), what=f"{what} contract_type"
    ).lower()
    if contract_type not in CONTRACT_TYPES:
        raise MassiveSchemaError(f"{what}: contract_type {contract_type!r} not call/put")
    exercise_style = _as_text(
        _require(record, "exercise_style", what=what), what=f"{what} exercise_style"
    ).lower()
    if exercise_style not in EXERCISE_STYLES:
        raise MassiveSchemaError(f"{what}: exercise_style {exercise_style!r} not american/european")
    strike = _as_decimal(_require(record, "strike_price", what=what), what=f"{what} strike_price")
    if strike <= 0:
        raise MassiveSchemaError(f"{what}: strike {strike} is not positive")
    shares = _as_int(
        _require(record, "shares_per_contract", what=what), what=f"{what} shares_per_contract"
    )
    if shares <= 0:
        raise MassiveSchemaError(f"{what}: shares_per_contract {shares} is not positive")
    return MassiveOptionContract(
        ticker=ticker,
        underlying=_as_text(
            _require(record, "underlying_ticker", what=what), what=f"{what} underlying_ticker"
        ),
        expiration=_as_date(
            _require(record, "expiration_date", what=what), what=f"{what} expiration_date"
        ),
        strike=strike,
        contract_type=contract_type,  # type: ignore[arg-type]
        exercise_style=exercise_style,  # type: ignore[arg-type]
        shares_per_contract=shares,
        primary_exchange=_as_text(
            record.get("primary_exchange", ""), what=f"{what} primary_exchange", allow_empty=True
        ),
        cfi=_as_text(record.get("cfi", ""), what=f"{what} cfi", allow_empty=True),
    )


def build_contract_master(
    records: Iterable[Mapping[str, Any]],
    *,
    underlying: str,
    as_of: date,
    pages_fetched: int = 0,
    request_ids: Sequence[str] = (),
) -> MassiveContractMaster:
    """Typed master from raw records, with full row accounting.

    Pure: no client, no clock. `fetch_contract_master` is this function
    plus a paginated GET, which is why every mapping rule is testable off
    committed fixtures."""
    contracts: list[MassiveOptionContract] = []
    issues: list[str] = []
    seen: set[str] = set()
    rows_total = duplicates = foreign = malformed = 0
    nonstandard = european = 0

    for index, record in enumerate(records):
        rows_total += 1
        try:
            contract = parse_option_contract(record)
        except MassiveSchemaError as exc:
            malformed += 1
            issues.append(f"record {index}: {exc} — refused")
            continue
        if contract.underlying != underlying:
            foreign += 1
            issues.append(
                f"record {index}: {contract.ticker} underlying {contract.underlying!r}"
                f" != requested {underlying!r} — refused"
            )
            continue
        if contract.ticker in seen:
            duplicates += 1
            issues.append(f"record {index}: duplicate ticker {contract.ticker} — refused")
            continue
        seen.add(contract.ticker)
        if not contract.is_standard_deliverable:
            nonstandard += 1
            issues.append(
                f"{contract.ticker}: shares_per_contract {contract.shares_per_contract}"
                " != 100 — adjusted/non-standard deliverable, do not pool with standard"
                " contracts"
            )
        if contract.is_european:
            european += 1
            issues.append(
                f"{contract.ticker}: exercise_style european — early exercise/assignment"
                " modelling does not apply"
            )
        contracts.append(contract)

    if not contracts:
        issues.append(f"{underlying} as of {as_of}: no contracts mapped")
    return MassiveContractMaster(
        underlying=underlying,
        as_of=as_of,
        contracts=tuple(sorted(contracts, key=lambda c: c.ticker)),
        stats=MassiveContractStats(
            rows_total=rows_total,
            rows_mapped=len(contracts),
            duplicate_rows=duplicates,
            foreign_underlying_rows=foreign,
            malformed_rows=malformed,
            nonstandard_deliverable_rows=nonstandard,
            european_rows=european,
        ),
        issues=tuple(issues),
        pages_fetched=pages_fetched,
        request_ids=tuple(request_ids),
    )


def fetch_contract_master(
    client: MassiveClient,
    underlying: str,
    as_of: date,
    *,
    limit: int = CONTRACTS_PAGE_LIMIT,
    max_pages: int | None = None,
    use_cache: bool = True,
) -> MassiveContractMaster:
    """The point-in-time contract universe of `underlying` on `as_of`.

    `as_of` is the vendor's own PIT selector: the response lists the
    contracts that were listed on that date, so no survivorship filter is
    applied here (and none may be applied downstream without saying so)."""
    if limit < 1 or limit > CONTRACTS_PAGE_LIMIT:
        raise ValueError(f"limit must be 1..{CONTRACTS_PAGE_LIMIT}, got {limit}")
    page = client.paginate(
        CONTRACTS_PATH,
        {
            "underlying_ticker": underlying,
            "as_of": as_of.isoformat(),
            "limit": limit,
        },
        max_pages=max_pages,
        use_cache=use_cache,
    )
    return build_contract_master(
        page.results,
        underlying=underlying,
        as_of=as_of,
        pages_fetched=page.pages_fetched,
        request_ids=page.request_ids,
    )


# ---- daily bars -------------------------------------------------------------


def parse_daily_bar(record: Mapping[str, Any], *, option_ticker: str) -> MassiveDailyBar:
    """One `/v2/aggs/.../range/1/day/...` result -> a typed bar."""
    what = f"bar {option_ticker}"
    epoch_ms = _as_int(_require(record, "t", what=what), what=f"{what} t")
    session = session_of_epoch_ms(epoch_ms)
    what = f"bar {option_ticker} {session}"
    low = _as_decimal(_require(record, "l", what=what), what=f"{what} l")
    high = _as_decimal(_require(record, "h", what=what), what=f"{what} h")
    open_ = _as_decimal(_require(record, "o", what=what), what=f"{what} o")
    close = _as_decimal(_require(record, "c", what=what), what=f"{what} c")
    vwap = _as_decimal(_require(record, "vw", what=what), what=f"{what} vw")
    if low > high:
        raise MassiveSchemaError(f"{what}: low {low} > high {high}")
    for name, value in (("open", open_), ("close", close), ("vwap", vwap)):
        if value < low or value > high:
            raise MassiveSchemaError(f"{what}: {name} {value} outside [{low}, {high}]")
    volume = _as_int(_require(record, "v", what=what), what=f"{what} v")
    if volume < 0:
        raise MassiveSchemaError(f"{what}: volume {volume} is negative")
    transactions = _as_int(record.get("n", 0), what=f"{what} n")
    if transactions < 0:
        raise MassiveSchemaError(f"{what}: transactions {transactions} is negative")
    return MassiveDailyBar(
        option_ticker=option_ticker,
        session=session,
        open=open_,
        high=high,
        low=low,
        close=close,
        vwap=vwap,
        volume=volume,
        transactions=transactions,
        epoch_ms=epoch_ms,
    )


def parse_daily_bars(
    body: Mapping[str, Any],
    *,
    option_ticker: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[MassiveDailyBar, ...]:
    """Typed bars from a decoded aggregates body, session-ascending.

    Integrity, all fail-closed: `resultsCount` must match the delivered
    rows (a mismatch means a truncated or edited body), sessions must be
    unique, and every session must fall inside the requested window when
    one is given."""
    results = body.get("results")
    if results is None:
        declared = body.get("resultsCount", 0)
        if _as_int(declared, what=f"bars {option_ticker} resultsCount") != 0:
            raise MassiveSchemaError(
                f"bars {option_ticker}: resultsCount {declared} but no `results` array"
            )
        return ()
    if not isinstance(results, list | tuple):
        raise MassiveSchemaError(
            f"bars {option_ticker}: `results` is {type(results).__name__}, expected a list"
        )
    if "resultsCount" in body:
        declared = _as_int(body["resultsCount"], what=f"bars {option_ticker} resultsCount")
        if declared != len(results):
            raise MassiveSchemaError(
                f"bars {option_ticker}: resultsCount {declared} != {len(results)} delivered rows"
            )
    bars = [parse_daily_bar(record, option_ticker=option_ticker) for record in results]
    sessions = [bar.session for bar in bars]
    if len(set(sessions)) != len(sessions):
        duplicated = sorted({s for s in sessions if sessions.count(s) > 1})
        raise MassiveSchemaError(f"bars {option_ticker}: duplicate sessions {duplicated}")
    for bar in bars:
        if start is not None and bar.session < start:
            raise MassiveSchemaError(f"bars {option_ticker}: {bar.session} precedes start {start}")
        if end is not None and bar.session > end:
            raise MassiveSchemaError(f"bars {option_ticker}: {bar.session} follows end {end}")
    return tuple(sorted(bars, key=lambda b: b.session))


def fetch_daily_bars(
    client: MassiveClient,
    option_ticker: str,
    start: date,
    end: date,
    *,
    adjusted: bool = True,
    use_cache: bool = True,
) -> tuple[MassiveDailyBar, ...]:
    """Daily bars for one option contract over an inclusive date window.

    The returned sessions are the ones on which the contract actually
    TRADED: absence is "no prints", not "not listed" — listing is the
    contract master's question, and conflating the two would understate
    the universe."""
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    path = AGGS_PATH_TEMPLATE.format(
        ticker=option_ticker, start=start.isoformat(), end=end.isoformat()
    )
    body = client.get_json(path, {"adjusted": "true" if adjusted else "false"}, use_cache=use_cache)
    return parse_daily_bars(body, option_ticker=option_ticker, start=start, end=end)


__all__ = [
    "AGGS_PATH_TEMPLATE",
    "CANDIDATE_INPUT_CAPABILITIES",
    "CAP_DAILY_OHLCV",
    "CAP_GREEKS",
    "CAP_OPEN_INTEREST",
    "CAP_QUOTES",
    "CAP_SPREAD",
    "CONTRACTS_PATH",
    "MASSIVE_FREE_CAPABILITIES",
    # Re-exported from `massive_client` so a provenance probe of THIS module
    # (the adapter surface: `scripts/inspect_structural_coverage.py::adapter_status`)
    # can name the provider/tier that produced a capture, instead of a bare
    # "PRESENT". Adapter identity belongs on the adapter module.
    "MASSIVE_PROVIDER",
    "STANDARD_SHARES_PER_CONTRACT",
    "MassiveCapabilityError",
    "MassiveContractMaster",
    "MassiveContractStats",
    "MassiveDailyBar",
    "MassiveOptionContract",
    "MassiveSchemaError",
    "SourceCapabilities",
    "build_contract_master",
    "build_option_candidate_inputs",
    "fetch_contract_master",
    "fetch_daily_bars",
    "instant_of_epoch_ms",
    "parse_daily_bar",
    "parse_daily_bars",
    "parse_option_contract",
    "session_of_epoch_ms",
]
