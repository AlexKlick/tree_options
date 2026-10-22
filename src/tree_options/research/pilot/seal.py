"""The apply step of the ``seal_registration`` command (C3.5).

The owner seals from the UI: the browser queues ``seal_registration`` with
``SealRegistrationArgs{program_id, registration_sha256}`` (the hash the
owner reviewed) through the family-signed command path; the desk owner
applies it here (a file operation, never a broker call). The sealer is the
VERIFIED actor: ``issued_by.sub`` must be in ``TREX_OWNER_EMAILS``, so the
loopback operator label (``loopback-operator``) can never seal; from a
loopback shell the owner uses the CLI and names their email explicitly.

Until the coordinator applies ``docs/product/pilot-seal-command.patch`` the
command kind does not exist; ``apply_seal_command`` is what its desk
dispatch calls (it takes the args and the actor, not the command model).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tree_options.research.pilot import codes
from tree_options.research.pilot.store import (
    PilotError,
    PilotStore,
    owners_from_env,
    pilot_dir_from_env,
)
from tree_options.trex.contracts.commands import Actor
from tree_options.trex.contracts.wave3 import SealRegistrationArgs

__all__ = ["SealOutcome", "apply_seal_command"]


@dataclass(frozen=True)
class SealOutcome:
    """Mirrors the desk's ``Outcome`` (``applied`` or ``refused``) without
    importing the desk: the dispatch maps it one to one."""

    status: Literal["applied", "refused"]
    reason: str | None = None
    detail: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def apply_seal_command(
    args: SealRegistrationArgs,
    actor: Actor,
    now: datetime,
    *,
    root: Path | None = None,
    owners: Iterable[str] | None = None,
) -> SealOutcome:
    """Seal ``args.program_id`` for ``actor`` if the actor is an owner and
    the draft's body hash is ``args.registration_sha256``. ``root`` and
    ``owners`` default to ``TREX_PILOT_DIR`` and ``TREX_OWNER_EMAILS``."""
    try:
        owner_set = owners_from_env() if owners is None else frozenset(owners)
    except PilotError:
        owner_set = frozenset()  # a malformed owner list seals for nobody
    store = PilotStore(pilot_dir_from_env() if root is None else root)
    if actor.sub not in owner_set:
        # judged before the store so a non-owner learns nothing about drafts
        return SealOutcome("refused", codes.SEALER_NOT_OWNER, f"{actor.ingress} actor")
    try:
        sealed = store.seal(
            args.program_id,
            reviewed_sha256=args.registration_sha256,
            sealed_by=actor.sub,
            owners=owner_set,
            at=now,
        )
    except PilotError as refusal:
        return SealOutcome("refused", refusal.code, refusal.detail)
    assert sealed.sealed_at is not None
    return SealOutcome(
        "applied",
        facts={
            "program_id": sealed.program_id,
            "registration_sha256": sealed.sha256,
            "sealed_by": sealed.sealed_by,
            "sealed_at": _iso(sealed.sealed_at),
            "path": f"registrations/{sealed.program_id}.sealed.json",
        },
    )
