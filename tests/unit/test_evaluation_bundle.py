"""Outcome-blind identity contract for the future M5 evaluation bundle."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from tree_options.evaluation.bundle import (
    COUNT_SEMANTICS,
    EVALUATION_BUNDLE_SCHEMA_VERSION,
    EvaluationBundle,
    EvaluationBundleError,
    HeldEvaluationArtifact,
    build_evaluation_bundle,
    verify_evaluation_bundle,
)

COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
EVALUATOR_VERSION = "m5-evaluator/1"

SINGLETON_ROLES = (
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


def _artifact(
    role: str,
    *,
    logical_id: str | None = None,
    raw: bytes | None = None,
    declared_record_count: int = 1,
) -> HeldEvaluationArtifact:
    name = logical_id or role
    return HeldEvaluationArtifact(
        role=role,
        logical_id=name,
        schema_version=f"synthetic-{role}/1",
        raw=raw or f'{{"synthetic_role":"{name}"}}'.encode(),
        declared_record_count=declared_record_count,
    )


def _artifacts() -> tuple[HeldEvaluationArtifact, ...]:
    singles = [
        _artifact(
            role,
            declared_record_count=7 if role == "trial_registry_snapshot" else 1,
        )
        for role in SINGLETON_ROLES
    ]
    return tuple(
        [
            singles[5],
            _artifact("lane_manifest", logical_id="lane-options"),
            *singles[:5],
            _artifact("lane_manifest", logical_id="lane-equity"),
            *singles[6:],
        ]
    )


def _build(
    artifacts: tuple[HeldEvaluationArtifact, ...] | None = None,
) -> EvaluationBundle:
    return build_evaluation_bundle(
        git_commit_sha=COMMIT_SHA,
        git_tree_sha=TREE_SHA,
        evaluator_version=EVALUATOR_VERSION,
        artifacts=artifacts or _artifacts(),
    )


def test_build_is_order_independent_and_binds_every_declared_identity() -> None:
    inputs = _artifacts()
    forward = _build(inputs)
    reverse = _build(tuple(reversed(inputs)))

    assert forward == reverse
    assert forward.schema_version == EVALUATION_BUNDLE_SCHEMA_VERSION
    assert forward.git_commit_sha == COMMIT_SHA
    assert forward.git_tree_sha == TREE_SHA
    assert forward.evaluator_version == EVALUATOR_VERSION
    assert forward.count_semantics == COUNT_SEMANTICS == "declared-metadata-only"
    assert forward.total_trial_count == 7
    assert tuple((item.role, item.logical_id) for item in forward.artifacts) == tuple(
        sorted((item.role, item.logical_id) for item in forward.artifacts)
    )
    assert {item.role for item in forward.artifacts} == {
        *SINGLETON_ROLES,
        "lane_manifest",
    }
    assert all(len(item.raw_sha256) == 64 for item in forward.artifacts)
    assert all(item.byte_count > 0 for item in forward.artifacts)


@pytest.mark.parametrize("removed_role", [*SINGLETON_ROLES, "lane_manifest"])
def test_missing_required_role_refuses(removed_role: str) -> None:
    inputs = list(_artifacts())
    inputs = [item for item in inputs if item.role != removed_role]
    with pytest.raises(EvaluationBundleError, match="missing required role"):
        _build(tuple(inputs))


def test_unknown_role_refuses() -> None:
    inputs = (*_artifacts(), _artifact("future_outcome_summary"))
    with pytest.raises(EvaluationBundleError, match="unknown artifact role"):
        _build(inputs)


@pytest.mark.parametrize("duplicated_role", SINGLETON_ROLES)
def test_duplicate_singleton_role_refuses(duplicated_role: str) -> None:
    original = next(item for item in _artifacts() if item.role == duplicated_role)
    duplicate = replace(original, logical_id=f"second-{duplicated_role}")
    with pytest.raises(EvaluationBundleError, match="must appear exactly once"):
        _build((*_artifacts(), duplicate))


def test_lane_names_must_be_unique() -> None:
    duplicate = _artifact(
        "lane_manifest",
        logical_id="lane-equity",
        raw=b'{"synthetic_role":"different-bytes-same-lane"}',
    )
    with pytest.raises(EvaluationBundleError, match="lane manifest logical_id"):
        _build((*_artifacts(), duplicate))


@pytest.mark.parametrize(
    ("coordinate", "drifted_value"),
    [
        ("git_commit_sha", "c" * 40),
        ("git_tree_sha", "c" * 40),
        ("evaluator_version", "m5-evaluator/2"),
    ],
)
def test_verification_refuses_coordinate_drift(coordinate: str, drifted_value: str) -> None:
    bundle = _build()
    kwargs = {
        "git_commit_sha": COMMIT_SHA,
        "git_tree_sha": TREE_SHA,
        "evaluator_version": EVALUATOR_VERSION,
        "artifacts": _artifacts(),
    }
    kwargs[coordinate] = drifted_value
    with pytest.raises(EvaluationBundleError, match="does not match held inputs"):
        verify_evaluation_bundle(bundle, **kwargs)


@pytest.mark.parametrize("role", [*SINGLETON_ROLES, "lane_manifest"])
def test_verification_refuses_byte_drift_for_every_role(role: str) -> None:
    bundle = _build()
    inputs = list(_artifacts())
    index = next(i for i, item in enumerate(inputs) if item.role == role)
    inputs[index] = replace(inputs[index], raw=inputs[index].raw + b"\n")

    with pytest.raises(EvaluationBundleError, match="does not match held inputs"):
        verify_evaluation_bundle(
            bundle,
            git_commit_sha=COMMIT_SHA,
            git_tree_sha=TREE_SHA,
            evaluator_version=EVALUATOR_VERSION,
            artifacts=tuple(inputs),
        )


@pytest.mark.parametrize("role", ["trial_registry_snapshot", "fold_definitions", "fill_ledger"])
def test_verification_refuses_declared_count_drift(role: str) -> None:
    bundle = _build()
    inputs = list(_artifacts())
    index = next(i for i, item in enumerate(inputs) if item.role == role)
    inputs[index] = replace(
        inputs[index],
        declared_record_count=inputs[index].declared_record_count + 1,
    )

    with pytest.raises(EvaluationBundleError, match="does not match held inputs"):
        verify_evaluation_bundle(
            bundle,
            git_commit_sha=COMMIT_SHA,
            git_tree_sha=TREE_SHA,
            evaluator_version=EVALUATOR_VERSION,
            artifacts=tuple(inputs),
        )


def test_total_trial_count_is_cross_joined_to_bound_metadata() -> None:
    bundle = _build()
    payload = bundle.model_dump()
    payload["total_trial_count"] += 1
    payload["bundle_content_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="trial_registry_snapshot"):
        EvaluationBundle.model_validate(payload)


def test_self_hash_tampering_refuses_even_if_model_copy_bypasses_validation() -> None:
    bundle = _build()
    tampered = bundle.model_copy(update={"bundle_content_sha256": "0" * 64})

    with pytest.raises(EvaluationBundleError, match="bundle_content_sha256"):
        verify_evaluation_bundle(
            tampered,
            git_commit_sha=COMMIT_SHA,
            git_tree_sha=TREE_SHA,
            evaluator_version=EVALUATOR_VERSION,
            artifacts=_artifacts(),
        )


def test_bundle_is_frozen_and_forbids_extra_fields() -> None:
    bundle = _build()
    with pytest.raises(ValidationError, match="frozen"):
        bundle.git_commit_sha = "c" * 40  # type: ignore[misc]

    payload = bundle.model_dump()
    payload["unexpected"] = "not part of the identity"
    with pytest.raises(ValidationError, match="extra"):
        EvaluationBundle.model_validate(payload)
