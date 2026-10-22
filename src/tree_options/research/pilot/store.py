"""The pilot registrations store (C3.5, C3.7): ``TREX_PILOT_DIR``.

Layout (``trex.contracts.wave3.PilotDir``)::

    registrations/<program_id>.draft.json    trex-pilot-registration/1, status draft
    registrations/<program_id>.sealed.json   the same, sealed; written ONCE (O_EXCL)
    usage/<program_id>/<YYYY-MM-DD>.json     trex-pilot-usage/1 (``usage.UsageMeter``)

A draft is editable and never runs. Sealing is an owner act: the owner names
the body hash they reviewed (``registration_body_sha256`` of the draft), the
sealer must be in ``TREX_OWNER_EMAILS``, and every control the program names
must already be sealed (a challenger is preregistered against a FIXED
baseline). The sealed file is never edited; a change is a new program id.
When both files exist the sealed one is the registration.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from tree_options.research.pilot import codes
from tree_options.research.pilot._files import create_once, read_regular, write_atomic
from tree_options.trex.contracts.canonical import PathSafeId, model_document
from tree_options.trex.contracts.wave3 import (
    TREX_OWNER_EMAILS_ENV,
    PilotDir,
    PilotRegistration,
    parse_owner_emails,
    registration_body_sha256,
    seal_registration,
)

__all__ = [
    "PILOT_DIR_DEFAULT",
    "PILOT_DIR_ENV",
    "PilotError",
    "PilotStore",
    "RegistrationState",
    "is_program_id",
    "owners_from_env",
    "pilot_dir_from_env",
]

PILOT_DIR_ENV = "TREX_PILOT_DIR"
PILOT_DIR_DEFAULT = "~/.local/state/trex/pilot"
_DRAFT_SUFFIX = ".draft.json"
_SEALED_SUFFIX = ".sealed.json"
_PROGRAM_ID: TypeAdapter[str] = TypeAdapter(PathSafeId)


class PilotError(Exception):
    """A refused pilot operation; ``code`` is a reason code (``codes``)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def pilot_dir_from_env(environ: Mapping[str, str] | None = None) -> Path:
    """``TREX_PILOT_DIR`` (default ``~/.local/state/trex/pilot``)."""
    env = os.environ if environ is None else environ
    return Path(env.get(PILOT_DIR_ENV) or PILOT_DIR_DEFAULT).expanduser()


def owners_from_env(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """``TREX_OWNER_EMAILS`` (empty when unset); a malformed list refuses
    ``owners_unconfigured`` rather than being read as some other list."""
    env = os.environ if environ is None else environ
    try:
        return parse_owner_emails(env.get(TREX_OWNER_EMAILS_ENV, ""))
    except ValueError as error:
        raise PilotError(codes.OWNERS_UNCONFIGURED, type(error).__name__) from error


def is_program_id(value: object) -> bool:
    try:
        _PROGRAM_ID.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


def _document_bytes(registration: PilotRegistration) -> bytes:
    text = json.dumps(model_document(registration), indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


@dataclass(frozen=True)
class RegistrationState:
    """One program's registration as the store reads it now.

    ``state``: ``sealed`` / ``draft`` (``registration`` set, ``reason`` None),
    ``missing`` (no file) or ``unavailable`` (a file that does not read as
    this program's registration); the last two carry a ``reason``."""

    program_id: str
    state: Literal["draft", "sealed", "missing", "unavailable"]
    registration: PilotRegistration | None
    reason: str | None


class PilotStore:
    """Registrations under one pilot dir."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.layout = PilotDir(self.root)

    @property
    def registrations(self) -> Path:
        return self.root / "registrations"

    # -- reading ----------------------------------------------------------------------

    def program_ids(self) -> tuple[str, ...]:
        """Every program with a draft or sealed file, sorted. An absent dir
        is an empty store; an unreadable one raises ``OSError``."""
        if not os.path.lexists(self.registrations):
            return ()
        ids: set[str] = set()
        for path in self.registrations.iterdir():
            for suffix in (_DRAFT_SUFFIX, _SEALED_SUFFIX):
                stem = path.name[: -len(suffix)]
                if path.name.endswith(suffix) and is_program_id(stem):
                    ids.add(stem)
        return tuple(sorted(ids))

    def _read(self, path: Path, program_id: str, status: str) -> PilotRegistration | None:
        try:
            registration = PilotRegistration.model_validate_json(read_regular(path))
        except (OSError, ValueError):
            return None
        if registration.program_id != program_id or registration.status != status:
            return None
        return registration

    def state(self, program_id: str) -> RegistrationState:
        """The sealed file wins; a file that does not read (either one) is
        ``unavailable``, never skipped in favour of the other."""
        files: tuple[tuple[Path, Literal["sealed", "draft"]], ...] = (
            (self.layout.sealed(program_id), "sealed"),
            (self.layout.draft(program_id), "draft"),
        )
        for path, status in files:
            if not os.path.lexists(path):
                continue
            registration = self._read(path, program_id, status)
            if registration is None:
                break
            return RegistrationState(program_id, status, registration, None)
        else:
            return RegistrationState(program_id, "missing", None, codes.REGISTRATION_MISSING)
        return RegistrationState(program_id, "unavailable", None, codes.REGISTRATION_UNREADABLE)

    # -- writing ----------------------------------------------------------------------

    def write_draft(self, registration: PilotRegistration) -> Path:
        """Write (or rewrite) a draft. Refused for a sealed document and for
        a program that is already sealed (a change is a new program id)."""
        if registration.status != "draft":
            raise PilotError(codes.REGISTRATION_NOT_DRAFT, registration.program_id)
        if os.path.lexists(self.layout.sealed(registration.program_id)):
            raise PilotError(codes.REGISTRATION_ALREADY_SEALED, registration.program_id)
        path = self.layout.draft(registration.program_id)
        write_atomic(path, _document_bytes(registration))
        return path

    def import_drafts(self, directory: Path) -> tuple[str, ...]:
        """Write every ``*.json`` registration in ``directory`` as a draft
        (the repo's ``data/research/pilot/``); returns the program ids."""
        out = []
        for path in sorted(Path(directory).glob("*.json")):
            registration = PilotRegistration.model_validate_json(read_regular(path))
            self.write_draft(registration)
            out.append(registration.program_id)
        return tuple(sorted(out))

    def seal(
        self,
        program_id: str,
        *,
        reviewed_sha256: str,
        sealed_by: str,
        owners: Iterable[str],
        at: datetime,
    ) -> PilotRegistration:
        """The owner's seal. Refusals (``PilotError.code``), in order:
        ``registration_already_sealed``, ``registration_missing`` /
        ``registration_unreadable``, ``registration_sha256_mismatch``,
        ``sealer_not_owner``, ``control_not_sealed:<id>``."""
        owners = frozenset(owners)
        if os.path.lexists(self.layout.sealed(program_id)):
            raise PilotError(codes.REGISTRATION_ALREADY_SEALED, program_id)
        state = self.state(program_id)
        if state.registration is None:
            raise PilotError(state.reason or codes.REGISTRATION_UNREADABLE, program_id)
        draft = state.registration
        if registration_body_sha256(draft) != reviewed_sha256:
            raise PilotError(codes.REGISTRATION_SHA256_MISMATCH, program_id)
        if sealed_by not in owners:
            raise PilotError(codes.SEALER_NOT_OWNER, program_id)
        for control in draft.controls:
            if self.state(control.program_id).state != "sealed":
                raise PilotError(f"{codes.CONTROL_NOT_SEALED}:{control.program_id}", program_id)
        try:
            sealed = seal_registration(draft, sealed_by=sealed_by, owners=owners, at=at)
        except ValueError as error:  # e.g. a seal instant before created_at
            raise PilotError(codes.REGISTRATION_SEAL_INVALID, type(error).__name__) from error
        try:
            create_once(self.layout.sealed(program_id), _document_bytes(sealed))
        except FileExistsError as error:
            raise PilotError(codes.REGISTRATION_ALREADY_SEALED, program_id) from error
        return sealed
