"""Durable run-state custody for long-running real-data campaigns (PR A/A1)."""

from tree_options.runstate.errors import (
    IllegalTransitionError,
    JournalCorruptError,
    LeaseHeldError,
    ManifestMismatchError,
    ProjectionTornError,
    RunStateError,
    StoreExistsError,
    UnknownRunError,
)
from tree_options.runstate.states import (
    LEGAL_EDGES,
    PROCESS_STATES,
    RESUMABLE_STATES,
    TERMINAL_STATES,
    RunIdentity,
    RunState,
    is_legal,
)
from tree_options.runstate.store import RunStatus, RunStore, compute_run_id

__all__ = [
    "LEGAL_EDGES",
    "PROCESS_STATES",
    "RESUMABLE_STATES",
    "TERMINAL_STATES",
    "IllegalTransitionError",
    "JournalCorruptError",
    "LeaseHeldError",
    "ManifestMismatchError",
    "ProjectionTornError",
    "RunIdentity",
    "RunState",
    "RunStateError",
    "RunStatus",
    "RunStore",
    "StoreExistsError",
    "UnknownRunError",
    "compute_run_id",
    "is_legal",
]
