#!/usr/bin/env python
"""Coverage-era census builder (PR A/A2): observed facts, never policy.

Consumes a SEALED capture directory (`capture_manifest.json` + masters +
spot proxy, i.e. `scripts/capture_massive_structural.py` output) plus the
declared universe manifest (`scripts/gen_coverage_universe.py` output) and
emits a typed `CoverageCensus` artifact under
`<out-root>/<content_sha256[:12]>/`:

- `census.json` — the artifact itself (byte-identical across re-runs over
  identical inputs; no clock ever enters the output);
- `census.md` — the human summary, including the G3 derivation-source
  contradiction VERBATIM (recorded, never papered over);
- `census.json.sha256` — hex digest of the census.json bytes.

The four-class value taxonomy is enforced by construction (see
`tree_options.data.coverage_census`): observed facts, predeclared
derivation inputs, owner-ratified policy values (EMPTY in this PR era) and
not-yet-decided slots. Every fact id lands in exactly one section and the
`value_registry` agrees with that placement — the artifact refuses to
validate otherwise.

Determinism: content is a pure function of (capture bytes, universe bytes,
protocol, uv.lock, HEAD, argv). The provenance `command` is `sys.argv`, so
two programmatic runs in one process are byte-identical; the git state is
resolved through the module-level `GIT_RUNNER` seam (a clean tracked tree
is required — untracked paths and anything under artifacts/ or dist/ are
ignored; there is no --allow-dirty flag).

Exit codes (contract):
  0  census emitted and coverage is whole: zero pairs in INCOMPLETE_CLASSES
     (MISSING/TRUNCATED/ERROR/SPOT_MISSING_SESSION — holiday-Friday spot gaps
     are EXPECTED and do not count) and masters observed ==
     universe.expected_masters
  2  capture manifest refused: unreadable, wrong shape, or failed
     verification against the capture directory (a MID-RUN era directory
     fails here BY DESIGN — the census consumes a sealed capture only);
     an existing-but-undecodable spot proxy refuses here too, and so does
     any pinned capture file (spot proxy, master) that drifted, vanished,
     or appeared unpinned between manifest verification and its read
     (round-6 review fix, 2026-08-24: the census derives from sealed bytes
     at the point of consumption, never from a re-read of a verified path);
     and the capture manifest itself swapped or vanished between
     verification and provenance (round-7 review fix, 2026-08-24: the
     provenance names the ONE verified byte set, never a re-read), or
     between that guard and the emission (round-8 review fix, 2026-08-24:
     a final-effect re-check runs immediately before anything is written
     under out_dir — a refusal emits nothing)
  3  universe manifest refused: unreadable, invalid, or tampered
  4  reproducibility refusal (git unusable, tracked tree dirty, protocol or
     uv.lock unreadable, calendar fixture refused, census self-check) — or
     the content-addressed output directory already exists (never overwrite)
     — or the EMISSION path refused (round-8 review fix, 2026-08-24: an
     output name that is not a regular file, or a publish whose identity or
     readback does not match the rendered content — CensusEmitRefused; or
     round-10 review fix, 2026-08-25: a bare OSError raised while publishing
     the set — the members already renamed into place are rolled back, so
     the set stays all-or-nothing and the retry is clean; or a refused
     emission's cleanup finds the output directory substituted or vanished
     after custody ended — the digest directory is removed only if it still
     maps to the held identity)
  5  census emitted but coverage incomplete (the artifact is STILL written;
     partial evidence is never swallowed)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, NamedTuple, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

import inspect_structural_coverage as isc  # type: ignore[import-not-found]  # noqa: E402, scripts/
from tree_options.data.coverage_census import (  # noqa: E402
    CENSUS_SCHEMA_VERSION,
    G3_DERIVATION_CONTRADICTION,
    INCOMPLETE_CLASSES,
    CensusFact,
    CensusProvenance,
    CensusTaxonomyError,
    CensusValues,
    CoverageBlock,
    CoverageCensus,
    CoverageUniverse,
    PairCoverage,
    PairFinding,
    ValueClass,
    census_content_sha256,
    classify_pair,
    validate_value_taxonomy,
    verify_census,
    verify_universe,
)
from tree_options.data.digest import sha256_hex  # noqa: E402
from tree_options.data.massive_manifest import (  # noqa: E402
    CAPTURE_MANIFEST_FILENAME,
    MASTERS_DIR,
    SPOT_PROXY_FILENAME,
    CaptureKind,
    MassiveCaptureManifest,
    MassiveManifestError,
    MasterEntry,
    load_massive_capture_manifest,
    verify_massive_capture_manifest,
)
from tree_options.protocol.loader import (  # noqa: E402
    load_protocol,
    protocol_hash,
    raw_file_hash,
)
from tree_options.time.calendar import StaticSessionCalendar  # noqa: E402

# The capture-version token this census understands (see
# inspect_structural_coverage.KNOWN_CAPTURE_VERSIONS — kept a string literal
# here for the same decoupling reason that module states).
CAPTURE_VERSION = "m4b-capture/1"

# The CaptureKind member that marks a masters/ envelope (the Literal's own
# master token — used for the round-7 pinned-vs-referenced completeness
# mirror in observe_masters).
MASTER_KIND: CaptureKind = "master"

CALENDAR_JSON_NAME = "nyse_sessions_2018_01_02_2026_12_31.json"
CALENDAR_SHA_NAME = "nyse_sessions_2018_01_02_2026_12_31.sha256"

DEFAULT_UNIVERSE = Path("data/coverage/coverage_universe.json")
DEFAULT_OUT_ROOT = Path("artifacts/census")
DEFAULT_CALENDAR_DIR = Path("data/calendar")

# Emitted outputs and vendored builds are never census INPUTS; dirt under
# these roots does not impede reproducibility.
IGNORED_DIRTY_PREFIXES = ("artifacts/", "dist/")

# The six pair-coverage counters, taken from the model so the two can never drift.
PAIR_CLASSES: tuple[str, ...] = tuple(PairCoverage.model_fields)

Confidence = Literal["EXACT", "PARTIAL"]

# ---- git state (factored so tests can inject a runner) --------------------------


class ReproducibilityError(RuntimeError):
    """The census cannot be tied to a clean, identifiable code state."""


class CensusEmitRefused(RuntimeError):
    """Round-8 review fix (2026-08-24, finding 5): an EMISSION-path refusal —
    an output name that is not a regular file (a symlink planted at it), a
    shared-inode temp, or a publish whose identity or readback does not
    match the rendered content. Mapped to exit 4: the reproducibility /
    emission refusal family (the same family as the output-dir-exists
    refusal — a refusal to emit, never a write through a swapped name)."""


GitRunner = Callable[..., str]


def _real_git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


# Test seam: monkeypatched by unit tests; production always runs real git.
GIT_RUNNER: GitRunner = _real_git


def _dirty_line_ignored(line: str) -> bool:
    """One `git status --porcelain` line -> ignored?

    Untracked paths (`??`) never block a census, and neither do changes
    confined to artifacts/ or dist/. Rename lines carry both sides
    (`R  old -> new`): every path on the line must be ignored for it to
    drop."""
    if line.startswith("??"):
        return True
    body = line[3:].strip() if len(line) > 3 else ""
    paths = [part.strip().strip('"') for part in body.split("->")]
    paths = [part for part in paths if part]
    return bool(paths) and all(part.startswith(IGNORED_DIRTY_PREFIXES) for part in paths)


def resolve_code_state(run: GitRunner) -> str:
    """HEAD sha of a CLEAN tracked tree, or a refusal.

    `run` executes one git subcommand in the repository and returns its
    stdout; it is a parameter so the refusal paths are unit-testable
    without touching a real checkout."""
    try:
        head = run("rev-parse", "HEAD").strip()
        status = run("status", "--porcelain")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ReproducibilityError(f"git unusable in {REPO_ROOT}: {exc}") from None
    dirty = [line for line in status.splitlines() if line and not _dirty_line_ignored(line)]
    if dirty:
        raise ReproducibilityError(
            f"tracked tree dirty ({len(dirty)} line(s), first {dirty[0]!r});"
            " commit before building a census"
        )
    if not head:
        raise ReproducibilityError("git rev-parse HEAD returned nothing")
    return head


# ---- inputs ---------------------------------------------------------------------


def load_universe(path: Path) -> CoverageUniverse:
    """Read + verify the declared universe manifest (fail closed)."""
    raw = path.read_bytes()
    universe = CoverageUniverse.model_validate(json.loads(raw))
    verify_universe(universe)
    return universe


def _pair_entries(manifest: MassiveCaptureManifest) -> dict[tuple[str, str], MasterEntry]:
    entries: dict[tuple[str, str], MasterEntry] = {}
    for entry in manifest.masters:
        key = (entry.underlying, entry.as_of)
        if key in entries:
            raise MassiveManifestError(
                f"masters[] lists ({entry.underlying}, {entry.as_of}) more than once"
            )
        entries[key] = entry
    return entries


def load_sealed_manifest(capture_dir: Path) -> tuple[MassiveCaptureManifest, str]:
    """Load + verify the capture manifest against the capture directory,
    reading the manifest file ONCE.

    Verification includes the on-disk reconciliation (every listed file
    re-hashed, no unlisted *.json), so a MID-RUN era directory refuses here
    by design: this census only ever consumes a sealed capture.

    Round-7 review fix (2026-08-24, finding 3): the manifest bytes are read
    a single time here and threaded into the loader (``raw=``), so the parse
    and everything derived downstream consume exactly the bytes that were
    verified; the sha256 of THOSE bytes is returned so the census provenance
    can name them without a second read (the pre-fix shape re-read the path
    at provenance time, so a manifest swapped in that window left the
    derivation consuming A-pinned bytes while the provenance named B)."""
    manifest_path = capture_dir / CAPTURE_MANIFEST_FILENAME
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise MassiveManifestError(
            f"{manifest_path}: manifest unreadable ({exc.strerror})"
        ) from None
    manifest = load_massive_capture_manifest(manifest_path, raw=raw)
    verify_massive_capture_manifest(manifest, capture_dir, capture_version=CAPTURE_VERSION)
    _pair_entries(manifest)
    return manifest, sha256_hex(raw)


def _read_pinned_bytes(capture_dir: Path, relative: str, pinned: Mapping[str, str]) -> bytes:
    """Round-6 review fix (2026-08-24, finding 5): the bytes the census
    DERIVES from must be the bytes the sealed manifest pins — read ONCE and
    re-hashed HERE, at the point of consumption (the discipline 7211e0a
    applied to BARS regeneration in ``bars_manifest._require_pinned_bytes``,
    mirrored for the census producer).

    ``load_sealed_manifest`` verifies every pinned file, but the consumers
    below used to RE-READ the paths afterwards; a swap in that window
    (byte-different spot JSON that HAS the session close) upgraded the
    sealed SPOT_MISSING_SESSION/exit-5 state into COMPLETE/exit 0. A pinned
    file unreadable, unpinned-on-disk, or drifted at read time refuses
    fail-closed naming the drift."""
    path = capture_dir / relative
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MassiveManifestError(
            f"listed file {relative} pinned by the sealed manifest is unreadable "
            f"at read time ({exc.strerror})"
        ) from None
    declared = pinned.get(relative)
    if declared is None:
        raise MassiveManifestError(
            f"{relative}: on disk but not pinned by the sealed manifest — the "
            "census derives from sealed bytes only"
        )
    if sha256_hex(raw) != declared:
        raise MassiveManifestError(
            f"listed file {relative} drifted from the sealed capture manifest at "
            f"read time (pin {declared[:12]}…, read now {sha256_hex(raw)[:12]}…)"
        )
    return raw


class _PinnedCaptureFile:
    """The read-once stand-in for a capture file whose bytes
    ``_read_pinned_bytes`` already re-hashed against the sealed manifest.

    ``inspect_structural_coverage`` is a protected baseline (blob-identical
    to base), so its loaders keep their base signatures — the pinned read
    reaches them through the exact Path protocol they use on a single
    capture file: ``read_bytes`` (the only content access — these bytes,
    never a second read of the real path), ``name``/``stem`` (lineage
    records, error strings, as_of-from-filename), and ``is_file``/
    ``is_dir`` (``_json_files`` single-file resolution). Parsing the real
    path again would re-open the verify-then-re-read swap race this fix
    closes."""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self.stem = Path(name).stem
        self._data = data

    def read_bytes(self) -> bytes:
        return self._data

    def is_file(self) -> bool:
        return True

    def is_dir(self) -> bool:
        return False


def verify_capture_manifest_at_emission(capture_dir: Path, verified_sha: str) -> None:
    """Round-8 review fix (2026-08-24, finding 2): the FINAL-EFFECT manifest
    guard, run immediately before anything is written under out_dir — and,
    since round-11 (finding 2), AGAIN inside ``_emit_census_set`` at the
    write moment, against the input manifest sha RECORDED in the census body
    being emitted.

    The round-7 provenance guard re-reads the manifest at census time —
    BEFORE the spot/master derivation and BEFORE the emission — so a
    manifest swapped in that window exited 0 with a census bound to the
    verified A bytes while the capture directory held B. This re-check sits
    at the final effect: the manifest NAME must lstat as a REGULAR file (a
    symlink there is never the verified manifest, whatever it points at)
    and an ``O_NOFOLLOW`` read of it must hash to the threaded verified
    sha. Any drift raises MassiveManifestError (exit 2, the manifest-tamper
    family) BEFORE ``out_dir.mkdir``, so a refusal emits nothing."""
    manifest_path = capture_dir / CAPTURE_MANIFEST_FILENAME
    try:
        info = os.lstat(manifest_path)
    except OSError as exc:
        raise MassiveManifestError(
            f"{manifest_path}: capture manifest unreadable at emission time "
            f"({exc.strerror}) — the census names ONE sealed byte set"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise MassiveManifestError(
            f"{manifest_path}: capture manifest is not a regular file at "
            f"emission time (lstat mode {stat.S_IFMT(info.st_mode):o} — a "
            "symlink at the name is never the verified manifest)"
        )
    try:
        fd = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise MassiveManifestError(
            f"{manifest_path}: capture manifest could not be opened without "
            f"following a symlink at emission time ({exc.strerror})"
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
    if now_sha != verified_sha:
        raise MassiveManifestError(
            f"{manifest_path}: capture manifest drifted between verification "
            f"and emission (verified {verified_sha[:12]}…, read now "
            f"{now_sha[:12]}…) — no census may be emitted against a capture "
            "directory that no longer holds the verified manifest bytes"
        )


def load_spot(
    capture_dir: Path, *, pinned: Mapping[str, str] | None = None
) -> dict[str, dict[date, Decimal]]:
    """The capture's spot proxy, or `{}` when the era wrote none.

    A spot proxy that EXISTS but will not decode is a capture-side refusal
    (the caller maps it to exit 2), never a silent empty.

    Round-6 review fix (2026-08-24, finding 5): with ``pinned`` (the
    sealed manifest's path->sha256 map) the proxy bytes are read ONCE,
    re-hashed against the pin, and PARSED from that same read — never a
    second read of the path. A proxy the manifest does not pin but that
    appeared on disk after verification refuses; a pinned proxy that is
    unreadable or drifted at read time refuses."""
    spot_path = capture_dir / SPOT_PROXY_FILENAME
    if pinned is None:
        if not spot_path.is_file():
            return {}
        return isc.load_spot_proxy(spot_path)
    if SPOT_PROXY_FILENAME not in pinned:
        if spot_path.is_file():
            raise MassiveManifestError(
                f"{SPOT_PROXY_FILENAME}: on disk but not pinned by the sealed "
                "manifest — the census derives from sealed bytes only"
            )
        return {}
    raw = _read_pinned_bytes(capture_dir, SPOT_PROXY_FILENAME, pinned)
    return isc.load_spot_proxy(cast(Path, _PinnedCaptureFile(spot_path.name, raw)))


# ---- reconciliation --------------------------------------------------------------


class Reconciled(NamedTuple):
    coverage: PairCoverage
    findings: list[PairFinding]
    holiday_fridays: list[str]
    session_spot_gaps: list[PairFinding]
    spot_sessions_with_close: int
    pair_classes: dict[tuple[str, str], str]


def reconcile_pairs(
    universe: CoverageUniverse,
    manifest: MassiveCaptureManifest,
    capture_dir: Path,
    spot_proxy: dict[str, dict[date, Decimal]],
    calendar: StaticSessionCalendar,
) -> Reconciled:
    """Classify EVERY (underlying, friday) pair in the universe."""
    entries = _pair_entries(manifest)
    counts = {cls: 0 for cls in PAIR_CLASSES}
    findings: list[PairFinding] = []
    session_gaps: list[PairFinding] = []
    holiday_fridays: list[str] = []
    spot_with_close = 0
    pair_classes: dict[tuple[str, str], str] = {}

    for friday in universe.as_of_fridays:
        friday_date = date.fromisoformat(friday)
        is_session = calendar.is_session(friday_date)
        if not is_session:
            holiday_fridays.append(friday)
        for underlying in universe.underlyings:
            spot_present = underlying in spot_proxy and friday_date in spot_proxy[underlying]
            if is_session and spot_present:
                spot_with_close += 1
            entry = entries.get((underlying, friday))
            has_file = (
                entry is not None
                and entry.file is not None
                and (capture_dir / MASTERS_DIR / entry.file).exists()
            )
            classification = classify_pair(
                underlying=underlying,
                as_of=friday,
                entry_complete=entry.complete if entry else None,
                entry_truncated=bool(entry.truncated) if entry else False,
                entry_error=(entry.error or "") if entry else "",
                has_file=has_file,
                spot_present=spot_present,
                is_session=is_session,
            )
            counts[classification] += 1
            pair_classes[(underlying, friday)] = classification
            if classification != "COMPLETE":
                finding = PairFinding(
                    underlying=underlying,
                    as_of=friday,
                    classification=classification,
                    detail=_finding_detail(
                        entry=entry,
                        has_file=has_file,
                        spot_present=spot_present,
                        is_session=is_session,
                    ),
                )
                findings.append(finding)
                if classification == "SPOT_MISSING_SESSION":
                    session_gaps.append(finding)
    return Reconciled(
        coverage=PairCoverage(**counts),
        findings=findings,
        holiday_fridays=holiday_fridays,
        session_spot_gaps=session_gaps,
        spot_sessions_with_close=spot_with_close,
        pair_classes=pair_classes,
    )


def _finding_detail(
    *,
    entry: MasterEntry | None,
    has_file: bool,
    spot_present: bool,
    is_session: bool,
) -> str:
    if entry is None:
        return "no manifest entry for this (underlying, as_of) pair"
    if not has_file:
        return "manifest entry has no master file on disk"
    if entry.error:
        return f"capture error: {entry.error}"
    if entry.truncated or not entry.complete:
        return "capture stopped at the page cap (next_url pending)"
    if not spot_present:
        return (
            "session friday with no spot close (vendor availability gap)"
            if is_session
            else "holiday friday: no close by definition (exchange closed)"
        )
    return ""


# ---- masters re-parse --------------------------------------------------------------


class MastersObserved(NamedTuple):
    masters_observed: int
    rows_declared_total: int
    rows_parsed_total: int
    distinct_contracts: int
    capture_complete_false: int
    rows_disagree: int
    unparseable: int
    notes: list[PairFinding]
    semantic: list[PairFinding]


# Round-3 review fix (2026-08-23, finding 2): the INCOMPLETE class each
# parsed-master disagreement downgrades a COMPLETE pair to. A mismatched
# envelope means the declared pair's master is not on disk (MISSING); an
# envelope whose own pages say the capture stopped early is TRUNCATED — the
# same class classify_pair gives an entry that admits it.
SEMANTIC_DOWNGRADES: dict[str, str] = {
    "MASTER_IDENTITY_MISMATCH": "MISSING",
    "MASTER_CAPTURE_TRUNCATED": "TRUNCATED",
}


def observe_masters(
    manifest: MassiveCaptureManifest,
    capture_dir: Path,
    *,
    pinned: Mapping[str, str] | None = None,
) -> MastersObserved:
    """Re-parse every master the manifest references; count what is there.

    A master that will not parse becomes a finding, never a crash: the file
    already passed the manifest's raw-byte re-hash, so an inspector refusal
    is an observed data defect the census reports.

    Round-6 review fix (2026-08-24, finding 5): with ``pinned`` (the sealed
    manifest's path->sha256 map) each master's bytes are read ONCE,
    re-hashed against the pin, and PARSED from that same read — the masters
    leg had exactly the verify-then-re-read gap the spot proxy had, and a
    swapped-but-parseable envelope used to feed the census rows the seal
    never attested."""
    rows_declared = 0
    rows_parsed = 0
    parsed = 0
    complete_false = 0
    rows_disagree = 0
    unparseable = 0
    notes: list[PairFinding] = []
    semantic: list[PairFinding] = []
    # Natural unique contract key: the OCC `ticker` — globally unique by
    # construction (root + expiry + C/P + strike encode the symbol) — carried
    # WITH its `underlying_ticker` so a cross-underlying collision would
    # count twice rather than silently merge. The same ticker captured on
    # several fridays is ONE contract identity, which is the point.
    contracts_seen: set[tuple[str, str]] = set()

    for entry in manifest.masters:
        rows_declared += entry.rows
        if not entry.file:
            continue
        master_path = capture_dir / MASTERS_DIR / entry.file
        try:
            if pinned is not None:
                raw = _read_pinned_bytes(capture_dir, f"{MASTERS_DIR}/{entry.file}", pinned)
                masters = isc.load_contract_masters(
                    cast(Path, _PinnedCaptureFile(master_path.name, raw))
                )
            else:
                masters = isc.load_contract_masters(master_path)
        except isc.StructuralCoverageError as exc:
            unparseable += 1
            notes.append(
                PairFinding(
                    underlying=entry.underlying,
                    as_of=entry.as_of,
                    classification="MASTER_UNPARSEABLE",
                    detail=str(exc),
                )
            )
            continue
        if len(masters) != 1:
            unparseable += 1
            notes.append(
                PairFinding(
                    underlying=entry.underlying,
                    as_of=entry.as_of,
                    classification="MASTER_UNPARSEABLE",
                    detail=f"{entry.file}: {len(masters)} masters parsed from one file",
                )
            )
            continue
        master = masters[0]
        parsed += 1
        rows_parsed += len(master.contracts)
        if not master.capture_complete:
            complete_false += 1
        # Round-3 review fix (2026-08-23, finding 2): join the PARSED
        # master's identity and completeness against the manifest entry
        # that selected it. The manifest's file hashes pin BYTES, not
        # MEANING — a correctly-hashed entry declaring complete
        # SPY/<as_of> could point at a valid pinned envelope for a
        # different pair, or one whose envelope says the capture stopped
        # early, and reconciliation alone counted the declared pair
        # COMPLETE. Every disagreement becomes a semantic finding that
        # apply_master_semantic_join folds into coverage (and exit 0).
        if (master.underlying, master.as_of.isoformat()) != (entry.underlying, entry.as_of):
            semantic.append(
                PairFinding(
                    underlying=entry.underlying,
                    as_of=entry.as_of,
                    classification="MASTER_IDENTITY_MISMATCH",
                    detail=(
                        f"{entry.file}: pinned envelope is "
                        f"{master.underlying}/{master.as_of.isoformat()}, "
                        f"not the declared {entry.underlying}/{entry.as_of}"
                    ),
                )
            )
        elif not master.capture_complete:
            semantic.append(
                PairFinding(
                    underlying=entry.underlying,
                    as_of=entry.as_of,
                    classification="MASTER_CAPTURE_TRUNCATED",
                    detail=(
                        f"{entry.file}: envelope capture_complete=false although "
                        "the manifest entry declares complete"
                    ),
                )
            )
        if len(master.contracts) != entry.rows:
            rows_disagree += 1
            notes.append(
                PairFinding(
                    underlying=entry.underlying,
                    as_of=entry.as_of,
                    classification="MASTER_ROWS_DISAGREE",
                    detail=f"manifest rows {entry.rows} != parsed {len(master.contracts)}",
                )
            )
        for contract in master.contracts:
            contracts_seen.add((contract.underlying_ticker, contract.ticker))

    # Round-7 review fix (2026-08-24, finding 4): completeness mirror of
    # 2c3db7b. files[] pins EVERY on-disk masters/*.json (the manifest
    # builder's directory scan), but masters[] (the metadata entries) may
    # reference only a subset: a stale master pinned in files[] yet
    # referenced by NO entry was never read by the entry-driven loop above,
    # and masters_observed == expected still exited 0 — a sealed master
    # silently ignored (the strengthened M199 test covers UNLISTED files;
    # this is LISTED-but-unreferenced). Every pinned master-kind file must
    # be referenced by an entry, or the census refuses naming every
    # unreferenced file. The check runs on BOTH invocation paths (pinned or
    # not): the defense holds regardless.
    pinned_master_files = {f.path for f in manifest.files if f.kind == MASTER_KIND}
    referenced_master_files = {
        f"{MASTERS_DIR}/{entry.file}" for entry in manifest.masters if entry.file
    }
    unreferenced = sorted(pinned_master_files - referenced_master_files)
    if unreferenced:
        raise MassiveManifestError(
            "capture manifest pins master file(s) no masters[] entry references: "
            f"{', '.join(unreferenced)} — every sealed master is census evidence; "
            "a pinned-but-unreferenced master is silently ignored evidence, and "
            "the census refuses to derive around it"
        )

    return MastersObserved(
        masters_observed=parsed,
        rows_declared_total=rows_declared,
        rows_parsed_total=rows_parsed,
        distinct_contracts=len(contracts_seen),
        capture_complete_false=complete_false,
        rows_disagree=rows_disagree,
        unparseable=unparseable,
        notes=notes,
        semantic=semantic,
    )


def apply_master_semantic_join(reconciled: Reconciled, semantic: list[PairFinding]) -> Reconciled:
    """Fold the parsed-master/manifest-entry disagreements into coverage.

    A pair in ANY class not already in INCOMPLETE_CLASSES — COMPLETE, and
    SPOT_MISSING_HOLIDAY (round-4 review fix, 2026-08-23: the holiday class
    is deliberately outside INCOMPLETE_CLASSES, so a holiday pair whose
    entry correctly hashes a foreign envelope used to record the finding yet
    keep the holiday class and exit 0 — but the declared pair's master is
    NOT on disk, which is missing data, not a holiday) — is demoted to the
    INCOMPLETE class its semantic finding names. Already-incomplete pairs
    keep their class (the finding is still recorded). Both paths block exit 0
    through the ordinary INCOMPLETE_CLASSES counters — the same exit
    semantics a reconciliation-side incomplete pair already has."""
    if not semantic:
        return reconciled
    counts = reconciled.coverage.model_dump()
    findings = list(reconciled.findings)
    for finding in sorted(semantic, key=lambda f: (f.underlying, f.as_of, f.classification)):
        findings.append(finding)
        current = reconciled.pair_classes.get((finding.underlying, finding.as_of))
        if current is not None and current not in INCOMPLETE_CLASSES:
            counts[current] -= 1
            counts[SEMANTIC_DOWNGRADES[finding.classification]] += 1
    return reconciled._replace(coverage=PairCoverage(**counts), findings=findings)


# ---- values -----------------------------------------------------------------------


def build_values(
    universe: CoverageUniverse,
    manifest: MassiveCaptureManifest,
    pairs_total: int,
    reconciled: Reconciled,
    masters: MastersObserved,
) -> CensusValues:
    """The four-class taxonomy, populated in one determinate place.

    Confidence rule (deterministic): every reconciliation fact is PARTIAL
    iff the masters rows do not reconcile OR any pair sits in an
    INCOMPLETE_CLASSES class; EXACT otherwise. A holiday Friday without a
    close is NOT incomplete — the exchange was closed."""
    incomplete_pairs = sum(getattr(reconciled.coverage, cls) for cls in sorted(INCOMPLETE_CLASSES))
    confidence: Confidence = (
        "EXACT"
        if masters.rows_parsed_total == masters.rows_declared_total and incomplete_pairs == 0
        else "PARTIAL"
    )
    cov = reconciled.coverage
    session_pairs = pairs_total - len(reconciled.holiday_fridays) * len(universe.underlyings)
    observed: dict[str, CensusFact] = {
        "pair_complete": CensusFact(
            v=cov.COMPLETE, support={"expected_pairs": pairs_total}, confidence=confidence
        ),
        "pair_truncated": CensusFact(
            v=cov.TRUNCATED, support={"expected_pairs": pairs_total}, confidence=confidence
        ),
        "pair_error": CensusFact(
            v=cov.ERROR, support={"expected_pairs": pairs_total}, confidence=confidence
        ),
        "pair_missing": CensusFact(
            v=cov.MISSING, support={"expected_pairs": pairs_total}, confidence=confidence
        ),
        "pair_spot_missing_session": CensusFact(
            v=cov.SPOT_MISSING_SESSION,
            support={"expected_pairs": pairs_total},
            confidence=confidence,
        ),
        "pair_spot_missing_holiday": CensusFact(
            v=cov.SPOT_MISSING_HOLIDAY,
            support={"expected_pairs": pairs_total},
            confidence=confidence,
        ),
        "masters_observed": CensusFact(
            v=masters.masters_observed,
            support={
                "expected_masters": universe.expected_masters,
                "manifest_entries": len(manifest.masters),
                "capture_complete_false": masters.capture_complete_false,
                "rows_disagree": masters.rows_disagree,
                "unparseable": masters.unparseable,
            },
            confidence=confidence,
        ),
        "rows_declared_total": CensusFact(
            v=masters.rows_declared_total,
            support={"manifest_entries": len(manifest.masters)},
            confidence=confidence,
        ),
        "rows_parsed_total": CensusFact(
            v=masters.rows_parsed_total,
            support={"masters_parsed": masters.masters_observed},
            confidence=confidence,
        ),
        "distinct_contracts": CensusFact(
            v=masters.distinct_contracts,
            support={"rows_parsed_total": masters.rows_parsed_total},
            confidence=confidence,
        ),
        "spot_sessions_with_close": CensusFact(
            v=reconciled.spot_sessions_with_close,
            support={"session_pairs": session_pairs},
            confidence=confidence,
        ),
        "spot_holiday_fridays": CensusFact(
            v=len(reconciled.holiday_fridays),
            support={"universe_fridays": len(universe.as_of_fridays)},
            confidence=confidence,
        ),
    }
    # The coverage era ran `--bars 0`: no option bar exists until the
    # ATM-grid bars era, which the declared sequence orders AFTER protocol
    # 0.2.1 — so every bar-volume derivation input is NOT_EVALUABLE, and the
    # contradiction is carried verbatim in not_yet_decided below.
    observed["bar_volume_observations"] = CensusFact(
        v=len(manifest.bars),
        support={"bar_files_listed": len(manifest.bars)},
        confidence="NOT_EVALUABLE",
    )
    predeclared: dict[str, CensusFact | str] = {
        "expected_masters": CensusFact(
            v=universe.expected_masters,
            support={
                "underlyings": len(universe.underlyings),
                "fridays": len(universe.as_of_fridays),
            },
            confidence="EXACT",
        ),
        "universe_underlyings": CensusFact(
            v=len(universe.underlyings), support={}, confidence="EXACT"
        ),
        "universe_fridays": CensusFact(
            v=len(universe.as_of_fridays), support={}, confidence="EXACT"
        ),
    }
    not_yet_decided: dict[str, str] = {
        "flow_min_session_volume": "AWAITING_OWNER_RULE — " + G3_DERIVATION_CONTRADICTION,
        "final_holdout_window": "AWAITING_OWNER_DECLARATION at era-results",
    }
    return CensusValues(
        observed_census_fact=observed,
        predeclared_derivation_input=predeclared,
        owner_ratified_policy_value={},  # EMPTY by construction in this era
        not_yet_decided=not_yet_decided,
    )


def build_registry(values: CensusValues) -> dict[str, ValueClass]:
    registry: dict[str, ValueClass] = {}
    for fact_id in values.observed_census_fact:
        registry[fact_id] = "observed_census_fact"
    for fact_id in values.predeclared_derivation_input:
        registry[fact_id] = "predeclared_derivation_input"
    for fact_id in values.owner_ratified_policy_value:
        registry[fact_id] = "owner_ratified_policy_value"
    for fact_id in values.not_yet_decided:
        registry[fact_id] = "not_yet_decided"
    return registry


# ---- artifact ----------------------------------------------------------------------


def _emit_census_set(
    out_fd: int,
    outputs: tuple[tuple[str, str], ...],
    *,
    capture_dir: Path,
    census: CoverageCensus,
) -> None:
    """Round-8 review fix (2026-08-24, finding 5) + round-11 review fixes
    (findings 2 and 9): publish the census outputs as ONE all-or-nothing
    set, each through custody — the local mirror of the amendment builder's
    custody write (kept local: that helper is private to
    protocol/amendment.py, and this script imports only the package models).

    Round-8: the pre-fix emitter made three plain ``write_text`` calls
    THROUGH the final names after ``out_dir.mkdir``: a ``census.json ->
    research_protocol.yaml`` link planted between the mkdir and the write
    truncated the PROTECTED protocol file with census JSON while the command
    still exited 0.

    Round-11 (finding 2, census half): the manifest identity is re-checked
    INSIDE the emit path at the WRITE MOMENT — the round-8 gate ran before
    the render and the emit, so a manifest swapped after it emitted an
    exit-0 census bound to the verified A bytes while the capture directory
    held B. The input manifest sha RECORDED in the census body being written
    must equal the manifest sha re-read here, at the boundary; divergence is
    a MassiveManifestError (exit 2, the manifest-tamper family) raised before
    anything is written.

    Round-11 (finding 9): publishing the three outputs sequentially meant a
    refusal at the SECOND output (a census.md planted symlink) left
    census.json PUBLISHED with exit 4 — and the retry refused on
    out_dir.exists() (never overwrite), so a refused emission was permanently
    unretryable. Now EVERY output is first vetted at its final name and
    written to an UNPREDICTABLE temp name under the custody fd
    (``secrets.token_hex`` — mkstemp cannot take a dir_fd — opened
    ``O_CREAT|O_EXCL|O_NOFOLLOW``), fsynced (``st_nlink == 1`` required) and
    read back; only when ALL of them hold their rendered bytes is the set
    published by rename in a FIXED order, and the complete set is verified at
    return (final name regular, published inode identity, full ``O_NOFOLLOW``
    readback). Any refusal unlinks every temp so NOTHING is published — the
    caller removes the then-empty digest dir and a retry is clean.

    Round-10 (finding 9): a failure AFTER the first final rename succeeded —
    including a BARE OSError (a directory planted at an output name makes
    ``os.replace`` raise outside both typed families) — rolls back the
    members THIS run published (identity-checked unlinks of exactly the
    inodes this run renamed into place) before anything re-raises, so the
    set is all-or-nothing including the raw-OSError path."""
    # F2 (round-11): the emission itself carries the manifest verification —
    # the LAST filesystem act before the census bytes are published. The
    # recorded input manifest sha must equal the sha re-read at this
    # boundary; any drift refuses with NOTHING written.
    verify_capture_manifest_at_emission(capture_dir, census.provenance.input_manifest_sha256)
    temps: list[tuple[str, str, tuple[int, int]]] = []  # (final, tmp, identity)
    try:
        for name, content in outputs:
            # (a) every final name, un-followed, is vetted BEFORE anything
            # publishes: a symlink at an output name is never this command's
            # artifact, whatever it points at.
            try:
                existing = os.stat(name, dir_fd=out_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass  # absent: the rename below will publish it
            else:
                if not stat.S_ISREG(existing.st_mode):
                    raise CensusEmitRefused(
                        f"{name}: the census output name is not a regular file "
                        f"(lstat mode {stat.S_IFMT(existing.st_mode):o} — a symlink at "
                        "an output name is never written through)"
                    )
            tmp_name = f".{name}.{secrets.token_hex(16)}.tmp"
            try:
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=out_fd,
                )
            except OSError as exc:
                raise CensusEmitRefused(
                    f"{name}: the census temp output could not be created ({exc.strerror})"
                ) from None
            try:
                os.fchmod(fd, 0o644)
                # fdopen owns the fd from here: the with-block closes it on
                # every path.
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                    written = os.fstat(handle.fileno())
                if written.st_nlink != 1:
                    raise CensusEmitRefused(
                        f"{tmp_name}: the temp output has {written.st_nlink} hard "
                        "links — refusing to publish a shared inode over the "
                        "output name"
                    )
                # byte custody on the temp: the bytes about to be renamed into
                # place are read back and must equal the rendered content.
                if _read_all_nofollow(out_fd, tmp_name) != content.encode("utf-8"):
                    raise CensusEmitRefused(
                        f"{tmp_name}: the temp output does not hold the rendered "
                        "bytes — refusing to publish the set"
                    )
            except BaseException:
                # on any refusal the half-written temp must not linger; on
                # success the set publish below consumes the name.
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_name, dir_fd=out_fd)
                raise
            temps.append((name, tmp_name, (written.st_dev, written.st_ino)))
        # (b) all outputs verified: publish the SET by rename, fixed order —
        # a rename swaps the DIRECTORY ENTRY, so a link planted at an output
        # name is unlinked by the swap and never written through.
        published: list[tuple[str, tuple[int, int]]] = []
        try:
            for name, tmp_name, identity in temps:
                os.replace(tmp_name, name, src_dir_fd=out_fd, dst_dir_fd=out_fd)
                published.append((name, identity))
            # (c) verify the complete set at return: the final name must
            # lstat as a regular file holding the inode this command wrote,
            # and a full O_NOFOLLOW readback must equal the rendered content.
            for (name, _tmp_name, identity), (_output_name, content) in zip(
                temps, outputs, strict=True
            ):
                try:
                    published_stat = os.stat(name, dir_fd=out_fd, follow_symlinks=False)
                except OSError as exc:
                    raise CensusEmitRefused(
                        f"{name}: the published census output vanished after the publish"
                        f" ({exc.strerror})"
                    ) from None
                if not stat.S_ISREG(published_stat.st_mode) or (
                    published_stat.st_dev,
                    published_stat.st_ino,
                ) != (identity[0], identity[1]):
                    raise CensusEmitRefused(
                        f"{name}: the published census output is not the inode this "
                        f"command wrote (wrote dev {identity[0]} ino {identity[1]},"
                        f" published dev {published_stat.st_dev} ino {published_stat.st_ino},"
                        f" mode {stat.S_IFMT(published_stat.st_mode):o})"
                        " — refusing to attest it"
                    )
                if _read_all_nofollow(out_fd, name) != content.encode("utf-8"):
                    raise CensusEmitRefused(
                        f"{name}: the published census output does not hold the "
                        "rendered bytes — refusing to attest it"
                    )
        except BaseException:
            # Round-10 P1 (finding 9): a failure after the first final rename
            # succeeded — including a BARE OSError from os.replace — rolls
            # back the members THIS run already published. They are provably
            # ours (vetted temps, renamed under the held fd); each is
            # unlinked only while the name still maps to the inode this run
            # renamed into it, so the set is all-or-nothing even on the
            # raw-OSError path.
            for name, identity in published:
                _unlink_published_if_ours(out_fd, name, identity)
            raise
    finally:
        # any refusal unlinks every temp: NOTHING of the set is published
        # under a temp name either.
        for _name, tmp_name, _identity in temps:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name, dir_fd=out_fd)


def _unlink_published_if_ours(dir_fd: int, name: str, identity: tuple[int, int]) -> None:
    """Round-10 P1 (finding 9): unlink ``name`` only while it still maps to
    ``identity`` — the rollback of a partially published census set removes
    exactly the inodes this command renamed into place, never whatever a
    concurrent substitution planted at the name."""
    try:
        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return
    if (named.st_dev, named.st_ino) == identity:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)


def _read_all_nofollow(dir_fd: int, name: str) -> bytes:
    """One full O_NOFOLLOW read of ``name`` inside the custody dir fd."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise CensusEmitRefused(
            f"{name}: the census output could not be re-read without following "
            f"a symlink ({exc.strerror})"
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
    return b"".join(chunks)


def render_json(census: CoverageCensus) -> str:
    return (
        json.dumps(
            json.loads(census.model_dump_json()),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_markdown(census: CoverageCensus) -> str:
    cov = census.coverage.observed
    lines: list[str] = []
    lines.append("# Coverage-era census")
    lines.append("")
    lines.append(f"- schema: `{census.schema_version}`")
    lines.append(f"- content_sha256: `{census.content_sha256}`")
    lines.append(f"- expected masters: {census.coverage.expected_masters}")
    lines.append("")
    lines.append("## Pair coverage")
    lines.append("")
    lines.append("| class | pairs |")
    lines.append("| --- | ---: |")
    for cls in PAIR_CLASSES:
        lines.append(f"| {cls} | {getattr(cov, cls)} |")
    holiday_list = ", ".join(census.coverage.holiday_fridays) or "(none)"
    lines.append("")
    lines.append(f"- holiday fridays (no close by definition): {holiday_list}")
    lines.append(
        f"- session fridays missing their spot close: {len(census.coverage.session_spot_gaps)}"
    )
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    if census.coverage.findings:
        for finding in census.coverage.findings:
            tail = f" — {finding.detail}" if finding.detail else ""
            lines.append(f"- {finding.underlying} {finding.as_of} {finding.classification}{tail}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Values (four-class taxonomy)")
    lines.append("")
    sections: tuple[tuple[str, Mapping[str, CensusFact | str]], ...] = (
        ("observed_census_fact", census.values.observed_census_fact),
        ("predeclared_derivation_input", census.values.predeclared_derivation_input),
        ("owner_ratified_policy_value", census.values.owner_ratified_policy_value),
        ("not_yet_decided", census.values.not_yet_decided),
    )
    for name, section in sections:
        lines.append(f"### {name}")
        lines.append("")
        if not section:
            lines.append("(empty by construction in this era)")
            lines.append("")
            continue
        for fact_id, value in section.items():
            if isinstance(value, CensusFact):
                lines.append(f"- `{fact_id}` = {value.v} (confidence {value.confidence})")
            else:
                lines.append(f"- `{fact_id}`:")
                for text_line in value.splitlines() or [value]:
                    lines.append(f"    > {text_line}")
        lines.append("")
    lines.append("### The derivation-source contradiction, verbatim")
    lines.append("")
    for text_line in G3_DERIVATION_CONTRADICTION.splitlines() or [G3_DERIVATION_CONTRADICTION]:
        lines.append(f"> {text_line}")
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    prov = census.provenance
    lines.append(f"- code_sha: `{prov.code_sha}`")
    lines.append(f"- protocol_hash: `{prov.protocol_hash}`")
    lines.append(f"- protocol_raw_sha256: `{prov.protocol_raw_sha256}`")
    lines.append(f"- input_manifest_sha256: `{prov.input_manifest_sha256}`")
    lines.append(f"- universe_manifest_sha256: `{prov.universe_manifest_sha256}`")
    lines.append(f"- uv_lock_sha256: `{prov.uv_lock_sha256}`")
    lines.append(f"- command: `{' '.join(prov.command)}`")
    lines.append("")
    lines.append(
        # Round-5 review fix (2026-08-24, finding 5): the old text ("0 iff
        # every universe pair is COMPLETE") was stale the moment the holiday
        # rule landed — SPOT_MISSING_HOLIDAY pairs legitimately exit 0. State
        # the real rule: the same conjunction main() computes.
        "Exit contract: 0 iff zero pairs sit in INCOMPLETE_CLASSES"
        " (MISSING/TRUNCATED/ERROR/SPOT_MISSING_SESSION) and masters"
        " observed == expected_masters — holiday Fridays without a close"
        " (SPOT_MISSING_HOLIDAY) are EXPECTED and do not block exit 0;"
        " otherwise this census was emitted with exit 5."
    )
    lines.append("")
    return "\n".join(lines)


# ---- CLI -----------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--capture-dir", type=Path, required=True, help="sealed era capture dir")
    parser.add_argument(
        "--universe", type=Path, default=DEFAULT_UNIVERSE, help="declared universe manifest"
    )
    parser.add_argument(
        "--out-root", type=Path, default=DEFAULT_OUT_ROOT, help="census output root"
    )
    parser.add_argument(
        "--calendar-dir", type=Path, default=DEFAULT_CALENDAR_DIR, help="session calendar dir"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # 1. The declared universe (any defect refuses before anything is read).
    try:
        universe = load_universe(args.universe)
    except (OSError, ValueError) as exc:
        print(f"UNIVERSE REFUSED: {exc}", file=sys.stderr)
        return 3

    # 2. The sealed capture manifest (a mid-run era dir fails here BY DESIGN).
    try:
        manifest, manifest_sha = load_sealed_manifest(args.capture_dir)
    except MassiveManifestError as exc:
        print(f"MANIFEST REFUSED: {exc}", file=sys.stderr)
        return 2
    # Round-6 review fix (2026-08-24, finding 5): the pin map the consumers
    # below re-hash every read against — the census derives from sealed
    # bytes at the point of consumption, not from bytes verified once and
    # re-read afterwards.
    pinned = {entry.path: entry.sha256 for entry in manifest.files}

    # 3. Reproducibility: clean tracked tree + pinned provenance inputs.
    try:
        code_sha = resolve_code_state(GIT_RUNNER)
        proto = load_protocol()
        proto_hash_value = protocol_hash(proto)
        proto_raw = raw_file_hash()
        uv_lock_sha = sha256_hex((REPO_ROOT / "uv.lock").read_bytes())
        calendar = StaticSessionCalendar(
            args.calendar_dir / CALENDAR_JSON_NAME, args.calendar_dir / CALENDAR_SHA_NAME
        )
    except (ReproducibilityError, OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        print(f"REPRODUCIBILITY REFUSED: {exc}", file=sys.stderr)
        return 4

    # Round-7 review fix (2026-08-24, finding 3): the provenance consumes the
    # VERIFIED manifest bytes (manifest_sha above) — never a second read. A
    # guard read HERE refuses when the file no longer holds them: a manifest
    # swapped between verification and provenance would otherwise leave the
    # census attesting bytes the derivation never consumed. The refusal is
    # exit 2 — the manifest-tamper family, the same exit the round-6
    # spot/master drift refusals use.
    try:
        guard_sha = sha256_hex((args.capture_dir / CAPTURE_MANIFEST_FILENAME).read_bytes())
    except OSError as exc:
        print(
            f"MANIFEST REFUSED: capture manifest unreadable at provenance time ({exc.strerror})",
            file=sys.stderr,
        )
        return 2
    if guard_sha != manifest_sha:
        print(
            "MANIFEST REFUSED: capture manifest drifted between verification and "
            f"provenance (verified {manifest_sha[:12]}…, read now {guard_sha[:12]}…) — "
            "the census derives from and names ONE sealed byte set",
            file=sys.stderr,
        )
        return 2
    manifest_bytes_sha = manifest_sha

    # 4. Spot proxy (absent -> no closes; present but undecodable -> exit 2;
    # pinned bytes drifted or vanished between verification and this read ->
    # exit 2 too — round-6 finding 5: the census derives from sealed bytes).
    try:
        spot_proxy = load_spot(args.capture_dir, pinned=pinned)
    except (isc.StructuralCoverageError, MassiveManifestError) as exc:
        print(f"SPOT PROXY REFUSED: {exc}", file=sys.stderr)
        return 2

    # 5. Reconciliation over every universe pair + masters re-parse, then
    # the round-3 semantic join: a master that disagrees with the entry
    # that selected it downgrades that pair and blocks exit 0 below. The
    # masters re-parse re-hashes every read against the manifest pin
    # (round-6 finding 5 — the same rule the spot proxy is held to).
    reconciled = reconcile_pairs(universe, manifest, args.capture_dir, spot_proxy, calendar)
    try:
        masters = observe_masters(manifest, args.capture_dir, pinned=pinned)
    except MassiveManifestError as exc:
        print(f"MANIFEST REFUSED: {exc}", file=sys.stderr)
        return 2
    reconciled = apply_master_semantic_join(reconciled, masters.semantic)

    # 6-7. Values, taxonomy, provenance — assembled and self-checked.
    pairs_total = len(universe.underlyings) * len(universe.as_of_fridays)
    values = build_values(universe, manifest, pairs_total, reconciled, masters)
    census = CoverageCensus(
        schema_version=CENSUS_SCHEMA_VERSION,
        provenance=CensusProvenance(
            code_sha=code_sha,
            protocol_hash=proto_hash_value,
            protocol_raw_sha256=proto_raw,
            input_manifest_sha256=manifest_bytes_sha,
            universe_manifest_sha256=universe.content_sha256,
            uv_lock_sha256=uv_lock_sha,
            command=tuple(sys.argv),
            # Round-4 review fix (finding 3): the token is REQUIRED on the
            # model, and the builder states it explicitly — same value the
            # old default supplied, so emitted bytes are unchanged.
            report_version=CENSUS_SCHEMA_VERSION,
        ),
        coverage=CoverageBlock(
            expected_masters=universe.expected_masters,
            observed=reconciled.coverage,
            findings=(*reconciled.findings, *masters.notes),
            holiday_fridays=tuple(reconciled.holiday_fridays),
            session_spot_gaps=tuple(reconciled.session_spot_gaps),
        ),
        values=values,
        value_registry=build_registry(values),
        content_sha256="",
    )
    census = census.model_copy(update={"content_sha256": census_content_sha256(census)})
    try:
        verify_census(census)
        validate_value_taxonomy(census)
    except CensusTaxonomyError as exc:  # pragma: no cover - build-time self-check
        print(f"CENSUS SELF-CHECK REFUSED: {exc}", file=sys.stderr)
        return 4

    # 8. Emit content-addressed; never overwrite.
    out_dir = args.out_root / census.content_sha256[:12]
    if out_dir.exists():
        print(f"OUTPUT EXISTS: {out_dir} — refusing to overwrite", file=sys.stderr)
        return 4
    # Round-8 review fix (finding 2): the FINAL-EFFECT manifest guard. The
    # round-7 provenance guard above runs before the spot/master derivation
    # and the emission; a swap in that window used to exit 0 bound to the
    # verified A bytes while the capture dir held B. This re-check runs
    # immediately before ANY file is written under out_dir — and before
    # out_dir.mkdir — so a refusal emits NOTHING. Round-11 (finding 2) adds
    # the same re-check INSIDE the emit path at the write moment, against the
    # input manifest sha recorded in the census body: a swap after THIS gate
    # refuses there (exit 2) with nothing published.
    try:
        verify_capture_manifest_at_emission(args.capture_dir, manifest_bytes_sha)
    except MassiveManifestError as exc:
        print(f"MANIFEST REFUSED: {exc}", file=sys.stderr)
        return 2
    body = render_json(census)
    out_dir.mkdir(parents=True)
    # Round-8 review fix (finding 5) + round-11 review fix (finding 9): the
    # three outputs are emitted through CUSTODY writes against the out dir
    # held as a REAL directory fd — the pre-fix write_text calls went through
    # whatever the final names pointed at, so a planted symlink could
    # truncate a protected file and exit 0. The outputs now publish as ONE
    # all-or-nothing set (see _emit_census_set): a refusal leaves NOTHING
    # published, the empty digest dir is removed, and a retry is clean — the
    # pre-fix sequential emission left census.json published when a later
    # output refused, and the retry then hit OUTPUT EXISTS forever.
    try:
        out_fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        print(
            f"EMISSION REFUSED: {out_dir} could not be taken into custody ({exc.strerror})",
            file=sys.stderr,
        )
        return 4
    emitted = False
    refusal_exit: int | None = None
    # Round-10 P1 (finding 8): capture the held digest directory's identity
    # BEFORE the fd closes — once custody ends, the PATHNAME alone proves
    # nothing, and the nothing-was-published cleanup below may delete only
    # the directory this run created.
    held_dir_stat = os.fstat(out_fd)
    held_dir_identity = (held_dir_stat.st_dev, held_dir_stat.st_ino)
    try:
        _emit_census_set(
            out_fd,
            (
                ("census.json", body),
                ("census.md", render_markdown(census)),
                ("census.json.sha256", sha256_hex(body.encode("utf-8")) + "\n"),
            ),
            capture_dir=args.capture_dir,
            census=census,
        )
        emitted = True
    except CensusEmitRefused as exc:
        print(f"EMISSION REFUSED: {exc}", file=sys.stderr)
        refusal_exit = 4
    except MassiveManifestError as exc:
        # round-11 finding 2: the manifest identity re-read at the emit
        # boundary no longer equals the recorded input manifest sha — the
        # manifest-tamper family, never a publish.
        print(f"MANIFEST REFUSED: {exc}", file=sys.stderr)
        refusal_exit = 2
    except OSError as exc:
        # round-10 finding 9: a BARE OSError from the publish (e.g. a
        # directory planted at an output name makes os.replace raise
        # IsADirectoryError) is an EMISSION-path refusal like the typed
        # ones — never an untyped traceback over a half-published set. The
        # members this run already published were rolled back inside
        # _emit_census_set before this handler sees the exception.
        print(
            f"EMISSION REFUSED: the census set could not be published ({exc})",
            file=sys.stderr,
        )
        refusal_exit = 4
    finally:
        os.close(out_fd)
    if not emitted:
        # nothing was published: drop the digest dir this run created so a
        # retry is clean — out_dir.exists() would otherwise refuse it
        # forever. Round-10 P1 (finding 8): the exists() check above
        # predates custody and proves nothing AFTER it ends, so the deletion
        # is verify-then-delete — the path is re-statted without following
        # symlinks and removed ONLY if it still maps to exactly the held
        # identity; a substituted (or vanished) directory is a stranger's
        # tree, left untouched and refused loudly, never silently recursed
        # into. rmtree never follows a symlinked entry; a decoy target is
        # untouched either way.
        try:
            named = os.stat(out_dir, follow_symlinks=False)
        except OSError as exc:
            print(
                f"EMISSION REFUSED: {out_dir} vanished after custody ended "
                f"({exc.strerror}) — nothing to clean; an output directory "
                "that no longer names the held inode is never deleted by "
                "pathname",
                file=sys.stderr,
            )
            return 4
        if (named.st_dev, named.st_ino) != held_dir_identity:
            print(
                f"EMISSION REFUSED: {out_dir} was substituted after custody "
                f"ended (held dev/inode {held_dir_identity[0]}/"
                f"{held_dir_identity[1]}, the name now holds dev/inode "
                f"{named.st_dev}/{named.st_ino}) — refusing to delete a "
                "directory this run did not create",
                file=sys.stderr,
            )
            return 4
        shutil.rmtree(out_dir, ignore_errors=True)
        assert refusal_exit is not None
        return refusal_exit

    # Whole coverage = zero INCOMPLETE pairs. Holiday Fridays
    # (SPOT_MISSING_HOLIDAY) are EXPECTED gaps — the exchange was closed —
    # and INCOMPLETE_CLASSES excludes them by design; requiring every pair
    # COMPLETE would make exit 0 unreachable for any grid containing a Good
    # Friday (the committed one contains 2025-04-18 and 2026-04-03).
    incomplete_pairs = sum(getattr(reconciled.coverage, cls) for cls in INCOMPLETE_CLASSES)
    whole = incomplete_pairs == 0 and masters.masters_observed == universe.expected_masters
    print(
        json.dumps(
            {
                "out": str(out_dir),
                "content_sha256": census.content_sha256,
                "pairs_complete": reconciled.coverage.COMPLETE,
                "pairs_incomplete": incomplete_pairs,
                "pairs_total": pairs_total,
                "masters_observed": masters.masters_observed,
                "expected_masters": universe.expected_masters,
                "complete": whole,
            },
            sort_keys=True,
        )
    )

    # 9. Whole coverage exits 0; anything else still EMITS above, then fails.
    if whole:
        return 0
    return 5


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
