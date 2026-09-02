"""Massive (Polygon) free-tier DERIVED option overlay (M4-C, $0 ruling).

`MassiveDerivedOverlay` is the free-tier derived sibling of
`tree_options.data.real_overlay.RealOptionOverlay`: it implements the read
surface `tree_options.data.options_pit.OptionPitSurface` consumes, so the
unmodified M3 PIT machinery constructs and answers publication/eligibility/
contract/ladder/expiry queries against it (duck-typed like the Cboe overlay;
static callers cast).

What the ruled $0 workaround gives this lane: per-contract DAILY AGGREGATES
with a real VWAP, volume and trade count, plus the point-in-time contract
master. `iv` and `abs_delta` are therefore DERIVED, not observed —
`tree_options.data.massive_derived.derived_abs_delta` inverts the repo's own
Black-Scholes pricer on the bar's Decimal VWAP under a versioned
`PricingAssumptions`. Vendor facts and derived fields are never conflated:
the facts (premium = the VWAP token, volume, transactions) and the derived
block (`DerivedPricing`) live on separate fields of `MassiveDerivedQuote`,
and every derived record stamps `model`, `assumptions_version` and
`provenance="model-derived-from-vwap"`.

THE FLOAT BOUNDARY (single, documented). `derived_abs_delta` is the lane's
sanctioned float island: the Decimal VWAP, the Decimal spot proxy and the
Decimal strike cross to `float` EXACTLY ONCE at its call site, and the two
float results are pinned back to `Decimal(repr(x))` at the record boundary.
No price field is ever built from, rounded through, or re-serialized via a
float; the VWAP token stays the vendor's exact Decimal end to end.

THE BID/ASK FINDING (disclosed, not papered over). The M3 quote-bearing
shapes have NO null mechanism for the quote sides: `OptionQuoteSnapshot`,
`QuoteEvent`, `OptionChainEntry` and `OptionDayFile` all carry REQUIRED
non-negative `bid`/`ask` (and `open_interest`) fields, and this tier carries
none of them (probed 2026-08-21: the snapshot endpoint answers HTTP 200
`NOT_AUTHORIZED`). A VWAP-only bar therefore CANNOT be encoded as an M3
chain entry without fabricating a two-sided market — `bid=ask=vwap` would be
a fabricated locked market, and the capability record says a bar close is
not an executable price. The overlay encodes this honestly through the
surface's own mechanisms, never fabrication:

- `entry_for` raises `ValueError`, the surface's not-in-file signal, so the
  unmodified `OptionPitSurface.candidate_snapshot` answers its documented
  None-inputs path (`abs_delta=None`, `bid=None`, ...) — the filter's
  NOT_EVALUABLE discipline, the same encoding the Cboe lane uses for
  not-delta-bearing rows (exclusion is the only fail-closed encoding).
- `day_file` / `quote_history` raise `MassiveCapabilityError` naming the
  tier: those reads REQUIRE a file-level underlying bid/ask pair and quote
  sides that no free-tier endpoint serves (the spot proxy is DECLARED
  INPUT, not a quote, and must not be laundered into one).
- derived reads live on this overlay's own surface: `derived_quote`,
  `derived_quotes_for`, `derived_stats`.

CANDIDATE WIRING IS RATIFIED (protocol 0.2.0) and lives outside this
module: `tree_options.data.massive_options.build_option_candidate_inputs`
builds an M3 `CandidateSnapshot` from one derived cell (|delta| under the
accepted `model-derived-from-vwap` provenance, the bar's volume, no
fabricated bid/ask/OI), and `tree_options.data.vwap_pit_surface.VwapPitSurface`
is the lane-2 read surface that feeds it — this module still deliberately
provides overlay reads only.

PIT semantics (identical to the Cboe lane, by reuse): an EOD bar for session
t is usable at t+1 09:00 America/New_York — `publication_of` IS
`tree_options.data.cboe_eod.publication_instant`, the shared T+1 wall, so a
decision at close(t) sees session t-1's bars, never session t's. Every
derived row carries `exchange_timestamp` = 16:00 ET close of the bar session
and `received_timestamp` = that wall, validated `exchange <= received`.

Staleness and refusals (the zero-greeks discipline, mirrored): a bar more
than `staleness_sessions` overlay sessions behind the capture frontier
provides NO derived quote (NOT_EVALUABLE "stale" — a stale VWAP is never
carried forward); a session with no bar is no trade (NOT_EVALUABLE
"no_bar"); a derivation refusal — `MassiveDerivationError` (premium under
intrinsic / outside the vol bracket), a zero-volume bar, a bar after
expiration, or a missing spot proxy — is NOT fatal: the cell is recorded
NOT_EVALUABLE with the reason, counted in `derived_stats()`, and named in
`refused` reasons, exactly as the M4-A lane disclosed its zero-greeks rows.

`weekday()`/`timedelta` arithmetic never appears here: session instants come
from `tree_options.time.sessions`, the T+1 wall and weekend-skipping from
`cboe_eod` (which routes through `time/`), and DTE is the house calendar-day
idiom `(expiration - session).days` (`candidates/filters.py`,
`options/strategy.py`). Calendar-day DTE feeds the model only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import ValidationError

from tree_options.data.cboe_eod import publication_instant
from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.massive_client import MASSIVE_PROVIDER, MassiveError, loads_exact
from tree_options.data.massive_derived import (
    MassiveDerivationError,
    PricingAssumptions,
    derived_abs_delta,
)
from tree_options.data.massive_manifest import (
    BARS_DIR,
    CAPTURE_MANIFEST_FILENAME,
    MASTERS_DIR,
    SPOT_PROXY_FILENAME,
    load_massive_capture_manifest,
    verify_massive_capture_manifest,
)
from tree_options.data.massive_options import (
    CAP_QUOTES,
    DERIVED_DELTA_PROVENANCE,
    MASSIVE_FREE_CAPABILITIES,
    MassiveContractMaster,
    MassiveDailyBar,
    MassiveOptionContract,
    build_contract_master,
    parse_daily_bars,
)
from tree_options.data.real_overlay import RealSessionCalendar
from tree_options.data.spot_token import SPOT_SENTINEL_SESSION, validated_spot_token
from tree_options.schemas.market import VwapQuoteEvent, ZeroVolumeVwapError
from tree_options.schemas.options import DeliverableSpec, OptionContract
from tree_options.synth_options.generate import CENT, contract_id_of
from tree_options.time.sessions import session_close_instant

MASSIVE_DERIVED_PROVIDER = "massive-derived-free/1"
MASSIVE_DERIVED_SCHEMA_VERSION = "m4-massive-derived/1"
# One owner of the token: massive_options owns it, this module re-exports
# (G3: the protocol's accepted-provenance list and the candidate-inputs
# builder share the same string by construction, not by copy).
DERIVATION_PROVENANCE = DERIVED_DELTA_PROVENANCE

# Domain separation for this lane's hashes (same pattern as the Cboe lane's
# REAL_CONTRACT_MASTER_DOMAIN / REAL_MANIFEST_DOMAIN).
_MASSIVE_DERIVED_MASTER_DOMAIN = b"tree-options-m4c-massive-derived-master-v1"
_MASSIVE_DERIVED_SOURCE_DOMAIN = b"tree-options-m4c-massive-derived-source-v1"

# The capture bridge's provenance stamps on every master envelope (the same
# tokens `scripts/inspect_structural_coverage.py` pins as STRING LITERALS):
# an unstamped or foreign-stamped envelope is an input of unknown origin and
# refuses here too. Bars bodies carry no stamps — the bridge writes them
# verbatim — so their provenance rides the optional capture manifest.
_KNOWN_CAPTURE_VERSIONS = frozenset({"m4b-capture/1"})
_OK_STATUSES = frozenset({"OK", "DELAYED"})

# OCC option ticker (mirrors the inspector's pattern): the master's root and
# a cross-check of its own expiration/strike/side against the typed record.
_OCC_PATTERN = re.compile(r"^O:([A-Z][A-Z0-9]{0,5})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_OCC_STRIKE_SCALE = Decimal(1000)

_STANDARD_DELIVERABLE = DeliverableSpec(shares_per_contract=Decimal("100"))

DERIVED: Literal["DERIVED"] = "DERIVED"
NOT_EVALUABLE: Literal["NOT_EVALUABLE"] = "NOT_EVALUABLE"

QuoteStatus = Literal["DERIVED", "NOT_EVALUABLE"]

STALE_REASON = "stale"
NO_BAR_REASON = "no_bar"
REFUSED_PREFIX = "refused: "


class MassiveOverlayError(MassiveError):
    """A derived-overlay load or lineage failure (fail closed)."""


@dataclass(frozen=True)
class MassiveDerivedOverlaySpec:
    """The derived counterpart of `RealOverlaySpec`: the fields the PIT
    surface reads (world_id, quote_source) plus this lane's lineage tokens."""

    world_id: str
    quote_source: str
    provider: str = MASSIVE_DERIVED_PROVIDER
    schema_version: str = MASSIVE_DERIVED_SCHEMA_VERSION
    source_sha256: str = ""


@dataclass(frozen=True)
class MassiveExpiryMeta:
    """The expiry view `OptionPitSurface.live_expiries_as_of` reads
    (`.expiration` only, mirroring `RealExpiryMeta` / `_ExpiryMeta`)."""

    expiration: date


class MassiveDerivedSessionCalendar(RealSessionCalendar):
    """Session calendar over the capture's own sessions (master as_ofs plus
    bar sessions). Daily aggregates carry no close times, so there is NO
    early-close signal: every `session_close` is the regular 16:00 ET
    instant, which is exactly the exchange timestamp stamped on derived
    rows. Reuses `RealSessionCalendar` (the Cboe lane's) with an empty
    early-close set."""

    def __init__(self, sessions: tuple[date, ...]) -> None:
        super().__init__(sessions, frozenset())
        self.name = "massive-derived-free"


@dataclass(frozen=True)
class DerivedPricing:
    """The model-derived half of a quote — kept structurally separate from
    the vendor facts so the two can never be conflated. `iv` and `abs_delta`
    are the float solver's outputs pinned once to `Decimal(repr(x))`; the
    stamps name the model and the assumption set they were derived under."""

    iv: Decimal
    abs_delta: Decimal
    model: str
    assumptions_version: str
    provenance: str


@dataclass(frozen=True)
class MassiveDerivedQuote:
    """One (contract, session) cell of the derived surface.

    Vendor facts (`premium` = the bar's exact VWAP token, `volume`,
    `transactions`) are present exactly when a bar exists — including stale
    or refused cells: a withheld derivation never erases an observed fact.
    `derived` is non-None only on DERIVED rows. The instants are the
    session's pit stamps (close of the bar session / T+1 receipt wall) and
    are present on every row, NOT_EVALUABLE included, because they describe
    the session the cell answers for."""

    contract_id: str
    option_ticker: str
    underlying_security_id: str
    session: date
    status: QuoteStatus
    reason: str | None
    premium: Decimal | None
    volume: int | None
    transactions: int | None
    exchange_timestamp: datetime
    received_timestamp: datetime
    derived: DerivedPricing | None
    provider: str = MASSIVE_DERIVED_PROVIDER
    schema_version: str = MASSIVE_DERIVED_SCHEMA_VERSION


@dataclass(frozen=True)
class MassiveDerivedStats:
    """The census the G3/evidence packet reads. Denominator: every overlay
    session inside each contract's OBSERVED listing window (masters + bars).
    `bars` counts bars matched to an overlay-master contract, so

        cells == derived_ok + not_evaluable_stale + not_evaluable_nobar
                          + not_evaluable_refused
        bars  == derived_ok + not_evaluable_stale + not_evaluable_refused

    Bars of tickers no master owns (never captured, or refused from the
    master) are named in `issues` / `unmatched_option_tickers`, not counted
    here."""

    contracts: int
    sessions: int
    bars: int
    derived_ok: int
    not_evaluable_stale: int
    not_evaluable_nobar: int
    not_evaluable_refused: int

    @property
    def cells(self) -> int:
        return (
            self.derived_ok
            + self.not_evaluable_stale
            + self.not_evaluable_nobar
            + self.not_evaluable_refused
        )


# ---- capture loading (parse side only — no client, no network) ----------------


def _dec(value: object, what: str) -> Decimal:
    """Decimal from an exact token (str/int/Decimal); a float refuses — the
    body must be decoded with `loads_exact`, never plain `json.loads`."""
    if isinstance(value, bool):
        raise MassiveOverlayError(f"{what}: bool is not a number")
    if isinstance(value, Decimal | int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except ArithmeticError:
            raise MassiveOverlayError(f"{what}: {value!r} is not a decimal") from None
    if isinstance(value, float):
        raise MassiveOverlayError(
            f"{what}: got a float — decode capture bodies with loads_exact"
            " (parse_float=Decimal) so prices keep the vendor's exact text"
        )
    raise MassiveOverlayError(f"{what}: {type(value).__name__} is not a number")


def _require_provenance(envelope: dict, *, source: str) -> None:
    provider = envelope.get("provider")
    if provider != MASSIVE_PROVIDER:
        raise MassiveOverlayError(
            f"{source}: capture envelope provider is {provider!r}, expected"
            f" {MASSIVE_PROVIDER!r} — an unstamped or foreign input has unknown origin"
        )
    version = envelope.get("capture_version")
    if version not in _KNOWN_CAPTURE_VERSIONS:
        raise MassiveOverlayError(
            f"{source}: capture envelope capture_version is {version!r}, expected one"
            f" of {sorted(_KNOWN_CAPTURE_VERSIONS)} — a capture format this overlay"
            " cannot interpret"
        )


def _as_of_of(envelope: dict, path: Path, *, source: str) -> date:
    if envelope.get("as_of") is not None:
        raw = envelope["as_of"]
        if not isinstance(raw, str):
            raise MassiveOverlayError(f"{source}: as_of is not an ISO date string")
        try:
            return date.fromisoformat(raw.strip())
        except ValueError as exc:
            raise MassiveOverlayError(f"{source}: as_of {raw!r} is not an ISO date") from exc
    matches = re.findall(r"\d{4}-\d{2}-\d{2}", path.stem)
    if not matches:
        raise MassiveOverlayError(
            f"{source}: cannot determine as_of — the envelope carries no 'as_of' and"
            " the filename has no ISO date"
        )
    if len(set(matches)) != 1:
        raise MassiveOverlayError(
            f"{source}: {sorted(set(matches))} ISO dates in the filename — ambiguous as_of"
        )
    return date.fromisoformat(matches[0])


def _pages_of(envelope: dict, *, source: str) -> list[dict]:
    """The vendor response(s) inside a capture envelope (`pages` list, or the
    envelope itself when the capture is single-page). Every page must be an
    OK vendor body with a results array — a NOT_AUTHORIZED body arrives at
    HTTP 200 and carries no data, so it refuses here, never parses empty."""
    raw_pages = envelope.get("pages")
    if raw_pages is None:
        pages: list[dict] = [envelope]
    elif isinstance(raw_pages, list) and raw_pages:
        for index, page in enumerate(raw_pages):
            if not isinstance(page, dict):
                raise MassiveOverlayError(f"{source}: pages[{index}] is not an object")
        pages = list(raw_pages)
    else:
        raise MassiveOverlayError(f"{source}: 'pages' must be a non-empty list of responses")
    for index, page in enumerate(pages):
        status = page.get("status")
        if status not in _OK_STATUSES:
            raise MassiveOverlayError(f"{source}: page {index} status {status!r} is not OK/DELAYED")
        if not isinstance(page.get("results"), list):
            raise MassiveOverlayError(f"{source}: page {index} has no 'results' list")
    return pages


def _load_masters(
    capture_dir: Path, lineage: list[tuple[str, str]]
) -> dict[tuple[str, date], MassiveContractMaster]:
    masters: dict[tuple[str, date], MassiveContractMaster] = {}
    sources: dict[tuple[str, date], str] = {}
    directory = capture_dir / MASTERS_DIR
    files = sorted(p for p in directory.glob("*.json") if p.is_file())
    if not files:
        raise MassiveOverlayError(f"{directory}: no master captures (*.json) — nothing to load")
    for path in files:
        source = path.name
        raw = path.read_bytes()
        lineage.append((f"{MASTERS_DIR}/{source}", sha256_hex(raw)))
        envelope = loads_exact(raw)
        if not isinstance(envelope, dict):
            raise MassiveOverlayError(f"{source}: top-level JSON is not an object")
        _require_provenance(envelope, source=source)
        as_of = _as_of_of(envelope, path, source=source)
        pages = _pages_of(envelope, source=source)
        records: list[Mapping[str, Any]] = []
        for page in pages:
            records.extend(page["results"])
        underlyings: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise MassiveOverlayError(f"{source}: a result row is not an object")
            symbol = record.get("underlying_ticker")
            if not isinstance(symbol, str) or not symbol:
                raise MassiveOverlayError(f"{source}: a result row carries no underlying_ticker")
            underlyings.add(symbol)
        if len(underlyings) != 1:
            raise MassiveOverlayError(
                f"{source}: {sorted(underlyings)} underlyings in one master —"
                " one master per (underlying, as_of)"
            )
        underlying = underlyings.pop()
        declared = envelope.get("underlying_ticker")
        if isinstance(declared, str) and declared and declared != underlying:
            raise MassiveOverlayError(
                f"{source}: envelope underlying {declared!r} != records {underlying!r}"
            )
        master = build_contract_master(
            records, underlying=underlying, as_of=as_of, pages_fetched=len(pages)
        )
        if not master.contracts:
            raise MassiveOverlayError(
                f"{source}: contract master is empty — a capture failure, not a fact"
            )
        key = (underlying, as_of)
        if key in masters:
            raise MassiveOverlayError(
                f"{source}: duplicate master for {underlying} as of {as_of}"
                f" (already loaded from {sources[key]})"
            )
        masters[key] = master
        sources[key] = source
    return masters


def _load_bars(
    capture_dir: Path, lineage: list[tuple[str, str]]
) -> dict[str, tuple[MassiveDailyBar, ...]]:
    series: dict[str, tuple[MassiveDailyBar, ...]] = {}
    directory = capture_dir / BARS_DIR
    if not directory.is_dir():
        return series
    for path in sorted(p for p in directory.glob("*.json") if p.is_file()):
        source = path.name
        raw = path.read_bytes()
        lineage.append((f"{BARS_DIR}/{source}", sha256_hex(raw)))
        body = loads_exact(raw)
        if not isinstance(body, dict):
            raise MassiveOverlayError(f"{source}: top-level JSON is not an object")
        status = body.get("status")
        if status not in _OK_STATUSES:
            raise MassiveOverlayError(
                f"{source}: status {status!r} is not OK/DELAYED — a NOT_AUTHORIZED"
                " body arrives at HTTP 200 and carries no data"
            )
        ticker = body.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise MassiveOverlayError(
                f"{source}: cannot determine the option ticker — the vendor body carries 'ticker'"
            )
        if ticker in series:
            raise MassiveOverlayError(f"{source}: duplicate bar series for {ticker}")
        series[ticker] = parse_daily_bars(body, option_ticker=ticker)
    return series


def _validated_spot_token(where: str, session: date, value: object) -> Decimal:
    """(R5-P2, Codex round 5) ONE row discipline for the ORDINARY spot
    proxy, shared by `_load_spot` (the file path) and the lane-2 adapter's
    constructor copy loop (`VwapPitSurface(spot=...)`) — the same shape
    `_validated_spot_v2_row` gives the v2 dollar-volume source, so an
    injected mapping can never carry what a file cannot.

    The value must be an EXACT decimal — the file's string token, parsed,
    or an already-exact Decimal (an int stays the file path's own accepted
    token) — and must be FINITE and positive. Finiteness comes FIRST:
    `_dec` accepts "Infinity" and it is POSITIVE-looking, so the old
    `<= 0`-only gate let it LOAD — an infinite spot then flows into
    intrinsic and the election policy, where any finite bid is below
    Infinity * 0.98 and malformed input forces an early-exercise election —
    while "NaN" raises InvalidOperation on comparison and escaped as a raw
    arithmetic crash. Refusals name the underlying (`where`), the session,
    and the token.

    (R6-P2, Codex round 6) the VALIDATION BODY now lives in
    `tree_options.data.spot_token` — the ONE contract, additionally shared
    with the census scripts (`inspect_structural_coverage` /
    `build_coverage_census`), whose own `_dec` + `<= 0` parser was a SECOND
    contract that accepted "Infinity". This wrapper keeps this module's own
    error shape (`MassiveOverlayError`) and its private name (the adapter's
    constructor imports it); it validates, never transforms."""
    return validated_spot_token(where, session, value, refuse=MassiveOverlayError)


def _load_spot(
    capture_dir: Path,
    lineage: list[tuple[str, str]],
    *,
    raw: bytes | None = None,
) -> dict[str, dict[date, Decimal]]:
    """`spot_proxy.json` — DECLARED INPUT, never a vendor quote: this tier
    carries no underlying price, and the derived lane says NOT_EVALUABLE
    (refused) without one rather than guessing a spot. Per-row VALUE
    validation is `_validated_spot_token` (R5-P2): exact, FINITE,
    positive — an infinite spot is malformed input, never a price.

    Round-5 review fix (2026-08-24, finding 4): ``raw`` lets a caller hand
    in the EXACT bytes the capture-manifest verification hashed (and
    re-verified against its pin) instead of a fresh disk read — the
    regeneration path must consume the same bytes the sealed manifest
    attests. ``None`` keeps the historical read-from-disk behavior (this
    lane's own flow)."""
    path = capture_dir / SPOT_PROXY_FILENAME
    if raw is None:
        if not path.is_file():
            return {}
        raw = path.read_bytes()
    lineage.append((SPOT_PROXY_FILENAME, sha256_hex(raw)))
    payload = loads_exact(raw)
    if not isinstance(payload, dict):
        raise MassiveOverlayError(f"{path.name}: top-level JSON is not an object")
    proxy: dict[str, dict[date, Decimal]] = {}
    for underlying, value in payload.items():
        if not isinstance(underlying, str) or not underlying:
            raise MassiveOverlayError(f"{path.name}: spot proxy key {underlying!r} is not a symbol")
        where = f"{path.name}[{underlying!r}]"
        sessions: dict[date, Decimal] = {}
        if isinstance(value, dict):
            for as_of, spot in value.items():
                try:
                    session = date.fromisoformat(str(as_of).strip())
                except ValueError as exc:
                    raise MassiveOverlayError(f"{where}: key {as_of!r} is not an ISO date") from exc
                sessions[session] = _validated_spot_token(where, session, spot)
        else:
            # The flat form {"SPY": "5750.00"} declares one spot for every
            # session; the shared sentinel (date.min — R6-P2 gave it one
            # owner, `tree_options.data.spot_token`) is the key the census
            # reads as covering every session too.
            sessions[SPOT_SENTINEL_SESSION] = _validated_spot_token(
                where, SPOT_SENTINEL_SESSION, value
            )
        proxy[underlying] = sessions
    return proxy


def load_spot_proxy(path: Path) -> dict[str, dict[date, Decimal]]:
    """Parse one `spot_proxy.json` (DECLARED INPUT) under exactly the
    discipline `_load_spot` applies — exact Decimal tokens, ISO session
    keys (or the flat one-spot-for-every-session form, keyed `date.min`),
    FINITE positive values, fail-closed on anything else.

    The lane-2 PIT adapter (`data.vwap_pit_surface.VwapPitSurface`) reads
    the coverage-era spot proxy through this loader; the bytes it parses
    are the caller's declared input, never a vendor quote."""
    path = Path(path)
    return _load_spot(path.parent, [], raw=path.read_bytes())


# ---- contract master construction --------------------------------------------


def _identity_of(row: MassiveOptionContract) -> tuple[object, ...]:
    """The economics a ticker's rows must agree on across as_ofs (descriptive
    fields like exchange/cfi may drift; the economics may not)."""
    return (
        row.underlying,
        row.expiration,
        row.strike,
        row.contract_type,
        row.exercise_style,
        row.shares_per_contract,
    )


def _option_contract_of(
    row: MassiveOptionContract, span: tuple[date, date], *, refused: list[str]
) -> OptionContract | None:
    """One vendor master row -> a repo `OptionContract`, or a NAMED refusal.

    Refused (appended to `refused`, excluded from the master — the row
    accounting discipline; the M3 schema cannot encode these truthfully):
    a nonstandard deliverable (the schema demands a corporate-action id
    this tier cannot know), a strike off the cent grid (the canonical id
    cannot round-trip it), a ticker that is not an OCC option ticker, a
    ticker that disagrees with its own typed row (vendor corruption —
    refuse rather than pick a side), and a listing window the schema
    rejects. The listing window is the OBSERVED first..last session."""
    what = row.ticker
    match = _OCC_PATTERN.match(what)
    if match is None:
        refused.append(f"{what}: not an OCC option ticker — refused")
        return None
    root, yy, mm, dd, call_put, strike_digits = match.groups()
    try:
        occ_expiration = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        refused.append(f"{what}: encodes an impossible expiration — refused")
        return None
    occ_strike = Decimal(int(strike_digits)) / _OCC_STRIKE_SCALE
    side = "C" if row.contract_type == "call" else "P"
    if occ_expiration != row.expiration or occ_strike != row.strike or side != call_put:
        refused.append(
            f"{what}: ticker disagrees with its record (exp {occ_expiration}/"
            f"{row.expiration}, strike {occ_strike}/{row.strike}, side {call_put}/"
            f"{side}) — refused"
        )
        return None
    if row.shares_per_contract != 100:
        refused.append(
            f"{what}: shares_per_contract {row.shares_per_contract} != 100 —"
            " nonstandard deliverable; the OptionContract schema demands a"
            " corporate-action id this tier cannot know"
        )
        return None
    if row.strike != row.strike.quantize(CENT):
        refused.append(
            f"{what}: strike {row.strike} is off the cent grid — the canonical"
            " contract id cannot round-trip it"
        )
        return None
    try:
        return OptionContract(
            contract_id=contract_id_of(row.underlying, row.expiration, side, row.strike),
            option_root=root,
            underlying_security_id=row.underlying,
            expiration=row.expiration,
            strike=row.strike.quantize(CENT),
            call_put=side,  # type: ignore[arg-type]
            multiplier=100,
            exercise_style=row.exercise_style,
            listing_start=span[0],
            listing_end=span[1],
            deliverable=_STANDARD_DELIVERABLE,
            standard_contract_flag=True,
            corporate_action_id=None,
        )
    except ValidationError as exc:
        refused.append(f"{what}: schema violation {exc.errors()[0]['msg']} — refused")
        return None


def _parse_canonical_id(contract_id: str) -> tuple[str, date, str, Decimal] | None:
    """`OPT-{sid}-{yymmdd}-{C/P}-{strike-cents}` -> its parts, or None. The
    sid segment may itself contain dashes (parts[1:-3], as in the Cboe
    overlay's decomposer)."""
    parts = contract_id.split("-")
    if len(parts) < 5 or parts[0] != "OPT":
        return None
    strike_cents, call_put, yymmdd = parts[-1], parts[-2], parts[-3]
    sid = "-".join(parts[1:-3])
    if (
        len(strike_cents) != 8
        or not strike_cents.isdigit()
        or call_put not in ("C", "P")
        or len(yymmdd) != 6
        or not yymmdd.isdigit()
        or not sid
    ):
        return None
    try:
        expiration = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return sid, expiration, call_put, Decimal(strike_cents).scaleb(-2)


# ---- the overlay ---------------------------------------------------------------


class MassiveDerivedOverlay:
    """The derived read surface over one Massive free-tier capture.

    The census runs once at construction: every (contract, session) cell
    inside a contract's observed window is classified once, derivations run
    there, and refusals are recorded — never raised — so the overlay is
    always loadable evidence of exactly what the tier could and could not
    derive (the M4-A zero-greeks discipline)."""

    def __init__(
        self,
        *,
        masters: dict[tuple[str, date], MassiveContractMaster],
        bars: dict[str, tuple[MassiveDailyBar, ...]],
        spot: dict[str, dict[date, Decimal]],
        lineage: tuple[tuple[str, str], ...],
        assumptions: PricingAssumptions,
        staleness_sessions: int,
        issues: tuple[str, ...] = (),
        spot_v2: Mapping[str, Mapping[date, tuple[Decimal, int]]] | None = None,
    ) -> None:
        if staleness_sessions < 0:
            raise ValueError(f"staleness_sessions must be >= 0, got {staleness_sessions}")
        self.assumptions = assumptions
        self.staleness_sessions = staleness_sessions
        self.source_sha256 = sha256_hex(
            _MASSIVE_DERIVED_SOURCE_DOMAIN
            + "".join(f"{path}:{digest}\n" for path, digest in lineage).encode()
        )
        self._masters = dict(masters)
        self._bars = {ticker: {bar.session: bar for bar in one} for ticker, one in bars.items()}
        self._spot = spot
        # (remediation-3, owner ruling 2026-09-02) the DAILY spot source: the
        # v2 dollar-volume sidecar's closes, consulted BEFORE the capture's
        # Friday-only spot proxy so a NON-Friday bar session (the T+1-visible
        # Thursday of a close(t)-Friday decision) can derive at all. Event-3's
        # root cause was exactly this gap: option bars are daily, the v1 proxy
        # is 99 sessions all Fridays, so every Thursday-visible cell refused
        # "no spot proxy", no candidate ever carried a derived |delta|
        # (no_in_band_strike 312/312), and the gate FAILED criterion 2 with
        # zero candidates constructed. The v1 proxy stays the BACKSTOP for
        # sessions the sidecar does not cover (the era's first Fridays) and
        # the flat-form sentinel keeps its meaning under it. Validation is the
        # SAME copy-loop discipline as the adapter's (R5-P2/R4-P2): an
        # injected mapping can never carry what a file cannot.
        self._spot_v2: dict[str, dict[date, Decimal]] = {}
        for v2_underlying, v2_sessions in (spot_v2 or {}).items():
            v2_where = f"spot_v2[{v2_underlying!r}]"
            v2_rows: dict[date, Decimal] = {}
            for v2_session, v2_row in v2_sessions.items():
                try:
                    v2_close, _v2_volume = v2_row
                except (TypeError, ValueError):
                    raise MassiveOverlayError(
                        f"{v2_where}[{v2_session.isoformat()}]: each session must"
                        " carry exactly the keys close+volume, got"
                        f" {v2_row!r}"
                    ) from None
                v2_rows[v2_session] = _validated_spot_token(v2_where, v2_session, v2_close)
            self._spot_v2[v2_underlying] = v2_rows
        self.underlyings: tuple[str, ...] = tuple(sorted({sid for sid, _as_of in masters}))
        extra_issues = list(issues)

        # ---- observed sessions per ticker (masters first, then bars) ----------
        observed: dict[str, set[date]] = {}
        for (_sid, as_of), master in masters.items():
            for row in master.contracts:
                observed.setdefault(row.ticker, set()).add(as_of)
        for ticker, one in self._bars.items():
            if ticker in observed:
                observed[ticker].update(one)

        # ---- contract master: vendor rows -> repo OptionContracts ------------
        # One contract observed on several as_ofs is the NORMAL case: rows are
        # grouped by ticker and must agree on their economics — a restatement
        # (shares/style/strike/expiry) or two tickers colliding on one canonical
        # id (the M4-A SPX/SPXW finding) is audited and the side(s) refused,
        # exactly as the Cboe lane audits root conflicts: first kept, never a
        # silent pick between disagreeing identities.
        rows_by_ticker: dict[str, list[MassiveOptionContract]] = {}
        for master in masters.values():
            for row in master.contracts:
                rows_by_ticker.setdefault(row.ticker, []).append(row)
        refused: list[str] = []
        contracts: dict[str, OptionContract] = {}
        id_to_ticker: dict[str, str] = {}
        for ticker in sorted(rows_by_ticker):
            rows = rows_by_ticker[ticker]
            if any(_identity_of(row) != _identity_of(rows[0]) for row in rows[1:]):
                refused.append(
                    f"{ticker}: identity restated across as_ofs (underlying/expiry/"
                    "strike/side/style/shares disagree) — refused rather than pick"
                )
                continue
            span = observed[ticker]
            contract = _option_contract_of(rows[0], (min(span), max(span)), refused=refused)
            if contract is None:
                continue
            existing = contracts.get(contract.contract_id)
            if existing is not None:
                refused.append(
                    f"{ticker}: canonical id collision with"
                    f" {id_to_ticker[existing.contract_id]} on {contract.contract_id}"
                    " — first kept, this row refused"
                )
                continue
            contracts[contract.contract_id] = contract
            id_to_ticker[contract.contract_id] = ticker
        if not contracts:
            raise MassiveOverlayError(
                f"no contracts representable in the overlay master — refusals: {refused[:3]}"
            )
        self._contracts = contracts
        self._id_to_ticker = id_to_ticker
        self._refused_master_contracts: tuple[str, ...] = tuple(refused)

        self._contracts_by_sid: dict[str, list[OptionContract]] = {
            sid: [] for sid in self.underlyings
        }
        for cid in sorted(contracts):
            self._contracts_by_sid[contracts[cid].underlying_security_id].append(contracts[cid])

        # ---- sessions: master as_ofs plus every bar session -------------------
        sessions = {as_of for (_sid, as_of) in masters}
        for one in self._bars.values():
            sessions.update(one)
        self._sessions: tuple[date, ...] = tuple(sorted(sessions))
        self._ordinals = {session: i for i, session in enumerate(self._sessions)}
        self.calendar = MassiveDerivedSessionCalendar(self._sessions)

        # ---- ladders / expiry windows over the mapped master ------------------
        ladder_sets: dict[tuple[str, date], set[Decimal]] = {}
        windows: dict[tuple[str, date], tuple[date, date]] = {}
        for contract in contracts.values():
            key = (contract.underlying_security_id, contract.expiration)
            ladder_sets.setdefault(key, set()).add(contract.strike)
            start, end = windows.get(key, (contract.listing_start, contract.listing_start))
            last = contract.listing_end or contract.listing_start
            windows[key] = (min(start, contract.listing_start), max(end, last))
        self._ladders = {key: tuple(sorted(s)) for key, s in ladder_sets.items()}
        self._expiry_windows = dict(windows)

        # ---- bars nobody's master owns: named, never silently dropped ----------
        owned = set(id_to_ticker.values())
        self._unmatched_option_tickers = tuple(sorted(set(self._bars) - owned))
        for ticker in self._unmatched_option_tickers:
            extra_issues.append(
                f"bar series {ticker}: no contract master owns it — named, not censused"
            )

        # ---- per-(underlying, session) bar coverage for has_file ----------------
        self._bar_sessions_by_sid: dict[str, set[date]] = {sid: set() for sid in self.underlyings}
        for cid, contract in contracts.items():
            for session in self._bars.get(id_to_ticker[cid], {}):
                self._bar_sessions_by_sid[contract.underlying_security_id].add(session)

        # ---- the derivation census ---------------------------------------------
        self._quotes, self._stats = self._census()

        self.issues: tuple[str, ...] = tuple(extra_issues)
        self.spec = MassiveDerivedOverlaySpec(
            world_id=f"massive-derived/{'+'.join(self.underlyings)}/{self.source_sha256[:12]}",
            quote_source=MASSIVE_DERIVED_PROVIDER,
            source_sha256=self.source_sha256,
        )

    # ---- the census ------------------------------------------------------------

    def _window_of(self, contract: OptionContract) -> tuple[date, ...]:
        """The overlay sessions inside the contract's observed listing window."""
        lo = self._ordinals[contract.listing_start]
        hi = self._ordinals[contract.listing_end or contract.listing_start]
        return self._sessions[lo : hi + 1]

    def _census(self) -> tuple[dict[tuple[str, date], MassiveDerivedQuote], MassiveDerivedStats]:
        quotes: dict[tuple[str, date], MassiveDerivedQuote] = {}
        derived_ok = stale_count = nobar_count = refused_count = 0
        frontier = len(self._sessions) - 1
        for cid in sorted(self._contracts):
            contract = self._contracts[cid]
            ticker = self._id_to_ticker[cid]
            bars = self._bars.get(ticker, {})
            for session in self._window_of(contract):
                fresh = (frontier - self._ordinals[session]) <= self.staleness_sessions
                quote = self._cell(contract, ticker, session, bar=bars.get(session), fresh=fresh)
                if quote.exchange_timestamp > quote.received_timestamp:
                    raise MassiveOverlayError(
                        f"{cid} on {session}: exchange {quote.exchange_timestamp} >"
                        f" received {quote.received_timestamp} — PIT invariant violated"
                    )
                quotes[(cid, session)] = quote
                if quote.status == DERIVED:
                    derived_ok += 1
                elif quote.reason == STALE_REASON:
                    stale_count += 1
                elif quote.reason == NO_BAR_REASON:
                    nobar_count += 1
                else:
                    refused_count += 1
        matched_bars = sum(
            len(self._bars.get(ticker, {})) for ticker in self._id_to_ticker.values()
        )
        stats = MassiveDerivedStats(
            contracts=len(self._contracts),
            sessions=len(self._sessions),
            bars=matched_bars,
            derived_ok=derived_ok,
            not_evaluable_stale=stale_count,
            not_evaluable_nobar=nobar_count,
            not_evaluable_refused=refused_count,
        )
        return quotes, stats

    def _cell(
        self,
        contract: OptionContract,
        ticker: str,
        session: date,
        *,
        bar: MassiveDailyBar | None,
        fresh: bool,
    ) -> MassiveDerivedQuote:
        """Classify one (contract, session) cell. Order: no bar, then
        staleness, then the per-bar honesty guards, then the derivation."""
        exchange = session_close_instant(session)
        received = publication_instant(session)

        def quote(
            status: QuoteStatus, reason: str | None, derived: DerivedPricing | None
        ) -> MassiveDerivedQuote:
            return MassiveDerivedQuote(
                contract_id=contract.contract_id,
                option_ticker=ticker,
                underlying_security_id=contract.underlying_security_id,
                session=session,
                status=status,
                reason=reason,
                premium=None if bar is None else bar.vwap,
                volume=None if bar is None else bar.volume,
                transactions=None if bar is None else bar.transactions,
                exchange_timestamp=exchange,
                received_timestamp=received,
                derived=derived,
            )

        if bar is None:
            return quote(NOT_EVALUABLE, NO_BAR_REASON, None)  # no trade that session
        if not fresh:
            return quote(NOT_EVALUABLE, STALE_REASON, None)  # never carried forward
        if session > contract.expiration:
            return quote(
                NOT_EVALUABLE,
                f"{REFUSED_PREFIX}bar after expiration {contract.expiration}",
                None,
            )
        if bar.volume == 0:
            return quote(
                NOT_EVALUABLE,
                f"{REFUSED_PREFIX}zero-volume bar — no trades, the VWAP token is not a trade price",
                None,
            )
        spot = self._spot_for(contract.underlying_security_id, session)
        if spot is None:
            return quote(
                NOT_EVALUABLE,
                f"{REFUSED_PREFIX}no spot proxy for {contract.underlying_security_id} on {session}",
                None,
            )
        # THE FLOAT BOUNDARY (see module docstring): the exact vendor Decimals
        # cross to float exactly once, here, into the sanctioned model island.
        try:
            iv, abs_delta = derived_abs_delta(
                premium=float(bar.vwap),
                spot=float(spot),
                strike=float(contract.strike),
                dte_calendar_days=(contract.expiration - session).days,
                call_put=contract.call_put,
                assumptions=self.assumptions,
            )
        except MassiveDerivationError as exc:
            return quote(NOT_EVALUABLE, f"{REFUSED_PREFIX}{exc}", None)
        return quote(
            DERIVED,
            None,
            DerivedPricing(
                iv=Decimal(repr(iv)),
                abs_delta=Decimal(repr(abs_delta)),
                model=self.assumptions.model,
                assumptions_version=self.assumptions.version,
                provenance=DERIVATION_PROVENANCE,
            ),
        )

    def _spot_for(self, sid: str, session: date) -> Decimal | None:
        # (remediation-3) the declared DAILY source first — the v1 Friday
        # proxy cannot answer a Thursday, and a Thursday-answerable spot is
        # the entire point of the v2 wiring; the v1 per-session proxy then
        # the flat-form sentinel follow as the backstop chain.
        daily = self._spot_v2.get(sid)
        if daily and session in daily:
            return daily[session]
        per_underlying = self._spot.get(sid)
        if not per_underlying:
            return None
        if session in per_underlying:
            return per_underlying[session]
        return per_underlying.get(date.min)

    # ---- derived reads (this lane's own surface) ------------------------------

    def derived_quote(self, contract_id: str, session: date) -> MassiveDerivedQuote:
        """The derived cell for one (contract, session). Unknown contracts
        refuse; a known contract with no censused cell (no trade evidence
        observed for that session) answers NOT_EVALUABLE `no_bar` — never a
        guess."""
        if contract_id not in self._contracts:
            raise MassiveOverlayError(f"unknown contract: {contract_id}")
        quote = self._quotes.get((contract_id, session))
        if quote is None:
            contract = self._contracts[contract_id]
            return self._cell(
                contract, self._id_to_ticker[contract_id], session, bar=None, fresh=True
            )
        return quote

    def derived_quotes_for(self, contract_id: str) -> tuple[MassiveDerivedQuote, ...]:
        """Every censused cell of one contract, session-ascending."""
        if contract_id not in self._contracts:
            raise MassiveOverlayError(f"unknown contract: {contract_id}")
        return tuple(
            self._quotes[(contract_id, session)]
            for session in self._sessions
            if (contract_id, session) in self._quotes
        )

    def derived_quotes(self) -> tuple[MassiveDerivedQuote, ...]:
        """Every censused cell, (contract_id, session)-ascending."""
        return tuple(self._quotes[key] for key in sorted(self._quotes))

    def derived_stats(self) -> MassiveDerivedStats:
        return self._stats

    @property
    def refused_master_contracts(self) -> tuple[str, ...]:
        """Vendor master rows refused from the overlay master, each naming its
        reason (the row-accounting discipline — never a silent drop)."""
        return self._refused_master_contracts

    @property
    def unmatched_option_tickers(self) -> tuple[str, ...]:
        """Bar-series tickers no overlay-master contract owns — counted and
        named, excluded from the census."""
        return self._unmatched_option_tickers

    # ---- eligibility / sessions ------------------------------------------------

    def world_sessions(self) -> tuple[date, ...]:
        return self._sessions

    def publication_of(self, session: date) -> datetime:
        """The T+1 receipt wall — the SAME helper the Cboe lane uses
        (`cboe_eod.publication_instant`): 09:00 America/New_York on the next
        weekend-skipping session. An EOD bar for session t is usable exactly
        from here on."""
        if session not in self._ordinals:
            raise MassiveOverlayError(f"no captured session {session}")
        return publication_instant(session)

    def eligible_on(self, session: date) -> tuple[str, ...]:
        return tuple(sid for sid in self.underlyings if self.has_file(sid, session))

    def eligible_sessions(self, sid: str) -> tuple[date, ...]:
        return tuple(session for session in self._sessions if self.has_file(sid, session))

    def underlyings_ever_eligible(self) -> tuple[str, ...]:
        return self.underlyings

    def has_file(self, sid: str, session: date) -> bool:
        """The (underlying, session) has captured data: a master snapshot
        whose as_of is that session, or a bar of one of its contracts."""
        if (sid, session) in self._masters:
            return True
        return session in self._bar_sessions_by_sid.get(sid, set())

    def has_any_file(self, session: date) -> bool:
        return session in self._ordinals

    # ---- contract master --------------------------------------------------------

    def contracts_for(self, sid: str) -> tuple[OptionContract, ...]:
        return tuple(self._contracts_by_sid.get(sid, ()))

    def contract(self, contract_id: str) -> OptionContract:
        """The master row when observed; otherwise the well-formed C/P grid
        cell of an observed ladder (same encoding as the Cboe overlay: the
        surface enumerates the full grid, and a synthesized cell simply has
        no entries anywhere — every quoting path answers NOT_EVALUABLE/None,
        never a fabricated quote). Malformed or foreign ids fail closed."""
        master = self._contracts.get(contract_id)
        if master is not None:
            return master
        parsed = _parse_canonical_id(contract_id)
        if parsed is None:
            raise ValueError(f"unknown contract: {contract_id}")
        sid, expiration, call_put, strike = parsed
        ladder = self._ladders.get((sid, expiration))
        if ladder is None or strike.quantize(CENT) not in ladder:
            raise ValueError(f"unknown contract: {contract_id}")
        siblings = [
            c
            for c in self._contracts_by_sid.get(sid, ())
            if c.expiration == expiration and c.strike == strike.quantize(CENT)
        ]
        if not siblings:  # pragma: no cover — the ladder is built from these
            raise ValueError(f"unknown contract: {contract_id}")
        start, end = self._expiry_windows[(sid, expiration)]
        return siblings[0].model_copy(
            update={
                "contract_id": contract_id,
                "call_put": call_put,  # type: ignore[arg-type]
                "listing_start": start,
                "listing_end": end,
            }
        )

    def contract_count(self) -> int:
        return len(self._contracts)

    def contract_master_sha256(self) -> str:
        digest = hashlib.sha256()
        digest.update(_MASSIVE_DERIVED_MASTER_DOMAIN)
        for cid in sorted(self._contracts):
            digest.update(canonical_bytes(self._contracts[cid]))
        return digest.hexdigest()

    def ladder_for(self, sid: str, expiration: date) -> tuple[Decimal, ...]:
        ladder = self._ladders.get((sid, expiration))
        if ladder is None:
            raise ValueError(f"{sid} has no expiry {expiration}")
        return ladder

    def live_expiries_on(self, sid: str, session: date) -> tuple[MassiveExpiryMeta, ...]:
        """Expiries of overlay-master contracts that EXIST on the session —
        the OBSERVED listing window (INV-09: existence is a session-date
        fact, not an availability fact) — gated on the session carrying
        captured data for the underlying (`has_file`)."""
        if not self.has_file(sid, session):
            return ()
        return tuple(
            MassiveExpiryMeta(e)
            for e in sorted(
                {c.expiration for c in self._contracts_by_sid.get(sid, ()) if c.exists_on(session)}
            )
        )

    # ---- chains / quotes: the capability boundary -------------------------------

    def day_file(self, sid: str, session: date) -> NoReturn:
        """Always refuses. `OptionDayFile` requires a file-level underlying
        bid/ask pair; the free tier serves no underlying quote (equity
        aggregates are a different endpoint) and the spot proxy is DECLARED
        INPUT, not a quote — dressing it up as both sides would fabricate a
        locked market. `spot_mid_as_of` therefore fails closed too."""
        MASSIVE_FREE_CAPABILITIES.require(CAP_QUOTES)
        raise AssertionError  # pragma: no cover — require() raises first

    def entry_for(self, sid: str, session: date, contract_id: str) -> NoReturn:
        """Always refuses with `ValueError` — the surface's own not-in-file
        signal, so the unmodified `OptionPitSurface.candidate_snapshot` answers
        its documented None-inputs / NOT_EVALUABLE path (see the bid/ask
        finding in the module docstring). Derived reads live at
        `derived_quote`; candidate wiring is RATIFIED (protocol 0.2.0) and
        lives outside this module — `build_option_candidate_inputs` builds
        the snapshot from a derived cell and
        `data.vwap_pit_surface.VwapPitSurface` is the lane-2 read surface
        that supplies it — so no chain entry is ever fabricated here."""
        raise ValueError(
            f"no M3 chain entry for {contract_id} on {session}:"
            f" {MASSIVE_DERIVED_PROVIDER} carries no bid/ask and no open interest —"
            " an OptionChainEntry would have to fabricate them; see"
            " MassiveDerivedOverlay.derived_quote"
        )

    def quote_history(self, contract_id: str) -> NoReturn:
        """Always refuses. `QuoteEvent` requires bid/ask sides; this tier has
        none (see the capability record)."""
        MASSIVE_FREE_CAPABILITIES.require(CAP_QUOTES)
        raise AssertionError  # pragma: no cover — require() raises first

    def median_dollar_volume(self, sid: str, session: date) -> Decimal:
        """Declared NOT_EVALUABLE-equivalent sentinel `Decimal("0")`, mirroring
        `RealOptionOverlay`: underlying dollar volume needs the equity
        aggregates endpoint this lane never calls. Returning None would crash
        `AsOf` comparisons; consumers set their liquidity threshold to 0."""
        if not self.has_file(sid, session):
            raise ValueError(f"no file for {sid} on {session}")
        return Decimal("0")


def vwap_quote_event(quote: MassiveDerivedQuote) -> VwapQuoteEvent:
    """One derived cell's VENDOR FACTS as the G3 vwap quote kind.

    This is the bar, not the model: `vwap` is the cell's exact Decimal VWAP
    token, `volume`/`trade_count` the observed counts, and the instants are
    the cell's own pit stamps — `received_timestamp` is the T+1 receipt
    wall, so a fill against this event can never land inside the bar's own
    session. Condition is "regular" by construction: this endpoint
    aggregates regular-session prints only (there is no per-print
    condition feed on the tier to misread). Refuses cells without a bar
    or with zero volume — the fill door's own graded refusals, raised
    here so the conversion never fabricates a tradable-looking event.
    """
    if quote.premium is None or quote.volume is None:
        raise MassiveOverlayError(
            f"no bar for {quote.contract_id} on {quote.session}:"
            " a vwap quote event needs the vendor's own aggregate"
        )
    if quote.volume < 1:
        raise ZeroVolumeVwapError(
            f"zero-volume session {quote.session} for {quote.contract_id}:"
            " unfillable, never converted"
        )
    return VwapQuoteEvent(
        contract_id=quote.contract_id,
        session=quote.session,
        exchange_timestamp=quote.exchange_timestamp,
        received_timestamp=quote.received_timestamp,
        vwap=quote.premium,
        volume=quote.volume,
        trade_count=quote.transactions or 0,
        quote_condition="regular",
        source=MASSIVE_DERIVED_PROVIDER,
    )


def load_derived_surface(
    capture_dir: Path,
    *,
    assumptions: PricingAssumptions | None = None,
    staleness_sessions: int = 5,
    spot_v2: Mapping[str, Mapping[date, tuple[Decimal, int]]] | None = None,
) -> MassiveDerivedOverlay:
    """Load one capture directory (masters/ + bars/ + optional spot_proxy.json
    and capture_manifest.json) into a derived overlay.

    Parse side only: captured files from disk, no client, no network, no key.
    Defaults: `assumptions=PricingAssumptions()` (the repo-consistent
    synthetic-world defaults) and `staleness_sessions=5`. When a capture
    manifest is present it is verified fail-closed first — a capture whose
    files do not reconcile with its manifest refuses here rather than
    loading unprovenance bytes.

    (remediation-3) `spot_v2` is the OPTIONAL declared daily underlying
    source (the sidecar's parsed close+volume rows): its closes are
    consulted BEFORE the capture's own Friday-only spot proxy so non-Friday
    bar sessions can derive (event-3's root cause). `None` keeps the
    historical Friday-only spot semantics."""

    capture_dir = Path(capture_dir)
    if not capture_dir.is_dir():
        raise MassiveOverlayError(f"{capture_dir}: not a capture directory")

    issues: list[str] = []
    manifest_path = capture_dir / CAPTURE_MANIFEST_FILENAME
    if manifest_path.is_file():
        manifest = load_massive_capture_manifest(manifest_path)
        if manifest.capture_version not in _KNOWN_CAPTURE_VERSIONS:
            raise MassiveOverlayError(
                f"{manifest_path}: capture_version {manifest.capture_version!r} is not"
                f" one of {sorted(_KNOWN_CAPTURE_VERSIONS)}"
            )
        verify_massive_capture_manifest(
            manifest, capture_dir, capture_version=manifest.capture_version
        )
    else:
        issues.append(
            f"no {CAPTURE_MANIFEST_FILENAME}: lineage is the raw file hashes only"
            " (manifest verification is available but was not supplied)"
        )

    lineage: list[tuple[str, str]] = []
    masters = _load_masters(capture_dir, lineage)
    bars = _load_bars(capture_dir, lineage)
    spot = _load_spot(capture_dir, lineage)
    if not bars:
        issues.append("no bar captures under bars/: every cell is NOT_EVALUABLE no_bar")
    if not spot:
        issues.append(f"no {SPOT_PROXY_FILENAME}: every derivation refuses (no spot proxy)")
    return MassiveDerivedOverlay(
        masters=masters,
        bars=bars,
        spot=spot,
        lineage=tuple(lineage),
        assumptions=assumptions if assumptions is not None else PricingAssumptions(),
        staleness_sessions=staleness_sessions,
        issues=tuple(issues),
        spot_v2=spot_v2,
    )


__all__ = [
    "DERIVATION_PROVENANCE",
    "DERIVED",
    "MASSIVE_DERIVED_PROVIDER",
    "MASSIVE_DERIVED_SCHEMA_VERSION",
    "NOT_EVALUABLE",
    "DerivedPricing",
    "MassiveDerivedOverlay",
    "MassiveDerivedOverlaySpec",
    "MassiveDerivedQuote",
    "MassiveDerivedSessionCalendar",
    "MassiveDerivedStats",
    "MassiveExpiryMeta",
    "MassiveOverlayError",
    "load_derived_surface",
    "load_spot_proxy",
    "vwap_quote_event",
]
