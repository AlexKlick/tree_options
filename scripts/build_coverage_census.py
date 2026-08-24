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
     an existing-but-undecodable spot proxy refuses here too
  3  universe manifest refused: unreadable, invalid, or tampered
  4  reproducibility refusal (git unusable, tracked tree dirty, protocol or
     uv.lock unreadable, calendar fixture refused, census self-check) — or
     the content-addressed output directory already exists (never overwrite)
  5  census emitted but coverage incomplete (the artifact is STILL written;
     partial evidence is never swallowed)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, NamedTuple

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


def load_sealed_manifest(capture_dir: Path) -> MassiveCaptureManifest:
    """Load + verify the capture manifest against the capture directory.

    Verification includes the on-disk reconciliation (every listed file
    re-hashed, no unlisted *.json), so a MID-RUN era directory refuses here
    by design: this census only ever consumes a sealed capture."""
    manifest = load_massive_capture_manifest(capture_dir / CAPTURE_MANIFEST_FILENAME)
    verify_massive_capture_manifest(manifest, capture_dir, capture_version=CAPTURE_VERSION)
    _pair_entries(manifest)
    return manifest


def load_spot(capture_dir: Path) -> dict[str, dict[date, Decimal]]:
    """The capture's spot proxy, or `{}` when the era wrote none.

    A spot proxy that EXISTS but will not decode is a capture-side refusal
    (the caller maps it to exit 2), never a silent empty."""
    spot_path = capture_dir / SPOT_PROXY_FILENAME
    if not spot_path.is_file():
        return {}
    return isc.load_spot_proxy(spot_path)


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


def observe_masters(manifest: MassiveCaptureManifest, capture_dir: Path) -> MastersObserved:
    """Re-parse every master the manifest references; count what is there.

    A master that will not parse becomes a finding, never a crash: the file
    already passed the manifest's raw-byte re-hash, so an inspector refusal
    is an observed data defect the census reports."""
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
        try:
            masters = isc.load_contract_masters(capture_dir / MASTERS_DIR / entry.file)
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
        manifest = load_sealed_manifest(args.capture_dir)
    except MassiveManifestError as exc:
        print(f"MANIFEST REFUSED: {exc}", file=sys.stderr)
        return 2

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
        manifest_bytes_sha = sha256_hex((args.capture_dir / CAPTURE_MANIFEST_FILENAME).read_bytes())
    except (ReproducibilityError, OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        print(f"REPRODUCIBILITY REFUSED: {exc}", file=sys.stderr)
        return 4

    # 4. Spot proxy (absent -> no closes; present but undecodable -> exit 2).
    try:
        spot_proxy = load_spot(args.capture_dir)
    except isc.StructuralCoverageError as exc:
        print(f"SPOT PROXY REFUSED: {exc}", file=sys.stderr)
        return 2

    # 5. Reconciliation over every universe pair + masters re-parse, then
    # the round-3 semantic join: a master that disagrees with the entry
    # that selected it downgrades that pair and blocks exit 0 below.
    reconciled = reconcile_pairs(universe, manifest, args.capture_dir, spot_proxy, calendar)
    masters = observe_masters(manifest, args.capture_dir)
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
    body = render_json(census)
    out_dir.mkdir(parents=True)
    (out_dir / "census.json").write_text(body, encoding="utf-8")
    (out_dir / "census.md").write_text(render_markdown(census), encoding="utf-8")
    (out_dir / "census.json.sha256").write_text(
        sha256_hex(body.encode("utf-8")) + "\n", encoding="utf-8"
    )

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
