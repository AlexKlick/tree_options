"""Budget enforcement at job admission (C3.5) for ``ResearchWorker``.

The worker calls ``admit(run)`` for every run that carries
``pilot_program_id`` before it claims the run, and ``settle(run, ...)`` when
the job ends (completed or failed). Admission, under the program's meter
lock:

1. a run left admitted by a worker that died (its job marker is still there)
   is charged the wall time since that admission as compute, the safe
   over-count, and its marker is closed;
2. the program's registration (``registration_missing`` /
   ``registration_unreadable`` when it does not read);
3. ``registration_run_blockers`` (the frozen C3.5 rule): a draft never runs,
   the sealer must still be an owner, unknown usage blocks, a budget is
   exhausted at ``used >= budget``;
4. admitted: one request is charged and a job marker opened BEFORE any
   compute, so a crash can over-count but never hide work.

Refusals charge nothing. The run record gains ``pilot_day``,
``pilot_admitted_at`` and ``pilot_registration_sha256``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree_options.research.pilot import PILOT_PROGRAM_FIELD, codes
from tree_options.research.pilot.store import (
    PilotStore,
    is_program_id,
    owners_from_env,
    pilot_dir_from_env,
)
from tree_options.research.pilot.usage import UsageMeter
from tree_options.trex.contracts.acceptance import et_date
from tree_options.trex.contracts.wave3 import registration_run_blockers

__all__ = ["PilotAdmission"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def whole_seconds(seconds: float) -> int:
    """Metered compute: whole seconds, rounded UP (never under-count)."""
    return max(0, math.ceil(seconds))


class PilotAdmission:
    """The worker's ``RunAdmission`` for pilot programs under one pilot dir."""

    def __init__(
        self,
        root: Path,
        *,
        owners: Iterable[str],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.store = PilotStore(root)
        self.meter = UsageMeter(root)
        self.owners = frozenset(owners)
        self.clock = clock

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PilotAdmission:
        """``TREX_PILOT_DIR`` and ``TREX_OWNER_EMAILS`` (refuses a malformed
        owner list with ``PilotError``)."""
        return cls(pilot_dir_from_env(environ), owners=owners_from_env(environ))

    def admit(self, run: Mapping[str, Any]) -> tuple[tuple[str, ...], dict[str, Any]]:
        """``(blockers, fields)``: empty blockers = admitted and charged, and
        ``fields`` go onto the run record; otherwise nothing was charged."""
        program_id, run_id = run.get(PILOT_PROGRAM_FIELD), run.get("run_id")
        if not is_program_id(program_id) or not is_program_id(run_id):
            return (codes.PILOT_PROGRAM_INVALID,), {}
        assert isinstance(program_id, str) and isinstance(run_id, str)
        now = self.clock()
        day = et_date(now)
        with self.meter.lock(program_id):
            self._recover(program_id, run_id, now)
            state = self.store.state(program_id)
            if state.registration is None:
                return (state.reason or codes.REGISTRATION_UNREADABLE,), {}
            reading = self.meter.read(program_id, day, now=now)
            blockers = registration_run_blockers(
                state.registration, usage=reading.usage, owners=self.owners
            )
            if blockers:
                return tuple(blockers), {}
            self.meter.charge(program_id, day, requests=1, at=now)
            self.meter.open_job(program_id, run_id, day, at=now)
        return (), {
            "pilot_day": day.isoformat(),
            "pilot_admitted_at": _iso(now),
            "pilot_registration_sha256": state.registration.sha256,
        }

    def _recover(self, program_id: str, run_id: str, now: datetime) -> None:
        """This run was admitted before and never settled: its worker died
        mid-job. Charge the wall time since then as compute (an over-count,
        the safe direction) to the day it was admitted on."""
        marker = self.meter.job(program_id, run_id)
        if marker is None:
            return
        elapsed = whole_seconds((now - marker.admitted_at).total_seconds())
        self.meter.charge(program_id, marker.day, compute_seconds=elapsed, at=now)
        self.meter.close_job(program_id, run_id)

    def settle(self, run: Mapping[str, Any], *, compute_seconds: int, storage_bytes: int) -> None:
        """Charge an admitted job's compute and storage to its admission day
        and close its marker. A run with no open marker was settled already
        (or never admitted): nothing to do."""
        program_id, run_id = run[PILOT_PROGRAM_FIELD], run["run_id"]
        with self.meter.lock(program_id):
            marker = self.meter.job(program_id, run_id)
            if marker is None:
                return
            self.meter.charge(
                program_id,
                marker.day,
                compute_seconds=compute_seconds,
                storage_bytes=storage_bytes,
                at=self.clock(),
            )
            self.meter.close_job(program_id, run_id)
