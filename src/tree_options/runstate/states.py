"""Typed lifecycle for long-running real-data campaigns (PR A / A1).

Sixteen states, one append-only journal, and a fail-closed transition table.
Two rules carry the whole design:

1. **UNKNOWN is a classification, never a journal target.** A timed-out or
   disconnected process is UNKNOWN (constraint 10 of the M4C handoff) — but
   UNKNOWN is what an OBSERVER concludes from liveness evidence; writing it
   into the journal would let a stale probe freeze the run forever. The
   journal records only deliberate transitions by machinery or operators;
   UNKNOWN appears in the *projection* via `store.status()`.
2. **The sealed lane is one-shot.** `FAILED` may never reach any `SEALED_*`
   state. A crash after authority consumption is UNKNOWN /
   RECONCILIATION_REQUIRED, never a retry — `seal/ledger.py` owns that
   refusal; this table refusing the edge is the lifecycle-side backstop.

`RunIdentity` is written once, at PLANNED, and never rewritten: it pins what
the run believed at creation (code, protocol, universe, capture manifest).
Later process identities (resumes after reboot, operator marks) live in the
lease and the journal's per-record actor fields, not here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from tree_options.schemas.common import IdStr, StrictModel


class RunState(StrEnum):
    PLANNED = "PLANNED"
    CAPTURING = "CAPTURING"
    CAPTURE_COMPLETE = "CAPTURE_COMPLETE"
    INSPECTION_RUNNING = "INSPECTION_RUNNING"
    INSPECTION_FAILED = "INSPECTION_FAILED"
    INSPECTED = "INSPECTED"
    AMENDMENT_PENDING_OWNER = "AMENDMENT_PENDING_OWNER"
    AMENDMENT_READY = "AMENDMENT_READY"
    BARS_READY = "BARS_READY"
    BARS_CAPTURING = "BARS_CAPTURING"
    BARS_COMPLETE = "BARS_COMPLETE"
    SEALED_PREFLIGHT_READY = "SEALED_PREFLIGHT_READY"
    SEALED_RUNNING = "SEALED_RUNNING"
    SEALED_COMPLETE = "SEALED_COMPLETE"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


# Monotone rank. A legal edge must be explicitly whitelisted AND (except for
# the named retry edges below) strictly forward in this order, so widening
# the whitelist alone can never legalize a skip.
STATE_ORDER: dict[RunState, int] = {
    state: rank
    for rank, state in enumerate(
        (
            RunState.PLANNED,
            RunState.CAPTURING,
            RunState.CAPTURE_COMPLETE,
            RunState.INSPECTION_RUNNING,
            RunState.INSPECTION_FAILED,
            RunState.INSPECTED,
            RunState.AMENDMENT_PENDING_OWNER,
            RunState.AMENDMENT_READY,
            RunState.BARS_READY,
            RunState.BARS_CAPTURING,
            RunState.BARS_COMPLETE,
            RunState.SEALED_PREFLIGHT_READY,
            RunState.SEALED_RUNNING,
            RunState.SEALED_COMPLETE,
            RunState.FAILED,
        )
    )
}

# Explicit retry edges: a FAILED capture/inspection may restart its own lane
# (the content-addressed cache makes that free), and an INSPECTION_FAILED
# inspection may re-run. Deliberately ABSENT: anything into SEALED_* — the
# sealed event is one-shot (see module docstring, rule 2).
_RETRY_EDGES: dict[RunState, frozenset[RunState]] = {
    RunState.FAILED: frozenset(
        {RunState.CAPTURING, RunState.INSPECTION_RUNNING, RunState.BARS_CAPTURING}
    ),
    RunState.INSPECTION_FAILED: frozenset({RunState.INSPECTION_RUNNING}),
}

_FORWARD_EDGES: dict[RunState, frozenset[RunState]] = {
    RunState.PLANNED: frozenset({RunState.CAPTURING}),
    RunState.CAPTURING: frozenset({RunState.CAPTURE_COMPLETE, RunState.FAILED}),
    RunState.CAPTURE_COMPLETE: frozenset({RunState.INSPECTION_RUNNING}),
    RunState.INSPECTION_RUNNING: frozenset({RunState.INSPECTED, RunState.INSPECTION_FAILED}),
    # INSPECTION_FAILED otherwise has no forward edge: a failed inspection
    # either re-runs (the retry edge above) or ends the run as FAILED.
    RunState.INSPECTION_FAILED: frozenset({RunState.FAILED}),
    RunState.INSPECTED: frozenset({RunState.AMENDMENT_PENDING_OWNER}),
    RunState.AMENDMENT_PENDING_OWNER: frozenset({RunState.AMENDMENT_READY}),
    RunState.AMENDMENT_READY: frozenset({RunState.BARS_READY}),
    RunState.BARS_READY: frozenset({RunState.BARS_CAPTURING}),
    RunState.BARS_CAPTURING: frozenset({RunState.BARS_COMPLETE, RunState.FAILED}),
    RunState.BARS_COMPLETE: frozenset({RunState.SEALED_PREFLIGHT_READY}),
    RunState.SEALED_PREFLIGHT_READY: frozenset({RunState.SEALED_RUNNING}),
    RunState.SEALED_RUNNING: frozenset({RunState.SEALED_COMPLETE}),
    RunState.SEALED_COMPLETE: frozenset(),  # terminal: nothing follows
    RunState.FAILED: frozenset(),
}

LEGAL_EDGES: dict[RunState, frozenset[RunState]] = {
    source: _FORWARD_EDGES[source] | _RETRY_EDGES.get(source, frozenset())
    for source in _FORWARD_EDGES
}

TERMINAL_STATES: frozenset[RunState] = frozenset({RunState.SEALED_COMPLETE, RunState.FAILED})

# States whose progress is owned by a live process; a dead/stale heartbeat
# here is UNKNOWN (resumable for the capture/inspection lanes, reconciliation
# for the sealed lane), never FAILED.
PROCESS_STATES: frozenset[RunState] = frozenset(
    {
        RunState.CAPTURING,
        RunState.INSPECTION_RUNNING,
        RunState.BARS_CAPTURING,
        RunState.SEALED_RUNNING,
    }
)

# Lanes where a dead process leaves work safely resumable from the
# content-addressed cache. SEALED_RUNNING is deliberately absent: authority
# was consumed; only reconciliation may proceed.
RESUMABLE_STATES: frozenset[RunState] = frozenset(
    {RunState.CAPTURING, RunState.INSPECTION_RUNNING, RunState.BARS_CAPTURING}
)


def is_legal(source: RunState, target: RunState) -> bool:
    """Fail-closed edge check: explicit whitelist AND monotone order.

    UNKNOWN is refused as a target everywhere (rule 1 in the module
    docstring): an observer's classification never mutates the record of
    what the run actually did.
    """
    if target is RunState.UNKNOWN or source is RunState.UNKNOWN:
        return False
    if target not in LEGAL_EDGES.get(source, frozenset()):
        return False
    # Retry edges are the only sanctioned regressions/side-steps; every
    # other edge must move strictly forward in STATE_ORDER.
    if target in _RETRY_EDGES.get(source, frozenset()):
        return True
    return STATE_ORDER[target] > STATE_ORDER[source]


class RunIdentity(StrictModel):
    """Immutable creation identity of one campaign run."""

    run_id: IdStr
    campaign: IdStr
    protocol_hash: str
    code_sha: str
    provider: IdStr
    capture_version: IdStr
    universe_manifest_sha256: str
    # The capture manifest does not exist at PLANNED time; it is pinned into
    # the journal (MANIFEST_PINNED) the first time the capture exits.
    capture_manifest_sha256: str | None = None
    boot_id: str
    pid: int
    pid_start_ticks: int
    started_epoch: int = Field(ge=0)
    args_hash: str
