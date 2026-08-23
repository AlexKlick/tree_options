#!/usr/bin/env python
"""G4 sealed-event preflight + one-shot execute authority guard (PR A/A5).

The G4 sealed event (docs/m4-g4-sealed-gate-plan.md §3) is executable exactly
ONCE, ever, per sealed content. This script implements the machinery and
consumes NO authority in PR A:

* ``preflight`` verifies the AVAILABILITY of the six sealed-run inputs and is
  structurally incapable of computing or displaying a verdict: the output
  model pins ``verdict`` to ``Literal[None]`` and ``verdict_computed`` to
  ``Literal[False]``, so any leak attempt is a validation error, and no code
  path computes, infers, or prints one. No network, no broker, no run.
* ``execute`` implements the one-shot consumption but is never invoked
  outside tests in PR A: the CLI wires NO runner (a --runner-inject seam on
  the CLI is forbidden — authority must never be consumable from a flag), so
  the CLI execute path refuses before touching the ledger. Tests call
  ``execute_sealed_run`` directly with an injected runner callable.

Execute semantics (``execute_sealed_run``):

1. read the ledger — any CONSUMPTION record whose sealed_run_id OR
   content_identity matches this run refuses (exit 7). Two checkouts of the
   same research content share a content_identity, so a second consumption
   under EITHER id is refused.
2. an APPROVAL record must exist whose identity, RECOMPUTED from the record's
   own payload, yields this run's sealed_run_id (exit 6) — a record's stored
   ids alone are never trusted.
3. the CONSUMPTION record is appended durably (flock + fsync file + fsync
   dir) BEFORE the runner is invoked. A crash after consumption is a
   documented UNKNOWN / RECONCILIATION_REQUIRED state that is NEVER
   auto-rerun: a later identical execute hits step 1 and refuses.

Exit codes:
  0  preflight: all six sealed-run inputs available (no verdict computed)
  2  bare invocation / unknown subcommand; preflight: one or more inputs
     unavailable; execute reached without internal runner wiring (refused —
     nothing is read, nothing is consumed)
  3  ledger unreadable: root refused (resolved under /tmp) or hash chain
     corrupt
  6  APPROVAL_INVALID — no approval record recomputes to this run's identity
  7  SECOND_EXECUTION_REFUSED — this sealed content was already consumed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from tree_options.schemas.common import StrictModel

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.protocol.loader import load_protocol, protocol_hash  # noqa: E402
from tree_options.seal import ledger as seal_ledger  # noqa: E402
from tree_options.seal.errors import (  # noqa: E402
    ApprovalInvalidError,
    SealError,
    SecondExecutionRefusedError,
)
from tree_options.seal.identity import (  # noqa: E402
    CALENDAR_PENDING,
    SealedIdentity,
    content_identity,
    sealed_run_id,
)
from tree_options.seal.ledger import (  # noqa: E402
    DEFAULT_G4_LEDGER_ROOT,
    KIND_APPROVAL,
    KIND_CONSUMPTION,
    LedgerRecord,
    read_ledger,
)

CRITERIA_PATH = REPO_ROOT / "data" / "g4" / "sealed-criteria.json"

#: The git-check seam: a callable with subprocess.run's shape. Tests inject a
#: canned runner; production shells out to git in the invoking checkout.
GitRunner = Callable[..., subprocess.CompletedProcess[str]]

Runner = Callable[[SealedIdentity], str]


# --------------------------------------------------------------------------
# preflight: input AVAILABILITY only — no verdict is computed or displayed
# --------------------------------------------------------------------------


class CriterionStatus(StrictModel):
    available: bool
    reason: str  # "" when available; the refusal reason otherwise
    evidence: str  # what was observed (sha, token, …); "" when nothing was


class PreflightReport(StrictModel):
    """Availability-only report. The Literal pins are the verdict firewall:
    there is no value this model can carry that declares PASS or FAIL, so a
    mutated construction that tries becomes a validation error."""

    verdict: Literal[None] = None
    verdict_computed: Literal[False] = False
    criteria_inputs: dict[str, CriterionStatus]


def _porcelain_dirty(lines: list[str]) -> list[str]:
    """Tracked-tree dirtiness, ignoring UNTRACKED artifacts/ and dist/ (the
    gitignored runtime outputs — untracked output is not dirty research
    content; a modified TRACKED file is)."""
    dirty: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"') if len(line) > 3 else ""
        untracked = line.startswith("??")
        ignored_output = path in ("artifacts", "dist") or path.startswith(("artifacts/", "dist/"))
        if untracked and ignored_output:
            continue
        dirty.append(line)
    return dirty


def git_code_sha(repo: Path, *, runner: GitRunner = subprocess.run) -> tuple[str | None, str, str]:
    """(code_sha, evidence, reason) for a clean tracked tree.

    Available only when HEAD resolves AND the tracked tree is clean — a
    sealed run must be re-derivable from its declared commit.
    """

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return runner(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)

    head = _git("rev-parse", "HEAD")
    if head.returncode != 0:
        return None, "", f"git rev-parse HEAD failed: {head.stderr.strip()[:120]}"
    sha = head.stdout.strip()
    status = _git("status", "--porcelain")
    if status.returncode != 0:
        return None, sha, f"git status --porcelain failed: {status.stderr.strip()[:120]}"
    dirty = _porcelain_dirty(status.stdout.splitlines())
    if dirty:
        return None, sha, f"tracked tree dirty ({len(dirty)} path(s), first: {dirty[0][:60]})"
    return sha, sha, ""


def _available(evidence: str) -> CriterionStatus:
    return CriterionStatus(available=True, reason="", evidence=evidence)


def _unavailable(reason: str, evidence: str = "") -> CriterionStatus:
    return CriterionStatus(available=False, reason=reason, evidence=evidence)


def _file_sha(path: Path | None) -> tuple[str | None, str]:
    if path is None:
        return None, "no manifest path supplied"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest(), ""
    except OSError as exc:
        return None, f"manifest unreadable: {exc}"


def _criteria_status(expected_sha: str | None) -> CriterionStatus:
    if expected_sha is None:
        return _unavailable("no expected criteria_sha256 supplied")
    try:
        raw = CRITERIA_PATH.read_bytes()
    except OSError as exc:
        return _unavailable(f"sealed criteria file unreadable: {exc}")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha:
        return _unavailable(
            f"criteria sha mismatch: file hashes {actual[:12]}…, expected {expected_sha[:12]}…"
        )
    try:
        json.loads(raw)
    except ValueError as exc:
        return _unavailable(f"sealed criteria file is not valid JSON: {exc}")
    return _available(actual)


def _build_statuses(
    args: argparse.Namespace, *, git_runner: GitRunner
) -> dict[str, CriterionStatus]:
    statuses: dict[str, CriterionStatus] = {}

    sha, evidence, reason = git_code_sha(args.repo, runner=git_runner)
    if sha is None:
        statuses["code_sha"] = _unavailable(reason, evidence)
    else:
        statuses["code_sha"] = _available(evidence)

    try:
        statuses["protocol_hash"] = _available(protocol_hash(load_protocol()))
    except Exception as exc:
        statuses["protocol_hash"] = _unavailable(f"protocol load failed: {exc}")

    for key, manifest in (
        ("lane1_manifest_sha256", args.lane1_manifest),
        ("lane2_manifest_sha256", args.lane2_manifest),
    ):
        digest, why_missing = _file_sha(manifest)
        if digest is None:
            statuses[key] = _unavailable(why_missing)
        else:
            statuses[key] = _available(digest)

    if args.calendar_decision == CALENDAR_PENDING or not args.calendar_decision:
        statuses["calendar_decision"] = _unavailable(
            "calendar decision is PENDING (G4 plan §2.5: must be declared BEFORE the run)",
            args.calendar_decision,
        )
    else:
        statuses["calendar_decision"] = _available(args.calendar_decision)

    statuses["criteria"] = _criteria_status(args.criteria_sha256)
    return statuses


def cmd_preflight(argv: list[str], *, git_runner: GitRunner = subprocess.run) -> int:
    args = _parse(["preflight", *argv])
    try:
        read_ledger(args.ledger_root)  # availability of the authority surface
    except SealError as exc:  # refused root / corrupt chain: incident outranks
        print(f"LEDGER UNREADABLE: {exc}", file=sys.stderr)  # input availability
        return 3
    statuses = _build_statuses(args, git_runner=git_runner)
    report = PreflightReport(
        verdict=None,
        verdict_computed=False,
        criteria_inputs=statuses,
    )
    print(json.dumps(json.loads(report.model_dump_json()), indent=2, sort_keys=True))
    if all(status.available for status in statuses.values()):
        return 0
    return 2


# --------------------------------------------------------------------------
# execute: the one-shot consumption (library only in PR A; CLI refuses)
# --------------------------------------------------------------------------


class ExecuteSummary(StrictModel):
    """Terminal summary of a consumed seal — returned on the execute path
    only, after the runner completed. There is no summary without a
    consumption, and no second consumption ever."""

    sealed_run_id: str
    content_identity: str
    consumption_record_sha256: str
    consumed_at_epoch: int
    runner_outcome: str


def execute_sealed_run(
    identity: SealedIdentity,
    *,
    ledger_root: Path,
    reason: str,
    at_epoch: int,
    runner: Runner,
) -> ExecuteSummary:
    """Consume the one-shot seal authority for ``identity``.

    Refuses a second execution matching by EITHER id (step 1), requires an
    approval that RECOMPUTES to this run's sealed_run_id from the record's
    own payload (step 2), and appends the CONSUMPTION record durably BEFORE
    invoking ``runner`` (step 3). A crash after the append and before (or
    during) the runner leaves a durable CONSUMPTION with no run result:
    UNKNOWN / RECONCILIATION_REQUIRED — never auto-rerun (a later identical
    execute hits step 1 and refuses).
    """
    run_id = sealed_run_id(identity)
    content_id = content_identity(identity)
    view = read_ledger(ledger_root)

    for record in view.records:
        if record.kind != KIND_CONSUMPTION:
            continue
        if record.sealed_run_id == run_id or record.content_identity == content_id:
            raise SecondExecutionRefusedError(
                run_id, "a CONSUMPTION record already matches this sealed content"
            )

    approval_ok = any(
        record.kind == KIND_APPROVAL
        and record.sealed_run_id == run_id
        and sealed_run_id(record.identity) == run_id
        for record in view.records
    )
    if not approval_ok:
        raise ApprovalInvalidError(
            run_id,
            "no APPROVAL record recomputes to this sealed run id from its own payload",
        )

    consumption_record = LedgerRecord(
        kind=KIND_CONSUMPTION,
        identity=identity,
        sealed_run_id=run_id,
        content_identity=content_id,
        reason=reason,
        at_epoch=at_epoch,
        prev_record_sha256=view.tail_hash,
    )
    consumption_sha = seal_ledger.append_record(ledger_root, consumption_record)
    outcome = runner(identity)
    return ExecuteSummary(
        sealed_run_id=run_id,
        content_identity=content_id,
        consumption_record_sha256=consumption_sha,
        consumed_at_epoch=at_epoch,
        runner_outcome=outcome,
    )


def cmd_execute(argv: list[str]) -> int:
    args = _parse(["execute", *argv])
    # PR A wires NO runner: the CLI exposes no runner option (a --runner-inject
    # seam here is forbidden — authority must never be consumable from a flag).
    # Refuse BEFORE touching the ledger: nothing is read, nothing consumed.
    print(
        f"EXECUTE REFUSED: this build wires no sealed-run runner (ledger root "
        f"{args.ledger_root} untouched); execute is library-only in PR A — call "
        "execute_sealed_run with an explicit runner callable",
        file=sys.stderr,
    )
    return 2


# --------------------------------------------------------------------------
# CLI plumbing
# --------------------------------------------------------------------------


def _add_preflight_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", type=Path, default=REPO_ROOT, help="git repo for the code_sha check")
    p.add_argument("--lane1-manifest", type=Path, help="lane 1 (Cboe) capture manifest file")
    p.add_argument(
        "--lane2-manifest", type=Path, help="lane 2 (massive-derived era) capture manifest file"
    )
    p.add_argument(
        "--calendar-decision",
        default=CALENDAR_PENDING,
        help="holiday-calendar decision token (PENDING means undecided => unavailable)",
    )
    p.add_argument(
        "--criteria-sha256",
        help="expected sha256 of data/g4/sealed-criteria.json (the SealedIdentity input)",
    )
    p.add_argument(
        "--ledger-root",
        type=Path,
        default=DEFAULT_G4_LEDGER_ROOT,
        help="authority ledger root (default: artifacts/g4-authority)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", metavar="{preflight,execute}")
    pre = sub.add_parser(
        "preflight",
        help="verify AVAILABILITY of the six sealed-run inputs; no verdict is computed or displayed",
    )
    _add_preflight_args(pre)
    exe = sub.add_parser(
        "execute",
        help="consume the one-shot authority (requires internal runner wiring; the CLI refuses)",
    )
    exe.add_argument(
        "--ledger-root",
        type=Path,
        default=DEFAULT_G4_LEDGER_ROOT,
        help="authority ledger root (default: artifacts/g4-authority)",
    )
    exe.add_argument("--reason", default="G4 sealed event", help="operator-visible why")
    return parser


def _parse(argv: list[str]) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    args = _parse(raw)
    if args.command is None:
        # Bare invocation must not guess: preflight is read-only, execute is
        # an authority spend — they have different implications.
        _build_parser().print_usage(sys.stderr)
        print("ERROR: a subcommand is required (preflight|execute)", file=sys.stderr)
        return 2
    rest = raw[1:]
    if args.command == "preflight":
        return cmd_preflight(rest)
    return cmd_execute(rest)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
