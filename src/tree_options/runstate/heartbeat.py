"""Heartbeat + staleness classification (constraint 10: UNKNOWN, never FAILED).

A heartbeat is a small atomically-written JSON file refreshed while a
process works. Its ABSENCE or staleness is evidence about liveness, not
about outcome: a disconnected process is `UNKNOWN_*`, and FAILED appears
only from an explicit journal transition, never from silence. The 900 s
stale threshold comfortably exceeds the capture lane's worst quiet stretch
(12 s governor sleeps times multi-attempt backoffs around long pages).
All math is integer epoch seconds (the repo AST-ban forbids timedelta
arithmetic outside `time/`).
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path

from tree_options.runstate.lease import proc_pid_alive, proc_start_ticks
from tree_options.runstate.states import (
    PROCESS_STATES,
    RESUMABLE_STATES,
    TERMINAL_STATES,
    RunState,
)
from tree_options.schemas.common import StrictModel

HEARTBEAT_INTERVAL_S = 60
STALE_AFTER_S = 900

HEARTBEAT_FILENAME = "heartbeat.json"


class Heartbeat(StrictModel):
    state: RunState
    pid: int
    pid_start_ticks: int
    boot_id: str
    at_epoch: int


class HeartbeatClass(StrEnum):
    ALIVE = "ALIVE"  # no liveness concern (fresh beat, or no process expected)
    ALIVE_SILENT = "ALIVE_SILENT"  # pid alive, beat stale: watch, do not act
    DEAD_TERMINAL = "DEAD_TERMINAL"  # process done; state agrees
    UNKNOWN_RESUMABLE = "UNKNOWN_RESUMABLE"  # dead mid-capture/inspection: cache resumes
    UNKNOWN_RECONCILIATION_REQUIRED = "UNKNOWN_RECONCILIATION_REQUIRED"  # sealed lane


def write(store_dir: Path, beat: Heartbeat) -> None:
    tmp = store_dir / f".{HEARTBEAT_FILENAME}.{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps(json.loads(beat.model_dump_json()), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, store_dir / HEARTBEAT_FILENAME)


def read(store_dir: Path) -> Heartbeat | None:
    path = store_dir / HEARTBEAT_FILENAME
    try:
        return Heartbeat.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _owner_gone(beat: Heartbeat, *, boot_id_now: str, proc_root: Path | None) -> bool:
    if beat.boot_id != boot_id_now:
        return True
    if not proc_pid_alive(beat.pid, proc_root):
        return True
    live_ticks = proc_start_ticks(beat.pid, proc_root)
    return live_ticks is None or live_ticks != beat.pid_start_ticks


def classify(
    beat: Heartbeat | None,
    state: RunState | None,
    *,
    now_epoch: int,
    boot_id_now: str,
    proc_root: Path | None = None,
    stale_after_s: int = STALE_AFTER_S,
) -> HeartbeatClass:
    """Classify liveness. Never returns FAILED-equivalent for silence.

    Round-1 review fix (2026-08-23, probe MISMATCHED_HEARTBEAT_CLASS):
    a heartbeat carries its recorded state, and the JOURNAL projection is
    the authority. A disagreement is never ALIVE — the journal says one
    thing, the beat says another, and no operator should act on a lie.
    """
    if state is None:
        # No GENESIS yet (or pre-journal legacy run): nothing terminal is
        # known, so the only honest answer is UNKNOWN.
        return HeartbeatClass.UNKNOWN_RESUMABLE
    if state in TERMINAL_STATES:
        # Terminal states need no liveness evidence: the run is over and
        # the journal already says how.
        return HeartbeatClass.DEAD_TERMINAL
    if state not in PROCESS_STATES:
        # Owner-gated / precondition states expect NO process: no heartbeat
        # is not a liveness concern.
        return HeartbeatClass.ALIVE
    if beat is None:
        # Missing log for a process state: UNKNOWN, never FAILED, never
        # silently healthy. The sealed lane escalates to reconciliation.
        if state is RunState.SEALED_RUNNING:
            return HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED
        return HeartbeatClass.UNKNOWN_RESUMABLE
    # Round-1 review: the heartbeat's recorded state MUST match the
    # journal projection's state. A disagreement is a corruption signal
    # (stale heartbeat file, half-written beat, beat from a previous
    # incarnation). Never classify such a case as ALIVE.
    if beat.state is not state:
        return HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED
    fresh = (now_epoch - beat.at_epoch) <= stale_after_s
    owner_gone = _owner_gone(beat, boot_id_now=boot_id_now, proc_root=proc_root)
    if fresh and not owner_gone:
        return HeartbeatClass.ALIVE
    if not owner_gone:
        # Alive but silent: report and keep watching; silence is not death.
        return HeartbeatClass.ALIVE_SILENT
    if state is RunState.SEALED_RUNNING:
        # Authority was consumed; only reconciliation may move this run.
        return HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED
    if state in RESUMABLE_STATES:
        return HeartbeatClass.UNKNOWN_RESUMABLE
    return HeartbeatClass.DEAD_TERMINAL
