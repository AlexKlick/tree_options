"""Cboe "Option EOD Summary" adapter (M4-A plan §3 WS-A).

Parses the 34-column DataShop CSV (`UnderlyingOptionsEODCalcs_*`) into the
M3 option shapes (`OptionDayFile` / `OptionChainEntry` /
`OptionQuoteSnapshot` imported from `tree_options.synth_options.generate` —
never duplicated) plus a fail-closed real-data manifest.

Mapping (spike §1 + retained-sample observations, all verified against the
retained demo, not assumed):

- identity: `underlying_symbol`/`expiration`/`strike`/`option_type` ->
  `contract_id_of(underlying, expiration, C/P, strike)` — the CANONICAL id
  form, reused from the M3 generator so the unmodified `OptionPitSurface`
  (which reconstructs ids via `contract_id_of`) resolves real contracts.
  `root` is recorded on `OptionContract.option_root`; a genuine root
  conflict on one (underlying, expiry, strike, C/P) cell is audited, first
  root kept; a full (session, contract) duplicate row is REFUSED.
- two snapshots per contract-day: the `*_1545` quartet (15:45 ET) and the
  `*_eod` quartet (session close). A session whose every mapped row lacks
  the 1545 quartet is an early close: `quote_1545 is None` and the EOD
  exchange timestamp is the 13:00 ET early-close instant.
- `trade_volume` -> `same_day_volume`; `open_interest` -> `open_interest`;
  `abs(delta_1545)` -> `abs_delta`.
- `underlying_bid/ask_1545` (+ `_eod`) are FILE-level per session: the
  1545 pair is preferred for `OptionDayFile.underlying_bid/ask`; the EOD
  pair is used only on early-close sessions; a material (>0.5%) mid drift
  between the two instants is recorded as an issue, never silently picked.
- `delivery_code` is EMPTY for standard deliverables (trailing empty field
  is normal). A NON-empty code means the deliverable is unverifiable from
  this product: the row is counted in `nonstandard_delivery_rows`, named
  in `issues`, and REFUSED (the `OptionContract` schema cannot truthfully
  describe an unknown deliverable).

Row accounting (M1 rule: zero silent drops). Every data row lands in
exactly one bucket:

    rows_total == rows_mapped + duplicate_rows + zero_greeks_rows
                  + nonstandard_delivery_rows
                  + (rows of unselected underlyings and malformed/refused
                     rows, accounted per-row or per-symbol in `issues`)

Bulk-but-benign classes (zero greeks, zero bid) get one summary issue line
plus their stat counter; per-row oddities (duplicates, non-standard
delivery, partial blanks, malformed fields) get per-row issue lines.

Not-delta-bearing representation (plan §6): a row whose `delta_1545` is
blank or `abs(delta) == 0` (vendor license flattening, deep wings) is
COUNTED in `zero_greeks_rows` and stays in the contract master, but is NOT
materialized as an `OptionChainEntry` — `entry_for` then raises and the
unmodified `OptionPitSurface.candidate_snapshot` answers `abs_delta=None`,
the NOT_EVALUABLE discipline. The entry shape cannot carry a null delta
(`abs_delta: Decimal`), so exclusion is the only fail-closed encoding; the
raw quotes stay in the source file for the coverage inspector to count.

Index underlyings: in the `no_cgi` variant the file zeroes the underlying
quotes of `^`-prefixed symbols — parse REFUSES, naming the symbol, rather
than ingest zeros as tradable quotes. Zero/degenerate underlying quotes in
ANY variant (the demo's ^VIX is zero even with CGI) likewise refuse.

PIT semantics: session t's file is received at 09:00 America/New_York on
the NEXT trading session, weekend-skipping only (holidays are a declared
approximation — the sample exercises none). Every snapshot is validated
`exchange_timestamp <= received_timestamp` at parse time; violations
refuse the parse.

Weekday arithmetic lives through the sanctioned `time/` helpers only (the
AST ban on naive date arithmetic outside `time/`): `is_friday` probes plus
`minus_calendar_days` with a negative offset (== plus k days).
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import NonNegativeInt, ValidationError

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.schemas.options import DeliverableSpec, OptionContract
from tree_options.synth_options.generate import (
    CENT,
    PUB_WALL,
    SNAPSHOT_1545,
    OptionChainEntry,
    OptionDayFile,
    OptionQuoteSnapshot,
    contract_id_of,
)
from tree_options.time.expiries import is_friday, minus_calendar_days
from tree_options.time.sessions import (
    SESSION_TIMEZONE,
    early_close_instant,
    session_close_instant,
)

if TYPE_CHECKING:
    from tree_options.data.real_overlay import RealOptionOverlay

REAL_OPTIONS_PROVIDER = "cboe-option-eod/1"
REAL_OPTIONS_SCHEMA_VERSION = "m4/1"
REAL_MANIFEST_DOMAIN = b"tree-options-m4-real-options-v1"
REAL_CONTRACT_MASTER_DOMAIN = b"tree-options-m4-real-contract-master-v1"

EodVariant = Literal["cgi_or_historical", "no_cgi"]

# The 34-column DataShop header, in delivery order (verified against the
# retained sample; any drift refuses the file — plan §6 sample/product risk).
CBOE_EOD_COLUMNS: tuple[str, ...] = (
    "underlying_symbol",
    "quote_date",
    "root",
    "expiration",
    "strike",
    "option_type",
    "open",
    "high",
    "low",
    "close",
    "trade_volume",
    "bid_size_1545",
    "bid_1545",
    "ask_size_1545",
    "ask_1545",
    "underlying_bid_1545",
    "underlying_ask_1545",
    "implied_underlying_price_1545",
    "active_underlying_price_1545",
    "implied_volatility_1545",
    "delta_1545",
    "gamma_1545",
    "theta_1545",
    "vega_1545",
    "rho_1545",
    "bid_size_eod",
    "bid_eod",
    "ask_size_eod",
    "ask_eod",
    "underlying_bid_eod",
    "underlying_ask_eod",
    "vwap",
    "open_interest",
    "delivery_code",
)

# Relative mid drift between the 15:45 and EOD underlying pairs that counts
# as material (file-level pair uses 1545 either way; the issue is the audit).
UNDERLYING_DRIFT_TOLERANCE = Decimal("0.005")

# Cboe EOD carries no quote-condition column; every snapshot is the day's
# regular NBBO observation. Declared assumption: tradability grading stays
# with as_tradable (crossed/locked/zero-size still reject at the fill).
REAL_QUOTE_CONDITION = "regular"

_STANDARD_DELIVERABLE = DeliverableSpec(shares_per_contract=Decimal("100"))

_I_UNDERLYING = 0
_I_QUOTE_DATE = 1
_I_ROOT = 2
_I_EXPIRATION = 3
_I_STRIKE = 4
_I_OPTION_TYPE = 5
_I_TRADE_VOLUME = 10
_I_BID_SIZE_1545, _I_BID_1545, _I_ASK_SIZE_1545, _I_ASK_1545 = 11, 12, 13, 14
_I_UNDER_BID_1545, _I_UNDER_ASK_1545 = 15, 16
_I_DELTA_1545 = 20
_I_BID_SIZE_EOD, _I_BID_EOD, _I_ASK_SIZE_EOD, _I_ASK_EOD = 25, 26, 27, 28
_I_UNDER_BID_EOD, _I_UNDER_ASK_EOD = 29, 30
_I_OPEN_INTEREST = 32
_I_DELIVERY_CODE = 33


class CboeEodError(RuntimeError):
    """Base class for Cboe EOD adapter failures (fail closed)."""


class CboeEodFormatError(CboeEodError):
    """The file is not the expected 34-column product (header/column drift)."""


class IndexUnderlyingNotLicensedError(CboeEodError):
    """A no_cgi file zeroes an index underlying's quotes: refuse, don't
    ingest zeros as tradable quotes."""


class CboeEodMultiUnderlyingError(CboeEodError):
    """The file bundles several underlyings; one must be selected."""


class CboeEodUnderlyingQuoteError(CboeEodError):
    """A session's underlying quote pair is unusable (zero/degenerate)."""


class PITInvariantError(CboeEodError):
    """A snapshot's exchange_timestamp exceeds its file's receipt instant."""


class RealOptionsManifestError(RuntimeError):
    pass


class _RowError(ValueError):
    """Per-row parse failure (the row is refused and named in issues)."""


@dataclass(frozen=True)
class CboeEodStats:
    """Every input row lands in exactly one bucket (see module docstring)."""

    rows_total: int = 0
    rows_mapped: int = 0
    duplicate_rows: int = 0
    zero_greeks_rows: int = 0
    zero_bid_rows: int = 0
    early_close_sessions: int = 0
    nonstandard_delivery_rows: int = 0


@dataclass(frozen=True)
class CboeEodParseResult:
    source_path: Path
    source_sha256: str
    underlying_security_id: str
    variant: EodVariant
    day_files: dict[date, OptionDayFile]
    stats: CboeEodStats
    issues: list[str]
    contracts: tuple[OptionContract, ...] = ()


# ---- session instants (weekday logic only through time/ helpers) ----------


def _is_weekend(d: date) -> bool:
    """Sat/Sun via Friday probes: x is Saturday iff the prior day is Friday;
    Sunday iff two days back is Friday. No weekday()/timedelta here."""
    return is_friday(minus_calendar_days(d, 1)) or is_friday(minus_calendar_days(d, 2))


def next_trading_session(session: date) -> date:
    """The next weekend-skipping trading session after `session` (the declared
    M4 publication clock; holidays are a documented approximation)."""
    for k in (1, 2, 3):
        candidate = minus_calendar_days(session, -k)  # -k == plus k days
        if not _is_weekend(candidate):
            return candidate
    raise CboeEodError(f"no weekday follows {session}")  # pragma: no cover


def publication_instant(session: date) -> datetime:
    """Receipt wall for session t's file: 09:00 America/New_York on the next
    weekend-skipping trading session (T+1)."""
    local = datetime.combine(next_trading_session(session), PUB_WALL, tzinfo=SESSION_TIMEZONE)
    return local.astimezone(UTC)


def snapshot_1545_instant(session: date) -> datetime:
    """15:45 America/New_York on the session date, as a UTC instant."""
    local = datetime.combine(session, SNAPSHOT_1545, tzinfo=SESSION_TIMEZONE)
    return local.astimezone(UTC)


def validate_pit_invariants(day_files: dict[date, OptionDayFile]) -> None:
    """exchange_timestamp <= received_timestamp for EVERY snapshot of EVERY
    file — shared by the parser (post-pass over its output) and the overlay
    constructor (defense in depth). Both instants are adapter-derived for
    CSV input, so a violation means internal corruption or hand-built files;
    it is refused either way, never passed through."""
    for session, file in sorted(day_files.items()):
        for entry in file.entries:
            snaps = [entry.quote_eod] + ([entry.quote_1545] if entry.quote_1545 else [])
            for snap in snaps:
                if snap.exchange_timestamp > file.received_at:
                    raise PITInvariantError(
                        f"{entry.contract_id} on {session}: exchange "
                        f"{snap.exchange_timestamp} > received {file.received_at}"
                    )


# ---- row-level parsing ----------------------------------------------------


@dataclass
class _UnderlyingPair:
    bid_1545: Decimal | None = None
    ask_1545: Decimal | None = None
    bid_eod: Decimal | None = None
    ask_eod: Decimal | None = None


@dataclass
class _RowDraft:
    line_no: int
    session: date
    contract_id: str
    root: str
    expiration: date
    strike: Decimal
    call_put: str
    snap_1545: OptionQuoteSnapshot | None
    eod_bid_size: int
    eod_bid: Decimal
    eod_ask_size: int
    eod_ask: Decimal
    open_interest: int
    same_day_volume: int
    abs_delta: Decimal | None  # None: not delta-bearing (blank/zero delta)


@dataclass
class _ContractAccum:
    underlying: str
    root: str
    expiration: date
    strike: Decimal
    call_put: str
    first_session: date
    last_session: date


def _blank(raw: str) -> bool:
    return raw.strip() == ""


def _parse_date(raw: str, *, what: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise _RowError(f"{what} {raw!r}: {exc}") from exc


def _parse_int(raw: str, *, what: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise _RowError(f"{what} {raw!r}: {exc}") from exc


def _parse_decimal(raw: str, *, what: str) -> Decimal:
    try:
        return Decimal(raw.strip())
    except InvalidOperation as exc:
        raise _RowError(f"{what} {raw!r}: {exc}") from exc


def _parse_quartet(
    row: list[str], size_i: int, price_i: int, *, line_no: int, what: str
) -> tuple[int, Decimal, int, Decimal] | None:
    """(bid_size, bid, ask_size, ask) from a size/price column pair plus the
    ask pair 2 columns on; None when ALL four are blank; partial blanks
    refuse the row."""
    fields = (row[size_i], row[price_i], row[size_i + 2], row[price_i + 2])
    if all(_blank(f) for f in fields):
        return None
    if any(_blank(f) for f in fields):
        raise _RowError(f"row {line_no}: partially blank {what} quartet")
    return (
        _parse_int(fields[0], what=f"{what} bid_size"),
        _parse_decimal(fields[1], what=f"{what} bid"),
        _parse_int(fields[2], what=f"{what} ask_size"),
        _parse_decimal(fields[3], what=f"{what} ask"),
    )


def _parse_pair(
    row: list[str], bid_i: int, ask_i: int, *, line_no: int, what: str
) -> tuple[Decimal, Decimal] | None:
    if _blank(row[bid_i]) and _blank(row[ask_i]):
        return None
    if _blank(row[bid_i]) or _blank(row[ask_i]):
        raise _RowError(f"row {line_no}: partially blank {what} underlying pair")
    return (
        _parse_decimal(row[bid_i], what=f"{what} underlying bid"),
        _parse_decimal(row[ask_i], what=f"{what} underlying ask"),
    )


def _normalize_strike(raw: str, *, line_no: int) -> Decimal:
    strike = _parse_decimal(raw, what="strike")
    snapped = strike.quantize(CENT)
    if strike != snapped:
        raise _RowError(f"row {line_no}: strike {raw!r} is off the cent grid")
    return snapped


def parse_cboe_eod_csv(
    path: Path,
    *,
    variant: EodVariant = "cgi_or_historical",
    underlying: str | None = None,
    raw: bytes | None = None,
) -> CboeEodParseResult:
    """Parse one Cboe Option EOD Summary CSV.

    `underlying` selects the ingested symbol when a file bundles several
    (the retained demo bundles SPY/TSLA/^SPX/^VIX; the purchased product is
    per-underlying). Rows of unselected symbols are counted in `rows_total`
    and accounted by a per-symbol summary issue — never silently dropped.
    Without a selection, a multi-underlying file refuses, naming them all.
    """
    # The optional bytes seam lets authority-sensitive callers parse exactly
    # the inode/bytes already read under no-follow custody. Ordinary callers
    # retain the convenient path-reading behavior.
    if raw is None:
        raw = path.read_bytes()
    source_sha256 = sha256_hex(raw)
    reader = csv.reader(io.StringIO(raw.decode("utf-8")))
    try:
        header = next(reader)
    except StopIteration:
        raise CboeEodFormatError(f"{path.name}: empty file") from None
    expected = list(CBOE_EOD_COLUMNS)
    if header != expected:
        raise CboeEodFormatError(
            f"{path.name}: header drift — missing "
            f"{[c for c in expected if c not in header][:5]}, unexpected "
            f"{[c for c in header if c not in expected][:5]}"
        )

    issues: list[str] = []
    rows_total = duplicate_rows = 0
    nonstandard_delivery_rows = 0
    drafts: dict[date, list[_RowDraft]] = {}
    seen: dict[date, set[str]] = {}
    masters: dict[str, _ContractAccum] = {}
    pairs: dict[date, _UnderlyingPair] = {}
    selected: str | None = None
    unselected: dict[str, int] = {}

    for line_no, row in enumerate(reader, start=2):
        if not row:
            continue  # trailing newline artifact, not a data row
        rows_total += 1
        if len(row) != len(CBOE_EOD_COLUMNS):
            issues.append(f"row {line_no}: {len(row)} fields, expected 34 — refused")
            continue
        try:
            symbol = row[_I_UNDERLYING].strip()
            if not symbol:
                raise _RowError("empty underlying_symbol")
            if underlying is not None and symbol != underlying:
                unselected[symbol] = unselected.get(symbol, 0) + 1
                continue
            if selected is None:
                selected = symbol
            elif symbol != selected:
                raise CboeEodMultiUnderlyingError(
                    f"{path.name}: multiple underlyings {sorted({selected, symbol})} —"
                    " pass underlying=<symbol> to select one"
                )
            if variant == "no_cgi" and symbol.startswith("^"):
                raise IndexUnderlyingNotLicensedError(
                    f"{path.name}: underlying {symbol} has zeroed underlying quotes"
                    " in the no_cgi variant — refusing rather than ingesting zeros"
                )
            session = _parse_date(row[_I_QUOTE_DATE], what="quote_date")
            expiration = _parse_date(row[_I_EXPIRATION], what="expiration")
            strike = _normalize_strike(row[_I_STRIKE], line_no=line_no)
            option_type = row[_I_OPTION_TYPE].strip().upper()
            if option_type not in ("C", "P"):
                raise _RowError(f"option_type {option_type!r} not C/P")
            contract_id = contract_id_of(symbol, expiration, option_type, strike)

            pair_1545 = _parse_pair(
                row, _I_UNDER_BID_1545, _I_UNDER_ASK_1545, line_no=line_no, what="1545"
            )
            pair_eod = _parse_pair(
                row, _I_UNDER_BID_EOD, _I_UNDER_ASK_EOD, line_no=line_no, what="EOD"
            )
            session_pair = pairs.setdefault(session, _UnderlyingPair())
            for attr, value in (
                ("bid_1545", pair_1545[0] if pair_1545 else None),
                ("ask_1545", pair_1545[1] if pair_1545 else None),
                ("bid_eod", pair_eod[0] if pair_eod else None),
                ("ask_eod", pair_eod[1] if pair_eod else None),
            ):
                if value is None:
                    continue
                existing = getattr(session_pair, attr)
                if existing is not None and existing != value:
                    issues.append(
                        f"row {line_no}: session {session} {attr} {value} conflicts"
                        f" with earlier {existing} — first value kept"
                    )
                else:
                    setattr(session_pair, attr, value)

            session_seen = seen.setdefault(session, set())
            if contract_id in session_seen:
                duplicate_rows += 1
                issues.append(f"row {line_no}: duplicate contract row {contract_id} — refused")
                continue
            session_seen.add(contract_id)

            delivery_code = row[_I_DELIVERY_CODE].strip()
            if delivery_code:
                nonstandard_delivery_rows += 1
                issues.append(
                    f"row {line_no}: {contract_id} delivery_code {delivery_code!r}:"
                    " deliverable unverifiable — row refused"
                )
                continue

            quartet_1545 = _parse_quartet(
                row, _I_BID_SIZE_1545, _I_BID_1545, line_no=line_no, what="1545"
            )
            snap_1545: OptionQuoteSnapshot | None = None
            if quartet_1545 is not None:
                snap_1545 = OptionQuoteSnapshot(
                    exchange_timestamp=snapshot_1545_instant(session),
                    bid=quartet_1545[1],
                    ask=quartet_1545[3],
                    bid_size=quartet_1545[0],
                    ask_size=quartet_1545[2],
                )
            quartet_eod = _parse_quartet(
                row, _I_BID_SIZE_EOD, _I_BID_EOD, line_no=line_no, what="EOD"
            )
            if quartet_eod is None:
                raise _RowError("no EOD quote quartet — row refused")

            abs_delta: Decimal | None = None
            delta_raw = row[_I_DELTA_1545]
            if not _blank(delta_raw):
                delta = _parse_decimal(delta_raw, what="delta_1545")
                if delta.copy_abs() != Decimal("0"):
                    abs_delta = delta.copy_abs()

            draft = _RowDraft(
                line_no=line_no,
                session=session,
                contract_id=contract_id,
                root=row[_I_ROOT].strip(),
                expiration=expiration,
                strike=strike,
                call_put=option_type,
                snap_1545=snap_1545,
                eod_bid_size=quartet_eod[0],
                eod_bid=quartet_eod[1],
                eod_ask_size=quartet_eod[2],
                eod_ask=quartet_eod[3],
                open_interest=_parse_int(row[_I_OPEN_INTEREST], what="open_interest"),
                same_day_volume=_parse_int(row[_I_TRADE_VOLUME], what="trade_volume"),
                abs_delta=abs_delta,
            )
            accum = masters.get(contract_id)
            if accum is None:
                masters[contract_id] = _ContractAccum(
                    underlying=symbol,
                    root=draft.root,
                    expiration=expiration,
                    strike=strike,
                    call_put=option_type,
                    first_session=session,
                    last_session=session,
                )
            else:
                if accum.root != draft.root:
                    issues.append(
                        f"row {line_no}: {contract_id} root {draft.root!r} conflicts"
                        f" with {accum.root!r} — first root kept"
                    )
                accum.first_session = min(accum.first_session, session)
                accum.last_session = max(accum.last_session, session)
            drafts.setdefault(session, []).append(draft)
        except _RowError as exc:
            issues.append(f"row {line_no}: {exc} — refused")
        except ValidationError as exc:
            issues.append(f"row {line_no}: schema violation {exc.errors()[0]['msg']} — refused")

    for symbol, count in sorted(unselected.items()):
        issues.append(f"unselected underlying {symbol}: {count} rows counted, not mapped")

    # Session classification: early close iff every mapped-eligible row lacks
    # the 15:45 snapshot; mixed sessions get an issue per shortfall count.
    early_sessions: set[date] = set()
    for session, rows in sorted(drafts.items()):
        with_1545 = [r for r in rows if r.snap_1545 is not None]
        if not with_1545:
            early_sessions.add(session)
        elif len(with_1545) < len(rows):
            issues.append(
                f"session {session}: {len(rows) - len(with_1545)} row(s) lack the"
                " 15:45 snapshot in a mixed session (quote_1545 None)"
            )

    day_files: dict[date, OptionDayFile] = {}
    zero_greeks_rows = zero_bid_rows = 0
    for session, rows in sorted(drafts.items()):
        received = publication_instant(session)
        eod_ts = (
            early_close_instant(session)
            if session in early_sessions
            else session_close_instant(session)
        )
        entries: list[OptionChainEntry] = []
        for draft in rows:
            if draft.abs_delta is None:
                zero_greeks_rows += 1
                continue
            try:
                entries.append(
                    OptionChainEntry(
                        contract_id=draft.contract_id,
                        quote_1545=draft.snap_1545,
                        quote_eod=OptionQuoteSnapshot(
                            exchange_timestamp=eod_ts,
                            bid=draft.eod_bid,
                            ask=draft.eod_ask,
                            bid_size=draft.eod_bid_size,
                            ask_size=draft.eod_ask_size,
                        ),
                        open_interest=draft.open_interest,
                        same_day_volume=draft.same_day_volume,
                        abs_delta=draft.abs_delta,
                        quote_condition=REAL_QUOTE_CONDITION,
                    )
                )
            except ValidationError as exc:
                issues.append(
                    f"row {draft.line_no}: schema violation {exc.errors()[0]['msg']} — refused"
                )
        if not entries:
            issues.append(f"session {session}: no usable chain rows — no day file")
            continue
        entries.sort(key=lambda e: e.contract_id)
        for entry in entries:
            snaps = [entry.quote_eod] + ([entry.quote_1545] if entry.quote_1545 else [])
            if all(s.bid == 0 for s in snaps):
                zero_bid_rows += 1
        file_bid, file_ask = _file_level_underlying(
            session, pairs.get(session, _UnderlyingPair()), issues=issues
        )
        day_files[session] = OptionDayFile(
            underlying_security_id=selected or "",
            session=session,
            received_at=received,
            underlying_bid=file_bid,
            underlying_ask=file_ask,
            underlying_20d_median_dollar_volume=Decimal(
                "0"
            ),  # not derivable from this product — see RealOptionOverlay note
            entries=tuple(entries),
        )

    if zero_greeks_rows:
        issues.append(
            "zero-greeks rows (not delta-bearing; counted, excluded from chain"
            f" entries): {zero_greeks_rows}"
        )
    if zero_bid_rows:
        issues.append(f"zero-bid rows (mapped; every present bid is zero): {zero_bid_rows}")
    if not day_files:
        issues.append("no sessions produced a day file")

    validate_pit_invariants(day_files)
    contracts = tuple(
        sorted((_contract_of(a) for a in masters.values()), key=lambda c: c.contract_id)
    )
    return CboeEodParseResult(
        source_path=path,
        source_sha256=source_sha256,
        underlying_security_id=selected or "",
        variant=variant,
        day_files=day_files,
        stats=CboeEodStats(
            rows_total=rows_total,
            rows_mapped=sum(len(f.entries) for f in day_files.values()),
            duplicate_rows=duplicate_rows,
            zero_greeks_rows=zero_greeks_rows,
            zero_bid_rows=zero_bid_rows,
            early_close_sessions=len(early_sessions),
            nonstandard_delivery_rows=nonstandard_delivery_rows,
        ),
        issues=issues,
        contracts=contracts,
    )


def _file_level_underlying(
    session: date, pair: _UnderlyingPair, *, issues: list[str]
) -> tuple[Decimal, Decimal]:
    """The file-level underlying pair: prefer 15:45; EOD only on early
    closes; material drift between the instants is audited as an issue."""
    if pair.bid_1545 is not None and pair.ask_1545 is not None:
        bid, ask = pair.bid_1545, pair.ask_1545
        if pair.bid_eod is not None and pair.ask_eod is not None:
            mid_1545 = (bid + ask) / 2
            mid_eod = (pair.bid_eod + pair.ask_eod) / 2
            if mid_1545 != 0:
                drift = (mid_1545 - mid_eod).copy_abs() / mid_1545
                if drift > UNDERLYING_DRIFT_TOLERANCE:
                    issues.append(
                        f"session {session}: underlying 15:45 mid {mid_1545} vs EOD mid"
                        f" {mid_eod} drift {float(drift):.2%} exceeds"
                        f" {UNDERLYING_DRIFT_TOLERANCE} — file-level pair uses 15:45"
                    )
    elif pair.bid_eod is not None and pair.ask_eod is not None:
        bid, ask = pair.bid_eod, pair.ask_eod
    else:
        raise CboeEodUnderlyingQuoteError(
            f"session {session}: no underlying quote pair in the file — refusing"
        )
    if ask <= 0 or bid < 0:
        raise CboeEodUnderlyingQuoteError(
            f"session {session}: degenerate underlying quote {bid}/{ask} — refusing"
            " rather than ingesting zeros as tradable quotes"
        )
    if bid > ask:
        issues.append(f"session {session}: crossed underlying pair {bid}/{ask} kept as-is")
    return bid, ask


def _contract_of(accum: _ContractAccum) -> OptionContract:
    # Listing windows are NOT delivered by this product (spike §1): they are
    # the OBSERVED first..last session in the parsed data. Index (^-prefixed)
    # options are european cash-settled; the OptionContract schema only
    # encodes a 100-share deliverable, so cash settlement is a declared
    # approximation flagged for the coverage brief (plan §4).
    return OptionContract(
        contract_id=contract_id_of(
            accum.underlying, accum.expiration, accum.call_put, accum.strike
        ),
        option_root=accum.root,
        underlying_security_id=accum.underlying,
        expiration=accum.expiration,
        strike=accum.strike,
        call_put=accum.call_put,  # type: ignore[arg-type]
        multiplier=100,
        exercise_style="european" if accum.underlying.startswith("^") else "american",
        listing_start=accum.first_session,
        listing_end=accum.last_session,
        deliverable=_STANDARD_DELIVERABLE,
        standard_contract_flag=True,
        corporate_action_id=None,
    )


# ---- manifest (mirrors options_manifest.py + quality_options.py) ----------


def sample_sessions(sessions: tuple[date, ...], cap: int = 8) -> tuple[date, ...]:
    """Deterministic even sample of the session list (all when <= cap)."""
    if len(sessions) <= cap:
        return sessions
    indices = sorted({(i * len(sessions)) // cap for i in range(cap)})
    return tuple(sessions[i] for i in indices)


class RealOptionsManifest(StrictModel):
    """Immutable lineage for one real-data overlay: pins the source bytes,
    the full row accounting, the contract master, per-session sample-slice
    hashes over canonical day-file bytes, and binds itself via
    content_sha256 (provider/schema tokens per the M4-A plan §3)."""

    provider: str = REAL_OPTIONS_PROVIDER
    schema_version: str = REAL_OPTIONS_SCHEMA_VERSION
    underlying_security_id: IdStr
    variant: EodVariant
    source_path: str
    source_sha256: str
    rows_total: NonNegativeInt
    stats: CboeEodStats
    sessions: tuple[str, ...]
    contract_count: NonNegativeInt
    contract_master_sha256: str
    sample_slice_hashes: tuple[tuple[str, str, str], ...]  # (sid, session, sha256)
    content_sha256: str


def build_real_options_manifest(
    result: CboeEodParseResult, *, overlay: RealOptionOverlay
) -> RealOptionsManifest:
    """Pure function of (parse result, overlay derived from it)."""
    sessions = tuple(sorted(result.day_files))
    sid = result.underlying_security_id
    slices = tuple(
        (
            sid,
            session.isoformat(),
            hashlib.sha256(overlay.canonical_file_bytes(sid, session)).hexdigest(),
        )
        for session in sample_sessions(sessions)
    )
    core = RealOptionsManifest(
        underlying_security_id=sid,
        variant=result.variant,
        source_path=result.source_path.as_posix(),
        source_sha256=result.source_sha256,
        rows_total=result.stats.rows_total,
        stats=result.stats,
        sessions=tuple(s.isoformat() for s in sessions),
        contract_count=overlay.contract_count(),
        contract_master_sha256=overlay.contract_master_sha256(),
        sample_slice_hashes=slices,
        content_sha256="",
    )
    digest = hashlib.sha256()
    digest.update(REAL_MANIFEST_DOMAIN)
    digest.update(canonical_bytes(core.model_copy(update={"content_sha256": ""})))
    return core.model_copy(update={"content_sha256": digest.hexdigest()})


def verify_real_options_manifest_tokens(manifest: RealOptionsManifest) -> None:
    """Verify the manifest's declared format/provider before payload use."""
    if manifest.schema_version != REAL_OPTIONS_SCHEMA_VERSION:
        raise RealOptionsManifestError(
            f"real options manifest: schema {manifest.schema_version} "
            f"!= {REAL_OPTIONS_SCHEMA_VERSION}"
        )
    if manifest.provider != REAL_OPTIONS_PROVIDER:
        raise RealOptionsManifestError(
            f"real options manifest: provider {manifest.provider} != {REAL_OPTIONS_PROVIDER}"
        )


def verify_real_options_manifest(
    manifest: RealOptionsManifest,
    result: CboeEodParseResult,
    *,
    overlay: RealOptionOverlay,
    source_bytes: bytes | None = None,
) -> None:
    """Fail-closed re-check of (manifest, parse result, overlay, source)."""

    def fail(detail: str) -> NoReturn:
        raise RealOptionsManifestError(
            f"real options manifest for {manifest.underlying_security_id}: {detail}"
        )

    verify_real_options_manifest_tokens(manifest)
    if manifest.underlying_security_id != result.underlying_security_id:
        fail(f"underlying {manifest.underlying_security_id} != result")
    if manifest.variant != result.variant:
        fail(f"variant {manifest.variant} != {result.variant}")
    if manifest.source_sha256 != result.source_sha256:
        fail("source hash does not match the parse result")
    if overlay.source_sha256 != manifest.source_sha256:
        fail("overlay lineage does not match the manifest source")

    if source_bytes is None:
        try:
            source_bytes = result.source_path.read_bytes()
        except OSError as exc:
            fail(f"source file unreadable: {exc}")
    if sha256_hex(source_bytes) != manifest.source_sha256:
        fail("source bytes re-hash mismatch: tampered source")

    if manifest.rows_total != result.stats.rows_total:
        fail(f"rows_total {manifest.rows_total} != result {result.stats.rows_total}")
    if manifest.stats != result.stats:
        fail("row accounting does not match the parse result")
    sessions = tuple(sorted(result.day_files))
    if manifest.sessions != tuple(s.isoformat() for s in sessions):
        fail("session list drift")
    if manifest.contract_count != overlay.contract_count():
        fail(f"contract count {manifest.contract_count} != overlay")
    if manifest.contract_master_sha256 != overlay.contract_master_sha256():
        fail("contract master hash mismatch: the manifest misdescribes the master")
    if manifest.underlying_security_id != overlay.underlying_security_id:
        fail("manifest underlying does not match the overlay")

    expected = sample_sessions(sessions)
    sid = result.underlying_security_id
    recorded_keys = [(s, d) for s, d, _h in manifest.sample_slice_hashes]
    if recorded_keys != [(sid, s.isoformat()) for s in expected]:
        fail("sample-slice selection drifted (selection is deterministic)")
    for (_sid, iso, recorded), session in zip(manifest.sample_slice_hashes, expected, strict=True):
        recomputed = hashlib.sha256(overlay.canonical_file_bytes(sid, session)).hexdigest()
        if recomputed != recorded:
            fail(f"sample slice {iso} hash mismatch: tampered day file")
    for session in sessions:
        if overlay.day_file(sid, session) != result.day_files[session]:
            fail(f"overlay day file {session} differs from the parsed result")

    core = manifest.model_copy(update={"content_sha256": ""})
    digest = hashlib.sha256()
    digest.update(REAL_MANIFEST_DOMAIN)
    digest.update(canonical_bytes(core))
    if digest.hexdigest() != manifest.content_sha256:
        fail("content_sha256 does not bind the manifest body")
