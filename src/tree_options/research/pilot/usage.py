"""``trex-pilot-usage/1`` metering (C3.5): one counter file per program per ET day.

``usage/<program_id>/<YYYY-MM-DD>.json`` holds ``BudgetUsage``: ``requests``
(one per admitted job), ``compute_seconds`` (whole seconds of the job's
compute, rounded up) and ``storage_bytes`` (the canonical bytes of what the
job published) for that America/New_York day. Admission charges the request
BEFORE the job starts and opens a job marker
(``usage/<program_id>/jobs/<run_id>.json``); the job's end charges its
compute and storage and closes the marker. While a marker stands, the
program's usage is UNKNOWN (its compute is not metered yet), so a second job
of the same program waits: one job per program at a time.

Unknown is never read as zero. A day file that is unreadable, not a valid
``BudgetUsage`` or another program's or day's reads as
``budget_usage_unknown:<detail>`` and nothing can be charged on top of it.
A day with no file and no job marker has had no admission (the meter is its
only writer and writes before any work starts), so it reads as zero.
"""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from tree_options.research.pilot import codes
from tree_options.research.pilot._files import (
    create_once,
    locked,
    read_regular,
    write_atomic,
)
from tree_options.research.pilot.store import PilotError
from tree_options.schemas.common import StrictModel
from tree_options.trex.contracts.canonical import PathSafeId, UTCInstant, model_document
from tree_options.trex.contracts.wave3 import BudgetUsage, PilotDir

__all__ = ["JOB_MARKER_SCHEMA", "JobMarker", "UsageMeter", "UsageReading"]

JOB_MARKER_SCHEMA = "trex-pilot-job/1"


class JobMarker(StrictModel):
    """A job admitted and not yet settled (lane-local, beside the counters):
    its request is charged, its compute is not metered yet."""

    schema_version: Literal["trex-pilot-job/1"] = Field(default="trex-pilot-job/1", alias="schema")
    program_id: PathSafeId
    run_id: PathSafeId
    day: date
    admitted_at: UTCInstant


@dataclass(frozen=True)
class UsageReading:
    """``usage`` set and ``reason`` None, or ``usage`` None and ``reason``
    ``budget_usage_unknown:<detail>``."""

    usage: BudgetUsage | None
    reason: str | None


def _unknown(detail: str) -> UsageReading:
    return UsageReading(None, f"{codes.BUDGET_USAGE_UNKNOWN}:{detail}")


def _usage_bytes(usage: BudgetUsage) -> bytes:
    return (json.dumps(model_document(usage), indent=2, sort_keys=True) + "\n").encode("utf-8")


class UsageMeter:
    """The usage counters and job markers under one pilot dir."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.layout = PilotDir(self.root)

    def program_dir(self, program_id: str) -> Path:
        return self.root / "usage" / program_id

    def jobs_dir(self, program_id: str) -> Path:
        return self.program_dir(program_id) / "jobs"

    def lock(self, program_id: str) -> AbstractContextManager[None]:
        """The per-program lock every read-modify-write of the meter holds."""
        return locked(self.root / "usage" / f".{program_id}.lock")

    # -- reading ----------------------------------------------------------------------

    def jobs(self, program_id: str) -> tuple[str, ...]:
        """Run ids with an open job marker (``OSError`` when unreadable)."""
        for folder in (self.root / "usage", self.program_dir(program_id)):
            if os.path.lexists(folder) and not folder.is_dir():
                raise NotADirectoryError(folder.name)
        folder = self.jobs_dir(program_id)
        if not os.path.lexists(folder):
            return ()
        names = (p.name for p in folder.iterdir())
        return tuple(sorted(n[: -len(".json")] for n in names if n.endswith(".json")))

    def job(self, program_id: str, run_id: str) -> JobMarker | None:
        path = self.jobs_dir(program_id) / f"{run_id}.json"
        if not os.path.lexists(path):
            return None
        return JobMarker.model_validate_json(read_regular(path))

    def _counted(self, program_id: str, day: date) -> UsageReading | None:
        """The day file as written (None when there is none)."""
        path = self.layout.usage(program_id, day)
        try:
            if not os.path.lexists(path):
                return None
            raw = read_regular(path)
        except (OSError, ValueError):
            return _unknown(codes.USAGE_UNREADABLE)
        try:
            usage = BudgetUsage.model_validate_json(raw)
        except ValidationError:
            return _unknown(codes.USAGE_INVALID if _is_json(raw) else codes.USAGE_UNREADABLE)
        if usage.program_id != program_id or usage.day != day:
            return _unknown(codes.USAGE_MISMATCH)
        return UsageReading(usage, None)

    def read(self, program_id: str, day: date, *, now: datetime) -> UsageReading:
        """The program's usage on ``day`` as admission must judge it."""
        try:
            in_flight = self.jobs(program_id)
        except OSError:
            return _unknown(codes.USAGE_UNREADABLE)
        counted = self._counted(program_id, day)
        if counted is not None and counted.usage is None:
            return counted
        if in_flight:
            return _unknown(codes.USAGE_JOB_IN_FLIGHT)
        if counted is not None:
            return counted
        zero = BudgetUsage(
            program_id=program_id,
            day=day,
            requests=0,
            compute_seconds=0,
            storage_bytes=0,
            as_of=now,
        )
        return UsageReading(zero, None)

    # -- writing (callers hold ``lock(program_id)`` across read and write) ------------

    def charge(
        self,
        program_id: str,
        day: date,
        *,
        requests: int = 0,
        compute_seconds: int = 0,
        storage_bytes: int = 0,
        at: datetime,
    ) -> BudgetUsage:
        """Add to the day's counters (creating the file at the first charge).
        Refuses ``budget_usage_unknown`` when the file does not read."""
        if min(requests, compute_seconds, storage_bytes) < 0:
            raise ValueError("a charge is never negative")
        counted = self._counted(program_id, day)
        if counted is not None and counted.usage is None:
            raise PilotError(codes.BUDGET_USAGE_UNKNOWN, counted.reason or "")
        base = counted.usage if counted is not None else None
        usage = BudgetUsage(
            program_id=program_id,
            day=day,
            requests=(base.requests if base else 0) + requests,
            compute_seconds=(base.compute_seconds if base else 0) + compute_seconds,
            storage_bytes=(base.storage_bytes if base else 0) + storage_bytes,
            as_of=at,
        )
        write_atomic(self.layout.usage(program_id, day), _usage_bytes(usage))
        return usage

    def open_job(self, program_id: str, run_id: str, day: date, *, at: datetime) -> JobMarker:
        marker = JobMarker(program_id=program_id, run_id=run_id, day=day, admitted_at=at)
        payload = (json.dumps(model_document(marker), sort_keys=True) + "\n").encode("utf-8")
        create_once(self.jobs_dir(program_id) / f"{run_id}.json", payload)
        return marker

    def close_job(self, program_id: str, run_id: str) -> None:
        (self.jobs_dir(program_id) / f"{run_id}.json").unlink(missing_ok=True)


def _is_json(raw: bytes) -> bool:
    try:
        json.loads(raw)
    except ValueError:
        return False
    return True
