"""Typed, immutable inputs for the one-shot M4 G4 sealed event.

The only constructor that produces a runner-ready bundle is
``verify_sealed_inputs``. It reads each file once under no-follow custody,
calls the real Cboe and Massive verifiers over those held bytes, validates the
typed owner calendar decision and frozen criteria/source join, checks the Git
checkout before and after the reads, and emits a self-binding packet. The
runner receives that same held-byte bundle; it never needs to re-open a path.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from tree_options.data.cboe_eod import (
    RealOptionsManifest,
    parse_cboe_eod_csv,
    verify_real_options_manifest,
    verify_real_options_manifest_tokens,
)
from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.massive_manifest import (
    CAPTURE_MANIFEST_FILENAME,
    MASSIVE_MANIFEST_SCHEMA_VERSION,
    load_massive_capture_manifest,
    verify_massive_capture_manifest,
    verify_massive_capture_manifest_tokens,
)
from tree_options.data.real_overlay import build_real_overlay
from tree_options.protocol.loader import load_protocol_bytes, protocol_hash
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.seal.errors import VerifiedInputsError
from tree_options.seal.identity import RUNNER_VERSION, SealedIdentity
from tree_options.seal.input_custody import hold_directory, read_file_once

VERIFIED_INPUTS_SCHEMA_VERSION = "m4-g4-verified-inputs/1"
CALENDAR_DECISION_SCHEMA_VERSION = "m4-g4-calendar-decision/1"
EXPECTED_MASSIVE_CAPTURE_VERSION = "m4b-capture/1"

VERIFIED_INPUTS_DOMAIN = b"tree-options-g4-verified-inputs-v1"
CALENDAR_DECISION_DOMAIN = b"tree-options-g4-calendar-decision-v1"
PAYLOAD_SET_DOMAIN = b"tree-options-g4-referenced-payload-set-v1"

CRITERIA_RELATIVE_PATH = Path("data/g4/sealed-criteria.json")
CRITERIA_SOURCE = "docs/m4-g4-sealed-gate-plan.md"
PROTOCOL_RELATIVE_PATH = Path("research_protocol.yaml")

CRITERION_IDS = (
    "manifest_integrity",
    "candidate_discipline",
    "fill_discipline",
    "rejection_paths_live",
    "determinism",
    "mutation_campaign",
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
CalendarDecision = Literal["repo-generated-calendar", "weekend-only-accepted"]
GitRunner = Callable[..., subprocess.CompletedProcess[str]]


class LaneManifestBinding(StrictModel):
    raw_sha256: Sha256
    typed_manifest_content_hash: Sha256
    manifest_version: IdStr
    referenced_payload_set_hash: Sha256


class CalendarDecisionArtifact(StrictModel):
    schema_version: Literal["m4-g4-calendar-decision/1"]
    decision: CalendarDecision
    owner_decision_id: IdStr
    decided_at: datetime
    rationale: IdStr
    content_sha256: Sha256

    @field_validator("decided_at")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("calendar decision timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _content_hash_binds_body(self) -> CalendarDecisionArtifact:
        core = self.model_copy(update={"content_sha256": ""})
        expected = sha256_hex(CALENDAR_DECISION_DOMAIN + canonical_bytes(core))
        if self.content_sha256 != expected:
            raise ValueError("calendar decision content_sha256 does not bind the typed body")
        return self


def build_calendar_decision_artifact(
    *,
    decision: CalendarDecision,
    owner_decision_id: str,
    decided_at: datetime,
    rationale: str,
) -> CalendarDecisionArtifact:
    fields = {
        "schema_version": CALENDAR_DECISION_SCHEMA_VERSION,
        "decision": decision,
        "owner_decision_id": owner_decision_id,
        "decided_at": decided_at,
        "rationale": rationale,
    }
    core = CalendarDecisionArtifact.model_construct(
        schema_version=CALENDAR_DECISION_SCHEMA_VERSION,
        decision=decision,
        owner_decision_id=owner_decision_id,
        decided_at=decided_at,
        rationale=rationale,
        content_sha256="",
    )
    digest = sha256_hex(CALENDAR_DECISION_DOMAIN + canonical_bytes(core))
    return CalendarDecisionArtifact.model_validate({**fields, "content_sha256": digest})


class CriteriaDecisionRule(StrictModel):
    evaluation_source: IdStr
    verdict_recording: IdStr
    one_shot: IdStr
    precondition_not_criterion: IdStr
    head: IdStr
    criteria_count: int = Field(strict=True)


class SealedCriterion(StrictModel):
    id: IdStr
    statement: IdStr


class SealedCriteriaArtifact(StrictModel):
    schema_version: int = Field(strict=True)
    gate: Literal["M4-G4 sealed real-data gate"]
    note: IdStr
    source: Literal["docs/m4-g4-sealed-gate-plan.md"]
    source_sha256: Sha256
    decision_rule: CriteriaDecisionRule
    criteria: tuple[SealedCriterion, ...]

    @model_validator(mode="after")
    def _frozen_shape(self) -> SealedCriteriaArtifact:
        if self.schema_version != 1:
            raise ValueError(f"criteria schema_version {self.schema_version!r} != 1")
        ids = tuple(criterion.id for criterion in self.criteria)
        if ids != CRITERION_IDS:
            raise ValueError(f"criterion identifiers/order {ids!r} != {CRITERION_IDS!r}")
        if self.decision_rule.criteria_count != len(CRITERION_IDS):
            raise ValueError("criteria_count does not match the frozen criterion identifiers")
        return self


class ReferencedPayload(StrictModel):
    logical_id: IdStr
    kind: IdStr
    raw_sha256: Sha256
    bytes: int = Field(ge=0, strict=True)


class ReferencedPayloadSet(StrictModel):
    payloads: tuple[ReferencedPayload, ...]


class VerifiedSealedInputs(StrictModel):
    schema_version: Literal["m4-g4-verified-inputs/1"]
    code_sha: GitSha
    protocol_hash: Sha256
    lane1_manifest: LaneManifestBinding
    lane2_manifest: LaneManifestBinding
    calendar_decision_artifact_sha256: Sha256
    criteria_artifact_sha256: Sha256
    criteria_source_document_sha256: Sha256
    runner_version: Literal["m4-g4-runner/1"]
    packet_content_sha256: Sha256

    @model_validator(mode="after")
    def _packet_hash_binds_body(self) -> VerifiedSealedInputs:
        core = self.model_copy(update={"packet_content_sha256": ""})
        expected = sha256_hex(VERIFIED_INPUTS_DOMAIN + canonical_bytes(core))
        if self.packet_content_sha256 != expected:
            raise ValueError("packet_content_sha256 does not bind the verified-input body")
        return self


def _build_packet(
    *,
    code_sha: str,
    protocol_sha: str,
    lane1: LaneManifestBinding,
    lane2: LaneManifestBinding,
    calendar_sha: str,
    criteria_sha: str,
    criteria_source_sha: str,
) -> VerifiedSealedInputs:
    fields = {
        "schema_version": VERIFIED_INPUTS_SCHEMA_VERSION,
        "code_sha": code_sha,
        "protocol_hash": protocol_sha,
        "lane1_manifest": lane1,
        "lane2_manifest": lane2,
        "calendar_decision_artifact_sha256": calendar_sha,
        "criteria_artifact_sha256": criteria_sha,
        "criteria_source_document_sha256": criteria_source_sha,
        "runner_version": RUNNER_VERSION,
    }
    core = VerifiedSealedInputs.model_construct(
        schema_version=VERIFIED_INPUTS_SCHEMA_VERSION,
        code_sha=code_sha,
        protocol_hash=protocol_sha,
        lane1_manifest=lane1,
        lane2_manifest=lane2,
        calendar_decision_artifact_sha256=calendar_sha,
        criteria_artifact_sha256=criteria_sha,
        criteria_source_document_sha256=criteria_source_sha,
        runner_version=RUNNER_VERSION,
        packet_content_sha256="",
    )
    digest = sha256_hex(VERIFIED_INPUTS_DOMAIN + canonical_bytes(core))
    return VerifiedSealedInputs.model_validate({**fields, "packet_content_sha256": digest})


@dataclass(frozen=True)
class HeldPayload:
    logical_id: str
    kind: str
    raw: bytes


@dataclass(frozen=True)
class HeldVerifiedSealedInputs:
    """The packet plus exactly the immutable bytes from which it was built."""

    packet: VerifiedSealedInputs
    protocol_bytes: bytes
    lane1_manifest_bytes: bytes
    lane1_payloads: tuple[HeldPayload, ...]
    lane2_manifest_bytes: bytes
    lane2_payloads: tuple[HeldPayload, ...]
    calendar_decision_artifact_bytes: bytes
    calendar_decision: CalendarDecisionArtifact
    criteria_artifact_bytes: bytes
    criteria: SealedCriteriaArtifact
    criteria_source_document_bytes: bytes


@dataclass(frozen=True)
class SealedInputPaths:
    repo: Path
    lane1_manifest: Path
    lane1_source: Path
    lane2_manifest: Path
    calendar_decision_artifact: Path


def _porcelain_dirty(lines: list[str]) -> list[str]:
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
    """Return a clean SHA-1 checkout identity plus evidence/refusal reason."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return runner(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        return None, "", f"git rev-parse HEAD failed: {head.stderr.strip()[:120]}"
    sha = head.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        return None, sha, f"git rev-parse HEAD returned non-canonical SHA-1 {sha!r}"
    status = git("status", "--porcelain")
    if status.returncode != 0:
        return None, sha, f"git status --porcelain failed: {status.stderr.strip()[:120]}"
    dirty = _porcelain_dirty(status.stdout.splitlines())
    if dirty:
        return None, sha, f"tracked tree dirty ({len(dirty)} path(s), first: {dirty[0][:60]})"
    return sha, sha, ""


def _raise(component: str, detail: str) -> NoReturn:
    raise VerifiedInputsError(component, detail)


def _typed_json(model: type[StrictModel], raw: bytes, *, component: str) -> StrictModel:
    try:
        return model.model_validate_json(raw)
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        _raise(component, f"typed JSON validation failed: {exc}")


def _payload_set_hash(payloads: tuple[HeldPayload, ...]) -> str:
    descriptors = tuple(
        ReferencedPayload(
            logical_id=payload.logical_id,
            kind=payload.kind,
            raw_sha256=sha256_hex(payload.raw),
            bytes=len(payload.raw),
        )
        for payload in sorted(payloads, key=lambda item: (item.logical_id, item.kind))
    )
    return sha256_hex(
        PAYLOAD_SET_DOMAIN + canonical_bytes(ReferencedPayloadSet(payloads=descriptors))
    )


def _verify_lane1(
    paths: SealedInputPaths,
) -> tuple[bytes, tuple[HeldPayload, ...], LaneManifestBinding]:
    component = "lane1"
    manifest_raw = read_file_once(
        paths.lane1_manifest,
        component=component,
        purpose="Cboe typed manifest",
    )
    manifest = _typed_json(RealOptionsManifest, manifest_raw, component=component)
    assert isinstance(manifest, RealOptionsManifest)
    try:
        verify_real_options_manifest_tokens(manifest)
    except Exception as exc:
        _raise(component, f"manifest version/provider verification failed: {exc}")

    source_raw = read_file_once(
        paths.lane1_source,
        component=component,
        purpose="Cboe source payload",
    )
    try:
        result = parse_cboe_eod_csv(
            paths.lane1_source,
            variant=manifest.variant,
            underlying=manifest.underlying_security_id,
            raw=source_raw,
        )
        overlay = build_real_overlay(result)
        verify_real_options_manifest(
            manifest,
            result,
            overlay=overlay,
            source_bytes=source_raw,
        )
    except Exception as exc:
        _raise(component, f"real Cboe manifest/payload verification failed: {exc}")

    payloads = (HeldPayload(logical_id="cboe/source", kind="source", raw=source_raw),)
    binding = LaneManifestBinding(
        raw_sha256=sha256_hex(manifest_raw),
        typed_manifest_content_hash=manifest.content_sha256,
        manifest_version=manifest.schema_version,
        referenced_payload_set_hash=_payload_set_hash(payloads),
    )
    return manifest_raw, payloads, binding


def _verify_lane2(
    paths: SealedInputPaths,
) -> tuple[bytes, tuple[HeldPayload, ...], LaneManifestBinding]:
    component = "lane2"
    if paths.lane2_manifest.name != CAPTURE_MANIFEST_FILENAME:
        _raise(
            component,
            f"manifest filename {paths.lane2_manifest.name!r} != {CAPTURE_MANIFEST_FILENAME!r}",
        )
    with hold_directory(
        paths.lane2_manifest.parent,
        component=component,
        purpose="Massive capture directory",
    ) as capture:
        manifest_raw = capture.read_name(
            paths.lane2_manifest.name,
            purpose="Massive typed manifest",
        )
        try:
            manifest = load_massive_capture_manifest(paths.lane2_manifest, raw=manifest_raw)
            verify_massive_capture_manifest_tokens(
                manifest,
                capture_version=EXPECTED_MASSIVE_CAPTURE_VERSION,
            )
        except Exception as exc:
            _raise(component, f"manifest version/provider verification failed: {exc}")

        payloads = tuple(
            HeldPayload(
                logical_id=entry.path,
                kind=entry.kind,
                raw=capture.read_relative(entry.path, purpose="Massive referenced payload"),
            )
            for entry in manifest.files
        )
        observed = capture.json_file_set(manifest_name=CAPTURE_MANIFEST_FILENAME)
        held_by_path = {payload.logical_id: payload.raw for payload in payloads}
        try:
            verify_massive_capture_manifest(
                manifest,
                paths.lane2_manifest.parent,
                capture_version=EXPECTED_MASSIVE_CAPTURE_VERSION,
                captured_files=held_by_path,
                observed_json_files=observed,
            )
        except Exception as exc:
            _raise(component, f"real Massive manifest/payload verification failed: {exc}")

        # Round-11 review fix (finding 10): the snapshot must still be TRUE at
        # the custody exit. json_file_set() froze the entry set the verifier
        # consumed, and the context exit itself checks only directory-inode
        # reachability — a file planted after the snapshot (e.g.
        # bars/unlisted.json) was invisible to the verifier, so the packet
        # attested a directory state that was already false at return. The
        # held directory is re-scanned here, at the exit boundary, and must
        # hold EXACTLY the entry set the verifier consumed; any divergence is
        # a corruption-class refusal, never a verified packet.
        rescanned = capture.json_file_set(manifest_name=CAPTURE_MANIFEST_FILENAME)
        if rescanned != observed:
            _raise(
                component,
                "the Massive capture directory entry set changed while the manifest"
                f" verifier consumed its snapshot (snapshot held {len(observed)} json"
                f" name(s), exit rescan found {len(rescanned)}; appeared:"
                f" {sorted(rescanned - observed)}; vanished:"
                f" {sorted(observed - rescanned)}) — the verified set was false"
                " at return",
            )

    binding = LaneManifestBinding(
        raw_sha256=sha256_hex(manifest_raw),
        typed_manifest_content_hash=manifest.content_sha256,
        manifest_version=MASSIVE_MANIFEST_SCHEMA_VERSION,
        referenced_payload_set_hash=_payload_set_hash(payloads),
    )
    return manifest_raw, payloads, binding


def verify_sealed_inputs(
    paths: SealedInputPaths,
    *,
    git_runner: GitRunner = subprocess.run,
) -> HeldVerifiedSealedInputs:
    """Verify and hold every input; emit no packet on any partial failure."""
    before_sha, evidence, reason = git_code_sha(paths.repo, runner=git_runner)
    if before_sha is None:
        _raise("code_sha", reason or f"checkout unavailable ({evidence})")

    protocol_raw = read_file_once(
        paths.repo / PROTOCOL_RELATIVE_PATH,
        component="protocol_hash",
        purpose="research protocol",
    )
    try:
        protocol_sha = protocol_hash(load_protocol_bytes(protocol_raw))
    except Exception as exc:
        _raise("protocol_hash", f"typed protocol validation failed: {exc}")

    lane1_raw, lane1_payloads, lane1_binding = _verify_lane1(paths)
    lane2_raw, lane2_payloads, lane2_binding = _verify_lane2(paths)

    calendar_raw = read_file_once(
        paths.calendar_decision_artifact,
        component="calendar_decision",
        purpose="typed owner calendar decision",
    )
    calendar = _typed_json(
        CalendarDecisionArtifact,
        calendar_raw,
        component="calendar_decision",
    )
    assert isinstance(calendar, CalendarDecisionArtifact)

    criteria_raw = read_file_once(
        paths.repo / CRITERIA_RELATIVE_PATH,
        component="criteria",
        purpose="sealed criteria artifact",
    )
    criteria = _typed_json(SealedCriteriaArtifact, criteria_raw, component="criteria")
    assert isinstance(criteria, SealedCriteriaArtifact)
    criteria_source_raw = read_file_once(
        paths.repo / CRITERIA_SOURCE,
        component="criteria",
        purpose="sealed criteria source document",
    )
    criteria_source_sha = sha256_hex(criteria_source_raw)
    if criteria.source_sha256 != criteria_source_sha:
        _raise(
            "criteria",
            "criteria source document SHA is stale: artifact records "
            f"{criteria.source_sha256}, held source hashes {criteria_source_sha}",
        )

    after_sha, after_evidence, after_reason = git_code_sha(paths.repo, runner=git_runner)
    if after_sha is None:
        _raise("code_sha", after_reason or f"checkout unavailable ({after_evidence})")
    if after_sha != before_sha:
        _raise(
            "code_sha",
            f"checkout moved from {before_sha} to {after_sha} while inputs were verified",
        )

    packet = _build_packet(
        code_sha=before_sha,
        protocol_sha=protocol_sha,
        lane1=lane1_binding,
        lane2=lane2_binding,
        calendar_sha=sha256_hex(calendar_raw),
        criteria_sha=sha256_hex(criteria_raw),
        criteria_source_sha=criteria_source_sha,
    )
    return HeldVerifiedSealedInputs(
        packet=packet,
        protocol_bytes=protocol_raw,
        lane1_manifest_bytes=lane1_raw,
        lane1_payloads=lane1_payloads,
        lane2_manifest_bytes=lane2_raw,
        lane2_payloads=lane2_payloads,
        calendar_decision_artifact_bytes=calendar_raw,
        calendar_decision=calendar,
        criteria_artifact_bytes=criteria_raw,
        criteria=criteria,
        criteria_source_document_bytes=criteria_source_raw,
    )


def identity_from_packet(packet: VerifiedSealedInputs) -> SealedIdentity:
    """The ledger identity derived from—never asserted alongside—the packet."""
    return SealedIdentity(
        code_sha=packet.code_sha,
        protocol_hash=packet.protocol_hash,
        lane1_manifest_sha256=packet.lane1_manifest.raw_sha256,
        lane2_manifest_sha256=packet.lane2_manifest.raw_sha256,
        calendar_decision_artifact_sha256=packet.calendar_decision_artifact_sha256,
        runner_version=packet.runner_version,
        criteria_artifact_sha256=packet.criteria_artifact_sha256,
        verified_packet_sha256=packet.packet_content_sha256,
    )


__all__ = [
    "CALENDAR_DECISION_SCHEMA_VERSION",
    "EXPECTED_MASSIVE_CAPTURE_VERSION",
    "VERIFIED_INPUTS_SCHEMA_VERSION",
    "CalendarDecisionArtifact",
    "GitRunner",
    "HeldPayload",
    "HeldVerifiedSealedInputs",
    "LaneManifestBinding",
    "SealedInputPaths",
    "VerifiedInputsError",
    "VerifiedSealedInputs",
    "build_calendar_decision_artifact",
    "git_code_sha",
    "identity_from_packet",
    "verify_sealed_inputs",
]
