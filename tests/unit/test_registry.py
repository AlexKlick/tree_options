"""Trial registry (INV-13 + audit §5.1): state machine, canonical scopes, cap."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from tree_options.registry.budget import TrialBudget
from tree_options.registry.errors import (
    DuplicateTrialError,
    InvalidTransitionError,
    NonCanonicalScopeError,
    RegistryError,
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
            registry.register(_record(scope_key="v0.1.0:fold-0"), scope=SCOPE)
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
        registry.register(_record(), scope=SCOPE)
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
        registry.register(_record(), scope=SCOPE)
        with pytest.raises(InvalidTransitionError) as ei:  # still REGISTERED
            registry.complete("TRIAL-1", "s3://m.json", outcome_at=T2)
        assert ei.value.code == "INVALID_TRANSITION"

    def test_no_double_outcome(self, registry):
        registry.register(_record(), scope=SCOPE)
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
        registry.register(_record(), scope=SCOPE)
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
        registry.register(_record(), scope=SCOPE)
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
        registry.register(_record(), scope=SCOPE)
        with pytest.raises(DuplicateTrialError) as ei:
            registry.register(_record(), scope=SCOPE)
        assert ei.value.code == "DUPLICATE_TRIAL_ID"

    def test_append_only_event_history(self, registry):
        registry.register(_record(), scope=SCOPE)
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
    def test_scope_cap_32_enforced(self, tmp_path):
        registry = TrialRegistry(tmp_path / "t32.db", budget=TrialBudget(cap=32))
        for i in range(32):
            registry.register(_record(trial_id=f"TRIAL-{i}"), scope=SCOPE)
        with pytest.raises(ScopeBudgetExceededError) as ei:
            registry.register(_record(trial_id="TRIAL-33"), scope=SCOPE)
        assert ei.value.code == "SCOPE_BUDGET_EXCEEDED"
        assert registry.count_scope(SCOPE.scope_key()) == 32
        registry.close()

    def test_two_connections_cannot_exceed_cap(self, tmp_path):
        """Two live connections race registrations; the cap holds."""
        import threading

        path = tmp_path / "trials.db"
        budget = TrialBudget(cap=10)
        reg1 = TrialRegistry(path, budget=budget)
        reg2 = TrialRegistry(path, budget=budget)
        for i in range(10):
            reg1.register(_record(trial_id=f"T-{i}"), scope=SCOPE)

        results = []

        def try_register(reg, trial_id):
            try:
                reg.register(_record(trial_id=trial_id), scope=SCOPE)
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
                "INSERT INTO trials (trial_id, scope_key, scope_json, hypothesis,"
                " hyperparameters_json, git_sha, config_hash, dataset_manifest_hash,"
                " registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "TRIAL-X",
                    "scope-v1:" + "0" * 64,
                    "{}",
                    "short",
                    "{}",
                    "g",
                    "c",
                    "d",
                    T0.isoformat(),
                ),
            )

    def test_persistence_across_reopen(self, tmp_path):
        path = tmp_path / "trials.db"
        reg = TrialRegistry(path)
        reg.register(_record(), scope=SCOPE)
        reg.close()
        reg2 = TrialRegistry(path)
        assert reg2.count_scope(SCOPE.scope_key()) == 1
        reg2.close()


class TestRegistryHardening:
    """Review F13 (scope derivation + mandatory cap) and F14 (CAS transitions)."""

    def test_cap_enforced_without_caller_budget(self, registry):
        """The registry owns a default budget: 33 registrations in one scope
        fail even when no budget argument is supplied."""
        for i in range(32):
            registry.register(_record(trial_id=f"T-{i}"), scope=SCOPE)
        with pytest.raises(ScopeBudgetExceededError):
            registry.register(_record(trial_id="T-32"), scope=SCOPE)

    def test_scope_key_must_derive_from_presented_scope(self, registry):
        """A syntactically valid hash that does not derive from the presented
        TrialScope is rejected — the registry recomputes and compares."""
        forged = "scope-v1:" + "a" * 64
        with pytest.raises(NonCanonicalScopeError) as ei:
            registry.register(_record(trial_id="T-FORGE", scope_key=forged), scope=SCOPE)
        assert ei.value.code == "NON_CANONICAL_SCOPE"

    def test_missing_scope_rejected(self, registry):
        """The error must be the PRESENTATION requirement itself, with the
        distinctive phrase, so it cannot be confused with the downstream
        derivation-check rejection (round 3: this defense has no single-mutant
        kill — the derivation check subsumes the same input — so it is proven
        by this direct test; M33 instead guards storage fidelity)."""
        with pytest.raises(NonCanonicalScopeError) as ei:
            registry.register(_record())
        assert "must be presented at registration" in ei.value.detail

    def test_per_call_budget_override_is_gone(self, registry):
        """F13: the budget is the registry's own — there is no per-call
        parameter to loosen. Passing one is a TypeError (the cap ceiling
        itself is proven by test_budget_cap_cannot_exceed_protocol_maximum)."""
        with pytest.raises(TypeError):
            registry.register(_record(), scope=SCOPE, budget=TrialBudget(cap=10))

    def test_budget_cap_cannot_exceed_protocol_maximum(self):
        """Round-3 F13: a scope budget may TIGHTEN below the protocol's 32
        but never loosen it — the constructor itself refuses a larger cap,
        closing the TrialRegistry(budget=TrialBudget(cap=1000)) path."""
        with pytest.raises(ValueError):
            TrialBudget(cap=1000)
        with pytest.raises(ValueError):
            TrialBudget(cap=33)
        assert TrialBudget(cap=32).cap == 32
        assert TrialBudget(cap=10).cap == 10  # tightening stays allowed

    def test_budget_cap_must_be_an_integer(self):
        """Round-4 NEW-5: the cap is an integer commitment. A float slips
        past both bound comparisons — float("nan") compared False against
        >= 1 AND <= 32, then made every `count >= nan` check False,
        disabling the cap entirely; 2.5 registered a third trial; True
        aliased cap=1. Non-integer caps are refused at construction."""
        with pytest.raises(ValueError):
            TrialBudget(cap=float("nan"))
        with pytest.raises(ValueError):
            TrialBudget(cap=float("2.5"))
        with pytest.raises(ValueError):
            TrialBudget(cap=True)  # bool is an int subclass — no silent cap=1

    def test_budget_cap_is_immutable_after_construction(self, tmp_path):
        """Round-5 NEW-6: `registry.budget.cap = float("nan")` re-disabled
        the cap AFTER construction — the bounds were checked once, then the
        stored value was freely mutable. The cap is read-only once set:
        assignment raises, both on a directly-constructed budget and on
        the registry's own default budget."""
        with pytest.raises(AttributeError):
            TrialBudget(cap=10).cap = float("nan")
        reg = TrialRegistry(tmp_path / "t-imm.db")
        with pytest.raises(AttributeError):
            reg.budget.cap = float("nan")
        assert reg.budget.cap == 32  # the registry default, untouched
        reg.close()

    def test_poisoned_backing_field_fails_closed(self, tmp_path):
        """Round-6 NEW-8: `object.__setattr__(budget, "_cap", nan)` poisons
        the backing field PAST the read-only property — and a plain
        `budget._cap = nan` does the same. check() therefore re-validates
        the cap at the enforcement point: a tampered value refuses
        registration instead of running without a bounded commitment."""
        budget = TrialBudget(cap=32)
        object.__setattr__(budget, "_cap", float("nan"))
        reg = TrialRegistry(tmp_path / "t-tamper.db", budget=budget)
        with pytest.raises(RegistryError) as ei:
            reg.register(_record(), scope=SCOPE)
        assert ei.value.code == "BUDGET_TAMPERED"
        reg.close()

    def test_registry_budget_reference_is_read_only(self, tmp_path):
        """Round-6 NEW-8: swapping the registry's budget object after
        construction (cap=10 -> cap=32) would loosen a pre-registered
        tightening — the peek-and-extend INV-13 exists to prevent. The
        registry's policy object is fixed at construction."""
        reg = TrialRegistry(tmp_path / "t-swap.db", budget=TrialBudget(cap=10))
        with pytest.raises(AttributeError):
            reg.budget = TrialBudget(cap=32)
        assert reg.budget.cap == 10
        reg.close()

    def test_committed_cap_cannot_be_loosened_mid_scope(self, tmp_path):
        """Round-7 residual: `budget._cap = 32` after registering under
        cap=10 is an IN-RANGE loosening — domain validation alone passes
        it, and 10 >= 32 is false, so the 11th registers. The cap is a
        COMMITMENT: fixed into the DB at the scope's first registration;
        a live budget that disagrees with the recorded commitment refuses
        registration."""
        budget = TrialBudget(cap=10)
        reg = TrialRegistry(tmp_path / "t-commit.db", budget=budget)
        for i in range(10):
            reg.register(_record(trial_id=f"T-{i}"), scope=SCOPE)
        budget._cap = 32  # direct private write — in-range loosening
        with pytest.raises(RegistryError) as ei:
            reg.register(_record(trial_id="T-10"), scope=SCOPE)
        assert ei.value.code == "BUDGET_COMMITMENT_CHANGED"
        reg.close()

    def test_swapped_budget_reference_refuses(self, tmp_path):
        """Round-7 residual: `reg._budget = TrialBudget(cap=32)` replaces
        the policy object with no code substitution. The recorded
        commitment catches the disagreement."""
        reg = TrialRegistry(tmp_path / "t-swap2.db", budget=TrialBudget(cap=10))
        reg.register(_record(), scope=SCOPE)
        reg._budget = TrialBudget(cap=32)
        with pytest.raises(RegistryError) as ei:
            reg.register(_record(trial_id="T-2"), scope=SCOPE)
        assert ei.value.code == "BUDGET_COMMITMENT_CHANGED"
        reg.close()

    def test_pre_remediation_database_fails_closed(self, tmp_path):
        """An existing DB written before scope_json existed cannot be opened:
        CREATE TABLE IF NOT EXISTS would not migrate it, so refuse."""
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE trials (trial_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL,"
            " hypothesis TEXT NOT NULL, hyperparameters_json TEXT NOT NULL,"
            " git_sha TEXT NOT NULL, config_hash TEXT NOT NULL,"
            " dataset_manifest_hash TEXT NOT NULL, registered_at TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'REGISTERED')"
        )
        conn.commit()
        conn.close()
        with pytest.raises(RegistryError) as ei:
            TrialRegistry(path)
        assert ei.value.code == "REGISTRY_SCHEMA_TOO_OLD"

    def test_stored_scope_json_verifiable(self, registry):
        registry.register(_record(), scope=SCOPE)
        stored = registry.scope_json("TRIAL-1")
        assert TrialScope(**json.loads(stored)) == SCOPE
        assert TrialScope(**json.loads(stored)).scope_key() == SCOPE.scope_key()

    def test_concurrent_completion_single_winner(self, tmp_path):
        """Two connections race complete() on a RUNNING trial: exactly one
        wins, one COMPLETED event, metrics_uri is the winner's."""
        import threading

        path = tmp_path / "trials.db"
        reg1 = TrialRegistry(path)
        reg2 = TrialRegistry(path)
        reg1.register(_record(), scope=SCOPE)
        reg1.mark_running(
            "TRIAL-1",
            git_sha="4a3eede",
            config_hash="cfg-1",
            dataset_manifest_hash="ds-1",
            at=T1,
        )
        outcomes = []

        def try_complete(reg, uri):
            try:
                reg.complete("TRIAL-1", uri, outcome_at=T2)
                outcomes.append("won")
            except InvalidTransitionError:
                outcomes.append("lost")

        t1 = threading.Thread(target=try_complete, args=(reg1, "s3://a.json"))
        t2 = threading.Thread(target=try_complete, args=(reg2, "s3://b.json"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert sorted(outcomes) == ["lost", "won"]
        assert reg1.status("TRIAL-1") == "COMPLETED"
        completed_events = [k for k, _ in reg1.events("TRIAL-1") if k == "COMPLETED"]
        assert len(completed_events) == 1
        winner = reg1.metrics_uri("TRIAL-1")
        assert winner in {"s3://a.json", "s3://b.json"}
        reg1.close()
        reg2.close()
