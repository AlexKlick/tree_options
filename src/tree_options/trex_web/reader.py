"""Read-only loaders for trex state.

Broker-free: only touches ``book.json``, ``events.jsonl`` and the plan TOML.
Never imports ``ib_async``; the web lane must be deployable without a
running IB Gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Literal

from tree_options.trex.clock import now_et
from tree_options.trex.plan import TradePlan, load_plan
from tree_options.trex.state import BookState, Status, StructureState

# The arm-gate freshness window. Mirrors ``monitor.HEARTBEAT_FRESH_SECONDS``
# so the "armed" pill on the GUI matches what ``enter.py`` would accept.
HEARTBEAT_FRESH_SECONDS = 30

# How many trailing events to render on the plan page. Full log stays
# available via ``/plan/{id}/events.jsonl``.
EVENTS_RENDER_LIMIT = 100

WindowState = Literal["before", "during", "after", "wrong_day"]


@dataclass(frozen=True)
class RunbookStatus:
    """Live runbook view surfaced on every plan page. Computed purely
    from clock + plan TOML + the persisted book's heartbeat — no broker
    contact, no filesystem writes. The IB Gateway reachability probe is
    filled in by the app layer (it needs socket I/O)."""

    now_et: datetime
    entry_date: date
    window_start: time
    window_end: time
    window_state: WindowState
    monitor_armed: bool
    heartbeat: datetime | None
    # structure_id -> days until deadline/expiry (negative if past).
    days_to_exit_deadline: dict[str, int] = field(default_factory=dict)
    days_to_expiry: dict[str, int] = field(default_factory=dict)

    @property
    def deadline_breached(self) -> bool:
        """True if any structure has passed its exit deadline without a
        CLOSED state — the monitor's time-stop should have flattened it."""
        return any(d < 0 for d in self.days_to_exit_deadline.values())

    @property
    def expiry_within_a_week(self) -> bool:
        return any(0 <= d <= 7 for d in self.days_to_expiry.values())


@dataclass(frozen=True)
class StructureView:
    """Per-structure display projection. Adds nothing the book doesn't carry —
    just exposes the same fields with derived card data (``open_qty``,
    realized cash) computed once at load time."""

    state: Status
    entry_fill: Decimal | None
    filled_qty: int
    open_qty: int
    exit_fill: Decimal | None
    exit_filled_qty: int
    entry_cycles: int
    exit_cycles: int
    exit_reason: str | None
    close_reason: str | None
    touch_ts: datetime | None
    updated_at: datetime | None

    @property
    def realized_pnl(self) -> Decimal | None:
        """Cash P&L on realized fills, in dollars (already includes the
        per-contract 100-multiplier). Negative if entry debit > exit credit.

        ``None`` until both ``entry_fill`` and ``exit_fill`` are recorded."""
        if self.entry_fill is None or self.exit_fill is None:
            return None
        if self.exit_filled_qty <= 0:
            return None
        return (self.exit_fill - self.entry_fill) * self.exit_filled_qty * 100


@dataclass(frozen=True)
class PlanView:
    """A plan + its persisted book + recent events. None for fields that
    are simply not present on disk yet (``heartbeat``, ``events``)."""

    plan: TradePlan
    structures: dict[str, StructureView]
    heartbeat: datetime | None
    armed: bool
    days_to_expiry: dict[str, int]
    days_to_deadline: dict[str, int]
    events: list[dict[str, object]]
    state_present: bool  # False when the run dir does not exist yet

    @property
    def total_open_qty(self) -> int:
        return sum(s.open_qty for s in self.structures.values())

    @property
    def total_committed_cash(self) -> Decimal:
        return self.plan.committed_at_caps

    @property
    def worst_state(self) -> Status | None:
        """The most-actionable status across all structures, used to badge
        the index card. ``None`` when the run dir does not exist yet."""
        if not self.state_present:
            return None
        states = {s.state for s in self.structures.values()}
        # Priority: open > entering > exiting > closed > planned.
        if Status.OPEN in states:
            return Status.OPEN
        if Status.ENTER_WORKING in states:
            return Status.ENTER_WORKING
        if Status.EXIT_WORKING in states:
            return Status.EXIT_WORKING
        if states and states == {Status.CLOSED}:
            return Status.CLOSED
        return Status.PLANNED


def _state_view(st: StructureState) -> StructureView:
    return StructureView(
        state=st.status,
        entry_fill=st.entry_fill,
        filled_qty=st.filled_qty,
        open_qty=st.open_qty,
        exit_fill=st.exit_fill,
        exit_filled_qty=st.exit_filled_qty,
        entry_cycles=st.entry_cycles,
        exit_cycles=st.exit_cycles,
        exit_reason=st.exit_reason,
        close_reason=st.close_reason,
        touch_ts=st.touch_ts,
        updated_at=st.updated_at,
    )


def _load_events(events_path: Path) -> list[dict[str, object]]:
    if not events_path.exists():
        return []
    out: list[dict[str, object]] = []
    # Tail the last N lines without buffering the whole file when it grows.
    try:
        # Efficient tail: read all lines, slice from the end. The trex log
        # grows by a handful of records per session day; well under any
        # practical limit for in-memory tailing.
        lines = events_path.read_text().splitlines()
    except OSError:
        return []
    for line in lines[-EVENTS_RENDER_LIMIT:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_marks_history(state_root: Path, plan_id: str) -> list[dict[str, object]]:
    """Bounded tail read of a plan's marks_history.jsonl (per-structure
    observation history). Junk-tolerant; sorted by parsed instant."""
    from tree_options.trex.history import read_tail

    rows = read_tail(state_root / plan_id / "marks_history.jsonl")
    return _sorted_by_instant(rows)


def read_account_history(discovery_root: Path) -> list[dict[str, object]]:
    """Account equity history owned by the discovery loop. Deduped by
    (account_id, ts), sorted by parsed instant (string compare reverses
    repeated hours across DST). Sized for the full retained window - the
    60k-line cap is ~9MB, comfortably under this read bound."""
    from tree_options.trex.history import read_tail

    rows = read_tail(discovery_root / "account_history.jsonl", max_bytes=16_000_000)
    seen: set[tuple[object, object]] = set()
    out: list[dict[str, object]] = []
    for row in _sorted_by_instant(rows):
        key = (row.get("account_id"), row.get("ts"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _sorted_by_instant(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    from datetime import datetime as _dt

    def inst(row: dict[str, object]) -> float:
        ts = row.get("ts")
        if not isinstance(ts, str):
            return 0.0
        try:
            return _dt.fromisoformat(ts).timestamp()
        except ValueError:
            return 0.0

    return sorted(rows, key=inst)


def _resolve_plan_toml(plan_id: str, plans_root: Path) -> Path | None:
    """Find a plan TOML by its in-file ``id`` field.

    Operators name TOML files by date (``2026-09-18.toml``) and put the
    stable id inside (``putspread-20260918``). The id is the URL-stable
    key the GUI uses; the filename is just a filesystem handle. Resolve
    by scanning, not by stem-equality, so the URL stays decoupled from
    the filename.
    """
    if not plans_root.exists():
        return None
    candidate = plans_root / f"{plan_id}.toml"
    if candidate.exists():
        return candidate
    for toml_path in plans_root.glob("*.toml"):
        try:
            plan = load_plan(toml_path)
        except (OSError, ValueError):
            continue
        if plan.id == plan_id:
            return toml_path
    return None


def load_plan_view(plan_id: str, state_root: Path, plans_root: Path) -> PlanView | None:
    """Load a plan + its persisted state. Returns None if no TOML has the
    given ``id`` (an unknown id is a 404 from the route, not a render)."""
    toml_path = _resolve_plan_toml(plan_id, plans_root)
    if toml_path is None:
        return None
    plan = load_plan(toml_path)

    state_dir = state_root / plan_id
    book_path = state_dir / "book.json"
    events_path = state_dir / "events.jsonl"
    state_present = state_dir.exists() and (book_path.exists() or events_path.exists())

    structure_ids = [s.id for s in plan.structures]
    if book_path.exists():
        book = BookState.load(book_path, structure_ids)
    else:
        book = BookState(structure_ids)

    structures = {sid: _state_view(book.structures[sid]) for sid in structure_ids}
    today = now_et().date()
    days_to_expiry = {s.id: (s.expiry - today).days for s in plan.structures}
    days_to_deadline = {s.id: (s.exit_deadline - today).days for s in plan.structures}
    armed = book.armed_within(HEARTBEAT_FRESH_SECONDS)
    events = _load_events(events_path)

    return PlanView(
        plan=plan,
        structures=structures,
        heartbeat=book.heartbeat,
        armed=armed,
        days_to_expiry=days_to_expiry,
        days_to_deadline=days_to_deadline,
        events=events,
        state_present=state_present,
    )


def compute_runbook_status_from_view(view: PlanView) -> RunbookStatus:
    """Convenience wrapper that takes a PlanView. Same as
    ``compute_runbook_status(plan, heartbeat, armed)`` but reads from the
    already-loaded view."""
    return compute_runbook_status(view.plan, view.heartbeat, view.armed)


def list_plans(state_root: Path, plans_root: Path) -> list[PlanView]:
    """One PlanView per TOML in ``plans_root``. Plans with no persisted state
    still render (with empty book + empty events) so the operator can see
    a plan exists before the monitor arms it."""
    if not plans_root.exists():
        return []
    out: list[PlanView] = []
    for toml_path in sorted(plans_root.glob("*.toml")):
        try:
            plan = load_plan(toml_path)
        except (OSError, ValueError):
            continue
        view = load_plan_view(plan.id, state_root, plans_root)
        if view is not None:
            out.append(view)
    return out


def load_marks(state_root: Path, plan_id: str) -> dict[str, object] | None:
    """Latest broker marks written by the monitor's observation loop.

    The web lane stays broker-free: it only ever reads the
    ``marks.json`` the monitor persists each tick.
    """
    path = state_root / plan_id / "marks.json"
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def marks_age_seconds(marks: dict[str, object] | None) -> int | None:
    """Age of the marks payload in whole seconds (None when unreadable)."""
    if not marks:
        return None
    ts = marks.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        stamped = datetime.fromisoformat(ts)
    except ValueError:
        return None
    age = (now_et() - stamped).total_seconds()
    return int(age) if age >= 0 else None


def _parse_window(bound: str) -> time:
    """``"HH:MM"`` -> ``time``. The plan's entry_window_start/end are stored
    as raw strings for TOML readability; the engine parses them on each tick.
    The web lane needs them as ``time`` for clock comparison."""
    h, m = bound.split(":", 1)
    return time(int(h), int(m))


def compute_runbook_status(
    plan: TradePlan, heartbeat: datetime | None, armed: bool
) -> RunbookStatus:
    """Compute the live runbook view for a plan. Pure: clock + plan + heartbeat."""
    now = now_et()
    today = now.date()
    window_start = _parse_window(plan.entry_window_start)
    window_end = _parse_window(plan.entry_window_end)
    now_t = now.time()

    # Find the structure whose entry_date is today (a plan book usually only
    # has one entry_date across all structures; if mixed, treat as wrong_day).
    entry_dates = {s.entry_date for s in plan.structures}
    if len(entry_dates) != 1:
        window_state: WindowState = "wrong_day"
    elif next(iter(entry_dates)) != today:
        window_state = "wrong_day"
    elif now_t < window_start:
        window_state = "before"
    elif now_t <= window_end:
        window_state = "during"
    else:
        window_state = "after"

    days_to_exit_deadline = {s.id: (s.exit_deadline - today).days for s in plan.structures}
    days_to_expiry = {s.id: (s.expiry - today).days for s in plan.structures}

    return RunbookStatus(
        now_et=now,
        entry_date=next(iter(entry_dates)),
        window_start=window_start,
        window_end=window_end,
        window_state=window_state,
        monitor_armed=armed,
        heartbeat=heartbeat,
        days_to_exit_deadline=days_to_exit_deadline,
        days_to_expiry=days_to_expiry,
    )
