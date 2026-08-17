"""Trial registry (INV-13 + audit §5.1): state machine, canonical scopes, cap."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from tree_options.registry.budget import TrialBudget
from tree_options.registry.errors import (
    DuplicateTrialError,
    InvalidTransitionError,
    NonCanonicalScopeError,
    ScopeBudgetExceededError,
    UnregisteredOutcomeError,
)
from tree_options.registry.scope import TrialScope
from tree_options.registry.sqlite import TrialRegistry
from tree_options.schemas.trial import TrialRecord

T0 = datetime(2026, 8, 17, 14, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 17, 16, 0, tzinfo=UTC)

SCOPE = TrialScope(
    protocol_id="tree_options",
    protocol_hash="abc123",
    outer_fold_id="fold-0",
    target_horizon="fwd_ret_5",
    feature_set_id="fs-v1",
    model_family="xgb",
)


def _record(trial_id="TRIAL-1", scope_key=None, **over):
    base = dict(
        trial_id=trial_id,
        created_at=T0,
        hypothesis="max_depth sweep on fold 0",
        git_sha="4a3eede",
        config_hash="cfg-1",
        dataset_manifest_hash="ds-1",
        hyperparameters={"max_depth": 4, "learning_rate": "0.05"},
        scope_key=scope_key or SCOPE.scope_key(),
    )
    base.update(over)
    return TrialRecord(**base)


@pytest.fixture()
def registry(tmp_path):
    reg = TrialRegistry(tmp_path / "trials.db")
    yield reg
    reg.close()


class TestCanonicalScopes:
    def test_free_form_scope_rejected(self, registry):
        with pytest.raises(NonCanonicalScopeError) as ei:
            registry.register(_record(scope_key="v0.1.0:fold-0"))
        assert ei.value.code == "NON_CANONICAL_SCOPE"

    def test_scope_hash_is_deterministic_and_field_sensitive(self):
        s2 = TrialScope(
            protocol_id="tree_options",
            protocol_hash="abc123",
            outer_fold_id="fold-1",  # different fold -> different scope
            target_horizon="fwd_ret_5",
            feature_set_id="fs-v1",
            model_family="xgb",
        )
        assert s2.scope_key() != SCOPE.scope_key()
        assert SCOPE.scope_key() == SCOPE.scope_key()


class TestStateMachine:
    def test_outcome_before_registration_rejected(self, registry):
        with pytest.raises(UnregisteredOutcomeError):
            registry.complete("TRIAL-GHOST", "s3://x.json", outcome_at=T1)

    def test_full_lifecycle(self, registry):
        registry.register(_record())
        assert registry.status("TRIAL-1") == "REGISTERED"
        registry.mark_running(
            "TRIAL-1",
            git_sha="4a3eede",
            config_hash="cfg-1",
            dataset_manifest_hash="ds-1",
            at=T1,
        )
        assert registry.status("TRIAL-1") == "RUNNING"
        registry.complete("TRIAL-1", "s3://m.json", outcome_at=T2)
        assert registry.status("TRIAL-1") == "COMPLETED"
        assert registry.metrics_uri("TRIAL-1") == "s3://m.json"

    def test_outcome_requires_running(self, registry):
        registry.register(_record())
        with pytest.raises(InvalidTransitionError) as ei:  # still REGISTERED
            registry.complete("TRIAL-1", "s3://m.json", outcome_at=T2)
        assert ei.value.code == "INVALID_TRANSITION"

    def test_no_double_outcome(self, registry):
        registry.register(_record())
        registry.mark_running(
            "TRIAL-1",
            git_sha="4a3eede",
            config_hash="cfg-1",
            dataset_manifest_hash="ds-1",
            at=T1,
        )
        registry.complete("TRIAL-1", "s3://a.json", outcome_at=T2)
        with pytest.raises(InvalidTransitionError):
            registry.complete("TRIAL-1", "s3://b.json", outcome_at=T2)

    def test_running_requires_matching_provenance(self, registry):
        registry.register(_record())
        with pytest.raises(InvalidTransitionError) as ei:
            registry.mark_running(
                "TRIAL-1",
                git_sha="WRONG",
                config_hash="cfg-1",
                dataset_manifest_hash="ds-1",
                at=T1,
            )
        assert "provenance" in ei.value.detail

    def test_failure_path(self, registry):
        registry.register(_record())
        registry.mark_running(
            "TRIAL-1",
            git_sha="4a3eede",
            config_hash="cfg-1",
            dataset_manifest_hash="ds-1",
            at=T1,
        )
        registry.fail("TRIAL-1", "diverged", at=T2)
        assert registry.status("TRIAL-1") == "FAILED"
        with pytest.raises(InvalidTransitionError):  # terminal
            registry.complete("TRIAL-1", "s3://a.json", outcome_at=T2)

    def test_duplicate_trial_id_rejected(self, registry):
        registry.register(_record())
        with pytest.raises(DuplicateTrialError) as ei:
            registry.register(_record())
        assert ei.value.code == "DUPLICATE_TRIAL_ID"

    def test_append_only_event_history(self, registry):
        registry.register(_record())
        registry.mark_running(
            "TRIAL-1",
            git_sha="4a3eede",
            config_hash="cfg-1",
            dataset_manifest_hash="ds-1",
            at=T1,
        )
        registry.complete("TRIAL-1", "s3://m.json", outcome_at=T2)
        kinds = [k for k, _payload in registry.events("TRIAL-1")]
        assert kinds == ["REGISTERED", "RUNNING", "COMPLETED"]


class TestBudgetAndConcurrency:
    def test_scope_cap_32_enforced(self, registry):
        budget = TrialBudget(cap=32)
        for i in range(32):
            registry.register(_record(trial_id=f"TRIAL-{i}"), budget=budget)
        with pytest.raises(ScopeBudgetExceededError) as ei:
            registry.register(_record(trial_id="TRIAL-33"), budget=budget)
        assert ei.value.code == "SCOPE_BUDGET_EXCEEDED"
        assert registry.count_scope(SCOPE.scope_key()) == 32

    def test_two_connections_cannot_exceed_cap(self, tmp_path):
        """Two live connections race registrations; the cap holds."""
        import threading

        path = tmp_path / "trials.db"
        reg1 = TrialRegistry(path)
        reg2 = TrialRegistry(path)
        budget = TrialBudget(cap=10)
        for i in range(10):
            reg1.register(_record(trial_id=f"T-{i}"), budget=budget)

        results = []

        def try_register(reg, trial_id):
            try:
                reg.register(_record(trial_id=trial_id), budget=budget)
                results.append("registered")
            except (ScopeBudgetExceededError, sqlite3.OperationalError):
                results.append("rejected")

        t1 = threading.Thread(target=try_register, args=(reg1, "T-99"))
        t2 = threading.Thread(target=try_register, args=(reg2, "T-98"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert reg1.count_scope(SCOPE.scope_key()) == 10  # cap never exceeded
        reg1.close()
        reg2.close()

    def test_cap_from_protocol_is_32(self, protocol):
        assert protocol.inner_loop.max_registered_configs == 32


class TestStorageIntegrity:
    def test_wal_mode_enabled(self, registry):
        assert registry.journal_mode() == "wal"

    def test_short_hypothesis_rejected_by_sql_check(self, registry):
        with pytest.raises(Exception, match=r"CHECK|check"):
            registry._execute_raw(
                "INSERT INTO trials (trial_id, scope_key, hypothesis, hyperparameters_json,"
                " git_sha, config_hash, dataset_manifest_hash, registered_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("TRIAL-X", "scope-v1:" + "0" * 64, "short", "{}", "g", "c", "d", T0.isoformat()),
            )

    def test_persistence_across_reopen(self, tmp_path):
        path = tmp_path / "trials.db"
        reg = TrialRegistry(path)
        reg.register(_record())
        reg.close()
        reg2 = TrialRegistry(path)
        assert reg2.count_scope(SCOPE.scope_key()) == 1
        reg2.close()
