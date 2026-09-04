#!/usr/bin/env python
"""ATM-grid bars-era launcher: read-only preflight + doubly-gated execute (PR A4).

CONTRACT: this tool cannot START an era by itself. The default mode is
``--preflight`` (omitting the flag runs preflight), which is READ-ONLY — it
creates nothing, writes nothing, and journals nothing. The ``--execute``
mode is doubly gated: the loaded protocol must be exactly the REQUIRED
version (0.2.2 — the live protocol since the 2026-08-28 flip; the gate was
re-opened at 0.2.2 on 2026-09-04 for the window-A-extension continuation,
whose new approval record binds the 0.2.2 hash) AND a BARS_LAUNCH_APPROVAL
authority record must bind the CURRENT loaded protocol hash (the standing
2026-08-26 record binds the 0.2.1-era hash, so on main today the RECORD
clause is the refusal — exit 2 is the DOCUMENTED correct answer until the
owner appends the continuation approval). The CLI execute path additionally
refuses outright: the runner is a FUNCTION PARAMETER of the library seam
only (never a flag — authority must not be consumable from a CLI argument),
so the CLI refuses before touching anything. Zero network in any code path
here.

Preflight checks (first failure wins; every check is read-only):

1. protocol gate — loaded protocol version == 0.2.2 AND a
   BARS_LAUNCH_APPROVAL record exists whose protocol_hash equals the CURRENT
   loaded protocol hash. Without a record this check cannot pass. On main
   today no record binds the 0.2.2 hash, so exit 2 is the DOCUMENTED
   correct answer.
2. census currency — the census re-hashes, passes its fail-closed
   verification, and still describes the capture manifest bytes on disk now
   (``provenance.input_manifest_sha256`` staleness double-check, same as A3).
3. work manifest — present, content-hash bound, canonically ordered, bound
   to the COMMITTED selection profile's hash and to the current capture
   manifest bytes, with the cost estimate restated through the imported
   bridge ``Budget``.
4. vendor key — presence + file mode ONLY (the capture script's key-path
   resolution pattern: env var first, then the key file; the file is never
   read and never echoed here — a stat, not a load).
5. run state — the run store must exist with state BARS_READY; an existing
   lease (HELD by a live owner = duplicate launch) refuses.
6. refuse-fallback — ``--vendor-host``, ``--endpoint-template``,
   ``--calendar-token``, ``--universe`` and ``--selection-rule`` overrides
   are REFUSED outright: the pinned constants (the committed universe's 29
   names, the calendar token, the selection-rule token, the vendor host and
   endpoint templates as imported constant strings) are the only accepted
   values and there is no code path that substitutes a fallback. A differing
   value exits 4 naming the pinned value (these are not secrets).

Execute (library seam ``run_execute(..., runner=...)`` only): the census is
re-verified AT EXECUTE TIME and cross-joined against the approval record
and the run-state store's identity — the approval's census_sha256 must
equal the verified census's content hash, and the store's code_sha /
universe_manifest_sha256 must equal the census provenance's (CensusProvenance
carries both; no git subprocess needed) — then the authority consumption
record is appended durably, the journal transitions BARS_READY ->
BARS_CAPTURING, and the lease is acquired — all BEFORE the runner is
invoked. A duplicate execution (the work manifest was already consumed)
refuses; a crash after consumption is RECONCILIATION_REQUIRED, never a retry.

Exit codes (contract):
  0  preflight: every gate passed (nothing started); execute: consumed + run
  1  unexpected error
  2  protocol gate: not 0.2.2, or no BARS_LAUNCH_APPROVAL record binds the
     current protocol hash (the record clause is the refusal on main today)
  3  census gate: census invalid, unhashable, or stale vs the capture manifest
  4  refuse-fallback: an override flag differs from its pinned constant
  5  run-state gate: store missing/unreadable, state != BARS_READY, or an
     existing lease (HELD = duplicate launch)
  6  execute: authority gates absent or mismatched (no approval record binds
     this protocol hash + work manifest, or the amendment packet hash
     differs), or the execute-time census verification / identity cross-join
     fails (census deleted, corrupt, or stale at execute time; approval
     names a different census; the store's code_sha / universe hash differ
     from the census provenance)
  7  execute: duplicate — this work manifest was already consumed
  8  work-manifest gate: missing, unbound, profile mismatch, or cost mismatch
  9  vendor-key gate: key file missing or group/world readable
 10  CLI --execute refused: no runner is wired on the CLI (nothing touched)

Usage:
  uv run --frozen python scripts/launch_bars_era.py \\
      --census artifacts/census/m4-coverage-census.json \\
      --capture-manifest artifacts/capture/m4b-manifest.json \\
      --work-manifest artifacts/bars/work-manifest.json --run-id <run-id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
for _entry in (REPO_ROOT / "src", REPO_ROOT / "scripts"):
    if str(_entry) not in sys.path:  # pragma: no cover - import plumbing
        sys.path.insert(0, str(_entry))

from tree_options.data.bars_manifest import (  # noqa: E402
    DEFAULT_BARS_AUTHORITY_ROOT,
    DEFAULT_SELECTION_PROFILE_PATH,
    KIND_BARS_LAUNCH_APPROVAL,
    KIND_BARS_LAUNCH_CONSUMED,
    BarsAuthorityRecord,
    BarsLedgerView,
    BarsManifestError,
    BarsWorkManifest,
    SecondExecutionRefusedError,
    append_bars_launch_consumed,
    load_selection_profile,
    parse_bars_work_manifest,
    read_bars_ledger,
    verify_bars_work_manifest,
)
from tree_options.data.coverage_census import (  # noqa: E402
    CoverageCensus,
    CoverageUniverse,
    verify_census,
    verify_universe,
)
from tree_options.data.massive_client import (  # noqa: E402
    API_KEY_ENV_VAR,
    DEFAULT_KEY_PATH,
    MASSIVE_BASE_URL,
)
from tree_options.data.massive_options import AGGS_PATH_TEMPLATE, CONTRACTS_PATH  # noqa: E402
from tree_options.protocol.loader import load_protocol, protocol_hash  # noqa: E402
from tree_options.protocol.schema import ResearchProtocol  # noqa: E402
from tree_options.runstate import RunState, RunStore  # noqa: E402
from tree_options.runstate import errors as rs_errors  # noqa: E402
from tree_options.runstate import lease as lease_module  # noqa: E402
from tree_options.runstate.store import DEFAULT_STORE_ROOT  # noqa: E402
from tree_options.schemas.common import StrictModel  # noqa: E402
from tree_options.seal.errors import SealError  # noqa: E402

# (window-A extension continuation, 2026-09-04) re-opened at the LIVE
# protocol version: the 0.2.1 freeze was the closed-era state ("the bars
# era is closed; the launcher is frozen" — the flip note in the tests).
# The continuation capture (Fridays 2026-08-28 onward, growing the world
# so the five window-A-excluded sealed dates become label-complete) runs
# under a NEW BARS_LAUNCH_APPROVAL record that binds the 0.2.2 protocol
# hash (owner-ratified in the 0.2.2 flip); the standing 0.2.1 approval
# can no longer open this gate — its protocol hash is not the loaded one.
REQUIRED_BARS_PROTOCOL_VERSION = "0.2.2"
COMMITTED_UNIVERSE_PATH = REPO_ROOT / "data" / "coverage" / "coverage_universe.json"

# ---- pinned constants: the ONLY accepted values (no fallback path exists) ------

PINNED_VENDOR_HOST = MASSIVE_BASE_URL.removeprefix("https://")
PINNED_ENDPOINT_TEMPLATES: dict[str, str] = {
    "contracts": CONTRACTS_PATH,
    "aggs": AGGS_PATH_TEMPLATE,
}
PINNED_CALENDAR_TOKEN = "nyse_sessions_2018_01_02_2026_12_31"
PINNED_SELECTION_RULE = "atm-grid"


@lru_cache(maxsize=1)
def _pinned_underlyings() -> tuple[str, ...]:
    """The committed coverage universe's underlyings, verified on first use.

    The launcher REFUSES any universe override: this committed manifest's 29
    names are the only accepted ``--universe`` value, so they are read from
    the committed artifact (never hardcoded a second time here) and verified
    through the universe's own fail-closed checks.
    """
    try:
        universe = CoverageUniverse.model_validate(
            json.loads(COMMITTED_UNIVERSE_PATH.read_text(encoding="utf-8"))
        )
        verify_universe(universe)
    except (OSError, ValueError) as exc:
        raise BarsManifestError(f"committed coverage universe unusable: {exc}") from None
    return universe.underlyings


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- preflight report -------------------------------------------------------------


class CheckStatus(StrictModel):
    ok: bool
    detail: str  # "" when ok; the refusal reason otherwise
    evidence: str = ""  # what was observed (hash, mode, state …)


class PreflightReport(StrictModel):
    mode: Literal["preflight"]
    run_id: str
    checks: dict[str, CheckStatus]


# ---- gate helpers -----------------------------------------------------------------


def _approvals_binding(
    view: BarsLedgerView, current_protocol_hash: str
) -> list[BarsAuthorityRecord]:
    """EVERY approval record binding the CURRENT protocol hash, in ledger order.

    Round-4 review fix (2026-08-23, finding 6): a ledger may legitimately
    carry APPROVAL(P, M1) and APPROVAL(P, M2) for the same protocol — the
    authority join in run_execute must narrow these candidates by every
    field the site compares, never trust one pre-selected record."""
    return [
        record
        for record in view.records
        if record.kind == KIND_BARS_LAUNCH_APPROVAL
        and record.protocol_hash == current_protocol_hash
    ]


def _matching_approval(
    view: BarsLedgerView, current_protocol_hash: str
) -> BarsAuthorityRecord | None:
    """The FIRST approval record binding the CURRENT protocol hash, if any.

    Protocol-gate use only: ANY such record opens the gate. The execute-side
    authority joins use _approvals_binding and narrow by every compared
    field (round-4 review fix, finding 6) — see run_execute."""
    approvals = _approvals_binding(view, current_protocol_hash)
    return approvals[0] if approvals else None


def _protocol_gate_failure(protocol: ResearchProtocol, view: BarsLedgerView) -> str | None:
    """None when the protocol gate holds; the refusal reason otherwise."""
    approval = _matching_approval(view, protocol_hash(protocol))
    if protocol.meta.protocol_version != REQUIRED_BARS_PROTOCOL_VERSION or approval is None:
        if protocol.meta.protocol_version != REQUIRED_BARS_PROTOCOL_VERSION:
            return (
                f"protocol version {protocol.meta.protocol_version!r} !="
                f" {REQUIRED_BARS_PROTOCOL_VERSION!r}"
            )
        return "no BARS_LAUNCH_APPROVAL record binds the loaded protocol hash"
    return None


def _protocol_check(args: argparse.Namespace) -> CheckStatus:
    try:
        protocol = load_protocol(args.protocol)
    except Exception as exc:
        return CheckStatus(ok=False, detail=f"protocol does not load: {exc}")
    try:
        view = read_bars_ledger(args.authority_root)
    except SealError as exc:
        return CheckStatus(ok=False, detail=f"authority ledger unreadable: {exc}")
    reason = _protocol_gate_failure(protocol, view)
    if reason is not None:
        return CheckStatus(
            ok=False, detail=reason, evidence=f"protocol_hash {protocol_hash(protocol)[:12]}…"
        )
    return CheckStatus(
        ok=True,
        detail="",
        evidence=(
            f"protocol {REQUIRED_BARS_PROTOCOL_VERSION} hash"
            f" {protocol_hash(protocol)[:12]}… bound by a BARS_LAUNCH_APPROVAL record"
        ),
    )


def _load_verified_census(
    args: argparse.Namespace,
) -> tuple[CoverageCensus | None, str | None]:
    """Parse + fail-closed-verify + staleness-check the census against the
    capture manifest bytes on disk now. Returns (census, None) on success or
    (None, refusal-reason) otherwise.

    Round-2 review fix (2026-08-23, finding 5, probe
    /tmp/pr-a-bars-execute-binding-probe.log): shared by preflight AND
    execute — the census is re-verified AT EXECUTE TIME, so a deleted,
    corrupt, or stale census refuses the launch before any authority is
    consumed (previously the only census read happened in preflight)."""
    census_path = Path(args.census)
    try:
        census = CoverageCensus.model_validate_json(census_path.read_text(encoding="utf-8"))
        verify_census(census)
    except (OSError, ValueError) as exc:
        return None, f"census invalid or tampered: {exc}"
    try:
        manifest_sha = _sha256_file(Path(args.capture_manifest))
    except OSError as exc:
        return None, f"capture manifest unreadable: {exc}"
    if census.provenance.input_manifest_sha256 != manifest_sha:
        return None, (
            "capture manifest drifted since the census: census bound"
            f" {census.provenance.input_manifest_sha256[:12]}…, on disk now"
            f" {manifest_sha[:12]}…"
        )
    return census, None


def _census_check(args: argparse.Namespace) -> CheckStatus:
    census, failure = _load_verified_census(args)
    if failure is not None or census is None:
        return CheckStatus(ok=False, detail=failure or "census unusable")
    return CheckStatus(
        ok=True,
        detail="",
        evidence=(
            f"census {census.content_sha256[:12]}… vs manifest"
            f" {census.provenance.input_manifest_sha256[:12]}…"
        ),
    )


def _work_manifest_check(
    args: argparse.Namespace,
) -> tuple[CheckStatus, BarsWorkManifest | None, str | None]:
    """(status, the VERIFIED work manifest or None, sha256 of the VERIFIED
    file bytes or None on failure).

    Round-3 review fix (2026-08-23, finding 2): the work-manifest bytes are
    read ONCE — these bytes are parsed, verified, hashed, and (by the
    execute path) consumed. The check previously re-read the path for its
    hash after verifying, so a swap between the two reads verified manifest
    A while the approval bound manifest B's hash, and execute's later
    re-parse consumed B without full verification. Same bytes-once
    discipline the round-3 amendment fix applied to the packet."""
    path = Path(args.work_manifest)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return (
            CheckStatus(ok=False, detail=f"work manifest refused: {path}: {exc.strerror}"),
            None,
            None,
        )
    try:
        manifest = parse_bars_work_manifest(raw, source=str(path))
        profile = load_selection_profile(Path(args.selection_profile))
        capture_manifest_sha = _sha256_file(Path(args.capture_manifest))
        # Round-1 review fix: verify_bars_work_manifest requires capture_dir
        # to regenerate the entries against the sealed capture (self-hash
        # alone is not a proof of provenance).
        capture_dir = args.capture_dir or Path(args.capture_manifest).parent
        verify_bars_work_manifest(
            manifest,
            profile=profile,
            capture_manifest_sha256=capture_manifest_sha,
            capture_dir=capture_dir,
        )
    except (OSError, BarsManifestError) as exc:
        return CheckStatus(ok=False, detail=f"work manifest refused: {exc}"), None, None
    evidence = (
        f"manifest {manifest.content_sha256[:12]}…,"
        f" profile {manifest.profile_sha256[:12]}…,"
        f" {manifest.cost.expected_requests} requests,"
        f" worst case {manifest.cost.worst_case_wire_requests} wire requests,"
        f" budget covers: {manifest.cost.budget_covers_worst_case}"
    )
    if not manifest.cost.budget_covers_worst_case:
        return (
            CheckStatus(
                ok=False,
                detail=(
                    f"declared budget {manifest.cost.budget_limit} cannot pre-charge the"
                    f" worst case {manifest.cost.worst_case_wire_requests} (Budget"
                    " charge_block refuses)"
                ),
                evidence=evidence,
            ),
            None,
            None,
        )
    return (
        CheckStatus(ok=True, detail="", evidence=evidence),
        manifest,
        hashlib.sha256(raw).hexdigest(),
    )


def _vendor_key_check(args: argparse.Namespace) -> CheckStatus:
    """Presence + mode ONLY: the key path is resolved the way the capture
    script resolves it (env var first, then the key file) — but the file is
    never read and its contents never echoed; a stat is the whole check."""
    if os.environ.get(API_KEY_ENV_VAR, "").strip():
        return CheckStatus(
            ok=True, detail="", evidence=f"{API_KEY_ENV_VAR} present in the environment"
        )
    key_path = Path(args.vendor_key) if args.vendor_key else DEFAULT_KEY_PATH
    if not key_path.is_file():
        return CheckStatus(
            ok=False,
            detail=f"no API key: {API_KEY_ENV_VAR} unset and {key_path} is missing",
        )
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & 0o077:
        return CheckStatus(
            ok=False,
            detail=f"{key_path}: key file mode {mode:04o} is group/world readable — chmod 600 it",
        )
    return CheckStatus(ok=True, detail="", evidence=f"{key_path} mode {mode:04o}")


def _open_store(args: argparse.Namespace) -> tuple[RunStore | None, str | None]:
    try:
        return RunStore.open(Path(args.store_root), args.run_id), None
    except rs_errors.UnknownRunError as exc:
        return None, f"unknown run: {exc}"
    except rs_errors.RunStateError as exc:
        return None, f"run store unreadable: {exc}"


def _lease_refusal(store_dir: Path, *, boot_id_now: str, proc_root: Path | None) -> str | None:
    """Any pre-existing lease refuses: HELD by a live owner is a duplicate
    launch; a stale/torn lease is an operator reconciliation, never a
    silent adoption by this launcher."""
    try:
        if lease_module.owner_exists(store_dir):
            classification = lease_module.classify_existing(
                store_dir, boot_id_now=boot_id_now, proc_root=proc_root
            )
            if classification is lease_module.LeaseClassification.HELD:
                return "duplicate launch: the run's lease is HELD by a live owner"
            return f"lease present ({classification.value}) — reconcile via era_status first"
    except rs_errors.RunStateError as exc:
        return f"lease custody refused: {exc}"
    return None


def _run_state_check(
    args: argparse.Namespace, *, boot_id_now: str, proc_root: Path | None
) -> CheckStatus:
    store, failure = _open_store(args)
    if store is None:
        return CheckStatus(ok=False, detail=failure or "run store unreadable")
    state = store.state
    if state is not RunState.BARS_READY:
        return CheckStatus(
            ok=False, detail=f"run state {state.value if state else None} != BARS_READY"
        )
    lease_failure = _lease_refusal(store.dir, boot_id_now=boot_id_now, proc_root=proc_root)
    if lease_failure is not None:
        return CheckStatus(ok=False, detail=lease_failure)
    tail = store.status(now_epoch=0, boot_id_now=boot_id_now, proc_root=proc_root).tail_hash
    return CheckStatus(
        ok=True,
        detail="",
        evidence=f"run {args.run_id} BARS_READY, journal tail {tail[:12]}…",
    )


def _override_refusal(args: argparse.Namespace) -> str | None:
    """None when no override differs from its pinned constant; the message
    otherwise. There is no fallback: a differing override is refused, never
    substituted. The pinned values are not secrets — they are printed."""
    pinned_universe = ",".join(_pinned_underlyings())
    checks: list[tuple[str, str | None, str, str]] = [
        ("--vendor-host", args.vendor_host, PINNED_VENDOR_HOST, "vendor host"),
        (
            "--calendar-token",
            args.calendar_token,
            PINNED_CALENDAR_TOKEN,
            "calendar token",
        ),
        (
            "--selection-rule",
            args.selection_rule,
            PINNED_SELECTION_RULE,
            "selection rule",
        ),
        ("--universe", args.universe, pinned_universe, "underlying universe (29 names)"),
    ]
    for flag, provided, pinned, what in checks:
        if provided is not None and provided != pinned:
            return (
                f"{flag} override refused: the pinned {what} is the only accepted"
                f" value (sha256 {hashlib.sha256(pinned.encode('utf-8')).hexdigest()[:12]}…,"
                f" value {pinned!r}); there is no fallback"
            )
    for template in args.endpoint_template or ():
        if template not in PINNED_ENDPOINT_TEMPLATES.values():
            pinned_values = ", ".join(
                f"{name}={value!r}" for name, value in sorted(PINNED_ENDPOINT_TEMPLATES.items())
            )
            return (
                f"--endpoint-template override {template!r} refused: the pinned endpoint"
                f" templates are the only accepted values ({pinned_values}); there is"
                " no fallback"
            )
    return None


# ---- preflight (read-only) ----------------------------------------------------------


def run_preflight(
    args: argparse.Namespace,
    *,
    boot_id_now: str,
    proc_root: Path | None = None,
) -> int:
    """Every gate, first failure wins, zero mutation of any state."""
    checks: dict[str, CheckStatus] = {}
    checks["protocol_gate"] = _protocol_check(args)
    if not checks["protocol_gate"].ok:
        return _report(checks, args, failing="protocol_gate", exit_code=2)
    checks["census_currency"] = _census_check(args)
    if not checks["census_currency"].ok:
        return _report(checks, args, failing="census_currency", exit_code=3)
    work_status, _, _ = _work_manifest_check(args)
    checks["work_manifest"] = work_status
    if not work_status.ok:
        return _report(checks, args, failing="work_manifest", exit_code=8)
    checks["vendor_key"] = _vendor_key_check(args)
    if not checks["vendor_key"].ok:
        return _report(checks, args, failing="vendor_key", exit_code=9)
    checks["run_state"] = _run_state_check(args, boot_id_now=boot_id_now, proc_root=proc_root)
    if not checks["run_state"].ok:
        return _report(checks, args, failing="run_state", exit_code=5)
    override_failure = _override_refusal(args)
    if override_failure is not None:
        checks["refuse_fallback"] = CheckStatus(ok=False, detail=override_failure)
        return _report(checks, args, failing="refuse_fallback", exit_code=4)
    checks["refuse_fallback"] = CheckStatus(
        ok=True, detail="", evidence="no override flags supplied or all equal the pinned constants"
    )
    report = PreflightReport(mode="preflight", run_id=args.run_id, checks=checks)
    print(json.dumps(json.loads(report.model_dump_json()), indent=2, sort_keys=True))
    return 0


def _report(
    checks: dict[str, CheckStatus], args: argparse.Namespace, *, failing: str, exit_code: int
) -> int:
    report = PreflightReport(mode="preflight", run_id=args.run_id, checks=checks)
    print(json.dumps(json.loads(report.model_dump_json()), indent=2, sort_keys=True))
    failed = checks[failing]
    print(f"PREFLIGHT REFUSED ({failing}): {failed.detail}", file=sys.stderr)
    return exit_code


# ---- execute (library seam only; the CLI refuses) -----------------------------------


class BarsExecuteContext(StrictModel):
    """What the runner is handed: the identity of the consumed launch."""

    run_id: str
    work_manifest_sha256: str
    census_sha256: str
    at_epoch: int


Runner = Callable[[BarsExecuteContext], str]


class BarsExecuteSummary(StrictModel):
    """Terminal summary — returned only on the execute path, only after the
    runner completed. There is no summary without a consumption, and no
    second consumption for the same work manifest, ever."""

    run_id: str
    state: str
    work_manifest_sha256: str
    census_sha256: str
    consumption_record_sha256: str
    runner_outcome: str
    at_epoch: int


def run_execute(
    args: argparse.Namespace,
    *,
    runner: Runner,
    now_epoch: int,
    boot_id_now: str,
    proc_root: Path | None = None,
) -> tuple[int, BarsExecuteSummary | None]:
    """The doubly gated launch. NEVER wired to the CLI (the runner is a
    function parameter — authority must not be consumable from a flag).

    Order is load-bearing: run-state gate, then authority gates, then the
    duplicate check, then — BEFORE the runner — lease acquisition, the
    BARS_LAUNCH_CONSUMED record, and the BARS_READY -> BARS_CAPTURING
    journal transition. A crash after the append and before (or during) the
    runner leaves durable consumption: RECONCILIATION_REQUIRED, never a retry.
    """
    store, failure = _open_store(args)
    if store is None:
        print(f"EXECUTE REFUSED (run_state): {failure}", file=sys.stderr)
        return 5, None
    state = store.state
    if state is not RunState.BARS_READY:
        print(
            f"EXECUTE REFUSED (run_state): run state {state.value if state else None}"
            " != BARS_READY",
            file=sys.stderr,
        )
        return 5, None
    lease_failure = _lease_refusal(store.dir, boot_id_now=boot_id_now, proc_root=proc_root)
    if lease_failure is not None:
        print(f"EXECUTE REFUSED (run_state): {lease_failure}", file=sys.stderr)
        return 5, None

    # authority gate 1: the current protocol + its approval record
    try:
        protocol = load_protocol(args.protocol)
        view = read_bars_ledger(args.authority_root)
    except (OSError, ValueError, SealError) as exc:
        print(f"EXECUTE REFUSED (authority): {exc}", file=sys.stderr)
        return 6, None
    current_hash = protocol_hash(protocol)
    reason = _protocol_gate_failure(protocol, view)
    # Round-4 review fix (2026-08-23, finding 6): the protocol gate opens on
    # ANY approval binding the protocol hash, but the execute-side authority
    # join narrows EVERY such record by the fields this site compares — the
    # first protocol-matching record used to shadow a later approval of a
    # DIFFERENT work manifest (APPROVAL(P, M1) then APPROVAL(P, M2): M2's
    # exact inputs refused exit 6 although APPROVAL(P, M2) grants them).
    approvals = _approvals_binding(view, current_hash)
    work_status, work_manifest, work_manifest_sha = _work_manifest_check(args)
    if reason is not None or not work_status.ok:
        detail = reason if reason is not None else work_status.detail
        print(f"EXECUTE REFUSED (authority): {detail}", file=sys.stderr)
        return 6, None
    # the work-manifest check succeeded: BOTH the verified model and the
    # hash of the verified bytes are bound here (round-3 finding 2).
    assert work_manifest is not None
    assert work_manifest_sha is not None
    approvals = [r for r in approvals if r.work_manifest_sha256 == work_manifest_sha]
    if not approvals:
        print(
            "EXECUTE REFUSED (authority): no BARS_LAUNCH_APPROVAL record binds"
            f" protocol hash {current_hash[:12]}… AND work manifest"
            f" {(work_manifest_sha or '')[:12]}…",
            file=sys.stderr,
        )
        return 6, None
    if args.amendment_packet is not None:
        try:
            packet_sha = _sha256_file(Path(args.amendment_packet))
        except OSError as exc:
            print(
                f"EXECUTE REFUSED (authority): amendment packet unreadable: {exc}", file=sys.stderr
            )
            return 6, None
        packet_bound = [r for r in approvals if r.amendment_packet_sha256 == packet_sha]
        if not packet_bound:
            print(
                f"EXECUTE REFUSED (authority): amendment packet hash {packet_sha[:12]}… !="
                f" the approved record's {approvals[0].amendment_packet_sha256[:12]}…",
                file=sys.stderr,
            )
            return 6, None
        approvals = packet_bound

    # Round-1 review fix (2026-08-23, probe FORGED_CONSUMPTION_REPLAYED for
    # bars-execute analogous): the runstate store's identity MUST agree with
    # the protocol and the work manifest's capture-manifest pin. Without
    # this cross-join, a happy-path store with placeholder hashes lets
    # execute consume authority against an unrelated run. Bindings:
    #   - store.identity.protocol_hash == current_hash
    #   - store.pinned_manifest_sha256 == work_manifest.capture_manifest_sha256
    #     (the capture manifest hash is bound into the work manifest; the
    #     approval record carries the work-manifest hash, not the capture
    #     manifest hash directly.)
    store_identity = store.identity
    if store_identity.protocol_hash != current_hash:
        print(
            "EXECUTE REFUSED (identity): run-state store protocol hash"
            f" {store_identity.protocol_hash[:12]}… does not match the"
            f" approved protocol hash {current_hash[:12]}…",
            file=sys.stderr,
        )
        return 6, None
    # The work manifest (verified above, from the ONE bytes read — round-3
    # finding 2) carries the capture-manifest hash; compare it to the store's
    # pinned manifest. The join consumes the VERIFIED model, never a re-parse
    # of the path (a swap after verification used to flow unverified bytes
    # into exactly these identity joins).
    pinned = store.pinned_manifest_sha256
    expected_capture_manifest = _sha256_file(Path(args.capture_manifest))
    if work_manifest.capture_manifest_sha256 != expected_capture_manifest:
        print(
            "EXECUTE REFUSED (identity): work manifest's capture_manifest_sha256"
            f" {work_manifest.capture_manifest_sha256[:12]}… does not match"
            f" the --capture-manifest hash {expected_capture_manifest[:12]}…",
            file=sys.stderr,
        )
        return 6, None
    if pinned != work_manifest.capture_manifest_sha256:
        print(
            "EXECUTE REFUSED (identity): run-state store pinned capture manifest"
            f" {pinned[:12] if pinned else None}… does not match the work"
            " manifest's capture-manifest pin"
            f" {work_manifest.capture_manifest_sha256[:12]}…",
            file=sys.stderr,
        )
        return 6, None

    # Round-2 review fix (2026-08-23, finding 5, probe
    # /tmp/pr-a-bars-execute-binding-probe.log): the census is re-verified AT
    # EXECUTE TIME and cross-joined against the approval record and the
    # run-state store's identity. Previously only the protocol hash and the
    # capture-manifest pin were joined, so a BARS_READY store with placeholder
    # code_sha/universe hashes and a DELETED census file still consumed
    # authority, transitioned, and invoked the runner. Every refusal below
    # fires BEFORE any lease, consumption, transition, or runner invocation.
    census, census_failure = _load_verified_census(args)
    if census is None or census_failure is not None:
        print(f"EXECUTE REFUSED (census): {census_failure}", file=sys.stderr)
        return 6, None
    assert census is not None  # narrowed for the joins below
    # Round-4 review fix (finding 6), continued: the census join narrows the
    # remaining approval candidates the same way — refuse only when NO record
    # binding the earlier joins names this census.
    census_bound = [r for r in approvals if r.census_sha256 == census.content_sha256]
    if not census_bound:
        print(
            "EXECUTE REFUSED (identity): the approval record names census"
            f" {approvals[0].census_sha256[:12]}… but the verified census on disk"
            f" is {census.content_sha256[:12]}… — the approval must name THIS"
            " census",
            file=sys.stderr,
        )
        return 6, None
    approvals = census_bound
    approval = approvals[0]
    if store_identity.code_sha != census.provenance.code_sha:
        print(
            "EXECUTE REFUSED (identity): run-state store code_sha"
            f" {store_identity.code_sha[:12]}… does not match the census"
            f" provenance code_sha {census.provenance.code_sha[:12]}…"
            " (different code produced the census)",
            file=sys.stderr,
        )
        return 6, None
    if store_identity.universe_manifest_sha256 != census.provenance.universe_manifest_sha256:
        print(
            "EXECUTE REFUSED (identity): run-state store"
            " universe_manifest_sha256"
            f" {store_identity.universe_manifest_sha256[:12]}… does not match"
            " the census provenance universe_manifest_sha256"
            f" {census.provenance.universe_manifest_sha256[:12]}…"
            " (a different universe fed the census)",
            file=sys.stderr,
        )
        return 6, None

    # duplicate: the one-shot rule for this work manifest. This scan is the
    # fast path (a crash-after-consumption re-execute refuses here, before
    # the lease); the AUTHORITATIVE check is inside the locked append below
    # (round-3 review fix, finding 1) — the scan alone is not atomic across
    # run stores, because it runs before the store-specific lease.
    for record in view.records:
        if (
            record.kind == KIND_BARS_LAUNCH_CONSUMED
            and record.work_manifest_sha256 == work_manifest_sha
        ):
            print(
                "EXECUTE REFUSED (duplicate): this work manifest was already"
                f" consumed (record {record.record_sha256[:12]}…)",
                file=sys.stderr,
            )
            return 7, None

    # BEFORE the runner: lease, consumption record, journal transition
    owner = lease_module.current_owner(now_epoch=now_epoch, proc_root=proc_root)
    if args.boot_id_override:
        owner = owner.model_copy(update={"boot_id": args.boot_id_override})
    try:
        lease_module.acquire(
            store.dir,
            owner,
            boot_id_now=boot_id_now,
            proc_root=proc_root,
            allow_stale_adopt=False,
        )
    except rs_errors.LeaseHeldError as exc:
        print(f"EXECUTE REFUSED (run_state): {exc}", file=sys.stderr)
        return 5, None
    except rs_errors.StoreCustodyError as exc:
        print(f"EXECUTE REFUSED (run_state custody): {exc}", file=sys.stderr)
        return 5, None
    census_sha = approval.census_sha256
    try:
        consumed = append_bars_launch_consumed(
            Path(args.authority_root),
            protocol_hash=current_hash,
            amendment_packet_sha256=approval.amendment_packet_sha256,
            census_sha256=census_sha,
            work_manifest_sha256=work_manifest_sha,
            reason=args.reason or "ATM-grid bars era launch (A4 execute seam)",
            at_epoch=now_epoch,
        )
    except SecondExecutionRefusedError as exc:
        # Round-3 review fix (2026-08-23, finding 1): a second store's
        # launcher lost the cross-store race — its scan predated the first
        # appender's CONSUMPTION, which the locked append now sees. Refuse
        # with the duplicate exit code, release the lease this launcher
        # took, and never invoke the runner.
        lease_module.release(store.dir, owner)
        print(f"EXECUTE REFUSED (duplicate): {exc}", file=sys.stderr)
        return 7, None
    store.transition(
        RunState.BARS_CAPTURING,
        reason=args.reason or "ATM-grid bars era launch (A4 execute seam)",
        now_epoch=now_epoch,
        actor_pid=owner.pid,
        actor_boot_id=boot_id_now,
        owner=owner,
    )

    outcome = runner(
        BarsExecuteContext(
            run_id=args.run_id,
            work_manifest_sha256=work_manifest_sha,
            census_sha256=census_sha,
            at_epoch=now_epoch,
        )
    )
    summary = BarsExecuteSummary(
        run_id=args.run_id,
        state=RunState.BARS_CAPTURING.value,
        work_manifest_sha256=work_manifest_sha,
        census_sha256=census_sha,
        consumption_record_sha256=consumed.record_sha256,
        runner_outcome=outcome,
        at_epoch=now_epoch,
    )
    print(json.dumps(json.loads(summary.model_dump_json()), indent=2, sort_keys=True))
    return 0, summary


def cmd_execute(args: argparse.Namespace) -> int:
    # No runner is wired on the CLI (a --runner-inject seam is forbidden —
    # authority must never be consumable from a flag). Refuse BEFORE touching
    # anything: nothing is read, nothing consumed, nothing journaled.
    print(
        f"EXECUTE REFUSED: this build wires no bars-era runner (authority root"
        f" {args.authority_root} untouched); execute is library-only — call"
        " run_execute with an explicit runner callable",
        file=sys.stderr,
    )
    return 10


# ---- CLI plumbing --------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--preflight",
        action="store_true",
        help="read-only gate checks (the DEFAULT when no mode flag is given)",
    )
    modes.add_argument(
        "--execute",
        action="store_true",
        help="doubly-gated launch (refuses on the CLI: the runner is library-only)",
    )
    parser.add_argument("--run-id", required=True, help="run id under the store root")
    parser.add_argument(
        "--census", type=Path, required=True, help="coverage census JSON (content-hash verified)"
    )
    parser.add_argument(
        "--capture-manifest", type=Path, required=True, help="the capture manifest on disk now"
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        help="the capture directory (round-1 review: required for"
        " verify_bars_work_manifest regeneration; defaults to"
        " capture-manifest's parent)",
    )
    parser.add_argument(
        "--work-manifest", type=Path, required=True, help="the bars work manifest to launch"
    )
    parser.add_argument(
        "--protocol",
        type=Path,
        default=REPO_ROOT / "research_protocol.yaml",
        help="protocol YAML (default: research_protocol.yaml at the repo root)",
    )
    parser.add_argument(
        "--selection-profile",
        type=Path,
        default=REPO_ROOT / DEFAULT_SELECTION_PROFILE_PATH,
        help="committed selection profile (default: data/bars/selection-profile.json)",
    )
    parser.add_argument(
        "--vendor-key", type=Path, help="key file to stat (default: the capture lane's path)"
    )
    parser.add_argument(
        "--store-root",
        type=Path,
        default=DEFAULT_STORE_ROOT,
        help="run-state root (default: artifacts/runstate)",
    )
    parser.add_argument(
        "--authority-root",
        type=Path,
        default=DEFAULT_BARS_AUTHORITY_ROOT,
        help="bars-authority ledger root (default: artifacts/bars-authority)",
    )
    parser.add_argument(
        "--amendment-packet",
        type=Path,
        help="amendment packet JSON; when given, its bytes must hash to the"
        " approval record's amendment_packet_sha256",
    )
    parser.add_argument("--reason", help="operator-visible why (execute path)")
    # Refuse-fallback probes: every one of these is pinned; a differing value
    # exits 4. They exist so an operator can SEE the pinned value, not so a
    # fallback can be supplied.
    parser.add_argument(
        "--vendor-host",
        help=argparse.SUPPRESS,  # test seam: refuse-fallback probe
    )
    parser.add_argument(
        "--endpoint-template",
        action="append",
        help=argparse.SUPPRESS,  # test seam: refuse-fallback probe (repeatable)
    )
    parser.add_argument(
        "--calendar-token",
        help=argparse.SUPPRESS,  # test seam: refuse-fallback probe
    )
    parser.add_argument(
        "--universe",
        help=argparse.SUPPRESS,  # test seam: refuse-fallback probe
    )
    parser.add_argument(
        "--selection-rule",
        help=argparse.SUPPRESS,  # test seam: refuse-fallback probe
    )
    parser.add_argument("--boot-id-override", help=argparse.SUPPRESS)  # test seam: injected boot
    parser.add_argument("--now-epoch", type=int, help=argparse.SUPPRESS)  # test seam: clock
    parser.add_argument("--proc-root", type=Path, help=argparse.SUPPRESS)  # test seam: /proc
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.execute:
        return cmd_execute(args)
    boot_id_now = args.boot_id_override or lease_module.read_boot_id(args.proc_root)
    return run_preflight(args, boot_id_now=boot_id_now, proc_root=args.proc_root)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
