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
whose resolved path lives where a reboot wipes it.

This module starts NOTHING: it computes and records. The launcher
(``scripts/launch_bars_era.py``) owns the gates; today both execute gates are
closed (protocol is 0.2.0, no authority record exists).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.massive_client import (
    BackoffPolicy,
    loads_exact,
)
from tree_options.data.massive_manifest import (
    MASTERS_DIR,
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


def rebuild_master_captures(
    capture_dir: Path,
    *,
    capture_manifest: Path | None = None,
) -> tuple[list[Any], dict[str, dict[str, str]], dict[str, _RowMeta]]:
    """Re-parse a capture directory into bridge ``MasterCapture`` objects.

    Loads every ``masters/*.json`` envelope with ``loads_exact`` and REUSES
    the existing loaders' checks (provenance stamps, page shape, as_of). When
    ``capture_manifest`` is given it is loaded and reconciled against the
    directory first (``verify_massive_capture_manifest``), so the rebuild
    starts from pinned bytes. Also returns the spot proxy (the selection's
    declared anchor — strings, keyed by ISO date) and the ticker index used
    to enrich picks into entries. Zero network, zero mutation.
    """
    if capture_manifest is not None:
        try:
            manifest = load_massive_capture_manifest(capture_manifest)
            verify_massive_capture_manifest(
                manifest, capture_dir, capture_version=manifest.capture_version
            )
        except MassiveManifestError as exc:
            raise BarsManifestError(f"capture dir does not match its manifest: {exc}") from None

    masters_dir = Path(capture_dir) / MASTERS_DIR
    files = sorted(p for p in masters_dir.glob("*.json") if p.is_file())
    if not files:
        raise BarsManifestError(f"{masters_dir}: no master captures (*.json) — nothing to re-run")

    bridge = _capture_bridge()
    captures: list[Any] = []
    ticker_index: dict[str, _RowMeta] = {}
    for path in files:
        source = path.name
        try:
            envelope = loads_exact(path.read_bytes())
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
        raw_text = path.read_bytes().decode("utf-8")
        captures.append(
            bridge.MasterCapture(
                underlying=underlying,
                as_of=as_of,
                pages=[
                    bridge.CapturedPage(body=page, text=raw_text, from_cache=True) for page in pages
                ],
            )
        )

    try:
        spot_by_date = _load_spot(Path(capture_dir), lineage=[])
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
    """
    verify_selection_profile(profile)
    max_attempts = (
        DEFAULT_MAX_ATTEMPTS_PER_REQUEST
        if max_attempts_per_request is None
        else max_attempts_per_request
    )
    captures, spot, ticker_index = rebuild_master_captures(
        capture_dir, capture_manifest=capture_manifest
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
    try:
        manifest_bytes = Path(capture_manifest).read_bytes()
    except OSError as exc:
        raise BarsManifestError(
            f"{capture_manifest}: capture manifest unreadable ({exc.strerror})"
        ) from None
    manifest = BarsWorkManifest(
        schema_version=BARS_WORK_SCHEMA_VERSION,
        profile_sha256=profile.content_sha256,
        capture_manifest_sha256=sha256_hex(manifest_bytes),
        entries=order_entries(entries),
        selection_notes=tuple(notes),
        cost=cost,
        content_sha256="",
    )
    return manifest.model_copy(update={"content_sha256": work_manifest_content_sha256(manifest)})


def load_bars_work_manifest(path: Path) -> BarsWorkManifest:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BarsManifestError(f"{path}: work manifest unreadable ({exc.strerror})") from None
    try:
        manifest = BarsWorkManifest.model_validate(json.loads(raw))
    except ValueError as exc:
        raise BarsManifestError(f"{path}: work manifest invalid ({exc})") from None
    return manifest


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


def read_bars_ledger(root: Path) -> BarsLedgerView:
    """Replay + verify the bars-authority ledger (read-only; never creates).

    An ABSENT ledger is not corruption (nothing approved, nothing consumed);
    a refused root (resolved under /tmp, via the imported seal rule) or a
    broken chain is.
    """
    root = validate_ledger_root(root)
    path = root / BARS_LEDGER_FILENAME
    if not path.exists():
        return BarsLedgerView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
    return _replay_bars_text(path.read_bytes().decode("utf-8", errors="replace"))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
    root.mkdir(parents=True, exist_ok=True)
    path = root / BARS_LEDGER_FILENAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            chunks: list[bytes] = []
            offset = 0
            while True:
                chunk = os.pread(fd, 65536, offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
            view = _replay_bars_text(b"".join(chunks).decode("utf-8", errors="replace"))
            if view.tail_damaged:
                raise LedgerCorruptError(
                    "the bars-authority ledger's final line is torn; reconcile it"
                    " before any further append — appending past it would hide an"
                    " unacknowledged write"
                )
            if guard is not None:
                guard(view)
            if record.prev_record_sha256 != view.tail_hash:
                raise LedgerCorruptError(
                    "supplied prev_record_sha256 does not match the verified"
                    " bars-authority tail — rebuild the record from read_bars_ledger()"
                )
            signed = record.model_copy(update={"record_sha256": _bars_record_hash(record)})
            os.lseek(fd, 0, os.SEEK_END)
            os.write(fd, (_encode_bars_record(signed) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    _fsync_dir(root)
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
    "profile_content_sha256",
    "read_bars_ledger",
    "rebuild_master_captures",
    "verify_bars_work_manifest",
    "verify_selection_profile",
    "work_manifest_content_sha256",
]
