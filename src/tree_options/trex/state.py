"""Durable book state for trex.

Two artifacts under a run directory (default ``~/.local/state/trex/<plan>/``):

- ``book.json`` — the current state per structure, written atomically
  (tmp + ``os.replace``) on every transition. On restart the broker is
  truth: the runner reconciles live orders/positions against this file and
  the file yields.
- ``events.jsonl`` — append-only, one JSON object per event. This is the
  trade log; it is never rewritten.

Transition legality is enforced here (``Status``/``transition``) so a
crashed-then-restarted runner cannot invent a path the engine never chose —
notably, no path returns from CLOSED, and no path exists from any state to
"add contracts".
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from tree_options.trex.clock import ET


class Status(StrEnum):
    PLANNED = "planned"
    ENTER_WORKING = "enter_working"
    OPEN = "open"
    EXIT_WORKING = "exit_working"
    CLOSED = "closed"


# CLOSED is terminal. PLANNED←ENTER_WORKING is a cancelled entry (retry
# budget still finite via entry_cycles). OPEN←EXIT_WORKING is a cancelled /
# rejected exit order — the runner retries, it never abandons the flatten.
_VALID: dict[Status, frozenset[Status]] = {
    Status.PLANNED: frozenset({Status.ENTER_WORKING, Status.CLOSED}),
    Status.ENTER_WORKING: frozenset({Status.PLANNED, Status.OPEN, Status.CLOSED}),
    Status.OPEN: frozenset({Status.EXIT_WORKING}),
    Status.EXIT_WORKING: frozenset({Status.OPEN, Status.CLOSED}),
    Status.CLOSED: frozenset(),
}


def transition_allowed(current: Status, target: Status) -> bool:
    return target in _VALID[current]


def _closure(start: Status) -> frozenset[Status]:
    seen = {start}
    frontier = [start]
    while frontier:
        for nxt in _VALID[frontier.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return frozenset(seen)


_REACHABLE: dict[Status, frozenset[Status]] = {s: _closure(s) for s in Status}


def status_reachable(current: Status, target: Status) -> bool:
    """True if the state machine can get from ``current`` to ``target`` (in
    any number of legal steps; a status reaches itself). A merge never
    writes an unreachable status over the disk's: CLOSED never comes back
    and nothing past the entry lane returns to it."""
    return target in _REACHABLE[current]


# Lane ownership of a shared book.json: a structure is the entry runner's
# while it is in ENTRY_LANE; only the entry runner moves it out (to OPEN or
# CLOSED), and from then on only the monitor writes it.
ENTRY_LANE = frozenset({Status.PLANNED, Status.ENTER_WORKING})


class BookUnreadableError(RuntimeError):
    """The book on disk can't be read: never overwritten blind (exposure
    can't be ruled out, and the other runner's writes would be lost)."""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class StructureState:
    """Persisted per-structure bookkeeping. Money fields are per-spread."""

    __slots__ = (
        "close_reason",
        "entry_cycles",
        "entry_fill",
        "entry_order",
        "entry_order_notional",
        "entry_order_seen",
        "exit_cycles",
        "exit_fill",
        "exit_filled_qty",
        "exit_order",
        "exit_order_notional",
        "exit_order_seen",
        "exit_reason",
        "filled_qty",
        "status",
        "touch_ts",
        "updated_at",
    )

    def __init__(
        self,
        status: Status = Status.PLANNED,
        entry_order: str | None = None,
        entry_fill: Decimal | None = None,
        filled_qty: int = 0,
        entry_cycles: int = 0,
        exit_order: str | None = None,
        exit_fill: Decimal | None = None,
        exit_filled_qty: int = 0,
        exit_cycles: int = 0,
        exit_reason: str | None = None,
        close_reason: str | None = None,
        touch_ts: datetime | None = None,
        updated_at: datetime | None = None,
        exit_order_seen: int = 0,
        exit_order_notional: Decimal | None = None,
        entry_order_seen: int = 0,
        entry_order_notional: Decimal | None = None,
    ) -> None:
        self.status = status
        self.entry_order = entry_order
        self.entry_fill = entry_fill
        self.filled_qty = filled_qty
        self.entry_cycles = entry_cycles
        self.exit_order = exit_order
        self.exit_fill = exit_fill
        self.exit_filled_qty = exit_filled_qty
        self.exit_cycles = exit_cycles
        self.exit_reason = exit_reason
        self.close_reason = close_reason
        self.touch_ts = touch_ts
        self.updated_at = updated_at
        # order-execution checkpoint (Codex-M2 #8): how much of THIS exit
        # order the book has already recorded, so a restarted monitor
        # adopts idempotently (no re-counting recorded fills) while still
        # absorbing fills that happened during downtime
        self.exit_order_seen = exit_order_seen
        self.exit_order_notional = exit_order_notional
        # the same checkpoint for the working ENTRY order (entry_order):
        # a restarted entry runner resumes from it instead of re-adding
        # fills the book already holds
        self.entry_order_seen = entry_order_seen
        self.entry_order_notional = entry_order_notional

    @property
    def open_qty(self) -> int:
        """Contracts (spreads) still held: entry fills minus exit fills."""
        return self.filled_qty - self.exit_filled_qty

    def to(self, status: Status, now: datetime) -> None:
        if not transition_allowed(self.status, status):
            raise ValueError(f"illegal transition {self.status} -> {status}")
        self.status = status
        self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "entry_order": self.entry_order,
            "entry_fill": str(self.entry_fill) if self.entry_fill is not None else None,
            "filled_qty": self.filled_qty,
            "entry_cycles": self.entry_cycles,
            "exit_order": self.exit_order,
            "exit_fill": str(self.exit_fill) if self.exit_fill is not None else None,
            "exit_filled_qty": self.exit_filled_qty,
            "exit_cycles": self.exit_cycles,
            "exit_reason": self.exit_reason,
            "close_reason": self.close_reason,
            "touch_ts": _iso(self.touch_ts),
            "updated_at": _iso(self.updated_at),
            "exit_order_seen": self.exit_order_seen,
            "exit_order_notional": (
                str(self.exit_order_notional)
                if self.exit_order_notional is not None
                else None
            ),
            "entry_order_seen": self.entry_order_seen,
            "entry_order_notional": (
                str(self.entry_order_notional) if self.entry_order_notional is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StructureState:
        return cls(
            status=Status(raw["status"]),
            entry_order=raw.get("entry_order"),
            entry_fill=Decimal(raw["entry_fill"]) if raw.get("entry_fill") else None,
            filled_qty=int(raw.get("filled_qty", 0)),
            entry_cycles=int(raw.get("entry_cycles", 0)),
            exit_order=raw.get("exit_order"),
            exit_fill=Decimal(raw["exit_fill"]) if raw.get("exit_fill") else None,
            exit_filled_qty=int(raw.get("exit_filled_qty", 0)),
            exit_cycles=int(raw.get("exit_cycles", 0)),
            exit_reason=raw.get("exit_reason"),
            close_reason=raw.get("close_reason"),
            touch_ts=_parse_iso(raw.get("touch_ts")),
            updated_at=_parse_iso(raw.get("updated_at")),
            exit_order_seen=int(raw.get("exit_order_seen", 0)),
            exit_order_notional=(
                Decimal(raw["exit_order_notional"])
                if raw.get("exit_order_notional")
                else None
            ),
            entry_order_seen=int(raw.get("entry_order_seen", 0)),
            entry_order_notional=(
                Decimal(raw["entry_order_notional"]) if raw.get("entry_order_notional") else None
            ),
        )


class BookState:
    """All structures plus the monitor heartbeat (the arm-before-enter gate)."""

    def __init__(self, structure_ids: list[str]) -> None:
        self.structures: dict[str, StructureState] = {sid: StructureState() for sid in structure_ids}
        self.heartbeat: datetime | None = None

    # -- persistence ------------------------------------------------------

    def save(self, path: Path) -> None:
        """Atomic write through a per-writer temp file (dot-prefixed, pid
        and thread qualified: two runners never rename each other's)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "heartbeat": _iso(self.heartbeat),
            "structures": {sid: st.to_dict() for sid, st in self.structures.items()},
        }
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2) + "\n")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def save_owned(
        self, path: Path, theirs: Callable[[StructureState, StructureState], bool]
    ) -> None:
        """Save, first adopting from disk every structure the OTHER process
        owns (``theirs(mine, on_disk)``). Both runners save the whole book,
        so a plain save reverts whatever the other one wrote since this
        process last read it: the monitor could hide a FLATTEN-settled
        entry, the entry runner could undo an exit (then a second sell).

        The read, merge and write are ONE critical section under an
        exclusive flock on ``<book>.lock`` shared by both runners, so the
        other's write can't land between them. Even an owned structure
        keeps the disk's status when the state machine can't get from it
        to ours (status_reachable): nothing is resurrected or moved back
        into the entry lane. An unreadable book is never overwritten.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if path.exists():
                    try:
                        disk = BookState.load(path, list(self.structures))
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        raise BookUnreadableError(
                            f"{path.name} unreadable ({type(exc).__name__}): not overwriting"
                        ) from exc
                    self._adopt(disk, theirs)
                self.save(path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _adopt(
        self, disk: BookState, theirs: Callable[[StructureState, StructureState], bool]
    ) -> None:
        # only the monitor beats: the fresher beat is the truth
        if disk.heartbeat is not None and (
            self.heartbeat is None or disk.heartbeat > self.heartbeat
        ):
            self.heartbeat = disk.heartbeat
        for sid, mine in list(self.structures.items()):
            on_disk = disk.structures.get(sid)
            if on_disk is None:
                continue
            if theirs(mine, on_disk) or not status_reachable(on_disk.status, mine.status):
                self.structures[sid] = on_disk

    @classmethod
    def load(cls, path: Path, structure_ids: list[str]) -> BookState:
        """Load a book, seeding any structure ids not yet on disk."""
        book = cls(structure_ids)
        if not path.exists():
            return book
        raw = json.loads(path.read_text())
        book.heartbeat = _parse_iso(raw.get("heartbeat"))
        for sid, state_raw in raw.get("structures", {}).items():
            book.structures[sid] = StructureState.from_dict(state_raw)
        for sid in structure_ids:  # plan grew a structure: seed it PLANNED
            book.structures.setdefault(sid, StructureState())
        return book

    # -- events -----------------------------------------------------------

    def event(self, path: Path, event: str, **payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(ET).isoformat(), "event": event, **payload}
        with path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    # -- arm gate ---------------------------------------------------------

    def beat(self) -> None:
        self.heartbeat = datetime.now(ET)

    def armed_within(self, seconds: int) -> bool:
        if self.heartbeat is None:
            return False
        age = (datetime.now(ET) - self.heartbeat).total_seconds()
        return age <= seconds
