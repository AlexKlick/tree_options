"""ATM-grid bars-era work manifest + the bars-authority mirror ledger (PR A4).

The bars era is the NEXT spend of live free-tier budget after the coverage
era, so its request list is pinned BEFORE the wire is touched, by a manifest
that is regenerated — not edited — from the captured masters:

* ``SelectionProfile`` (committed at ``data/bars/selection-profile.json``)
  declares the parameters of the capture bridge's UNMODIFIED
  ``select_atm_grid_bars``; every value is tagged draft and the file carries
  ``"status": "PENDING owner ratification"``. The profile's content hash is
  bound into the work manifest, so ratifying a different profile invalidates
  every manifest built from the old one.
* ``build_bars_work_manifest`` re-parses the captured master envelopes
  (``loads_exact``, provenance-stamp and page-shape checks REUSED from
  ``massive_overlay``), re-runs the bridge's selection verbatim over them,
  and orders the entries deterministically (underlying, as_of, expiry,
  strike-rank, call-before-put, ticker). Regeneration over identical inputs
  is byte-identical: no clock, no host path, no dict-order dependence.
* cost arithmetic is RESTATED through the bridge's own ``Budget`` model
  (imported, never re-implemented): the whole manifest must be pre-chargeable
  in one ``charge_block`` of ``expected_requests * max_attempts`` — exactly
  the discipline the live capture pays per wire call.

The bars-authority ledger MIRRORS ``seal.ledger`` mechanics under its OWN
domain (``tree-options-bars-authority-v1``) with its OWN record model, so a
bars record can never be spliced into — or confused with — the G4 seal
ledger. The /tmp root refusal is not re-implemented: ``validate_ledger_root``
is imported from ``seal.ledger`` and reused, so both ledgers refuse a root
whose resolved path lives where a reboot wipes it. The root itself is taken
into custody as a REAL directory (round-6 review fix, 2026-08-24: opened
``O_NOFOLLOW``, every later open/fsync rides that one dir fd), so a root
swapped to a symlink between validation and the open refuses with ``ELOOP``
instead of landing authority under the link's target.

This module starts NOTHING: it computes and records. The launcher
(``scripts/launch_bars_era.py``) owns the gates; today both execute gates are
closed (protocol is 0.2.0, no authority record exists).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import Field, field_validator, model_validator

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.massive_client import (
    BackoffPolicy,
    loads_exact,
)
from tree_options.data.massive_manifest import (
    MASTERS_DIR,
    SPOT_PROXY_FILENAME,
    MassiveManifestError,
    load_massive_capture_manifest,
    verify_massive_capture_manifest,
)
from tree_options.data.massive_overlay import (
    MassiveOverlayError,
    _as_of_of,
    _load_spot,
    _pages_of,
    _require_provenance,
)
from tree_options.runstate import custody
from tree_options.schemas.common import StrictModel
from tree_options.seal.errors import LedgerCorruptError
from tree_options.seal.ledger import GENESIS_PREV, validate_ledger_root

BARS_WORK_SCHEMA_VERSION = "m4-bars-work/1"
BARS_WORK_DOMAIN = b"tree-options-m4-bars-work-v1"
SELECTION_PROFILE_SCHEMA_VERSION = "m4-bars-selection-profile/1"
SELECTION_PROFILE_DOMAIN = b"tree-options-m4-bars-selection-profile-v1"
PROFILE_STATUS_PENDING = "PENDING owner ratification"

BARS_AUTHORITY_DOMAIN = b"tree-options-bars-authority-v1"
BARS_LEDGER_FILENAME = "ledger.jsonl"
#: RELATIVE on purpose: resolves against the invoking checkout, stays out of
#: git (artifacts/ is ignored). Tests ALWAYS inject their own root.
DEFAULT_BARS_AUTHORITY_ROOT = Path("artifacts/bars-authority")

#: The committed selection profile, relative to the repo root.
DEFAULT_SELECTION_PROFILE_PATH = Path("data/bars/selection-profile.json")

#: The capture bridge module name whose ``select_atm_grid_bars`` is the one
#: and only selection this manifest may re-run.
SELECTION_SELECTOR = "capture_massive_structural.select_atm_grid_bars"

# The bridge defaults restated as the pre-charge inputs (draft in the
# profile; the manifest records what it actually used).
DEFAULT_MAX_ATTEMPTS_PER_REQUEST = BackoffPolicy().max_attempts

BARS_EXPIRY_FILTERS = ("all", "monthly")
BARS_SIDE_FILTERS = ("call", "both")


class BarsManifestError(ValueError):
    """A bars work manifest / authority refusal (fail closed, never repaired)."""


class SecondExecutionRefusedError(BarsManifestError):
    """This work manifest was already consumed once (launcher exit 7).

    Round-3 review fix (2026-08-23, finding 1): the launcher's duplicate
    scan ran before the store-specific lease, and the append path reread the
    latest ledger tail without rechecking uniqueness — so two valid
    BARS_READY stores for the same approved work manifest could BOTH pass
    the scan and both append a consumption. The uniqueness recheck now runs
    INSIDE ``append_bars_record`` under the exclusive flock, against the
    freshly replayed view, so the second appender sees the first's
    CONSUMPTION and refuses instead of chaining a second one.
    """

    def __init__(self, work_manifest_sha256: str, detail: str) -> None:
        super().__init__(
            f"work manifest {work_manifest_sha256[:12]}…: {detail}; the launch"
            " authority is one-shot per work manifest — a crash after"
            " consumption is RECONCILIATION_REQUIRED, never a re-run"
        )


# ---- selection profile -----------------------------------------------------------


class DraftParameter(StrictModel):
    """One DRAFT selection parameter: a value plus why it is provisional.

    A bool is refused by validation — it is not a count and not a filter
    token, and silently accepting one would let ``true`` stand in for a band
    edge the owner never saw.
    """

    value: int | str
    tag: Literal["draft"] = "draft"
    rationale: str = ""

    @field_validator("value", mode="before")
    @classmethod
    def _bool_refused(cls, v: object) -> object:
        if isinstance(v, bool):
            raise ValueError("a bool is not a selection parameter value")
        return v


class SelectionProfile(StrictModel):
    """The DECLARED parameters of the unmodified ATM-grid selection.

    Field names are the real keyword arguments of
    ``capture_massive_structural.select_atm_grid_bars`` — transcribed, never
    renamed — so the profile can only declare what the selection actually
    accepts. ``status`` is pinned to the pending marker: no value of this
    model can claim ratification.
    """

    schema_version: str
    status: Literal["PENDING owner ratification"]
    selector: str
    wanted: DraftParameter
    dte_min: DraftParameter
    dte_max: DraftParameter
    strike_band: DraftParameter
    expiries: DraftParameter
    sides: DraftParameter
    notes: tuple[str, ...] = ()
    content_sha256: str


def profile_content_sha256(profile: SelectionProfile) -> str:
    core = profile.model_copy(update={"content_sha256": ""})
    return sha256_hex(SELECTION_PROFILE_DOMAIN + canonical_bytes(core))


def _profile_int(parameter: DraftParameter, name: str) -> int:
    if not isinstance(parameter.value, int):
        raise BarsManifestError(
            f"selection profile {name}.value must be an int, got {parameter.value!r}"
        )
    return parameter.value


def _profile_token(parameter: DraftParameter, name: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(parameter.value, str) or parameter.value not in allowed:
        raise BarsManifestError(
            f"selection profile {name}.value must be one of {list(allowed)},"
            f" got {parameter.value!r}"
        )
    return parameter.value


def verify_selection_profile(profile: SelectionProfile) -> None:
    """Fail-closed profile checks: hash binding plus parameter sanity."""
    if profile.schema_version != SELECTION_PROFILE_SCHEMA_VERSION:
        raise BarsManifestError(
            f"profile schema_version {profile.schema_version!r} !="
            f" {SELECTION_PROFILE_SCHEMA_VERSION!r}"
        )
    if profile.selector != SELECTION_SELECTOR:
        raise BarsManifestError(
            f"profile selector {profile.selector!r} != {SELECTION_SELECTOR!r}:"
            " only the unmodified bridge selection is declarable"
        )
    if profile_content_sha256(profile) != profile.content_sha256:
        raise BarsManifestError("profile content_sha256 does not bind the profile body")
    wanted = _profile_int(profile.wanted, "wanted")
    dte_min = _profile_int(profile.dte_min, "dte_min")
    dte_max = _profile_int(profile.dte_max, "dte_max")
    strike_band = _profile_int(profile.strike_band, "strike_band")
    if wanted < 1:
        raise BarsManifestError(f"profile wanted must be >= 1, got {wanted}")
    if dte_min < 0 or dte_max < dte_min:
        raise BarsManifestError(
            f"profile DTE band is not 0 <= min <= max, got {dte_min}..{dte_max}"
        )
    if strike_band < 0:
        raise BarsManifestError(f"profile strike_band must be >= 0, got {strike_band}")
    _profile_token(profile.expiries, "expiries", BARS_EXPIRY_FILTERS)
    _profile_token(profile.sides, "sides", BARS_SIDE_FILTERS)


def load_selection_profile(path: Path) -> SelectionProfile:
    """Load + verify a selection profile file (the committed one by default)."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BarsManifestError(f"{path}: selection profile unreadable ({exc.strerror})") from None
    try:
        profile = SelectionProfile.model_validate(json.loads(raw))
    except ValueError as exc:
        raise BarsManifestError(f"{path}: selection profile invalid ({exc})") from None
    verify_selection_profile(profile)
    return profile


# ---- work manifest ----------------------------------------------------------------


class BarsWorkEntry(StrictModel):
    """One contract the bars era will request, with its grid coordinates.

    ``strike`` is the EXACT text of the vendor's strike token (``"587.5"``
    stays ``"587.5"``); ``strike_rank`` is the strike's rank in the ATM grid
    of its (underlying, as_of, expiry) master — 0 is the closest distinct
    strike to spot — and ``side`` orders calls before puts.
    """

    underlying: str
    as_of: str
    expiry: str
    strike: str
    strike_rank: int = Field(ge=0)
    side: Literal["call", "put"]
    ticker: str


_SIDE_ORDER: dict[str, int] = {"call": 0, "put": 1}


def _entry_order_key(entry: BarsWorkEntry) -> tuple[str, str, str, int, int, str]:
    """The deterministic ordering contract: underlying, as_of, expiry,
    strike-rank, call-before-put, ticker."""
    return (
        entry.underlying,
        entry.as_of,
        entry.expiry,
        entry.strike_rank,
        _SIDE_ORDER[entry.side],
        entry.ticker,
    )


def order_entries(
    entries: list[BarsWorkEntry] | tuple[BarsWorkEntry, ...],
) -> tuple[BarsWorkEntry, ...]:
    """The canonical entry order. Pure; the manifest validates against it."""
    return tuple(sorted(entries, key=_entry_order_key))


class BarsCostEstimate(StrictModel):
    """The request arithmetic, RESTATED from the bridge's ``Budget`` model.

    ``worst_case_wire_requests`` is what a live run must be able to PRE-CHARGE
    for this manifest (one ``charge_block``); ``budget_covers_worst_case`` is
    the imported Budget's own answer, never a re-implementation.
    """

    expected_requests: int = Field(ge=1)
    max_attempts_per_request: int = Field(ge=1)
    worst_case_wire_requests: int = Field(ge=1)
    budget_limit: int = Field(ge=1)
    budget_covers_worst_case: bool


class BarsWorkManifest(StrictModel):
    """The pinned request list of one bars era, bound to profile + captures."""

    schema_version: str
    profile_sha256: str
    capture_manifest_sha256: str
    entries: tuple[BarsWorkEntry, ...] = Field(min_length=1)
    selection_notes: tuple[str, ...] = ()
    cost: BarsCostEstimate
    content_sha256: str

    @model_validator(mode="after")
    def _entries_canonically_ordered(self) -> BarsWorkManifest:
        if list(self.entries) != list(order_entries(self.entries)):
            raise ValueError(
                "entries are not in canonical order (underlying, as_of, expiry,"
                " strike-rank, call-before-put, ticker) — regenerate the manifest,"
                " never hand-order it"
            )
        return self


def work_manifest_content_sha256(manifest: BarsWorkManifest) -> str:
    core = manifest.model_copy(update={"content_sha256": ""})
    return sha256_hex(BARS_WORK_DOMAIN + canonical_bytes(core))


# ---- the unmodified bridge (selection + Budget) ------------------------------------


def _capture_bridge() -> Any:
    """Import ``scripts/capture_massive_structural.py`` — the selection OWNER.

    The bridge is a script, not a package, so the repo's scripts directory is
    put on ``sys.path`` the same way the bridge itself puts ``src`` on the
    path. The module is imported UNMODIFIED (repo rule) precisely so the
    work manifest re-runs the one selection that will run live, and so the
    cost arithmetic is the ``Budget`` the live run will actually charge.
    """
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import capture_massive_structural  # type: ignore[import-not-found]

    return capture_massive_structural


def estimate_bars_cost(
    expected_requests: int,
    *,
    max_attempts_per_request: int,
    budget_limit: int,
) -> BarsCostEstimate:
    """Restate the pre-charge arithmetic through the imported ``Budget``.

    ``Budget.charge_block`` is the live capture's discipline: a call whose
    worst case (``backoff.max_attempts`` wire requests) cannot be paid never
    goes out. The manifest applies the SAME test to the whole request list at
    once — one pre-charge of ``expected * max_attempts`` — and records the
    imported model's own verdict on whether the declared budget covers it.
    """
    if expected_requests < 1 or max_attempts_per_request < 1 or budget_limit < 1:
        raise BarsManifestError(
            "cost inputs must be >= 1, got"
            f" expected={expected_requests}"
            f" max_attempts={max_attempts_per_request}"
            f" budget_limit={budget_limit}"
        )
    bridge = _capture_bridge()
    budget = bridge.Budget(limit=budget_limit)  # spent=0, reserved=0
    worst = expected_requests * max_attempts_per_request
    try:
        budget.charge_block("m4-bars-work worst case", blocks=worst)
        covers = True
    except bridge.BudgetExhausted:
        covers = False
    return BarsCostEstimate(
        expected_requests=expected_requests,
        max_attempts_per_request=max_attempts_per_request,
        worst_case_wire_requests=worst,
        budget_limit=budget_limit,
        budget_covers_worst_case=covers,
    )


# ---- capture re-parse + selection re-run -------------------------------------------


class _RowMeta(StrictModel):
    """One master row, indexed by ticker for pick-to-entry enrichment."""

    underlying: str
    expiration: str
    strike: str
    kind: str


def _exact_number_text(value: object, where: str) -> str:
    """The exact text of a vendor number token (int or Decimal — never float)."""
    if isinstance(value, bool):
        raise BarsManifestError(f"{where}: a bool is not a number token")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    raise BarsManifestError(f"{where}: {type(value).__name__} is not an exact number token")


def _require_pinned_bytes(
    pinned: Mapping[str, str], relative: str, raw: bytes, *, source: str
) -> None:
    """Round-5 review fix (2026-08-24, finding 4): the bytes regeneration
    PARSES must be the bytes the capture manifest PINS.

    ``verify_massive_capture_manifest`` hashed every pinned file, but the
    rebuild then re-read the masters from disk — a swap in that window fed
    the selection (and therefore the derived request list) bytes that did
    not match the sealed manifest, and with semantically-identical swapped
    bytes the regenerated manifest still verified. Re-hashing the one read
    here closes the window: the parse consumes bytes whose sha256 equals the
    manifest pin or the regeneration refuses naming both hashes. (Chosen
    over having verify RETURN the hashed bytes because a drifted capture
    dir must REFUSE — fail closed — not silently proceed on cached bytes;
    it also keeps the verify signature and every other caller unchanged.)"""
    declared = pinned.get(relative)
    if declared is None:
        raise BarsManifestError(
            f"{source}: on disk but not pinned by the capture manifest — "
            "regeneration derives from sealed bytes only"
        )
    if sha256_hex(raw) != declared:
        raise BarsManifestError(
            f"{source}: bytes drifted from the sealed capture manifest (pin "
            f"{declared[:12]}…, read now {sha256_hex(raw)[:12]}…) — refusing "
            "to derive from a capture directory that no longer matches its "
            "manifest"
        )


def rebuild_master_captures(
    capture_dir: Path,
    *,
    capture_manifest: Path | None = None,
    capture_manifest_raw: bytes | None = None,
) -> tuple[list[Any], dict[str, dict[str, str]], dict[str, _RowMeta]]:
    """Re-parse a capture directory into bridge ``MasterCapture`` objects.

    Loads every ``masters/*.json`` envelope with ``loads_exact`` and REUSES
    the existing loaders' checks (provenance stamps, page shape, as_of). When
    ``capture_manifest`` is given it is loaded and reconciled against the
    directory first (``verify_massive_capture_manifest``), so the rebuild
    starts from pinned bytes — and each envelope/spot byte read afterwards
    is re-hashed against that pin (round-5 review fix, finding 4), so the
    parse consumes exactly what the sealed manifest attests. Also returns
    the spot proxy (the selection's declared anchor — strings, keyed by ISO
    date) and the ticker index used to enrich picks into entries. Zero
    network, zero mutation.

    Round-7 review fix (2026-08-24, finding 3): ``capture_manifest_raw`` is
    the bytes-once seam — a caller that already read the manifest file once
    (to verify AND to bind the work manifest's
    ``capture_manifest_sha256``) passes those bytes here so the loader
    parses the SAME read instead of re-reading the path. Default ``None``
    keeps the read-from-path behavior."""
    pinned: dict[str, str] | None = None
    if capture_manifest is not None:
        try:
            manifest = load_massive_capture_manifest(capture_manifest, raw=capture_manifest_raw)
            verify_massive_capture_manifest(
                manifest, capture_dir, capture_version=manifest.capture_version
            )
        except MassiveManifestError as exc:
            raise BarsManifestError(f"capture dir does not match its manifest: {exc}") from None
        pinned = {entry.path: entry.sha256 for entry in manifest.files}

    masters_dir = Path(capture_dir) / MASTERS_DIR
    files = sorted(p for p in masters_dir.glob("*.json") if p.is_file())
    if not files:
        raise BarsManifestError(f"{masters_dir}: no master captures (*.json) — nothing to re-run")

    bridge = _capture_bridge()
    captures: list[Any] = []
    ticker_index: dict[str, _RowMeta] = {}
    for path in files:
        source = path.name
        # ONE read feeds the pin check, the parse, and the page text below
        # (the old shape read the path twice: parse and text could diverge).
        raw = path.read_bytes()
        if pinned is not None:
            _require_pinned_bytes(pinned, f"{MASTERS_DIR}/{source}", raw, source=source)
        try:
            envelope = loads_exact(raw)
            if not isinstance(envelope, dict):
                raise BarsManifestError(f"{source}: top-level JSON is not an object")
            _require_provenance(envelope, source=source)
            as_of = _as_of_of(envelope, path, source=source)
            pages = _pages_of(envelope, source=source)
        except (MassiveOverlayError, ValueError) as exc:
            raise BarsManifestError(f"{source}: master envelope refused ({exc})") from None

        underlying: str | None = None
        for page in pages:
            for row in page.get("results") or ():
                if not isinstance(row, dict):
                    raise BarsManifestError(f"{source}: a result row is not an object")
                symbol = row.get("underlying_ticker")
                ticker = row.get("ticker")
                if not isinstance(symbol, str) or not symbol:
                    raise BarsManifestError(f"{source}: a result row carries no underlying_ticker")
                if not isinstance(ticker, str) or not ticker:
                    raise BarsManifestError(f"{source}: a result row carries no ticker")
                if underlying is None:
                    underlying = symbol
                elif underlying != symbol:
                    raise BarsManifestError(
                        f"{source}: {sorted({underlying, symbol})} underlyings in one master"
                        " — one master per (underlying, as_of)"
                    )
                ticker_index[ticker] = _RowMeta(
                    underlying=symbol,
                    expiration=str(row["expiration_date"]),
                    strike=_exact_number_text(row["strike_price"], f"{ticker}.strike_price"),
                    kind=str(row["contract_type"]),
                )
        if underlying is None:
            raise BarsManifestError(f"{source}: master envelope has no result rows")
        declared = envelope.get("underlying_ticker")
        if isinstance(declared, str) and declared and declared != underlying:
            raise BarsManifestError(
                f"{source}: envelope underlying {declared!r} != records {underlying!r}"
            )
        # The verbatim page text is never used downstream (the selection reads
        # decoded bodies; only the bridge's writer re-serialises text), so the
        # envelope bytes stand in for it here.
        raw_text = raw.decode("utf-8")
        captures.append(
            bridge.MasterCapture(
                underlying=underlying,
                as_of=as_of,
                pages=[
                    bridge.CapturedPage(body=page, text=raw_text, from_cache=True) for page in pages
                ],
            )
        )

    # Round-6 review fix (2026-08-24, finding 4): COMPLETENESS. The
    # enumeration above lists the PRESENT masters and the loop re-hashes
    # those — a pinned master DELETED between manifest verification and the
    # enumeration was silently absent (never read, never pin-checked), and
    # regeneration from the survivors could be byte-identical to the
    # approved manifest (the reviewer's case: a puts-only master under a
    # sides=call profile contributes nothing to the selection). Every file
    # the manifest pins under masters/ must have been READ (hashed) here:
    # pinned-minus-read must be empty, or the derivation refuses fail-closed
    # naming the missing files. (Pinned bars/ files are not derivation
    # inputs for this manifest and stay outside this set; the spot proxy is
    # held to the same rule just below.)
    if pinned is not None:
        pinned_masters = {rel for rel in pinned if rel.startswith(f"{MASTERS_DIR}/")}
        read_masters = {f"{MASTERS_DIR}/{p.name}" for p in files}
        missing = sorted(pinned_masters - read_masters)
        if missing:
            raise BarsManifestError(
                f"pinned capture file(s) absent at derivation time: "
                f"{', '.join(missing)} — the sealed capture manifest pins them "
                "but they were never read; regeneration must derive from "
                "every sealed master, and a survivor-only rebuild can "
                "silently reproduce the approved manifest"
            )

    # The spot proxy feeds the selection too (the ATM ranking keys on it), so
    # under a manifest it is held to the same pinned-bytes rule (round-5
    # review fix, finding 4): read once, re-hash against the pin, parse those
    # bytes. An unpinned spot file on disk is unprovenance — the verified
    # state had none.
    if pinned is None:
        try:
            spot_by_date = _load_spot(Path(capture_dir), lineage=[])
        except (MassiveOverlayError, ValueError) as exc:
            raise BarsManifestError(f"spot proxy refused ({exc})") from None
    else:
        spot_path = Path(capture_dir) / SPOT_PROXY_FILENAME
        declared = pinned.get(SPOT_PROXY_FILENAME)
        if declared is None:
            if spot_path.exists():
                raise BarsManifestError(
                    f"{spot_path}: on disk but not pinned by the capture "
                    "manifest — regeneration derives from sealed bytes only"
                )
            spot_by_date = {}
        else:
            try:
                spot_raw = spot_path.read_bytes()
            except OSError as exc:
                # Round-6 review fix (2026-08-24, finding 4): a PINNED spot
                # proxy deleted between manifest verification and this read
                # is the same completeness hole — refuse fail-closed naming
                # it, never silently derive from "no closes".
                raise BarsManifestError(
                    f"{SPOT_PROXY_FILENAME}: pinned by the capture manifest "
                    f"but unreadable at derivation time ({exc.strerror}) — "
                    "regeneration derives from sealed bytes only"
                ) from None
            _require_pinned_bytes(pinned, SPOT_PROXY_FILENAME, spot_raw, source=SPOT_PROXY_FILENAME)
            try:
                spot_by_date = _load_spot(Path(capture_dir), lineage=[], raw=spot_raw)
            except (MassiveOverlayError, ValueError) as exc:
                raise BarsManifestError(f"spot proxy refused ({exc})") from None
    spot: dict[str, dict[str, str]] = {
        name: {
            day.isoformat(): _exact_number_text(close, f"spot {name} {day}")
            for day, close in sessions.items()
        }
        for name, sessions in spot_by_date.items()
    }
    return captures, spot, ticker_index


def _entries_from_picks(picks: list[Any], ticker_index: dict[str, _RowMeta]) -> list[BarsWorkEntry]:
    """Selection picks -> entries, with strike-rank from the selection's own
    emission order (ranked strikes are emitted closest-first, so a strike's
    first appearance within its master/expiry IS its grid rank)."""
    ranks: dict[tuple[str, str, str], list[str]] = {}
    entries: list[BarsWorkEntry] = []
    for ticker, as_of, expiration in picks:
        meta = ticker_index[ticker]
        as_of_text = as_of.isoformat()
        expiry_text = expiration.isoformat()
        if meta.expiration != expiry_text:
            raise BarsManifestError(
                f"selection picked {ticker} for expiry {expiry_text} but the master"
                f" row says {meta.expiration!r}"
            )
        side: Literal["call", "put"]
        if meta.kind == "call":
            side = "call"
        elif meta.kind == "put":
            side = "put"
        else:
            raise BarsManifestError(f"{ticker}: contract_type {meta.kind!r} is not call/put")
        ladder = ranks.setdefault((meta.underlying, as_of_text, expiry_text), [])
        if meta.strike not in ladder:
            ladder.append(meta.strike)
        entries.append(
            BarsWorkEntry(
                underlying=meta.underlying,
                as_of=as_of_text,
                expiry=expiry_text,
                strike=meta.strike,
                strike_rank=ladder.index(meta.strike),
                side=side,
                ticker=ticker,
            )
        )
    return entries


def build_bars_work_manifest(
    capture_dir: Path,
    *,
    profile: SelectionProfile,
    capture_manifest: Path,
    budget_limit: int,
    max_attempts_per_request: int | None = None,
) -> BarsWorkManifest:
    """Regenerate the work manifest from the captures + the declared profile.

    Deterministic over identical inputs: the same capture bytes, the same
    profile, and the same cost inputs yield byte-identical output (no clock,
    no host paths). Re-runs the bridge's UNMODIFIED ``select_atm_grid_bars``.

    Round-7 review fix (2026-08-24, finding 3): the capture manifest bytes
    are read ONCE at the top and threaded into ``rebuild_master_captures``
    (``capture_manifest_raw=``), so verification, derivation, AND the work
    manifest's ``capture_manifest_sha256`` binding consume one byte set —
    the pre-fix shape re-read the path at bind time, so a manifest swapped
    in that window left the derivation consuming the verified A bytes while
    the emitted binding named B. A guard read at bind time refuses naming
    both hashes when the file no longer holds the verified bytes: emitting
    a manifest bound to bytes the capture dir no longer holds would produce
    a manifest that can never re-verify, so the drift refuses HERE, fail
    closed."""
    verify_selection_profile(profile)
    max_attempts = (
        DEFAULT_MAX_ATTEMPTS_PER_REQUEST
        if max_attempts_per_request is None
        else max_attempts_per_request
    )
    try:
        manifest_raw = Path(capture_manifest).read_bytes()
    except OSError as exc:
        raise BarsManifestError(
            f"{capture_manifest}: capture manifest unreadable ({exc.strerror})"
        ) from None
    captures, spot, ticker_index = rebuild_master_captures(
        capture_dir,
        capture_manifest=capture_manifest,
        capture_manifest_raw=manifest_raw,
    )
    bridge = _capture_bridge()
    picks, notes = bridge.select_atm_grid_bars(
        captures,
        spot,
        wanted=_profile_int(profile.wanted, "wanted"),
        dte_min=_profile_int(profile.dte_min, "dte_min"),
        dte_max=_profile_int(profile.dte_max, "dte_max"),
        strike_band=_profile_int(profile.strike_band, "strike_band"),
        expiries=_profile_token(profile.expiries, "expiries", BARS_EXPIRY_FILTERS),
        sides=_profile_token(profile.sides, "sides", BARS_SIDE_FILTERS),
    )
    entries = _entries_from_picks(picks, ticker_index)
    if not entries:
        raise BarsManifestError(
            "the declared profile selected no contracts — a bars era with zero"
            " requests is a configuration defect, not an empty manifest"
        )
    cost = estimate_bars_cost(
        len(entries), max_attempts_per_request=max_attempts, budget_limit=budget_limit
    )
    # Bind-time guard (round-7, finding 3): the binding below pins the
    # VERIFIED bytes (manifest_raw); the file must still hold them NOW.
    try:
        guard_raw = Path(capture_manifest).read_bytes()
    except OSError as exc:
        raise BarsManifestError(
            f"{capture_manifest}: capture manifest unreadable at bind time "
            f"({exc.strerror}) — regeneration binds the verified bytes only"
        ) from None
    if guard_raw != manifest_raw:
        raise BarsManifestError(
            f"{capture_manifest}: capture manifest drifted between verification "
            f"and the work-manifest binding (verified "
            f"{sha256_hex(manifest_raw)[:12]}…, read now {sha256_hex(guard_raw)[:12]}…)"
            " — refusing to bind a work manifest to bytes the capture directory "
            "no longer holds"
        )
    manifest = BarsWorkManifest(
        schema_version=BARS_WORK_SCHEMA_VERSION,
        profile_sha256=profile.content_sha256,
        capture_manifest_sha256=sha256_hex(manifest_raw),
        entries=order_entries(entries),
        selection_notes=tuple(notes),
        cost=cost,
        content_sha256="",
    )
    # Round-8 review fix (finding 2): the record above is bound the moment
    # this function returns it — the final effect. The early bind-time
    # guard (round-7) runs before the ordering/model construction, so a
    # swap in that gap used to return a manifest bound to the verified A
    # bytes while the capture dir held B. This re-check refuses at the
    # return, naming both hashes.
    _require_capture_manifest_at_final_effect(capture_manifest, manifest_raw)
    return manifest.model_copy(update={"content_sha256": work_manifest_content_sha256(manifest)})


def _require_capture_manifest_at_final_effect(path: Path, verified_raw: bytes) -> None:
    """Round-8 review fix (2026-08-24, finding 2): the FINAL-EFFECT manifest
    guard, run immediately before the bound BarsWorkManifest record is
    returned.

    The round-7 bind-time guard re-reads the manifest BEFORE the
    ordering/model construction — so a swap in that window returned a work
    manifest bound to the verified A bytes while the capture directory held
    B. This re-check sits at the binding's final effect: the manifest NAME
    must lstat as a REGULAR file (a symlink there is never the verified
    manifest, whatever it points at) and an ``O_NOFOLLOW`` read of it must
    hash to the threaded verified sha. Any drift raises
    ``BarsManifestError`` naming both hashes."""
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise BarsManifestError(
            f"{path}: capture manifest unreadable at binding time ({exc.strerror})"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise BarsManifestError(
            f"{path}: capture manifest is not a regular file at binding time "
            f"(lstat mode {stat.S_IFMT(info.st_mode):o} — a symlink at the "
            "name is never the verified manifest)"
        )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BarsManifestError(
            f"{path}: capture manifest could not be opened without following "
            f"a symlink at binding time ({exc.strerror})"
        ) from None
    try:
        chunks: list[bytes] = []
        offset = 0
        while True:
            chunk = os.pread(fd, 65536, offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
    finally:
        os.close(fd)
    now_sha = sha256_hex(b"".join(chunks))
    verified_sha = sha256_hex(verified_raw)
    if now_sha != verified_sha:
        raise BarsManifestError(
            f"{path}: capture manifest drifted between verification and the "
            f"work-manifest binding's final effect (verified "
            f"{verified_sha[:12]}…, read now {now_sha[:12]}…) — refusing to "
            "return a work manifest bound to bytes the capture directory no "
            "longer holds"
        )


def parse_bars_work_manifest(
    raw: bytes, *, source: str = "<work-manifest bytes>"
) -> BarsWorkManifest:
    """Parse work-manifest BYTES through the same error mapping the loader
    uses (round-3 review fix, 2026-08-23, finding 2): callers that must read
    the file exactly once — verify, hash, and consume the same bytes — parse
    the one read here instead of re-reading the path."""
    try:
        return BarsWorkManifest.model_validate(json.loads(raw))
    except ValueError as exc:
        raise BarsManifestError(f"{source}: work manifest invalid ({exc})") from None


def load_bars_work_manifest(path: Path) -> BarsWorkManifest:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BarsManifestError(f"{path}: work manifest unreadable ({exc.strerror})") from None
    return parse_bars_work_manifest(raw, source=str(path))


def verify_bars_work_manifest(
    manifest: BarsWorkManifest,
    *,
    profile: SelectionProfile | None = None,
    capture_manifest_sha256: str | None = None,
    capture_dir: Path | None = None,
) -> None:
    """Fail-closed verification: binding, ordering, and cost restatement.

    Round-1 review fix (2026-08-23, probe FORGED_BARS_ENTRY_VERIFIED):
    self-hash binding alone does not prove the entries came from captured
    masters + the unmodified selector. With capture_dir supplied, this
    function RE-REGENERATES the manifest by running rebuild_master_captures
    and the selection function over the sealed capture, then compares
    the regenerated manifest to the committed one. Any divergence is
    refused. capture_dir is REQUIRED for preflight/execute paths.

    Round-2 review fix (2026-08-23, finding 5, probe
    /tmp/pr-a-bars-dual-manifest-probe.log): the launcher hashes capture
    manifest A while regeneration used to silently load whatever sat at
    capture_dir/capture_manifest.json — two distinct self-consistent
    manifests both verified. When capture_manifest_sha256 is supplied, the
    bytes at that path must hash to it BEFORE regeneration (the manifest
    you hashed is the one regeneration must use), and the FULL regenerated
    manifest must equal the committed one.
    """
    if manifest.schema_version != BARS_WORK_SCHEMA_VERSION:
        raise BarsManifestError(
            f"work manifest schema_version {manifest.schema_version!r} !="
            f" {BARS_WORK_SCHEMA_VERSION!r}"
        )
    if work_manifest_content_sha256(manifest) != manifest.content_sha256:
        raise BarsManifestError("work manifest content_sha256 does not bind the body")
    if profile is not None and manifest.profile_sha256 != profile.content_sha256:
        raise BarsManifestError(
            "work manifest is bound to a different selection profile:"
            f" manifest {manifest.profile_sha256[:12]}…, committed profile"
            f" {profile.content_sha256[:12]}…"
        )
    if (
        capture_manifest_sha256 is not None
        and manifest.capture_manifest_sha256 != capture_manifest_sha256
    ):
        raise BarsManifestError(
            "work manifest is bound to different capture manifest bytes:"
            f" manifest {manifest.capture_manifest_sha256[:12]}…, on disk now"
            f" {capture_manifest_sha256[:12]}…"
        )
    restated = estimate_bars_cost(
        len(manifest.entries),
        max_attempts_per_request=manifest.cost.max_attempts_per_request,
        budget_limit=manifest.cost.budget_limit,
    )
    if restated != manifest.cost:
        raise BarsManifestError(
            f"cost estimate does not restate the Budget arithmetic: manifest says"
            f" {manifest.cost}, Budget computes {restated}"
        )
    if capture_dir is None:
        # capture_dir is required by preflight/execute; tests may opt in
        # by passing it. The launcher passes it always.
        raise BarsManifestError(
            "verify_bars_work_manifest requires capture_dir to regenerate"
            " and re-derive the entries; self-hash alone is not a proof"
            " (round-1 review fix 2026-08-23)"
        )
    # Round-1 fix: regenerate the manifest from the sealed capture + the
    # committed profile + the UNMODIFIED selector, and require equality.
    if profile is None:
        raise BarsManifestError("regeneration requires the selection profile; pass profile=")
    candidate = capture_dir / "capture_manifest.json"
    if not candidate.exists():
        raise BarsManifestError(
            f"regeneration needs capture_manifest.json at {candidate} (the"
            " standard location inside capture_dir); not found. Pass"
            " capture_dir that contains the sealed capture manifest."
        )
    if capture_manifest_sha256 is not None:
        # Round-2 fix, layer 1: bind the supplied hash to the exact bytes
        # regeneration is about to use, BEFORE regenerating.
        try:
            candidate_bytes = candidate.read_bytes()
        except OSError as exc:
            raise BarsManifestError(
                f"{candidate}: capture manifest unreadable ({exc.strerror})"
            ) from None
        candidate_sha = sha256_hex(candidate_bytes)
        if candidate_sha != capture_manifest_sha256:
            raise BarsManifestError(
                "capture_dir/capture_manifest.json is not the manifest the work"
                " manifest was verified against: supplied hash"
                f" {capture_manifest_sha256[:12]}…, bytes on disk now hash"
                f" {candidate_sha[:12]}… — the pinned manifest and the one"
                " regeneration would use are two different manifests"
            )
    rebuilt = build_bars_work_manifest(
        capture_dir,
        profile=profile,
        capture_manifest=candidate,
        budget_limit=manifest.cost.budget_limit,
        max_attempts_per_request=manifest.cost.max_attempts_per_request,
    )
    # Round-2 fix, layer 2: compare the FULL manifest, not just entries —
    # the capture-manifest binding, profile pin, cost, and notes must all
    # reproduce. We do NOT expose the divergent fields (could be misleading
    # in operator logs).
    if rebuilt != manifest:
        raise BarsManifestError(
            "regenerated bars work manifest does not reproduce the committed"
            " manifest (entries, capture-manifest binding, profile, cost, or"
            " notes diverge from what the selector over the sealed capture"
            " would produce)"
        )


# ---- the bars-authority mirror ledger ----------------------------------------------


BarsAuthorityKind = Literal["BARS_LAUNCH_APPROVAL", "BARS_LAUNCH_CONSUMED"]
KIND_BARS_LAUNCH_APPROVAL: BarsAuthorityKind = "BARS_LAUNCH_APPROVAL"
KIND_BARS_LAUNCH_CONSUMED: BarsAuthorityKind = "BARS_LAUNCH_CONSUMED"


class BarsAuthorityRecord(StrictModel):
    """One bars-authority ledger line (own domain, own model — never a seal)."""

    kind: BarsAuthorityKind
    protocol_hash: str
    amendment_packet_sha256: str
    census_sha256: str
    work_manifest_sha256: str
    reason: str
    at_epoch: int = Field(ge=0)
    prev_record_sha256: str
    record_sha256: str = ""  # filled by the writer; "" is invalid on disk


@dataclass(frozen=True)
class BarsLedgerView:
    records: tuple[BarsAuthorityRecord, ...]
    tail_hash: str  # GENESIS_PREV when empty
    tail_damaged: bool


def _bars_record_hash(record: BarsAuthorityRecord) -> str:
    body = json.dumps(
        {k: v for k, v in record.model_dump().items() if k != "record_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_hex(BARS_AUTHORITY_DOMAIN + body)


def _encode_bars_record(record: BarsAuthorityRecord) -> str:
    return json.dumps(
        json.loads(record.model_dump_json()),
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_bars_record(line: str) -> BarsAuthorityRecord | None:
    try:
        return BarsAuthorityRecord.model_validate(json.loads(line))
    except Exception:
        return None


def _verify_bars_chain(record: BarsAuthorityRecord, prev_hash: str) -> bool:
    if record.prev_record_sha256 != prev_hash:
        return False
    return record.record_sha256 == _bars_record_hash(record)


def _replay_bars_text(text: str) -> BarsLedgerView:
    lines = text.splitlines()
    records: list[BarsAuthorityRecord] = []
    prev_hash = GENESIS_PREV
    damaged_tail = False
    for index, line in enumerate(lines):
        record = _decode_bars_record(line)
        is_final = index == len(lines) - 1
        if record is None or not _verify_bars_chain(record, prev_hash):
            if is_final:
                damaged_tail = True
                continue
            raise LedgerCorruptError(
                f"bars-authority line {index + 1} failed decode/hash/chain verification"
            )
        records.append(record)
        prev_hash = record.record_sha256
    return BarsLedgerView(records=tuple(records), tail_hash=prev_hash, tail_damaged=damaged_tail)


def _open_bars_ledger_root(root: Path, *, create: bool) -> int | None:
    """Round-6 review fix (2026-08-24, finding 3): take custody of the
    bars-authority ROOT as a REAL directory, once, with ``O_NOFOLLOW`` (the
    same rule ``seal.ledger._open_ledger_root`` enforces for seal authority).

    The final-name ``O_NOFOLLOW`` (round-5) guards only ``ledger.jsonl``.
    Between ``validate_ledger_root()`` and the mkdir/open, an attacker could
    create the (previously nonexistent) allowed root as a directory SYMLINK:
    ``mkdir(exist_ok=True)`` accepts a directory symlink, the ledger name
    inside it is a regular file, and the final-name ``O_NOFOLLOW`` never
    fired — bars authority landed under the link's target. The root is now
    opened ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` (a symlink at the root is
    ``ELOOP``, refused by name) and every later operation — the ledger open,
    the directory fsync — rides that one dir fd, so the root PATHNAME is
    never re-resolved (and therefore never re-followed) after custody is
    taken. ``create`` is append-only; a lost mkdir race re-opens under the
    same ``O_NOFOLLOW`` rule, so a FileExistsError is never suppressed on
    the attacker's terms.

    Round-7 review fix (2026-08-24, finding 2): a single open of the root
    path guards only the FINAL component — a symlink planted at any
    INTERMEDIATE ancestor (e.g. a renamed ``artifacts/`` planted as a link
    to an attack dir holding a real root) was followed silently and custody
    landed on the target. Custody is now taken COMPONENT-WISE from ``/``
    (mirroring ``seal.ledger._open_ledger_root``): each component is opened
    ``O_NOFOLLOW|O_DIRECTORY`` relative to the previous component's fd;
    ``ELOOP``/``ENOTDIR`` at ANY component refuses naming it, and the
    ``create`` branch mkdirs a missing component one at a time under the
    walked prefix, re-opening it under the no-follow rule.

    R15 (finding 3, 2026-08-25): the walk is a DURABLE TRAVERSAL — the same
    semantics ``custody.open_directory(durable=True)`` and the seal
    ledger-root walk apply. The PARENT fd is fsynced for EVERY successfully
    traversed component, on BOTH branches (created and existing-open), so a
    freshly created bars root is committed in ITS parent before the append
    can acknowledge (pre-R15 a reboot could drop the whole root entry and
    the next read returned an empty view over an acknowledged consumption),
    and a residue component a prior invocation left uncommitted is repaired
    by the next walk that merely opens it. Implemented INLINE in this
    walk's local loop — it must keep the local component-wise custody walk
    for its own error family (``LedgerCorruptError``) and its absent-root
    read semantics; the pattern is the shared mechanism's, not a
    re-derivation."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def _refuse_component(component: str, exc: OSError) -> NoReturn:
        raise LedgerCorruptError(
            f"{root}: bars-authority component {component!r} is not a real "
            f"directory (opened O_NOFOLLOW|O_DIRECTORY component-wise from /, "
            f"errno {exc.errno}) — bars authority is never created or followed "
            "through a symlinked path component"
        ) from None

    resolved = Path(os.path.abspath(str(root)))  # validated + resolved by the
    # caller; abspath only normalizes — re-resolving would FOLLOW a swapped
    # ancestor and change the components under custody.
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    for component in resolved.parts[1:]:
        prev = fd
        parent_committed = False
        try:
            fd = os.open(component, flags, dir_fd=prev)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                os.close(prev)
                _refuse_component(component, exc)
            if exc.errno == errno.ENOENT and create:
                try:
                    os.mkdir(component, 0o755, dir_fd=prev)
                except FileExistsError:
                    pass  # lost race: the no-follow open below refuses a symlink
                else:
                    # R15 (finding 3): a freshly created component is
                    # committed in its PARENT before the walk proceeds —
                    # otherwise a reboot could drop the root entry together
                    # with the ledger beside it and the next read would be an
                    # EMPTY view over an acknowledged consumption.
                    os.fsync(prev)
                    parent_committed = True
                try:
                    fd = os.open(component, flags, dir_fd=prev)
                except OSError as retry:
                    os.close(prev)
                    if retry.errno in (errno.ELOOP, errno.ENOTDIR):
                        _refuse_component(component, retry)
                    raise
            else:
                os.close(prev)
                if exc.errno == errno.ENOENT:
                    return None  # absent root on the read path: an empty view
                raise
        if not parent_committed:
            # R15 (finding 3): the component already existed — commit its
            # entry in the parent before relying on anything beneath it (a
            # prior invocation may have crashed between its mkdir and this
            # fsync; restart closure).
            os.fsync(prev)
        os.close(prev)
    return fd


def _open_bars_ledger_nofollow(path: Path, root_fd: int, flags: int) -> int:
    """Round-5 review fix (2026-08-24, finding 3): open the bars-authority
    ledger NAME without ever following a symlink at it (same rule as
    ``seal.ledger._open_ledger_nofollow``). ``Path.exists()`` is False for a
    DANGLING symlink, so the read used to treat a symlinked ledger as
    absent and the append's ``os.open(O_RDWR|O_CREAT)`` FOLLOWED the link —
    creating bars authority under /tmp. ``O_NOFOLLOW`` turns a symlink at
    the final component into ``ELOOP`` regardless of its target, refused by
    name.

    Round-6 review fix (2026-08-24, finding 3): the open rides the custody
    root fd (``dir_fd=root_fd``, opening the bare NAME), so even a symlink
    swap of the root PATHNAME after custody was taken cannot redirect it."""
    try:
        return os.open(path.name, flags | os.O_NOFOLLOW, 0o644, dir_fd=root_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LedgerCorruptError(
                f"{path}: the bars-authority ledger name is a SYMLINK — bars"
                " authority is never created or followed through a symlink"
            ) from None
        raise


def _binding_refusal(detail: str) -> NoReturn:
    """The durable name→inode binding refuses in the ledger error family."""
    raise LedgerCorruptError(detail) from None


def _verify_or_bind_bars_ledger_name(
    root: Path, root_fd: int, ledger_fd: int
) -> custody.NameBinding:
    """Round-11 review fix (2026-08-25, finding 3 — mirror of the seal
    ledger's): the durable name→inode binding, the successor-window closer.

    The round-8 in-append name check can only see swaps that land BEFORE it;
    a swap landing after it but before the return left an approval-only byte
    clone at the authority name that a SECOND launcher then consumed — split
    launch authority. At ledger creation (an empty ledger, under the flock,
    BEFORE the first append lands) the file's ``(st_dev, st_ino)`` is pinned
    in a companion identity record written through custody; every open after
    that verifies the name still maps to the bound inode. A clone has the
    wrong inode and is refused at the next open — it can never be consumed.
    An unbound NON-EMPTY ledger is reconciliation, never an append and never
    a silent re-bind.

    R15 (finding 4): returns the binding so the caller runs the
    committed-extent check against it — the companion is this ledger's ONLY
    durable extent record (there is no second tree here).
    """
    purpose = "bars-authority ledger.jsonl"
    binding = custody.load_name_binding(
        root, root_fd, BARS_LEDGER_FILENAME, purpose=purpose, refuse=_binding_refusal
    )
    if binding is None:
        held = os.fstat(ledger_fd)
        if held.st_size != 0:
            raise LedgerCorruptError(
                f"{root / BARS_LEDGER_FILENAME}: the bars-authority ledger holds "
                f"{held.st_size} bytes with no durable name binding — an unbound "
                "ledger is never appended or re-bound in place; reconcile with "
                "the owner (this refusal is RECONCILIATION, never success)"
            ) from None
        return custody.bind_name_identity(
            root,
            root_fd,
            BARS_LEDGER_FILENAME,
            ledger_fd,
            purpose=purpose,
            refuse=_binding_refusal,
        )
    custody.verify_name_binding(
        root_fd,
        BARS_LEDGER_FILENAME,
        ledger_fd,
        binding,
        purpose=purpose,
        refuse=_binding_refusal,
    )
    return binding


def _verify_bound_bars_ledger_name(
    root: Path, root_fd: int, ledger_fd: int
) -> custody.NameBinding | None:
    """The read-side rule: a bound name that stopped mapping to its bound
    inode is refused; an EMPTY unbound ledger carries no authority yet (the
    creation crash window — bound at the next append); a NON-EMPTY unbound
    ledger is reconciliation. Returns the binding (None when nothing was
    ever bound) for the caller's committed-extent check."""
    purpose = "bars-authority ledger.jsonl"
    binding = custody.load_name_binding(
        root, root_fd, BARS_LEDGER_FILENAME, purpose=purpose, refuse=_binding_refusal
    )
    if binding is None:
        if os.fstat(ledger_fd).st_size != 0:
            raise LedgerCorruptError(
                f"{root / BARS_LEDGER_FILENAME}: the bars-authority ledger holds "
                f"{os.fstat(ledger_fd).st_size} bytes with no durable name "
                "binding — an unbound ledger is never read as authority; "
                "reconcile with the owner (this refusal is RECONCILIATION, "
                "never success)"
            ) from None
        return None
    custody.verify_name_binding(
        root_fd,
        BARS_LEDGER_FILENAME,
        ledger_fd,
        binding,
        purpose=purpose,
        refuse=_binding_refusal,
    )
    return binding


def _check_bars_committed_extent(
    root: Path,
    binding: custody.NameBinding,
    *,
    ledger_bytes: int,
    view: BarsLedgerView,
    raw_ledger_bytes: bytes,
) -> None:
    """R15 (finding 4): this ledger's committed-extent rule — the ONE class
    mechanism (``custody.check_committed_extent``, R15 finding 1) applied to
    the companion identity record, the bars ledger's only durable extent
    record. A valid chain prefix is not committed authority: a same-inode
    truncation/prefix rollback refuses, an in-place rewrite of the pinned
    bytes refuses, and a ledger LARGER than the pinned extent is accepted
    only through the prefix proof."""
    custody.check_committed_extent(
        extent_size=binding.extent_size,
        committed_tail_sha256=binding.committed_tail_sha256,
        ledger_bytes=ledger_bytes,
        view_tail_sha256=view.tail_hash,
        raw_ledger_bytes=raw_ledger_bytes,
        replay_prefix=_replay_bars_text,
        subject=str(root / BARS_LEDGER_FILENAME),
        origin="the companion identity record",
        refuse=_binding_refusal,
    )


def read_bars_ledger(root: Path) -> BarsLedgerView:
    """Replay + verify the bars-authority ledger (read-only; never creates).

    An ABSENT ledger is not corruption (nothing approved, nothing consumed);
    a refused root (resolved under /tmp, via the imported seal rule), a
    symlink at the ledger name (round-5 review fix: never created or
    followed), or a broken chain is.

    Round-6 review fix (2026-08-24, finding 3): the root is taken into
    custody as a REAL directory (``O_NOFOLLOW``; a root swapped to a symlink
    between validation and the open is ``ELOOP`` → ``LedgerCorruptError``)
    and the ledger is opened BY NAME inside that custody fd — the read can
    never be redirected through a symlinked root.
    """
    root = validate_ledger_root(root)
    root_fd = _open_bars_ledger_root(root, create=False)
    if root_fd is None:
        return BarsLedgerView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
    try:
        try:
            fd = _open_bars_ledger_nofollow(root / BARS_LEDGER_FILENAME, root_fd, os.O_RDONLY)
        except FileNotFoundError:
            # Round-11 (finding 3): a bound name may not silently vanish —
            # the absent-name case stays an empty view only when NOTHING was
            # ever bound.
            if (
                custody.load_name_binding(
                    root,
                    root_fd,
                    BARS_LEDGER_FILENAME,
                    purpose="bars-authority ledger.jsonl",
                    refuse=_binding_refusal,
                )
                is not None
            ):
                raise LedgerCorruptError(
                    f"{root / BARS_LEDGER_FILENAME}: the bars-authority NAME is "
                    "absent while its durable name binding exists — bound "
                    "authority may not silently vanish; this refusal is "
                    "RECONCILIATION, never an empty view"
                ) from None
            return BarsLedgerView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
        try:
            binding = _verify_bound_bars_ledger_name(root, root_fd, fd)
            chunks: list[bytes] = []
            offset = 0
            while True:
                chunk = os.pread(fd, 65536, offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    raw = b"".join(chunks)
    view = _replay_bars_text(raw.decode("utf-8", errors="replace"))
    # R15 (finding 4): the companion's COMMITTED EXTENT — a valid prefix is
    # not committed authority, and a larger-than-pinned ledger must PROVE
    # the pinned prefix (the class extent check, all three branches).
    if binding is not None:
        _check_bars_committed_extent(
            root,
            binding,
            ledger_bytes=len(raw),
            view=view,
            raw_ledger_bytes=raw,
        )
    return view


def append_bars_record(
    root: Path,
    record: BarsAuthorityRecord,
    *,
    guard: Callable[[BarsLedgerView], None] | None = None,
) -> str:
    """Append one hash-chained record under the exclusive flock; fsync file +
    parent dir before returning. Refuses a torn tail and a stale prev hash
    (mirrors ``seal.ledger.append_record`` under this ledger's own domain).

    Round-3 review fix (2026-08-23, finding 1): an optional ``guard`` is
    evaluated against the LOCKED, freshly replayed view, before the write —
    the only point where check-then-append is atomic. The one-shot
    consumption rule rides on this: the duplicate recheck lives here, under
    the flock, not in the caller's earlier (racy) scan.
    """
    root = validate_ledger_root(root)
    root_fd = _open_bars_ledger_root(root, create=True)
    assert root_fd is not None  # create=True always returns an open fd or raises
    try:
        path = root / BARS_LEDGER_FILENAME
        fd = _open_bars_ledger_nofollow(path, root_fd, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                # Round-11 (finding 3): bind at creation (empty ledger,
                # before the first append lands) or verify the name still
                # maps to the bound inode — under the flock either way.
                binding = _verify_or_bind_bars_ledger_name(root, root_fd, fd)
                chunks: list[bytes] = []
                offset = 0
                while True:
                    chunk = os.pread(fd, 65536, offset)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    offset += len(chunk)
                raw = b"".join(chunks)
                view = _replay_bars_text(raw.decode("utf-8", errors="replace"))
                if view.tail_damaged:
                    raise LedgerCorruptError(
                        "the bars-authority ledger's final line is torn; reconcile it"
                        " before any further append — appending past it would hide an"
                        " unacknowledged write"
                    )
                if guard is not None:
                    guard(view)
                # R15 (finding 4): never append onto a ledger that no longer
                # holds its committed extent in full as a PROVEN prefix — a
                # truncated or unproven-larger rewrite must not be re-spent
                # by this append.
                _check_bars_committed_extent(
                    root,
                    binding,
                    ledger_bytes=len(raw),
                    view=view,
                    raw_ledger_bytes=raw,
                )
                if record.prev_record_sha256 != view.tail_hash:
                    raise LedgerCorruptError(
                        "supplied prev_record_sha256 does not match the verified"
                        " bars-authority tail — rebuild the record from read_bars_ledger()"
                    )
                signed = record.model_copy(update={"record_sha256": _bars_record_hash(record)})
                os.lseek(fd, 0, os.SEEK_END)
                # Round-11 (finding 5): the looped authority write — a short
                # write is completed (or raises), never acknowledged torn.
                line = (_encode_bars_record(signed) + "\n").encode("utf-8")
                custody.write_all(fd, line)
                os.fsync(fd)
                # Round-8 review fix (2026-08-24, finding 3): the bars-authority
                # mirror of seal.ledger's name check. The append must verify the
                # NAME still maps to the locked inode before returning success:
                # a rename+clone during the append landed the consumption under
                # a renamed file while a clone held the authority name, and a
                # second launcher on the clone spent the one-shot launch
                # authority again. Checked under the same flock and custody
                # root fd; a divergence is RECONCILIATION, never success.
                locked = os.fstat(fd)
                try:
                    named = os.stat(BARS_LEDGER_FILENAME, dir_fd=root_fd, follow_symlinks=False)
                except OSError as exc:
                    raise LedgerCorruptError(
                        f"{path}: the bars-authority NAME vanished after the "
                        f"append ({exc.strerror}) — the one-shot lock domain is "
                        "the locked inode, so authority may have been consumed "
                        "under a renamed file: this refusal is RECONCILIATION, "
                        "never success"
                    ) from None
                if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (
                    locked.st_dev,
                    locked.st_ino,
                ):
                    raise LedgerCorruptError(
                        f"{path}: the bars-authority NAME no longer maps to the "
                        f"locked inode (locked fd dev {locked.st_dev} ino "
                        f"{locked.st_ino}, name holds dev {named.st_dev} ino "
                        f"{named.st_ino}, mode {stat.S_IFMT(named.st_mode):o}) — "
                        "authority may have been consumed under a renamed file "
                        "while a clone holds the name: this refusal is "
                        "RECONCILIATION, never success"
                    )
                # R15 (finding 4): the append is durable and the name still
                # maps to the locked inode, so the companion's COMMITTED
                # EXTENT advances to it now — the last act under the flock.
                # A refusal here leaves the record durable and the next open
                # accepting it only through the prefix proof (the crash
                # window), re-anchoring at the next append.
                custody.advance_name_binding_extent(
                    root,
                    root_fd,
                    BARS_LEDGER_FILENAME,
                    fd,
                    new_extent_size=offset + len(line),
                    new_committed_tail_sha256=signed.record_sha256,
                    purpose="bars-authority ledger.jsonl",
                    refuse=_binding_refusal,
                )
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        # directory durability on the CUSTODY fd: the root pathname is never
        # re-resolved (round-6 finding 3), so a swapped root cannot redirect
        # this fsync either.
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return signed.record_sha256


def _refuse_duplicate_consumption(
    work_manifest_sha256: str,
) -> Callable[[BarsLedgerView], None]:
    """The one-shot guard: no second CONSUMPTION may bind the same work
    manifest — evaluated under the ledger flock (round-3 review fix)."""

    def guard(view: BarsLedgerView) -> None:
        for record in view.records:
            if (
                record.kind == KIND_BARS_LAUNCH_CONSUMED
                and record.work_manifest_sha256 == work_manifest_sha256
            ):
                raise SecondExecutionRefusedError(
                    work_manifest_sha256,
                    f"a BARS_LAUNCH_CONSUMED record ({record.record_sha256[:12]}…)"
                    " already binds this work manifest under the ledger lock",
                )

    return guard


def _append_bars_kind(
    root: Path,
    kind: BarsAuthorityKind,
    *,
    protocol_hash: str,
    amendment_packet_sha256: str,
    census_sha256: str,
    work_manifest_sha256: str,
    reason: str,
    at_epoch: int,
    guard: Callable[[BarsLedgerView], None] | None = None,
) -> BarsAuthorityRecord:
    view = read_bars_ledger(root)
    record = BarsAuthorityRecord(
        kind=kind,
        protocol_hash=protocol_hash,
        amendment_packet_sha256=amendment_packet_sha256,
        census_sha256=census_sha256,
        work_manifest_sha256=work_manifest_sha256,
        reason=reason,
        at_epoch=at_epoch,
        prev_record_sha256=view.tail_hash,
    )
    digest = append_bars_record(root, record, guard=guard)
    return record.model_copy(update={"record_sha256": digest})


def append_bars_launch_approval(
    root: Path,
    *,
    protocol_hash: str,
    amendment_packet_sha256: str,
    census_sha256: str,
    work_manifest_sha256: str,
    reason: str,
    at_epoch: int,
) -> BarsAuthorityRecord:
    """Record the owner's approval of one (protocol, packet, census, manifest)
    tuple (library API — the CLI never writes authority)."""
    return _append_bars_kind(
        root,
        KIND_BARS_LAUNCH_APPROVAL,
        protocol_hash=protocol_hash,
        amendment_packet_sha256=amendment_packet_sha256,
        census_sha256=census_sha256,
        work_manifest_sha256=work_manifest_sha256,
        reason=reason,
        at_epoch=at_epoch,
    )


def append_bars_launch_consumed(
    root: Path,
    *,
    protocol_hash: str,
    amendment_packet_sha256: str,
    census_sha256: str,
    work_manifest_sha256: str,
    reason: str,
    at_epoch: int,
) -> BarsAuthorityRecord:
    """Spend the launch authority for this work manifest (execute path only).

    Round-3 review fix (2026-08-23, finding 1): the one-shot rule is
    enforced HERE — inside the locked append, against the freshly replayed
    view — so a second launcher whose earlier scan predated the first's
    append still refuses (``SecondExecutionRefusedError``, launcher exit 7)
    instead of chaining a second consumption."""
    return _append_bars_kind(
        root,
        KIND_BARS_LAUNCH_CONSUMED,
        protocol_hash=protocol_hash,
        amendment_packet_sha256=amendment_packet_sha256,
        census_sha256=census_sha256,
        work_manifest_sha256=work_manifest_sha256,
        reason=reason,
        at_epoch=at_epoch,
        guard=_refuse_duplicate_consumption(work_manifest_sha256),
    )


__all__ = [
    "BARS_AUTHORITY_DOMAIN",
    "BARS_LEDGER_FILENAME",
    "BARS_WORK_DOMAIN",
    "BARS_WORK_SCHEMA_VERSION",
    "DEFAULT_BARS_AUTHORITY_ROOT",
    "DEFAULT_MAX_ATTEMPTS_PER_REQUEST",
    "DEFAULT_SELECTION_PROFILE_PATH",
    "KIND_BARS_LAUNCH_APPROVAL",
    "KIND_BARS_LAUNCH_CONSUMED",
    "PROFILE_STATUS_PENDING",
    "SELECTION_PROFILE_DOMAIN",
    "SELECTION_PROFILE_SCHEMA_VERSION",
    "SELECTION_SELECTOR",
    "BarsAuthorityKind",
    "BarsAuthorityRecord",
    "BarsCostEstimate",
    "BarsLedgerView",
    "BarsManifestError",
    "BarsWorkEntry",
    "BarsWorkManifest",
    "DraftParameter",
    "SecondExecutionRefusedError",
    "SelectionProfile",
    "append_bars_launch_approval",
    "append_bars_launch_consumed",
    "append_bars_record",
    "build_bars_work_manifest",
    "estimate_bars_cost",
    "load_bars_work_manifest",
    "load_selection_profile",
    "order_entries",
    "parse_bars_work_manifest",
    "profile_content_sha256",
    "read_bars_ledger",
    "rebuild_master_captures",
    "verify_bars_work_manifest",
    "verify_selection_profile",
    "work_manifest_content_sha256",
]
