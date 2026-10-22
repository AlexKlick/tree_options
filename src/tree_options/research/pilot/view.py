"""``trex-pilot-view/1``: what ``GET /api/pilot`` serves the ``#/pilot`` page.

Lane-local read shape (pending coordinator adoption into the contracts), made
only of frozen C3.5 shapes: each program's registration (sealed wins over
draft), the body hash the owner reviews and seals (``review_sha256``), the
program's usage for today's ET day and ``registration_run_blockers``. The C3.2
null rule holds: unknown is null with a reason, never zero or ``[]``.

Nothing here runs, seals or orders anything; ``execution_enabled`` and
``live_authorized`` are literally false.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import Field, model_validator

from tree_options.research.pilot import codes
from tree_options.research.pilot.store import PilotStore, RegistrationState
from tree_options.research.pilot.usage import UsageMeter
from tree_options.schemas.common import StrictModel
from tree_options.trex.contracts.acceptance import FalseLiteral, ReasonCode, et_date
from tree_options.trex.contracts.canonical import (
    PathSafeId,
    Sha256Hex,
    UTCInstant,
    model_document,
)
from tree_options.trex.contracts.commands import CommandKind
from tree_options.trex.contracts.wave3 import (
    PILOT_ROLES,
    BudgetUsage,
    PilotRegistration,
    PilotRole,
    pilot_roster_problems,
    registration_body_sha256,
    registration_run_blockers,
)

__all__ = [
    "PILOT_VIEW_PATH",
    "PILOT_VIEW_SCHEMA",
    "PilotProgramRow",
    "PilotView",
    "pilot_view",
    "seal_command_available",
]

PILOT_VIEW_SCHEMA = "trex-pilot-view/1"
PILOT_VIEW_PATH = "/api/pilot"
_ROLE_ORDER = {role: index for index, role in enumerate(PILOT_ROLES)}


class PilotProgramRow(StrictModel):
    """One program. ``state`` ``unavailable``: ``unavailable_reason`` set and
    ``role``, ``registration``, ``review_sha256`` null. ``usage`` is null
    exactly when ``usage_unavailable_reason`` names why. ``run_blockers``
    empty = a job of this program would be admitted now."""

    program_id: PathSafeId
    role: PilotRole | None
    state: Literal["draft", "sealed", "unavailable"]
    unavailable_reason: ReasonCode | None
    registration: PilotRegistration | None
    review_sha256: Sha256Hex | None
    usage: BudgetUsage | None
    usage_unavailable_reason: ReasonCode | None
    run_blockers: tuple[ReasonCode, ...]

    @model_validator(mode="after")
    def _nulls(self) -> PilotProgramRow:
        known = self.state != "unavailable"
        fields = (self.role, self.registration, self.review_sha256)
        if any((value is not None) != known for value in fields):
            raise ValueError("role, registration and review_sha256 are set exactly when known")
        if (self.unavailable_reason is None) != known:
            raise ValueError("unavailable_reason is set exactly when unavailable")
        if (self.usage is None) == (self.usage_unavailable_reason is None):
            raise ValueError("usage is null exactly when usage_unavailable_reason says why")
        if self.registration is not None:
            if (self.registration.program_id, self.registration.status, self.registration.role) != (
                self.program_id,
                self.state,
                self.role,
            ):
                raise ValueError("the row repeats its registration's id, status and role")
            if self.review_sha256 != registration_body_sha256(self.registration):
                raise ValueError("review_sha256 is the registration body hash")
        return self


class PilotView(StrictModel):
    """The whole pilot. ``status`` ``unavailable`` (the registrations do not
    list): ``programs`` and ``roster_problems`` null with the reason."""

    schema_version: Literal["trex-pilot-view/1"] = Field(
        default="trex-pilot-view/1", alias="schema"
    )
    now: UTCInstant
    status: Literal["ok", "unavailable"]
    unavailable_reason: ReasonCode | None
    programs: tuple[PilotProgramRow, ...] | None
    roster_problems: tuple[str, ...] | None
    owners_configured: bool
    seal_command_available: bool
    execution_enabled: FalseLiteral
    live_authorized: FalseLiteral

    @model_validator(mode="after")
    def _nulls(self) -> PilotView:
        known = self.status == "ok"
        if (self.unavailable_reason is None) != known:
            raise ValueError("unavailable_reason is set exactly when unavailable")
        if (self.programs is not None) != known or (self.roster_problems is not None) != known:
            raise ValueError("programs and roster_problems are set exactly when ok")
        return self


def seal_command_available() -> bool:
    """True once ``trex-command/1`` carries the ``seal_registration`` kind
    (the coordinator's amendment, docs/product/pilot-seal-command.patch)."""
    return "seal_registration" in get_args(CommandKind)


def _row(
    state: RegistrationState, meter: UsageMeter, owners: frozenset[str], now: datetime
) -> PilotProgramRow:
    reading = meter.read(state.program_id, et_date(now), now=now)
    registration = state.registration
    if registration is None:
        reason = state.reason or codes.REGISTRATION_UNREADABLE
        blockers: tuple[str, ...] = (reason,)
    else:
        reason = None
        blockers = tuple(
            registration_run_blockers(registration, usage=reading.usage, owners=owners)
        )
    return PilotProgramRow(
        program_id=state.program_id,
        role=None if registration is None else registration.role,
        state="unavailable" if registration is None else registration.status,
        unavailable_reason=reason,
        registration=registration,
        review_sha256=None if registration is None else registration_body_sha256(registration),
        usage=reading.usage,
        usage_unavailable_reason=reading.reason,
        run_blockers=blockers,
    )


def _order(row: PilotProgramRow) -> tuple[int, str]:
    rank = len(PILOT_ROLES) if row.role is None else _ROLE_ORDER[row.role]
    return rank, row.program_id


def pilot_view(
    root: Path,
    *,
    now: datetime,
    owners: frozenset[str],
    owners_configured: bool | None = None,
    available: Callable[[], bool] = seal_command_available,
) -> dict[str, Any]:
    """The ``trex-pilot-view/1`` document (validated, JSON-ready)."""
    store, meter = PilotStore(root), UsageMeter(root)
    configured = bool(owners) if owners_configured is None else owners_configured
    common: dict[str, Any] = {
        "now": now,
        "owners_configured": configured,
        "seal_command_available": available(),
        "execution_enabled": False,
        "live_authorized": False,
    }
    try:
        program_ids = store.program_ids()
    except OSError:
        view = PilotView(
            status="unavailable",
            unavailable_reason=codes.PILOT_DIR_UNREADABLE,
            programs=None,
            roster_problems=None,
            **common,
        )
        return _document(view)
    rows = sorted((_row(store.state(pid), meter, owners, now) for pid in program_ids), key=_order)
    registrations = [row.registration for row in rows if row.registration is not None]
    view = PilotView(
        status="ok",
        unavailable_reason=None,
        programs=tuple(rows),
        roster_problems=pilot_roster_problems(registrations),
        **common,
    )
    return _document(view)


def _document(view: PilotView) -> dict[str, Any]:
    return model_document(view)
