"""Immutable, outcome-blind identity envelope for a future M5 evaluation.

This module binds the byte identity of every load-bearing evaluation input
without parsing, opening, or interpreting strategy outcomes.  Callers must
first hold the bytes they intend to evaluate and present them as
``HeldEvaluationArtifact`` objects.  The builder hashes those exact bytes,
canonicalizes the artifact census, and emits a domain-separated self-hashed
``EvaluationBundle``.  Verification rebuilds the bundle from held bytes and
current Git/evaluator coordinates, then compares the complete typed value.

``declared_record_count`` and ``total_trial_count`` are explicitly BOUND
METADATA.  This foundation does not infer record counts or semantic
completeness from opaque bytes.  Later artifact-specific typed parsers must
attest those properties before any M5 result can be considered evaluable.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Literal, cast

from pydantic import Field, StringConstraints, ValidationError, model_validator

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.schemas.common import IdStr, StrictModel

EVALUATION_BUNDLE_SCHEMA_VERSION: Literal["m5-evaluation-bundle/1"] = "m5-evaluation-bundle/1"
EVALUATION_BUNDLE_DOMAIN = b"tree-options-m5-evaluation-bundle-v1"
COUNT_SEMANTICS: Literal["declared-metadata-only"] = "declared-metadata-only"

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitObjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]

ArtifactRole = Literal[
    "protocol",
    "config",
    "dataset_manifest",
    "lane_manifest",
    "trial_registry_snapshot",
    "fold_definitions",
    "prediction_ledger",
    "order_ledger",
    "fill_ledger",
    "fee_ledger",
    "cash_ledger",
    "position_ledger",
    "direct_equity_output",
    "option_portfolio_output",
    "evaluator_source",
]

SINGLETON_ARTIFACT_ROLES: tuple[ArtifactRole, ...] = (
    "protocol",
    "config",
    "dataset_manifest",
    "trial_registry_snapshot",
    "fold_definitions",
    "prediction_ledger",
    "order_ledger",
    "fill_ledger",
    "fee_ledger",
    "cash_ledger",
    "position_ledger",
    "direct_equity_output",
    "option_portfolio_output",
    "evaluator_source",
)
KNOWN_ARTIFACT_ROLES = frozenset((*SINGLETON_ARTIFACT_ROLES, "lane_manifest"))


class EvaluationBundleError(ValueError):
    """The evaluation identity is incomplete, ambiguous, or has drifted."""


@dataclass(frozen=True)
class HeldEvaluationArtifact:
    """One already-held opaque payload plus its declared metadata.

    ``declared_record_count`` is deliberately not derived from ``raw`` here.
    A later typed parser is responsible for attesting that relationship.
    """

    role: str
    logical_id: str
    schema_version: str
    raw: bytes
    declared_record_count: int


class EvaluationArtifactIdentity(StrictModel):
    """Immutable byte identity and bound (not inferred) record metadata."""

    role: ArtifactRole
    logical_id: IdStr
    schema_version: IdStr
    raw_sha256: Sha256
    byte_count: int = Field(ge=1, strict=True)
    declared_record_count: int = Field(ge=0, strict=True)


class EvaluationBundle(StrictModel):
    """Complete identity census required before future M5 evaluation."""

    schema_version: Literal["m5-evaluation-bundle/1"]
    git_commit_sha: GitObjectId
    git_tree_sha: GitObjectId
    evaluator_version: IdStr
    count_semantics: Literal["declared-metadata-only"]
    total_trial_count: int = Field(ge=0, strict=True)
    artifacts: tuple[EvaluationArtifactIdentity, ...] = Field(min_length=1)
    bundle_content_sha256: Sha256

    @model_validator(mode="after")
    def _complete_canonical_self_binding(self) -> EvaluationBundle:
        counts = Counter(item.role for item in self.artifacts)
        missing = [role for role in SINGLETON_ARTIFACT_ROLES if counts[role] == 0]
        if counts["lane_manifest"] == 0:
            missing.append("lane_manifest")
        if missing:
            raise ValueError(f"missing required role(s): {', '.join(missing)}")

        duplicated = [role for role in SINGLETON_ARTIFACT_ROLES if counts[role] != 1]
        if duplicated:
            raise ValueError(f"singleton role(s) must appear exactly once: {', '.join(duplicated)}")

        lane_names = [item.logical_id for item in self.artifacts if item.role == "lane_manifest"]
        if len(lane_names) != len(set(lane_names)):
            raise ValueError("lane manifest logical_id values must be unique")

        identities = [(item.role, item.logical_id) for item in self.artifacts]
        if len(identities) != len(set(identities)):
            raise ValueError("artifact (role, logical_id) identities must be unique")
        if identities != sorted(identities):
            raise ValueError("artifacts are not in canonical (role, logical_id) order")

        trial_snapshot = next(
            item for item in self.artifacts if item.role == "trial_registry_snapshot"
        )
        if self.total_trial_count != trial_snapshot.declared_record_count:
            raise ValueError(
                "total_trial_count must equal trial_registry_snapshot declared_record_count"
            )

        expected = evaluation_bundle_content_sha256(self)
        if self.bundle_content_sha256 != expected:
            raise ValueError("bundle_content_sha256 does not bind the evaluation bundle body")
        return self


def evaluation_bundle_content_sha256(bundle: EvaluationBundle) -> str:
    """Hash a bundle with its self-hash blanked under the M5 identity domain."""
    core = bundle.model_copy(update={"bundle_content_sha256": ""})
    return sha256_hex(EVALUATION_BUNDLE_DOMAIN + canonical_bytes(core))


def _validate_held_artifact(item: HeldEvaluationArtifact) -> None:
    if not isinstance(item, HeldEvaluationArtifact):
        raise EvaluationBundleError(
            f"artifacts must be HeldEvaluationArtifact values, got {type(item)!r}"
        )
    if item.role not in KNOWN_ARTIFACT_ROLES:
        raise EvaluationBundleError(f"unknown artifact role {item.role!r}")
    for field_name, value in (
        ("logical_id", item.logical_id),
        ("schema_version", item.schema_version),
    ):
        if not isinstance(value, str) or not value or value != value.strip():
            raise EvaluationBundleError(
                f"{item.role} {field_name} must be a non-empty, already-trimmed string"
            )
    if type(item.raw) is not bytes or not item.raw:
        raise EvaluationBundleError(f"{item.role} raw payload must be non-empty bytes")
    if type(item.declared_record_count) is not int or item.declared_record_count < 0:
        raise EvaluationBundleError(f"{item.role} declared_record_count must be an integer >= 0")


def _artifact_identity(item: HeldEvaluationArtifact) -> EvaluationArtifactIdentity:
    return EvaluationArtifactIdentity(
        role=cast(ArtifactRole, item.role),
        logical_id=item.logical_id,
        schema_version=item.schema_version,
        raw_sha256=sha256_hex(item.raw),
        byte_count=len(item.raw),
        declared_record_count=item.declared_record_count,
    )


def build_evaluation_bundle(
    *,
    git_commit_sha: str,
    git_tree_sha: str,
    evaluator_version: str,
    artifacts: Iterable[HeldEvaluationArtifact],
) -> EvaluationBundle:
    """Build a canonical identity bundle from exact already-held payload bytes."""
    held = tuple(artifacts)
    for item in held:
        _validate_held_artifact(item)

    counts = Counter(item.role for item in held)
    missing = [role for role in SINGLETON_ARTIFACT_ROLES if counts[role] == 0]
    if counts["lane_manifest"] == 0:
        missing.append("lane_manifest")
    if missing:
        raise EvaluationBundleError(f"missing required role(s): {', '.join(missing)}")

    duplicated = [role for role in SINGLETON_ARTIFACT_ROLES if counts[role] != 1]
    if duplicated:
        raise EvaluationBundleError(
            f"singleton role(s) must appear exactly once: {', '.join(duplicated)}"
        )

    lane_names = [item.logical_id for item in held if item.role == "lane_manifest"]
    if len(lane_names) != len(set(lane_names)):
        raise EvaluationBundleError("lane manifest logical_id values must be unique")

    identities = tuple(
        sorted(
            (_artifact_identity(item) for item in held),
            key=lambda item: (item.role, item.logical_id),
        )
    )
    trial_snapshot = next(item for item in identities if item.role == "trial_registry_snapshot")
    core = EvaluationBundle.model_construct(
        schema_version=EVALUATION_BUNDLE_SCHEMA_VERSION,
        git_commit_sha=git_commit_sha,
        git_tree_sha=git_tree_sha,
        evaluator_version=evaluator_version,
        count_semantics=COUNT_SEMANTICS,
        total_trial_count=trial_snapshot.declared_record_count,
        artifacts=identities,
        bundle_content_sha256="",
    )
    digest = evaluation_bundle_content_sha256(core)
    payload = core.model_dump(mode="python")
    payload["bundle_content_sha256"] = digest
    try:
        return EvaluationBundle.model_validate(payload)
    except ValidationError as exc:
        raise EvaluationBundleError(f"invalid evaluation bundle identity: {exc}") from None


def verify_evaluation_bundle(
    bundle: EvaluationBundle,
    *,
    git_commit_sha: str,
    git_tree_sha: str,
    evaluator_version: str,
    artifacts: Iterable[HeldEvaluationArtifact],
) -> None:
    """Refuse if a bundle differs from current coordinates or held payloads."""
    try:
        validated = EvaluationBundle.model_validate(bundle.model_dump(mode="python"))
    except (AttributeError, ValidationError) as exc:
        raise EvaluationBundleError(f"invalid evaluation bundle: {exc}") from None

    rebuilt = build_evaluation_bundle(
        git_commit_sha=git_commit_sha,
        git_tree_sha=git_tree_sha,
        evaluator_version=evaluator_version,
        artifacts=artifacts,
    )
    if validated != rebuilt:
        raise EvaluationBundleError(
            "evaluation bundle does not match held inputs or current Git/evaluator identity"
        )
