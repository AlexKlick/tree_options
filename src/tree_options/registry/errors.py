"""Registry error family (INV-13): shared by sqlite and budget modules."""

from __future__ import annotations


class RegistryError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class DuplicateTrialError(RegistryError):
    def __init__(self, trial_id: str) -> None:
        super().__init__("DUPLICATE_TRIAL_ID", f"trial {trial_id} already registered")


class UnregisteredOutcomeError(RegistryError):
    def __init__(self, trial_id: str) -> None:
        super().__init__(
            "OUTCOME_BEFORE_REGISTRATION",
            f"outcome recorded for {trial_id} which was never registered",
        )


class OutcomeAlreadyRecordedError(RegistryError):
    def __init__(self, trial_id: str) -> None:
        super().__init__("OUTCOME_ALREADY_RECORDED", f"trial {trial_id} already has an outcome")


class ScopeBudgetExceededError(RegistryError):
    def __init__(self, scope_key: str, cap: int) -> None:
        super().__init__(
            "SCOPE_BUDGET_EXCEEDED",
            f"scope {scope_key} already holds {cap} registered configs (cap {cap})",
        )


class NonCanonicalScopeError(RegistryError):
    def __init__(self, scope_key: str, detail: str) -> None:
        super().__init__("NON_CANONICAL_SCOPE", f"{detail}: {scope_key[:40]}")


class InvalidTransitionError(RegistryError):
    def __init__(self, trial_id: str, detail: str) -> None:
        super().__init__("INVALID_TRANSITION", f"trial {trial_id}: {detail}")
