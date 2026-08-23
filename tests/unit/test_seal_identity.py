"""SealedIdentity + the two domain-separated ids (run id vs content identity)."""

from __future__ import annotations

import pytest

from tree_options.seal.identity import (
    CALENDAR_PENDING,
    CONTENT_IDENTITY_DOMAIN,
    RUNNER_VERSION,
    SEALED_RUN_DOMAIN,
    SealedIdentity,
    content_identity,
    sealed_run_id,
)


def _identity(**overrides: str) -> SealedIdentity:
    fields = dict(
        code_sha="a" * 40,
        protocol_hash="b" * 64,
        lane1_manifest_sha256="c" * 64,
        lane2_manifest_sha256="d" * 64,
        calendar_decision="repo-generated-calendar",
        criteria_sha256="e" * 64,
    )
    fields.update(overrides)
    return SealedIdentity(**fields)


def test_ids_are_deterministic_for_equal_identities():
    assert sealed_run_id(_identity()) == sealed_run_id(_identity())
    assert content_identity(_identity()) == content_identity(_identity())


def test_run_id_differs_from_content_identity():
    # Different domains over (almost) the same payload: they can never collide.
    assert sealed_run_id(_identity()) != content_identity(_identity())


def test_content_identity_stable_across_code_sha_change_while_run_id_changes():
    i1 = _identity(code_sha="1" * 40)
    i2 = _identity(code_sha="2" * 40)
    assert sealed_run_id(i1) != sealed_run_id(i2)
    assert content_identity(i1) == content_identity(i2)


def test_content_identity_changes_when_content_fields_change():
    # code_sha is the ONLY blanked field: any research-content change moves
    # BOTH ids.
    base = _identity()
    assert content_identity(base) != content_identity(
        _identity(calendar_decision="weekend-only-accepted")
    )
    assert content_identity(base) != content_identity(_identity(criteria_sha256="f" * 64))
    assert sealed_run_id(base) != sealed_run_id(_identity(lane2_manifest_sha256="0" * 64))


def test_runner_version_default_names_the_machinery():
    assert _identity().runner_version == "m4-g4-runner/1"
    assert RUNNER_VERSION == "m4-g4-runner/1"


def test_domains_are_distinct_constants():
    assert SEALED_RUN_DOMAIN != CONTENT_IDENTITY_DOMAIN
    assert SEALED_RUN_DOMAIN == b"tree-options-g4-seal-run-v1"
    assert CONTENT_IDENTITY_DOMAIN == b"tree-options-g4-content-v1"


def test_pending_calendar_token_is_not_a_decision():
    assert CALENDAR_PENDING == "PENDING"
    # The token round-trips through the identity; "unavailable" is preflight's
    # classification of it, never a value the identity silently rewrites.
    assert _identity(calendar_decision=CALENDAR_PENDING).calendar_decision == "PENDING"


def test_identity_is_strict_no_extra_fields():
    with pytest.raises(Exception, match="extra"):
        SealedIdentity(
            code_sha="a" * 40,
            protocol_hash="b" * 64,
            lane1_manifest_sha256="c" * 64,
            lane2_manifest_sha256="d" * 64,
            calendar_decision="x",
            criteria_sha256="e" * 64,
            surprise="not part of the seal",
        )
