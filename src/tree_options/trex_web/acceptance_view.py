"""``GET /api/acceptance``: ``trex-acceptance-view/1`` (Wave 4 lane H, C3.2/C3.4).

Read-only and broker-free. It serves ``<TREX_ACCEPTANCE_DIR>/acceptance.json``
(written by ``trex.acceptance publish``) only after:

1. the file is a regular file (never a symlink) of at most
   ``MAX_DOCUMENT_BYTES``;
2. it parses as ``AcceptanceDocument``, whose validation recomputes every
   derived field from the inputs it carries (a hand-set verdict, streak,
   counter or drill result cannot parse);
3. it is not dated more than ``FUTURE_SKEW_S`` after the server's clock;
4. ``verify_acceptance`` accepts it against the repo NYSE calendar (another
   calendar, or a window that hides a trailing session, refuses).

Anything else is ``unavailable`` with the contract's reason
(``acceptance_missing``, ``acceptance_invalid``,
``acceptance_calendar_mismatch``, ``acceptance_window_mismatch``) and no
document: never a partial or zero figure. ``live_authorized`` is the literal
``false`` in the view and in every document: acceptance never unlocks live
trading. The directory resolves per request (default: ``TREX_ACCEPTANCE_DIR``).

Wiring (coordinator, ``trex_web/app.py`` inside ``create_app``)::

    from tree_options.trex_web.acceptance_view import attach as attach_acceptance

    attach_acceptance(app, clock=clock)
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from tree_options.time.calendar import SessionCalendar
from tree_options.time.sessions import shift_instant
from tree_options.trex.clock import session_calendar
from tree_options.trex.contracts.acceptance import (
    AcceptanceDocument,
    AcceptanceView,
    verify_acceptance,
)
from tree_options.trex.contracts.canonical import model_document
from tree_options.trex.contracts.wave3 import ACCEPTANCE_PATH, AcceptanceDir, Wave3Reason

__all__ = [
    "FUTURE_SKEW_S",
    "MAX_DOCUMENT_BYTES",
    "acceptance_payload",
    "acceptance_view",
    "attach",
]

#: A document bigger than this is not the publisher's; refuse to parse it.
MAX_DOCUMENT_BYTES: Final = 32 * 1024 * 1024
#: A document may be dated at most this far after the server's clock.
FUTURE_SKEW_S: Final = 300
#: ``verify_acceptance`` refusals the contract names as view reasons.
_VERIFY_REASONS: Final = frozenset(
    {
        Wave3Reason.ACCEPTANCE_CALENDAR_MISMATCH.value,
        Wave3Reason.ACCEPTANCE_WINDOW_MISMATCH.value,
    }
)


class _Unavailable(Exception):
    def __init__(self, reason: Wave3Reason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _read_document(path: Path) -> bytes:
    """The document's bytes; ``_Unavailable`` for a missing or unsafe file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise _Unavailable(Wave3Reason.ACCEPTANCE_MISSING) from None
    except OSError:
        raise _Unavailable(Wave3Reason.ACCEPTANCE_INVALID) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_BYTES:
            raise _Unavailable(Wave3Reason.ACCEPTANCE_INVALID)
        chunks = []
        total = 0
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DOCUMENT_BYTES:
                raise _Unavailable(Wave3Reason.ACCEPTANCE_INVALID)
        return b"".join(chunks)
    except OSError:
        raise _Unavailable(Wave3Reason.ACCEPTANCE_INVALID) from None
    finally:
        os.close(fd)


def _verified(path: Path, now: datetime, calendar: SessionCalendar) -> AcceptanceDocument:
    raw = _read_document(path)
    try:
        document = AcceptanceDocument.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise _Unavailable(Wave3Reason.ACCEPTANCE_INVALID) from None
    if document.computed_at > shift_instant(now, FUTURE_SKEW_S):
        raise _Unavailable(Wave3Reason.ACCEPTANCE_INVALID)
    try:
        verify_acceptance(document, calendar)
    except ValueError as error:
        reason = str(error)
        if reason in _VERIFY_REASONS:
            raise _Unavailable(Wave3Reason(reason)) from None
        raise _Unavailable(Wave3Reason.ACCEPTANCE_INVALID) from None
    return document


def acceptance_view(
    acceptance_dir: Path, now: datetime, calendar: SessionCalendar
) -> AcceptanceView:
    """The view of the document under ``acceptance_dir`` at ``now`` (pure
    read: never writes, never creates the directory)."""
    try:
        document = _verified(AcceptanceDir(acceptance_dir).document(), now, calendar)
    except _Unavailable as refused:
        return AcceptanceView(
            now=now,
            status="unavailable",
            unavailable_reason=refused.reason.value,
            document=None,
            live_authorized=False,
        )
    return AcceptanceView(
        now=now, status="ok", unavailable_reason=None, document=document, live_authorized=False
    )


def acceptance_payload(
    acceptance_dir: Path, now: datetime, calendar: SessionCalendar
) -> dict[str, Any]:
    """The ``trex-acceptance-view/1`` document (JSON mode, by alias)."""
    return model_document(acceptance_view(acceptance_dir, now, calendar))


def _env_dir() -> Path:
    from tree_options.trex.acceptance import acceptance_dir_from_env

    return acceptance_dir_from_env(os.environ)


def attach(
    app: FastAPI,
    *,
    clock: Callable[[], datetime],
    acceptance_dir: Callable[[], Path] | None = None,
    calendar: Callable[[], SessionCalendar] = session_calendar,
) -> None:
    """Mount ``GET /api/acceptance``; the directory resolves per request
    (default: ``TREX_ACCEPTANCE_DIR``, else ``~/.local/state/trex/acceptance``)."""
    resolve = acceptance_dir if acceptance_dir is not None else _env_dir

    @app.get(ACCEPTANCE_PATH)
    def acceptance() -> JSONResponse:
        payload = acceptance_payload(resolve(), clock(), calendar())
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
