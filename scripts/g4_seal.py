#!/usr/bin/env python
"""G4 sealed-event preflight + one-shot execute authority guard (PR A/A5).

The G4 sealed event (docs/m4-g4-sealed-gate-plan.md §3) is executable exactly
ONCE, ever, per sealed content. This script implements the machinery and
consumes NO authority in PR A:

* ``preflight`` builds a self-binding ``VerifiedSealedInputs`` packet from
  bytes read once under no-follow custody and accepted by the real Cboe and
  Massive typed verifiers. It is structurally incapable of computing or
  displaying a verdict: the output model pins ``verdict`` to
  ``Literal[None]`` and ``verdict_computed`` to ``Literal[False]``. No
  network, no broker, no run.
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
   own payload, yields this run's packet-bound sealed_run_id (exit 6) — a
   record's stored ids alone are never trusted.
3. current checkout/protocol/lane payloads/calendar/criteria and runner
   version are re-verified and cross-joined to the approved packet.
4. the CONSUMPTION record is appended durably (flock + fsync file + fsync
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
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal, Protocol

from tree_options.schemas.common import StrictModel

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.seal import ledger as seal_ledger  # noqa: E402
from tree_options.seal.errors import (  # noqa: E402
    ApprovalInvalidError,
    LedgerCorruptError,
    SealError,
    SecondExecutionRefusedError,
    VerifiedInputsError,
)
from tree_options.seal.identity import (  # noqa: E402
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
from tree_options.seal.verified_inputs import (  # noqa: E402
    GitRunner,
    HeldVerifiedSealedInputs,
    SealedInputPaths,
    VerifiedSealedInputs,
    identity_from_packet,
    verify_sealed_inputs,
)


class Runner(Protocol):
    """A sealed runner identifies its exact machinery and consumes held bytes."""

    runner_version: str

    def __call__(self, inputs: HeldVerifiedSealedInputs) -> str: ...


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
    verified_inputs: VerifiedSealedInputs | None


def _available(evidence: str) -> CriterionStatus:
    return CriterionStatus(available=True, reason="", evidence=evidence)


def _unavailable(reason: str, evidence: str = "") -> CriterionStatus:
    return CriterionStatus(available=False, reason=reason, evidence=evidence)


_INPUT_STATUS_KEYS = (
    "code_sha",
    "protocol_hash",
    "lane1",
    "lane2",
    "calendar_decision",
    "criteria",
)


def _input_paths(args: argparse.Namespace) -> SealedInputPaths:
    for attribute, component in (
        ("lane1_manifest", "lane1"),
        ("lane1_source", "lane1"),
        ("lane2_manifest", "lane2"),
        ("calendar_decision_artifact", "calendar_decision"),
    ):
        if getattr(args, attribute) is None:
            raise VerifiedInputsError(component, f"--{attribute.replace('_', '-')} is required")
    return SealedInputPaths(
        repo=args.repo,
        lane1_manifest=args.lane1_manifest,
        lane1_source=args.lane1_source,
        lane2_manifest=args.lane2_manifest,
        calendar_decision_artifact=args.calendar_decision_artifact,
    )


def _verified_statuses(packet: VerifiedSealedInputs) -> dict[str, CriterionStatus]:
    return {
        "code_sha": _available(packet.code_sha),
        "protocol_hash": _available(packet.protocol_hash),
        "lane1": _available(
            f"raw={packet.lane1_manifest.raw_sha256}; "
            f"typed={packet.lane1_manifest.typed_manifest_content_hash}; "
            f"payloads={packet.lane1_manifest.referenced_payload_set_hash}"
        ),
        "lane2": _available(
            f"raw={packet.lane2_manifest.raw_sha256}; "
            f"typed={packet.lane2_manifest.typed_manifest_content_hash}; "
            f"payloads={packet.lane2_manifest.referenced_payload_set_hash}"
        ),
        "calendar_decision": _available(packet.calendar_decision_artifact_sha256),
        "criteria": _available(
            f"artifact={packet.criteria_artifact_sha256}; "
            f"source={packet.criteria_source_document_sha256}"
        ),
    }


def _refused_statuses(exc: VerifiedInputsError) -> dict[str, CriterionStatus]:
    statuses = {
        key: _unavailable("not verified because the typed input bundle refused")
        for key in _INPUT_STATUS_KEYS
    }
    key = exc.component if exc.component in statuses else "criteria"
    statuses[key] = _unavailable(exc.detail)
    return statuses


def cmd_preflight(argv: list[str], *, git_runner: GitRunner = subprocess.run) -> int:
    args = _parse(["preflight", *argv])
    try:
        read_ledger(args.ledger_root)  # availability of the authority surface
    except SealError as exc:  # refused root / corrupt chain: incident outranks
        print(f"LEDGER UNREADABLE: {exc}", file=sys.stderr)  # input availability
        return 3
    packet: VerifiedSealedInputs | None = None
    try:
        held = verify_sealed_inputs(_input_paths(args), git_runner=git_runner)
    except VerifiedInputsError as exc:
        statuses = _refused_statuses(exc)
    else:
        packet = held.packet
        statuses = _verified_statuses(packet)
    report = PreflightReport(
        verdict=None,
        verdict_computed=False,
        criteria_inputs=statuses,
        verified_inputs=packet,
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


def _check_authority(view: seal_ledger.LedgerView, identity: SealedIdentity) -> None:
    """Refuse duplicate content and require an exact recomputed approval."""
    run_id = sealed_run_id(identity)
    content_id = content_identity(identity)
    for record in view.records:
        if record.kind != KIND_CONSUMPTION:
            continue
        try:
            record_run_id = sealed_run_id(record.identity)
            record_content_id = content_identity(record.identity)
        except Exception:
            raise LedgerCorruptError(
                f"CONSUMPTION record {record.record_sha256[:12]}… has an"
                " unparseable identity payload; the duplicate guard cannot"
                " be evaluated safely — refusing to append"
            ) from None
        if record.sealed_run_id != record_run_id or record.content_identity != record_content_id:
            raise LedgerCorruptError(
                f"CONSUMPTION record {record.record_sha256[:12]}… stored"
                " ids disagree with its own identity payload (corruption)"
            )
        if record_run_id == run_id or record_content_id == content_id:
            raise SecondExecutionRefusedError(
                run_id, "a CONSUMPTION record already matches this sealed content"
            )

    approval_ok = any(
        record.kind == KIND_APPROVAL
        and record.sealed_run_id == run_id
        and record.content_identity == content_id
        and sealed_run_id(record.identity) == run_id
        and content_identity(record.identity) == content_id
        and record.identity.verified_packet_sha256 == identity.verified_packet_sha256
        for record in view.records
    )
    if not approval_ok:
        raise ApprovalInvalidError(
            run_id,
            "no APPROVAL record recomputes to this verified packet and sealed run id",
        )


def execute_sealed_run(
    expected_packet: VerifiedSealedInputs,
    *,
    inputs: SealedInputPaths,
    ledger_root: Path,
    reason: str,
    at_epoch: int,
    runner: Runner,
    git_runner: GitRunner = subprocess.run,
) -> ExecuteSummary:
    """Cross-join and consume one-shot authority for a verified packet.

    Refuses a second execution matching by EITHER id (step 1), requires an
    approval that RECOMPUTES to this run's sealed_run_id from the record's
    own payload (step 2), and appends the CONSUMPTION record durably BEFORE
    invoking ``runner`` (step 3). A crash after the append and before (or
    during) the runner leaves a durable CONSUMPTION with no run result:
    UNKNOWN / RECONCILIATION_REQUIRED — never auto-rerun (a later identical
    execute hits step 1 and refuses).
    """
    # Revalidate the self-hash even if a caller used Pydantic's low-level
    # model_construct escape hatch to manufacture the typed object.
    try:
        expected_packet = VerifiedSealedInputs.model_validate_json(
            expected_packet.model_dump_json()
        )
    except Exception as exc:
        raise VerifiedInputsError(
            "packet", f"expected packet self-validation failed: {exc}"
        ) from None
    identity = identity_from_packet(expected_packet)
    run_id = sealed_run_id(identity)
    content_id = content_identity(identity)
    view = read_ledger(ledger_root)
    _check_authority(view, identity)

    # Reconstruct from current paths immediately before the authority spend.
    # Equality covers checkout, protocol, typed manifest content, every
    # referenced payload, calendar decision, criteria/source, and machinery.
    current = verify_sealed_inputs(inputs, git_runner=git_runner)
    if current.packet != expected_packet:
        raise ApprovalInvalidError(
            run_id,
            "current typed inputs do not equal the owner-approved verified packet",
        )
    try:
        presented_runner_version = runner.runner_version
    except AttributeError:
        presented_runner_version = "<missing>"
    if presented_runner_version != expected_packet.runner_version:
        raise ApprovalInvalidError(
            run_id,
            f"runner version {presented_runner_version!r} does not equal "
            f"approved {expected_packet.runner_version!r}",
        )

    # Verification may take time. Re-read the ledger at the final effect
    # boundary, so an interleaved consumption or approval change is joined to
    # the packet before the append. append_record additionally rejects a stale
    # tail while holding the ledger lock.
    view = read_ledger(ledger_root)
    _check_authority(view, identity)

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
    outcome = runner(current)
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
        "--lane1-source", type=Path, help="lane 1 Cboe source CSV pinned by the manifest"
    )
    p.add_argument(
        "--lane2-manifest", type=Path, help="lane 2 (massive-derived era) capture manifest file"
    )
    p.add_argument(
        "--calendar-decision-artifact",
        type=Path,
        help="typed, owner-issued holiday-calendar decision artifact",
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
