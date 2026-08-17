"""Trial registry (INV-13): register before outcome, duplicate reject, 32-cap."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tree_options.registry.budget import TrialBudget
from tree_options.registry.sqlite import (
    DuplicateTrialError,
    OutcomeAlreadyRecordedError,
    ScopeBudgetExceededError,
    TrialRegistry,
    UnregisteredOutcomeError,
)
from tree_options.schemas.trial import TrialRecord

T0 = datetime(2026, 8, 17, 14, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)


def _record(trial_id="TRIAL-1", scope_key="v0.1.0:fold-0", **over):
    base = dict(
        trial_id=trial_id,
        created_at=T0,
        hypothesis="max_depth sweep on fold 0",
        git_sha="da79ab2",
        config_hash="cfg-1",
        dataset_manifest_hash="ds-1",
        hyperparameters={"max_depth": 4, "learning_rate": "0.05"},
        scope_key=scope_key,
    )
    base.update(over)
    return TrialRecord(**base)


@pytest.fixture()
def registry(tmp_path):
    reg = TrialRegistry(tmp_path / "trials.db")
    yield reg
    reg.close()


class TestRegisterBeforeOutcome:
    def test_outcome_before_registration_rejected(self, registry):
        with pytest.raises(UnregisteredOutcomeError) as ei:
            registry.record_outcome("TRIAL-GHOST", "s3://metrics/TRIAL-GHOST.json", outcome_at=T1)
        assert ei.value.code == "OUTCOME_BEFORE_REGISTRATION"

    def test_register_then_outcome_ok(self, registry):
        registry.register(_record())
        registry.record_outcome("TRIAL-1", "s3://metrics/TRIAL-1.json", outcome_at=T1)
        assert registry.has_outcome("TRIAL-1")
        assert registry.metrics_uri("TRIAL-1") == "s3://metrics/TRIAL-1.json"

    def test_double_outcome_rejected(self, registry):
        registry.register(_record())
        registry.record_outcome("TRIAL-1", "s3://a.json", outcome_at=T1)
        with pytest.raises(OutcomeAlreadyRecordedError) as ei:
            registry.record_outcome("TRIAL-1", "s3://b.json", outcome_at=T1)
        assert ei.value.code == "OUTCOME_ALREADY_RECORDED"

    def test_duplicate_trial_id_rejected(self, registry):
        registry.register(_record())
        with pytest.raises(DuplicateTrialError) as ei:
            registry.register(_record())  # identical id, different hypothesis
        assert ei.value.code == "DUPLICATE_TRIAL_ID"


class TestBudget:
    def test_scope_cap_32_enforced(self, registry, tmp_path):
        budget = TrialBudget(cap=32)
        for i in range(32):
            registry.register(_record(trial_id=f"TRIAL-{i}"), budget=budget)
        assert registry.count_scope("v0.1.0:fold-0") == 32
        with pytest.raises(ScopeBudgetExceededError) as ei:
            registry.register(_record(trial_id="TRIAL-33"), budget=budget)
        assert ei.value.code == "SCOPE_BUDGET_EXCEEDED"
        assert registry.count_scope("v0.1.0:fold-0") == 32  # rejected write is gone

    def test_scopes_are_independent(self, registry):
        budget = TrialBudget(cap=2)
        registry.register(_record(scope_key="v0.1.0:fold-0"), budget=budget)
        registry.register(_record(trial_id="TRIAL-B", scope_key="v0.1.0:fold-1"), budget=budget)

    def test_cap_from_protocol_is_32(self, protocol):
        assert protocol.inner_loop.max_registered_configs == 32


class TestStorageIntegrity:
    def test_wal_mode_enabled(self, registry):
        assert registry.journal_mode() == "wal"

    def test_short_hypothesis_rejected_by_sql_check(self, registry, tmp_path):
        """The DB-level CHECK is the last line of defense for INV-13 hygiene."""
        with pytest.raises(Exception, match=r"CHECK|check"):
            registry._execute_raw(
                "INSERT INTO trials (trial_id, scope_key, hypothesis, hyperparameters_json,"
                " registered_at) VALUES (?, ?, ?, ?, ?)",
                ("TRIAL-X", "s", "short", "{}", T0.isoformat()),
            )

    def test_hyperparameters_must_be_json(self, registry):
        with pytest.raises(Exception, match=r"CHECK|check"):
            registry._execute_raw(
                "INSERT INTO trials (trial_id, scope_key, hypothesis, hyperparameters_json,"
                " registered_at) VALUES (?, ?, ?, ?, ?)",
                ("TRIAL-X", "s", "a long enough hypothesis", "{not json", T0.isoformat()),
            )

    def test_persistence_across_reopen(self, tmp_path):
        path = tmp_path / "trials.db"
        reg = TrialRegistry(path)
        reg.register(_record())
        reg.close()
        reg2 = TrialRegistry(path)
        assert reg2.count_scope("v0.1.0:fold-0") == 1
        reg2.close()
