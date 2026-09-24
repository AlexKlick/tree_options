"""The account's exposure as the rails see it (a READ-ONLY book view).

Two sources, one :class:`~tree_options.desk.rails.BookView`:

* the LEGACY trex books: every plan TOML under ``plans_root`` (default
  ``<repo>/plans``) with its state at ``<state_root>/<plan id>/book.json``
  (default ``$TREX_STATE`` else ``~/.local/state/trex``, the monitor's own
  rule). Read with ``BookState.load``, which never writes (no lock file,
  no temp file): the live monitor and entry runner own those files;
* the DESK structure specs: ``<desk_specs>/*.json``, one
  :class:`~tree_options.trex.plan.LegStructure` per file
  (``LegStructure.model_dump_json()``), with their state in an optional
  desk ``book.json`` (the same ``BookState`` format). The Wave 3 runtime
  owns that directory; an absent directory is an empty desk.

Each structure maps to at most one position (id ``legacy:<plan>/<sid>`` or
``desk:<sid>``, unique across books):

* CLOSED: not exposure;
* OPEN / EXIT_WORKING: "open", max loss at its entry fill for the open
  quantity (debit kinds: fill x qty x 100; credit kinds: (width - credit)
  x qty x 100); an open position with no fill on record is counted at its
  cap (plan quantity when the book's open quantity is not positive);
* ENTER_WORKING: "working", at its cap for the full quantity (an order
  may be live at the broker, whatever the date);
* PLANNED: a legacy structure whose entry date has passed is dormant (the
  engine aborts it, "entry date passed") and not counted; one entering
  today or later is "working" at its cap. A PLANNED desk spec is always
  "working" at its cap (the runtime picks it up).

Fail closed: an unreadable plan, book or spec, a missing plans dir, or a
book entry that is not CLOSED but unknown to its plan/specs is recorded in
``BookView.problems``, which makes every book rule NOT_EVALUABLE. Greeks
are the caller's: positions come without risk inputs
(:meth:`BookView.with_risk` attaches them).
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from tree_options.desk import paths
from tree_options.desk.rails import BookPosition, BookView
from tree_options.trex.plan import LegStructure, PutSpread, load_plan
from tree_options.trex.state import BookState, Status, StructureState

_OPEN = frozenset({Status.OPEN, Status.EXIT_WORKING})
_HUNDRED = Decimal(100)


def default_plans_root() -> Path:
    return paths.repo_root() / "plans"


def default_state_root() -> Path:
    """The legacy monitor's state root (``TREX_STATE``, else
    ``~/.local/state/trex``), resolved at call time."""
    raw = os.environ.get("TREX_STATE", "").strip()
    return Path(raw) if raw else Path.home() / ".local" / "state" / "trex"


def _open_loss(struct: PutSpread | LegStructure, st: StructureState) -> tuple[Decimal, str]:
    """(max loss, how) of an OPEN / EXIT_WORKING structure."""
    qty = st.open_qty if st.open_qty > 0 else struct.quantity
    if st.entry_fill is None:
        per = struct.limit_cap if isinstance(struct, PutSpread) else struct.max_loss_per_package()
        return per * _HUNDRED * qty, f"open {qty}, no fill on record: at cap"
    if isinstance(struct, PutSpread):
        per = st.entry_fill
    else:
        per = struct.model_copy(update={"limit": st.entry_fill}).max_loss_per_package()
    return per * _HUNDRED * qty, f"open {qty} @ {st.entry_fill}"


def _cap_loss(struct: PutSpread | LegStructure) -> Decimal:
    return struct.max_debit if isinstance(struct, PutSpread) else struct.max_loss()


def _position(
    pid: str,
    source: str,
    struct: PutSpread | LegStructure,
    st: StructureState,
    *,
    as_of: date,
    planned_dormant_after_entry: bool,
) -> BookPosition | None:
    status = st.status
    if status is Status.CLOSED:
        return None
    if status in _OPEN:
        loss, how = _open_loss(struct, st)
        return BookPosition(pid, source, struct.underlying, "open", loss, detail=how)
    if status is Status.PLANNED and planned_dormant_after_entry and as_of > struct.entry_date:
        return None
    how = f"{status.value}: at cap for {struct.quantity}"
    return BookPosition(pid, source, struct.underlying, "working", _cap_loss(struct), detail=how)


def _legacy(
    plans_root: Path, state_root: Path, as_of: date
) -> tuple[list[BookPosition], list[str]]:
    positions: list[BookPosition] = []
    problems: list[str] = []
    if not plans_root.is_dir():
        return positions, [f"legacy plans dir {plans_root} missing"]
    for toml_path in sorted(plans_root.glob("*.toml")):
        try:
            plan = load_plan(toml_path)
        except (OSError, ValueError) as exc:
            problems.append(f"legacy plan {toml_path.name} unreadable ({type(exc).__name__})")
            continue
        structs: dict[str, PutSpread | LegStructure] = {s.id: s for s in plan.structures}
        structs.update({s.id: s for s in plan.leg_structures})
        book_path = state_root / plan.id / "book.json"
        try:
            book = BookState.load(book_path, list(structs))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f"legacy book {plan.id}/book.json unreadable ({type(exc).__name__})")
            continue
        source = f"legacy:{plan.id}"
        for sid, st in book.structures.items():
            struct = structs.get(sid)
            if struct is None:
                if st.status is not Status.CLOSED:
                    problems.append(
                        f"legacy book {plan.id}: {sid} ({st.status.value}) not in its plan"
                    )
                continue
            pos = _position(
                f"{source}/{sid}", source, struct, st, as_of=as_of, planned_dormant_after_entry=True
            )
            if pos is not None:
                positions.append(pos)
    return positions, problems


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
    if book_path is not None:
        try:
            book = BookState.load(book_path, list(specs))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f"desk book {book_path.name} unreadable ({type(exc).__name__})")
            return positions, problems
        states = book.structures
    else:
        states = {sid: StructureState() for sid in specs}
    for sid, st in states.items():
        held = specs.get(sid)
        if held is None:
            if st.status is not Status.CLOSED:
                problems.append(f"desk book: {sid} ({st.status.value}) has no spec")
            continue
        pos = _position(
            f"desk:{sid}", "desk", held, st, as_of=as_of, planned_dormant_after_entry=False
        )
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
        plans_root or default_plans_root(), state_root or default_state_root(), as_of
    )
    desk, desk_problems = _desk(desk_specs, desk_book, as_of)
    return BookView(positions=tuple(legacy + desk), problems=tuple(legacy_problems + desk_problems))
