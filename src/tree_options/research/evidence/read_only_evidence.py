"""Read-only EvidenceStore wrapper for the research lane.

Handoff §9: "Research writes to its own workspace, not the live evidence
audit chain or execution book. Read sealed artifacts through adapters
without mutating them."

The research lane's evidence drawer reads from the same SQLite store the
desk writes to, but ONLY in read-only / non-transient mode. There is no
``put``, no ``atomic``, no ``backup``. The constructor raises if the
caller tries to obtain a write handle.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.paths import state_root


@contextmanager
def open_read_only_evidence(
    *, database: Path | None = None,
) -> Iterator[EvidenceStore | None]:
    """Open the desk evidence store in read-only, non-transient mode.

    The ``readonly=True, transient=False`` pair is the only allowed
    combination for the research lane. Any attempt to obtain a write
    handle (e.g. ``store.atomic()``) raises ``EvidenceError`` per the
    desk's contract — the research lane never sees a writable store.

    Defaults to ``<TREX_DESK_STATE>/evidence/desk.sqlite3`` per
    ``desk.paths.state_root()``. Override via ``database`` only in tests.

    Yields ``None`` when the store file does not yet exist (no live desk
    evidence yet — common before the first fire of ``desk-evidence.service``).
    Callers must treat ``None`` as "no audit envelopes available" rather
    than as an error.
    """
    db = database or state_root() / "evidence" / "desk.sqlite3"
    if not db.is_file():
        yield None
        return
    with EvidenceStore(db, readonly=True, transient=False) as store:
        yield store


__all__ = ["open_read_only_evidence"]
