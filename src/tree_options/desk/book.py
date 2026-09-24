"""The account's exposure as the rails see it (a READ-ONLY book view).

Two sources, one :class:`~tree_options.desk.rails.BookView`:

* the LEGACY trex books: every plan TOML under ``plans_root`` (default
  ``<repo>/plans``) with its state at ``<state_root>/<plan id>/book.json``
  (default ``$TREX_STATE`` else ``~/.local/state/trex``, the monitor's own
  rule), AND every other ``<state_root>/*/book.json`` (a book whose plan
  TOML was removed or archived still holds its positions). The persisted
  JSON is read directly (``StructureState.from_dict`` per entry): never
  through ``BookState.load``, whose seeding would invent a PLANNED state
  for anything missing, and never written (no lock or temp file either);
* the DESK structure specs: ``<desk_specs>/*.json``, one
  :class:`~tree_options.trex.plan.LegStructure` per file
  (``LegStructure.model_dump_json()``), with their state in an optional
  desk ``book.json`` (the same format). The Wave 3 runtime owns that
  directory; an absent directory is an empty desk. When its run dir lives
  under the legacy state root (``desk-paper/``), pass its book here: it is
  then the desk's, not an orphan.

Each structure maps to at most one position (id ``legacy:<plan>/<sid>`` or
``desk:<sid>``, unique across books):

* CLOSED: not exposure;
* OPEN / EXIT_WORKING: "open", max loss at its entry fill for the open
  quantity (debit kinds: fill x qty x 100; credit kinds: (width - credit)
  x qty x 100), computed here from validated numbers (no model copy); no
  fill on record: at its cap;
* ENTER_WORKING: "working", at its cap for the full quantity (an order
  may be live at the broker, whatever the date);
* PLANNED as PERSISTED: a legacy structure whose entry date has passed is
  dormant (the engine aborts it, "entry date passed") and not counted; one
  entering today or later is "working" at its cap. A desk spec without a
  CLOSED state is always "working" at its cap (the runtime picks it up);
* NO persisted state (no book file, an emptied book, a missing entry): a
  legacy structure whose entry date is today or later is "working" at its
  cap (the monitor may not have saved it yet); one whose entry date has
  passed is UNRESOLVED (a problem): its history is gone.

Fail closed: an unreadable plan, book or spec, a missing plans dir, a
duplicate plan id, an orphan legacy book with anything not CLOSED, a book
entry that is not CLOSED but unknown to its plan/specs, and an inconsistent
entry (negative or non-finite fill, a fill that leaves no loss, quantities
negative, above the structure's, or exits above entries, OPEN with nothing
open) are recorded in ``BookView.problems``, which makes every book rule
NOT_EVALUABLE. Greeks are the caller's: positions come without risk inputs
(:meth:`BookView.with_risk` attaches them).
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from tree_options.desk import paths
from tree_options.desk.rails import BookPosition, BookView
from tree_options.trex.plan import LegStructure, PutSpread, load_plan
from tree_options.trex.state import Status, StructureState

_OPEN = frozenset({Status.OPEN, Status.EXIT_WORKING})
_HUNDRED = Decimal(100)
_ZERO = Decimal(0)

Struct = PutSpread | LegStructure


class _Inconsistent(Exception):
    """A persisted state the adapter can't turn into a bounded exposure."""


def default_plans_root() -> Path:
    return paths.repo_root() / "plans"


def default_state_root() -> Path:
    """The legacy monitor's state root (``TREX_STATE``, else
    ``~/.local/state/trex``), resolved at call time."""
    raw = os.environ.get("TREX_STATE", "").strip()
    return Path(raw) if raw else Path.home() / ".local" / "state" / "trex"


def _read_states(path: Path) -> dict[str, StructureState] | None:
    """The PERSISTED structure states of a book file (no seeding); None
    when the file does not exist. Anything unreadable raises."""
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("book is not a JSON object")
    structures = raw.get("structures", {})
    if not isinstance(structures, dict):
        raise ValueError("structures is not an object")
    return {str(sid): StructureState.from_dict(entry) for sid, entry in structures.items()}


def _cap_per_package(struct: Struct) -> Decimal:
    return struct.limit_cap if isinstance(struct, PutSpread) else struct.max_loss_per_package()


def _fill_per_package(struct: Struct, fill: Decimal) -> Decimal:
    """Max loss of one package opened at ``fill`` (debit orientation)."""
    if isinstance(struct, LegStructure) and struct.is_credit:
        width = struct.width
        assert width is not None  # credit kinds are verticals and condors
        return width - fill
    return fill


def _validate(struct: Struct, st: StructureState) -> None:
    filled, exited = st.filled_qty, st.exit_filled_qty
    if filled < 0 or exited < 0:
        raise _Inconsistent(f"negative quantity (filled {filled}, exited {exited})")
    if exited > filled:
        raise _Inconsistent(f"exited {exited} > filled {filled}")
    if filled > struct.quantity:
        raise _Inconsistent(f"filled {filled} > the structure's quantity {struct.quantity}")
    if st.status is Status.OPEN and st.open_qty < 1:
        raise _Inconsistent(f"open with {st.open_qty} open")
    if st.status is Status.EXIT_WORKING and filled < 1:
        raise _Inconsistent("exiting with nothing filled")
    fill = st.entry_fill
    if fill is not None:
        if not fill.is_finite() or fill <= 0:
            raise _Inconsistent(f"entry fill {fill} is not a positive price")
        if st.status in _OPEN and not _fill_per_package(struct, fill) > 0:
            raise _Inconsistent(f"entry fill {fill} leaves no loss: max loss would be <= 0")


def _position(
    pid: str,
    source: str,
    struct: Struct,
    st: StructureState,
    *,
    as_of: date,
    planned_dormant_after_entry: bool,
) -> BookPosition | None:
    _validate(struct, st)
    status = st.status
    if status is Status.CLOSED:
        return None
    if status in _OPEN:
        qty = st.open_qty
        if st.entry_fill is None:
            loss, how = _cap_per_package(struct) * _HUNDRED * qty, f"open {qty}, no fill: at cap"
        else:
            per = _fill_per_package(struct, st.entry_fill)
            loss, how = per * _HUNDRED * qty, f"open {qty} @ {st.entry_fill}"
        return BookPosition(pid, source, struct.underlying, "open", loss, detail=how)
    if status is Status.PLANNED and planned_dormant_after_entry and as_of > struct.entry_date:
        return None
    loss = _cap_per_package(struct) * _HUNDRED * struct.quantity
    how = f"{status.value}: at cap for {struct.quantity}"
    return BookPosition(pid, source, struct.underlying, "working", loss, detail=how)


def _unpersisted(pid: str, source: str, struct: Struct, as_of: date) -> BookPosition:
    """A legacy structure with no persisted state."""
    if as_of > struct.entry_date:
        raise _Inconsistent(
            f"no persisted state and its entry date {struct.entry_date} has passed: unresolved"
        )
    loss = _cap_per_package(struct) * _HUNDRED * struct.quantity
    how = f"no state yet (entry {struct.entry_date}): at cap for {struct.quantity}"
    return BookPosition(pid, source, struct.underlying, "working", loss, detail=how)


def _legacy(
    plans_root: Path, state_root: Path, as_of: date, exclude: Path | None
) -> tuple[list[BookPosition], list[str]]:
    positions: list[BookPosition] = []
    problems: list[str] = []
    if not plans_root.is_dir():
        return positions, [f"legacy plans dir {plans_root} missing"]
    seen: set[str] = set()
    for toml_path in sorted(plans_root.glob("*.toml")):
        try:
            plan = load_plan(toml_path)
        except (OSError, ValueError) as exc:
            problems.append(f"legacy plan {toml_path.name} unreadable ({type(exc).__name__})")
            continue
        if plan.id in seen:
            problems.append(f"legacy plan {toml_path.name}: duplicate plan id {plan.id}")
            continue
        seen.add(plan.id)
        structs: dict[str, Struct] = {s.id: s for s in plan.structures}
        structs.update({s.id: s for s in plan.leg_structures})
        try:
            states = _read_states(state_root / plan.id / "book.json")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            problems.append(f"legacy book {plan.id}/book.json unreadable ({type(exc).__name__})")
            continue
        states = states or {}
        source = f"legacy:{plan.id}"
        for sid, st in states.items():
            if sid not in structs and st.status is not Status.CLOSED:
                problems.append(f"legacy book {plan.id}: {sid} ({st.status.value}) not in its plan")
        for sid, struct in structs.items():
            pid = f"{source}/{sid}"
            try:
                persisted = states.get(sid)
                pos = (
                    _unpersisted(pid, source, struct, as_of)
                    if persisted is None
                    else _position(
                        pid,
                        source,
                        struct,
                        persisted,
                        as_of=as_of,
                        planned_dormant_after_entry=True,
                    )
                )
            except _Inconsistent as exc:
                problems.append(f"{pid}: {exc}")
                continue
            if pos is not None:
                positions.append(pos)
    problems += _orphans(state_root, seen, exclude)
    return positions, problems


def _orphans(state_root: Path, known: set[str], exclude: Path | None) -> list[str]:
    """Books under the state root with no plan TOML: each must be verifiably
    all CLOSED, else its exposure is unknown (a problem)."""
    if not state_root.is_dir():
        return []
    skip = exclude.resolve() if exclude is not None else None
    problems: list[str] = []
    for book_path in sorted(state_root.glob("*/book.json")):
        name = book_path.parent.name
        if name in known or (skip is not None and book_path.resolve() == skip):
            continue
        try:
            states = _read_states(book_path) or {}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            problems.append(
                f"legacy book {name}/book.json (no plan) unreadable ({type(exc).__name__})"
            )
            continue
        live = sorted(sid for sid, st in states.items() if st.status is not Status.CLOSED)
        if live:
            problems.append(
                f"legacy book {name}/book.json has no plan TOML and holds non-closed"
                f" structures {live}: exposure unknown"
            )
    return problems


def _desk(
    specs_dir: Path | None, book_path: Path | None, as_of: date
) -> tuple[list[BookPosition], list[str]]:
    positions: list[BookPosition] = []
    problems: list[str] = []
    specs: dict[str, LegStructure] = {}
    if specs_dir is not None and specs_dir.is_dir():
        for spec_path in sorted(specs_dir.glob("*.json")):
            try:
                spec = LegStructure.model_validate_json(spec_path.read_bytes())
            except (OSError, ValueError) as exc:
                problems.append(f"desk spec {spec_path.name} unreadable ({type(exc).__name__})")
                continue
            if spec.id in specs:
                problems.append(f"desk spec {spec_path.name}: duplicate id {spec.id}")
                continue
            specs[spec.id] = spec
    states: dict[str, StructureState] = {}
    if book_path is not None:
        try:
            states = _read_states(book_path) or {}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            problems.append(f"desk book {book_path.name} unreadable ({type(exc).__name__})")
            return positions, problems
    for sid, st in states.items():
        if sid not in specs and st.status is not Status.CLOSED:
            problems.append(f"desk book: {sid} ({st.status.value}) has no spec")
    for sid, spec in specs.items():
        pid = f"desk:{sid}"
        try:
            pos = _position(
                pid,
                "desk",
                spec,
                states.get(sid) or StructureState(),
                as_of=as_of,
                planned_dormant_after_entry=False,
            )
        except _Inconsistent as exc:
            problems.append(f"{pid}: {exc}")
            continue
        if pos is not None:
            positions.append(pos)
    return positions, problems


def load_book(
    *,
    as_of: date,
    plans_root: Path | None = None,
    state_root: Path | None = None,
    desk_specs: Path | None = None,
    desk_book: Path | None = None,
) -> BookView:
    """The account's open and working positions as of ``as_of`` (the
    entry session), legacy trex books plus desk specs. Never writes."""
    legacy, legacy_problems = _legacy(
        plans_root or default_plans_root(),
        state_root or default_state_root(),
        as_of,
        desk_book,
    )
    desk, desk_problems = _desk(desk_specs, desk_book, as_of)
    return BookView(positions=tuple(legacy + desk), problems=tuple(legacy_problems + desk_problems))
