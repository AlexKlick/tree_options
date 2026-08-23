"""Run-state error family: shared by the journal, lease, and store modules.

Every refusal names its code so an operator reading a log can tell a
duplicate launcher (LEASE_HELD) from a corrupted journal (JOURNAL_CORRUPT)
from an illegal lifecycle move (ILLEGAL_TRANSITION) without archaeology.
"""

from __future__ import annotations


class RunStateError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class IllegalTransitionError(RunStateError):
    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__("ILLEGAL_TRANSITION", f"run {run_id}: {detail}")


class LeaseHeldError(RunStateError):
    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(
            "LEASE_HELD",
            f"run {run_id}: lease held by a live owner ({detail}); "
            "refusing a second launcher — investigate before adopting",
        )


class JournalCorruptError(RunStateError):
    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(
            "JOURNAL_CORRUPT",
            f"run {run_id}: {detail}; a mid-file journal record failed "
            "verification — the store is evidence and is never auto-repaired",
        )


class ProjectionTornError(RunStateError):
    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(
            "PROJECTION_TORN",
            f"run {run_id}: {detail}; the projection can be rebuilt from the "
            "journal, but only a caller that is allowed to WRITE may do it "
            "(a status command reports instead)",
        )


class UnknownRunError(RunStateError):
    def __init__(self, run_id: str) -> None:
        super().__init__("UNKNOWN_RUN", f"no run-state store for {run_id!r}")


class StoreExistsError(RunStateError):
    def __init__(self, run_id: str) -> None:
        super().__init__(
            "STORE_EXISTS",
            f"a store for {run_id!r} already exists; open it — never recreate "
            "over a live run's evidence",
        )


class ManifestMismatchError(RunStateError):
    def __init__(self, run_id: str, pinned: str, observed: str) -> None:
        super().__init__(
            "MANIFEST_MISMATCH",
            f"run {run_id}: pinned capture manifest {pinned[:12]}… but the "
            f"on-disk manifest hashes {observed[:12]}…; resume refuses. The "
            "manifest is DERIVED evidence — re-derive it by re-running the "
            "capture (the cache makes that free), never hand-edit it",
        )
