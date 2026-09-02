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
  the CLI execute path refuses before touching the ledger. The runner
  machinery is resolved from the module-level REGISTRY
  (``tree_options.seal.verified_inputs.RUNNER_REGISTRY``, seeded explicitly
  by the owning layer or a test via ``register_runner``) — never from a
  caller-presented callable — and the approved packet binds the registered
  implementation's code-file hash (round-11 finding 8).
* 0.2.1 ratification (owner decision 2026-08-26): ``preflight`` wires the
  PRODUCTION machinery when the registry is empty —
  ``tree_options.seal.runner.wire_production_runner``, whose configuration
  digest binds the protocol-declared repo calendar (the ratified
  ``repo-generated-calendar`` decision). It never REPLACES an existing
  registration, and the execute path stays unwired (runbook §4.6: EXECUTE
  IS PROHIBITED).

Execute semantics (``execute_sealed_run``):

1. read the ledger — a CONSUMPTION record whose sealed_run_id matches this
   run refuses (exit 7) ABSOLUTELY (the exact checkout is never re-runnable);
   a CONSUMPTION whose content_identity matches refuses (exit 7) unless owner
   RECONCILIATION records re-arm the content, one further consumption each
   (consumptions(content) may exceed reconciliations(content) by at most the
   original, unreconciled spend). Two checkouts of the same research content
   share a content_identity, so content authority is one-shot per sealed
   CONTENT, not per checkout.
2. an APPROVAL record must exist whose identity, RECOMPUTED from the record's
   own payload, yields this run's packet-bound sealed_run_id (exit 6) — a
   record's stored ids alone are never trusted. A re-armed successor checkout
   still needs its OWN approval: reconciliation re-arms the content, the
   approval authorizes the new checkout.
3. current checkout/protocol/lane payloads/calendar/criteria and runner
   version are re-verified and cross-joined to the approved packet.
4. the CONSUMPTION record is appended durably (flock + fsync file + fsync
   dir) BEFORE the runner is invoked. A crash after consumption is a
   documented UNKNOWN / RECONCILIATION_REQUIRED state that is NEVER
   auto-rerun: a later identical execute hits step 1 and refuses (the exact
   run) or the budget arithmetic (a successor checkout without a fresh owner
   reconciliation). The remediation path for consumed-without-verdict content
   is the owner's RECONCILIATION record (``ledger.append_reconciliation``,
   library-only like every authority act) naming the consumed identity.

Exit codes:
  0  preflight: all six sealed-run inputs available (no verdict computed)
  2  bare invocation / unknown subcommand; preflight: one or more inputs
     unavailable; execute reached without internal runner wiring (refused —
     nothing is read, nothing is consumed)
  3  ledger unreadable: root refused (resolved under /tmp) or hash chain
     corrupt
  6  APPROVAL_INVALID — no approval record recomputes to this run's identity
  7  SECOND_EXECUTION_REFUSED — this sealed content was already consumed
  8  RECONCILIATION_INVALID — a reconciliation record cannot be minted for
     this identity (no matching CONSUMPTION: nothing consumed, nothing to
     re-arm)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from tree_options.schemas.common import StrictModel

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.seal import ledger as seal_ledger  # noqa: E402
from tree_options.seal import runner as runner_wiring  # noqa: E402
from tree_options.seal.errors import (  # noqa: E402
    ApprovalInvalidError,
    LedgerCorruptError,
    ReconciliationInvalidError,
    SealError,
    SecondExecutionRefusedError,
    VerifiedInputsError,
)
from tree_options.seal.identity import (  # noqa: E402
    RUNNER_VERSION,
    SealedIdentity,
    content_identity,
    sealed_run_id,
)
from tree_options.seal.ledger import (  # noqa: E402
    DEFAULT_G4_LEDGER_ROOT,
    KIND_APPROVAL,
    KIND_CONSUMPTION,
    KIND_RECONCILIATION,
    LedgerRecord,
    read_ledger,
)
from tree_options.seal.verified_inputs import (  # noqa: E402
    RUNNER_REGISTRY,
    GitRunner,
    HeldVerifiedSealedInputs,
    SealedInputPaths,
    VerifiedSealedInputs,
    identity_from_packet,
    runner_implementation_qualname,
    runner_implementation_sha256,
    verify_sealed_inputs,
)
from tree_options.time.calendar import CalendarError  # noqa: E402


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
    "spot_proxy_v2",
    "criteria",
    "runner",
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
    # (remediation-3 review P1-1) the preflight is the DOCUMENTED
    # approval-packet route: it must be able to produce the sidecar-bearing
    # packet the next sealed run executes, or the approved packet can never
    # equal the executed one. An explicitly supplied sidecar that is not a
    # file REFUSES here (a typo'd path is not an absent declaration)
    spot_proxy_v2 = getattr(args, "spot_proxy_v2", None)
    if spot_proxy_v2 is not None and not spot_proxy_v2.is_file():
        raise VerifiedInputsError(
            "spot_proxy_v2",
            f"--spot-proxy-v2 {spot_proxy_v2} is not a file — an explicitly"
            " declared sidecar must exist (omit the flag for the v1-only"
            " packet shape)",
        )
    return SealedInputPaths(
        repo=args.repo,
        lane1_manifest=args.lane1_manifest,
        lane1_source=args.lane1_source,
        lane2_manifest=args.lane2_manifest,
        calendar_decision_artifact=args.calendar_decision_artifact,
        spot_proxy_v2=spot_proxy_v2,
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
        # (remediation-3) the sidecar is an availability input of its own:
        # the preflight DISCLOSES whether the packet carries it (the v1-only
        # shape prints absent — a visible fact, never a silent default)
        "spot_proxy_v2": _available(
            packet.spot_proxy_v2_sha256
            if packet.spot_proxy_v2_sha256 is not None
            else "absent (the v1-only packet shape)"
        ),
        "criteria": _available(
            f"artifact={packet.criteria_artifact_sha256}; "
            f"source={packet.criteria_source_document_sha256}"
        ),
        # round-11 finding 8 + round-10 finding 4: the runner machinery is
        # itself an availability input — the packet binds the registered
        # implementation's qualified name, code-file hash, and configuration
        # digest.
        "runner": _available(
            f"version={packet.runner_version};"
            f" qualname={packet.runner_implementation_qualname};"
            f" implementation={packet.runner_implementation_sha256};"
            f" config={packet.runner_config_digest}"
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


def _ensure_production_runner_wired() -> None:
    """0.2.1 ratification (owner decision 2026-08-26): preflight is the
    owning layer that wires the calendar-bound machinery WHEN THE REGISTRY
    IS EMPTY — it never replaces an existing registration (a test's stub,
    or the sealed event's own machinery authored later at the owner-declared
    head, stays authoritative). The CLI execute path stays unwired exactly
    as the closeout runbook §4.6 requires."""
    if RUNNER_REGISTRY.get(RUNNER_VERSION) is not None:
        return
    try:
        runner_wiring.wire_production_runner(REPO_ROOT)
    except (CalendarError, OSError, ValueError) as exc:
        raise VerifiedInputsError(
            "runner",
            f"production runner wiring failed: {exc}; no verified packet was"
            " emitted and no G4 authority was consumed",
        ) from None


def cmd_preflight(argv: list[str], *, git_runner: GitRunner = subprocess.run) -> int:
    args = _parse(["preflight", *argv])
    try:
        read_ledger(args.ledger_root)  # availability of the authority surface
    except SealError as exc:  # refused root / corrupt chain: incident outranks
        print(f"LEDGER UNREADABLE: {exc}", file=sys.stderr)  # input availability
        return 3
    _ensure_production_runner_wired()
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
    """Refuse duplicate content and require an exact recomputed approval.

    The sealed-RUN arm is absolute: a CONSUMPTION matching this exact
    checkout's sealed_run_id refuses forever — the crashed checkout is never
    re-runnable (the remediation is, by construction, a different head). The
    sealed-CONTENT arm is re-armable by owner RECONCILIATION records, each
    permitting exactly ONE further consumption: a new consumption of content
    C is refused while consumptions(C) > reconciliations(C). Causal order is
    enforced at EVERY prefix (round 2, P1-1): a hash-valid reconciliation
    credited AHEAD of any consumption of its content is corruption, never a
    budget the arithmetic may honor."""
    run_id = sealed_run_id(identity)
    content_id = content_identity(identity)
    content_consumptions = 0
    reconciliations = 0
    for record in view.records:
        if record.kind == KIND_RECONCILIATION:
            try:
                reconciliation_content_id = content_identity(record.identity)
            except Exception:
                raise LedgerCorruptError(
                    f"RECONCILIATION record {record.record_sha256[:12]}… has an"
                    " unparseable identity payload; the re-arm budget cannot"
                    " be evaluated safely — refusing to append"
                ) from None
            if (
                record.content_identity != reconciliation_content_id
                or record.sealed_run_id != sealed_run_id(record.identity)
            ):
                raise LedgerCorruptError(
                    f"RECONCILIATION record {record.record_sha256[:12]}… stored"
                    " ids disagree with its own identity payload (corruption)"
                )
            if reconciliation_content_id == content_id:
                reconciliations += 1
                if reconciliations > content_consumptions:
                    raise LedgerCorruptError(
                        f"RECONCILIATION record {record.record_sha256[:12]}… is"
                        " credited AHEAD of any consumption of this content"
                        f" (prefix holds {reconciliations} reconciliation(s)"
                        f" against {content_consumptions} consumption(s)) —"
                        " authority is never granted ahead of the spend it"
                        " names, not even in a hash-valid hand-chained ledger"
                    )
            continue
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
        if record_run_id == run_id:
            raise SecondExecutionRefusedError(
                run_id, "a CONSUMPTION record already matches this exact sealed run"
            )
        if record_content_id == content_id:
            content_consumptions += 1
    if content_consumptions > reconciliations:
        raise SecondExecutionRefusedError(
            run_id,
            f"{content_consumptions} CONSUMPTION record(s) already match this"
            f" sealed content against {reconciliations} owner RECONCILIATION"
            " record(s) — each reconciliation re-arms exactly one further"
            " consumption; consumed-without-verdict content is re-armed by an"
            " owner reconciliation, never by a re-run",
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


def reconcile_consumed_without_verdict(
    ledger_root: Path,
    repo_root: Path,
    identity: SealedIdentity,
    *,
    reason: str,
    at_epoch: int,
) -> seal_ledger.LedgerRecord:
    """The GUARDED owner reconciliation for the successor-event driver.

    The ledger itself is verdict-blind BY DESIGN — it holds authority
    records only and has no artifact-layout knowledge — so the verdict guard
    lives HERE, at the orchestrator-facing layer that owns the paths. Two
    bindings, both fail-closed:

    * the root must be a HOSTING root — the consumed checkout's run-scoped
      workspace exists under it (identity-bound: the workspace is keyed by
      this identity's sealed_run_id), or the LEGACY residue of a sealed run
      exists there (the registry/artifacts/scratch the pre-run-scoping
      layout leaves — not identity-bound, disclosed). A root with neither
      cannot attest verdict ABSENCE, and a negative check against the wrong
      root is not evidence (round 2, P1-2).
    * a ``sealed-gate-summary.json`` at either summary location means the
      consumption HAS its verdict — re-arming verdicted content is not
      reconciliation and refuses as ``RECONCILIATION_INVALID``.

    Disclosed boundaries: the summary check is a negative filesystem check —
    a summary created in the window between the checks and the ledger
    append is not seen (the owner act is the authority; this guard is the
    driver's honesty check, not a lock); and the LEGACY summary block is
    intentionally conservative and global (any legacy summary blocks every
    reconciliation until the owner looks). The raw
    ``ledger.append_reconciliation`` stays available as the owner's explicit
    override act (hash-chained and reasoned like every authority record);
    the driver path uses THIS entry point, so the operational sequence
    cannot reconcile a verdicted event by accident."""
    from tree_options.seal.g4_gate import production_gate_paths

    run_id = sealed_run_id(identity)
    run_scoped = production_gate_paths(repo_root, run_key=run_id)
    legacy = production_gate_paths(repo_root)
    run_scoped_summary = run_scoped.artifacts_dir / "sealed-gate-summary.json"
    legacy_summary = legacy.artifacts_dir / "sealed-gate-summary.json"
    hosted_here = (
        run_scoped.registry.exists()
        or run_scoped.artifacts_dir.exists()
        or run_scoped.scratch_root.exists()
        or legacy.registry.exists()
        or legacy.artifacts_dir.exists()
        or legacy.scratch_root.exists()
    )
    if not hosted_here:
        raise ReconciliationInvalidError(
            run_id,
            f"{repo_root} shows no sealed-run workspace for the consumed"
            " checkout (no run-scoped registry/artifacts/scratch under"
            " g4-sealed-runs/<sealed_run_id>/ and no legacy residue) — the"
            " verdict-absence check cannot be bound to a root that did not"
            " host the run; pass the HOSTING root, or use the raw ledger API"
            " as the owner's explicit override act",
        )
    if run_scoped_summary.is_file():
        raise ReconciliationInvalidError(
            run_id,
            f"the consumed checkout's run-scoped workspace holds {run_scoped_summary}"
            " — its verdict EXISTS; re-arming verdicted content is not"
            " reconciliation (the raw ledger API is the owner's explicit"
            " override act, never the driver path)",
        )
    if legacy_summary.is_file():
        raise ReconciliationInvalidError(
            run_id,
            f"the legacy sealed artifacts hold {legacy_summary} — the consumed"
            " checkout's verdict EXISTS (the pre-run-scoping layout); re-arming"
            " verdicted content is not reconciliation",
        )
    return seal_ledger.append_reconciliation(
        ledger_root, identity, reason=reason, at_epoch=at_epoch
    )


def execute_sealed_run(
    expected_packet: VerifiedSealedInputs,
    *,
    inputs: SealedInputPaths,
    ledger_root: Path,
    reason: str,
    at_epoch: int,
    git_runner: GitRunner = subprocess.run,
) -> ExecuteSummary:
    """Cross-join and consume one-shot authority for a verified packet.

    Refuses a second execution matching by EITHER id (step 1), requires an
    approval that RECOMPUTES to this run's sealed_run_id from the record's
    own payload (step 2), and appends the CONSUMPTION record durably BEFORE
    invoking the runner (step 3). A crash after the append and before (or
    during) the runner leaves a durable CONSUMPTION with no run result:
    UNKNOWN / RECONCILIATION_REQUIRED — never auto-rerun (a later identical
    execute hits step 1 and refuses).

    Round-11 review fix (finding 8): the runner MACHINERY is resolved from
    the module-level REGISTRY keyed by the approved runner_version — this
    function no longer accepts a caller-presented callable as authority. The
    approved packet carries the registered implementation's code-file sha256
    (recorded at approval time); execution re-hashes the registered
    implementation's code file NOW and refuses on any divergence before a
    single byte of authority is spent. A foreign callable carrying the
    approved version literal is never registered, so it is never authority.

    Round-10 review fix (finding 4): the code-file hash alone is not a
    callable identity — every callable in the implementation's module shares
    it, and every configuration of the implementation's class shares the
    class file. The packet now binds (version, qualname, file_sha256,
    config_digest), and execution re-derives ALL FOUR from the registry
    entry and requires exact equality on all of them: a same-file foreign
    callable and a differently-configured instance of the approved class are
    both refused before any consumption.
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
    # Round-11 finding 8 + round-10 finding 4: bind runner IDENTITY, not a
    # caller-asserted string. The machinery comes from the REGISTRY under the
    # approved version; the registry entry's CURRENT binding — version,
    # qualified name, code-file hash, and configuration digest — must equal
    # all four members of the binding the owner approved inside the packet.
    registered = RUNNER_REGISTRY.get(expected_packet.runner_version)
    if registered is None:
        raise ApprovalInvalidError(
            run_id,
            f"no runner machinery is registered for {expected_packet.runner_version!r}"
            " — a foreign callable carrying the approved literal is never"
            " registered and is never authority",
        )
    presented_runner_version = registered.runner_version
    if presented_runner_version != expected_packet.runner_version:
        raise ApprovalInvalidError(
            run_id,
            f"runner version {presented_runner_version!r} does not equal "
            f"approved {expected_packet.runner_version!r}",
        )
    current_qualname = runner_implementation_qualname(registered.implementation)
    if current_qualname != expected_packet.runner_implementation_qualname:
        raise ApprovalInvalidError(
            run_id,
            f"the registered runner implementation's qualified name {current_qualname!r}"
            " does not equal the approved packet's machinery binding "
            f"{expected_packet.runner_implementation_qualname!r} — a callable"
            " sharing the approved implementation's CODE FILE is a different"
            " callable, never the machinery the owner approved",
        )
    current_code_sha = runner_implementation_sha256(registered.implementation)
    if current_code_sha != expected_packet.runner_implementation_sha256:
        raise ApprovalInvalidError(
            run_id,
            "the registered runner implementation's current code hash "
            f"{current_code_sha[:12]}… does not equal the approved packet's"
            f" machinery binding {expected_packet.runner_implementation_sha256[:12]}…"
            " — the machinery changed since approval and is not the"
            " implementation the owner approved",
        )
    if registered.config_digest != expected_packet.runner_config_digest:
        raise ApprovalInvalidError(
            run_id,
            f"the registered runner configuration digest {registered.config_digest[:12]}…"
            " does not equal the approved packet's machinery binding "
            f"{expected_packet.runner_config_digest[:12]}… — the same machinery"
            " class configured differently is not the configuration the owner"
            " approved",
        )
    runner = registered.implementation
    # R13 (round-11 finding 2): the configuration digest is RE-DERIVED from
    # the LIVE implementation at the effect boundary. RegisteredRunner is
    # frozen, but the implementation OBJECT it references is not: a mutable
    # configured runner registered with the truthful digest could be flipped
    # after approval without re-registering, and both the packet rebuild and
    # the stored-digest comparison above still saw the registration-time
    # string. The registering layer's config_digest_fn reads the live
    # instance NOW; a mutated configuration changes the recomputed digest
    # and is refused BEFORE any authority is spent.
    recomputed_config_digest = registered.config_digest_fn(runner)
    if recomputed_config_digest != expected_packet.runner_config_digest:
        raise ApprovalInvalidError(
            run_id,
            "the runner configuration digest recomputed from the LIVE implementation "
            f"{recomputed_config_digest[:12]}… does not equal the approved packet's "
            f"machinery binding {expected_packet.runner_config_digest[:12]}… — the "
            "registered machinery's configuration changed after approval, and a "
            "mutated instance is not the configuration the owner approved",
        )

    # Verification may take time. Re-read the ledger at the final effect
    # boundary, so an interleaved consumption or approval change is joined to
    # the packet before the append. append_record additionally rejects a stale
    # tail while holding the ledger lock.
    view = read_ledger(ledger_root)
    _check_authority(view, identity)

    # Round-4 P0: the CONSUMPTION below is DURABLE the moment it is
    # appended, and the runner raises BEFORE any verdict when the gate's
    # auxiliary inputs cannot evaluate (an unloadable registry, an
    # unparseable or shape-invalid mutation report, a malformed era census).
    # Preflight HERE — after the authority cross-join, before the append —
    # so a refusal costs nothing: no consumption, no workspace, the
    # approval intact for a corrected attempt. This changes no authority
    # semantics: the run is still one-shot, still approval-cross-joined.
    # Round-5 P0: this is the SINGLE preflight point — register_runner
    # guarantees every authority-grade runner exposes preflight(), and the
    # runner itself does NOT re-preflight inside __call__ (a second check
    # after the append would re-open the consumed-without-verdict race).
    # The cast narrows the Callable-typed registry surface, not an option.
    cast(Any, runner).preflight()

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
        f"{args.ledger_root} untouched); execute is library-only in PR A — the "
        "runner machinery must be registered (verified_inputs.register_runner) "
        "before execute_sealed_run can bind it",
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
        "--spot-proxy-v2",
        type=Path,
        default=None,
        help="the OPTIONAL declared daily spot sidecar (remediation-3): when"
        " supplied it is held, validated, and hash-bound into the packet —"
        " omit for the v1-only packet shape; a supplied path that is not a"
        " file refuses",
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
