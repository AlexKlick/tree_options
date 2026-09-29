"""E5 desk runtime, v1: the exit owner of supervised positions.

OPERATOR RULING 2026-09-28 (2b): the E5 desk runtime owns the protective
exits of supervised positions. This module is that owner. It is the SAME
process (and IBKR clientId, ``supervised_ibkr.SUPERVISED_CLIENT_ID``) that
sends supervised entries: IBKR lets only the placing client see and cancel
an order, so one process owning entry and exit removes the cross-client
cancel and two-writer book classes the legacy enter/monitor pair has.

Decisions come from the pure desk engine (``engine.step``: drain the
working order's fills, then decide); this module is only I/O around it:

- **Registration before sending.** ``register`` writes the structure's
  spec (one ``LegStructure`` per file, the desk spec format) and a PLANNED
  book entry BEFORE the supervised send. Each tick resolves a PLANNED
  structure from the broker's live tagged order and the supervised outbox
  (acknowledged -> ENTER_WORKING; rejected or confirmed-not-submitted ->
  CLOSED; uncertain -> held). An order at the broker is never unowned.
- **Entries are cancel-only.** A supervised permit binds one limit; a
  reprice would need a new permit. ``AbortEntry`` cancels the working
  entry; ``EntryOrder`` repricing is never sent.
- **Exits** (``CloseOrder``) are placed with ``orderRef trex:desk:<sid>``
  and an EXPLICIT account, repriced cancel -> confirm -> merge the final
  fills -> replace the remainder (the monitor's discipline).
- **Legs-held guard.** Before ANY close order the broker's leg positions
  in the bound account must equal the book's open quantity (BUY legs
  long, SELL legs short). Anything else refuses and alerts: a double
  close (a reversed position) cannot be sent, across restarts too.
- **Exit guards (Stage A).** Before every close order leaves the desk the
  session must be the supervised paper session (``paper_blockers`` over
  the SAME connection), and the broker's OPEN-order view must show no
  other order carrying the desk tag (an unreadable view refuses: absence
  is unproven; a same-day EXECUTED desk order is legal in a reprice
  cycle). Guards passing is the ``exit_authority`` evidence event; every
  refusal is fail-closed-with-retry — nothing is sent, one urgent
  ``exit_blocked`` push, the next tick re-enters the lane. An expired
  mandate NEVER blocks an exit (ruling 2b: the runtime is the exit
  owner, not a permit holder).
- **Health.** ``monitor.json`` (``DeskPaths.health``) feeds the exit
  machine watchdog (``trex.exit_watch``): the tick outcome the heartbeat
  cannot show. Written at acquire, after every tick and on every beat;
  failure-isolated so it can never break the trading loop.
- **Kill files** in the run dir: ``HALT`` places no new orders (fills
  still drain); ``FLATTEN`` cancels working entries and closes open
  structures at marketable prices.
- **Arm gate.** ``exit_owner_ready`` = a fresh heartbeat AND the runtime's
  process lock held: the canary's ``protective_exit_ready`` input.

Assignment risk: ``assignment_plan`` is the canary's
``assignment_plan_verified`` input. A structure without a short call is
covered by the engine's expiry safety; one with a short call needs a fresh
declared-dividend feed and no projected (undeclared) ex-date inside its
hold window, because the engine's pre-ex-date rule needs the exact date.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.desk.dividends import DividendSnapshot, ex_dividends
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.clock import ET, EntryWindow
from tree_options.trex.engine import (
    AbortEntry,
    CloseOrder,
    DividendCalendar,
    EngineConfig,
    EntryOrder,
    ExitReason,
    NoAction,
    Snapshot,
    WorkingOrder,
    adopted_role,
    configure,
    drain,
    last_hold_session,
    short_call_key,
    step,
)
from tree_options.trex.ibkr import DONE_STATES, ORDER_REF_PREFIX, IbkrTrex, OrderRef
from tree_options.trex.plan import LegStructure, cents
from tree_options.trex.state import BookState, Status, StructureState
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised_ibkr import (
    IbkrSupervisedBroker,
    SupervisedEffect,
    supervised_order_ref,
)

log = logging.getLogger("trex.desk_runtime")

SPEC_SCHEMA = "trex-desk-spec/1"
DESK_REF_PREFIX = ORDER_REF_PREFIX + "desk:"
#: the E6 entry window; supervised entries only ever cancel inside it
DEFAULT_ENTRY_WINDOW = EntryWindow(time(9, 50), time(11, 30))
HEARTBEAT_MAX_AGE_S = 30
CANCEL_CONFIRM_POLLS = 6
CANCEL_POLL_S = 0.5
_CANCELLED = frozenset({"Cancelled", "ApiCancelled", "Inactive"})


def desk_order_ref(structure_id: str) -> str:
    return DESK_REF_PREFIX + structure_id


class DeskSpec(StrictModel):
    """One supervised structure the runtime owns (``specs/<sid>.json``)."""

    schema_version: Literal["trex-desk-spec/1"] = Field(default="trex-desk-spec/1", alias="schema")
    intent_id: IdStr
    account_id: IdStr
    structure: LegStructure
    entry_order_ref: str
    registered_at: datetime


@dataclass(frozen=True)
class DeskPaths:
    root: Path

    @classmethod
    def default(cls) -> DeskPaths:
        from tree_options.desk.enter import execution_directory

        return cls(execution_directory())

    def specs(self) -> Path:
        return self.root / "specs"

    def spec(self, structure_id: str) -> Path:
        return self.specs() / f"{structure_id}.json"

    def book(self) -> Path:
        return self.root / "book.json"

    def health(self) -> Path:
        """``monitor.json``: what trex.exit_watch reads (tick outcome)."""
        return self.root / "monitor.json"

    def events(self) -> Path:
        return self.root / "events.jsonl"

    def lock(self) -> Path:
        return self.root / "runtime.lock"

    def halt(self) -> Path:
        return self.root / "HALT"

    def flatten(self) -> Path:
        return self.root / "FLATTEN"

    def resolve_flat(self, structure_id: str) -> Path:
        """Operator confirmation that a structure is flat at the broker."""
        return self.root / f"RESOLVE-FLAT-{structure_id}"


class RuntimeLocked(RuntimeError):
    """Another desk runtime holds the run dir."""


def exit_owner_ready(paths: DeskPaths, now: datetime,
                     max_age_s: int = HEARTBEAT_MAX_AGE_S) -> bool:
    """The canary's ``protective_exit_ready``: a fresh heartbeat AND the
    runtime lock held (a heartbeat alone can outlive a dead process)."""
    if not paths.book().exists():
        return False
    try:
        beat = BookState.load(paths.book(), []).heartbeat
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if beat is None:
        return False
    age = (now - beat).total_seconds()
    if not 0 <= age <= max_age_s:
        return False
    try:
        with open(paths.lock(), "a+b") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
    except BlockingIOError:
        return True  # held: the runtime is alive
    except OSError:
        return False
    return False


def assignment_plan(structure: LegStructure, snapshot: DividendSnapshot | None,
                    as_of: date) -> tuple[bool, str]:
    """The canary's ``assignment_plan_verified`` input, with the reason."""
    short_calls = [g for g in structure.legs if g.action == "SELL" and g.right == "C"]
    if not short_calls:
        return True, "no short call: expiry safety covers the hold"
    if snapshot is None:
        return False, "short call without a fresh dividend snapshot"
    hold_end = last_hold_session(structure.first_expiry)
    found = ex_dividends(snapshot, as_of=as_of, start=as_of, end=hold_end)
    if found is None:
        return False, "dividend schedule unknown"
    projected = [d for d in found if d.status != "declared"]
    if projected:
        return False, f"undeclared ex-dividend projected in the hold window ({projected[0].detail})"
    return True, f"{len(found)} declared ex-dividend(s) fed to the assignment exit"


def declared_dividend(snapshot: DividendSnapshot | None, as_of: date,
                      hold_end: date) -> DividendCalendar | None:
    """The next DECLARED ex-dividend in [as_of, hold_end] as the engine's input."""
    if snapshot is None:
        return None
    found = ex_dividends(snapshot, as_of=as_of, start=as_of, end=hold_end)
    for ex in sorted(found or (), key=lambda d: d.ex_date):
        if ex.status == "declared" and ex.cash_amount is not None and ex.cash_amount > 0:
            return DividendCalendar(ex_date=ex.ex_date, prev_session=last_hold_session(ex.ex_date),
                                    amount=ex.cash_amount)
    return None


SpotSource = Callable[[Sequence[str], datetime], Mapping[str, Decimal]]
DividendSource = Callable[[str, date], DividendSnapshot | None]
#: (title, message, priority) — the ntfy shape from trex.notify.send
NotifyFn = Callable[[str, str, str], None]

#: events worth a push, with their priority. Risk events are urgent;
#: lifecycle events are default. Everything else stays in events.jsonl only
#: (``exit_authority`` included: evidence, not an alert).
_NOTIFY_EVENTS: dict[str, str] = {
    "unknown_exposure": "urgent",
    "legs_mismatch": "urgent",
    "exit_flat_unexplained": "urgent",
    "entry_uncertain_held": "urgent",
    "gateway_lost": "urgent",
    "exit_blocked": "urgent",
    "entry_request": "default",
    "entry_filled": "default",
    "exit_begin": "default",
    "closed": "default",
}


class DeskRuntime:
    """Owns supervised structures from registration to CLOSED."""

    def __init__(self, ib: IbkrTrex, paths: DeskPaths, *,
                 supervised: SupervisedPaths | None = None,
                 spots: SpotSource | None = None,
                 dividends: DividendSource | None = None,
                 config: EngineConfig | None = None,
                 clock: Callable[[], datetime] | None = None,
                 notify: NotifyFn | None = None) -> None:
        self.ib = ib
        self.paths = paths
        self.supervised = supervised or SupervisedPaths.default()
        self.spots = spots
        self.dividends = dividends
        self.clock = clock or (lambda: datetime.now(ET))
        self.notify = notify
        configure(config or EngineConfig(entry_window=DEFAULT_ENTRY_WINDOW))
        self.orders: dict[str, Any] = {}  # structure id -> the last ib_async Trade seen
        self._lock_handle: Any = None
        self._noted: set[tuple[str, str]] = set()
        # the exit guards' read-only broker view (paper_blockers) over the
        # SAME session: no new connection, no order routing
        self._guard = IbkrSupervisedBroker(ib)
        self._started_at: float | None = None
        self._tick_failures = 0
        self._last_tick_ok_at: float | None = None

    # -- lifecycle -----------------------------------------------------------

    def acquire(self) -> None:
        """Hold the run dir for this process (one owner, one writer)."""
        self.paths.root.mkdir(parents=True, exist_ok=True)
        handle = open(self.paths.lock(), "a+b")  # held for the process life
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeLocked(str(self.paths.lock())) from None
        self._lock_handle = handle
        self._start_health()  # the lock holder is the health writer

    def release(self) -> None:
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    # -- health ---------------------------------------------------------------

    def _start_health(self) -> None:
        """Mark this run and carry the last good tick across restarts: a
        runtime killed mid-tick and restarted must not look freshly
        healthy, and its loop may never reach its own write."""
        try:
            prior = json.loads(self.paths.health().read_bytes())
            ok_at = prior.get("last_tick_ok_at")
            if isinstance(ok_at, (int, float)):
                self._last_tick_ok_at = float(ok_at)
        except (OSError, ValueError, AttributeError):
            pass  # no (readable) prior health: nothing to carry
        self._started_at = self._now().timestamp()
        self._write_health(None, tick_ran=False)

    def _write_health(self, tick_error: str | None, *, tick_ran: bool = True) -> None:
        """``monitor.json`` for the exit-machine watchdog (trex.exit_watch):
        the tick outcome, which the heartbeat cannot show (a runtime whose
        every tick fails still beats). Error class only, never the message:
        broker errors can carry account details. Failure-isolated: a health
        write can never raise into the trading loop. ``tick_ran=False``
        (acquire, beat) touches ``at`` without claiming a good tick —
        load-bearing overnight, where stale-at alarms fire at any hour."""
        try:
            now = self._now().timestamp()
            if tick_ran and tick_error is None:
                self._tick_failures = 0
                self._last_tick_ok_at = now
            elif tick_ran:
                self._tick_failures += 1
            payload = {
                "at": now,
                "started_at": self._started_at,
                "pid": os.getpid(),
                "connected": bool(self.ib.connected),
                "tick_failures": self._tick_failures,
                "last_tick_ok_at": self._last_tick_ok_at,
                "last_error": tick_error,
                "halt": self.paths.halt().exists(),
                "flatten": self.paths.flatten().exists(),
            }
            path = self.paths.health()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, path)
        except Exception:
            log.exception("desk health write failed (exit watch unaffected)")

    # -- state ---------------------------------------------------------------

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now.astimezone(ET)

    def specs(self) -> dict[str, DeskSpec]:
        out: dict[str, DeskSpec] = {}
        if not self.paths.specs().is_dir():
            return out
        for path in sorted(self.paths.specs().glob("*.json")):
            spec = DeskSpec.model_validate(json.loads(path.read_bytes()))
            if path.stem != spec.structure.id:
                raise ValueError(f"{path.name}: names structure {spec.structure.id}")
            out[spec.structure.id] = spec
        return out

    def _book(self, specs: Mapping[str, DeskSpec]) -> BookState:
        return BookState.load(self.paths.book(), list(specs))

    def _save(self, book: BookState) -> None:
        book.save(self.paths.book())

    def _event(self, event: str, **payload: Any) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        record = {"ts": self._now().isoformat(), "event": event, **payload}
        with self.paths.events().open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        priority = _NOTIFY_EVENTS.get(event)
        if priority is not None and self.notify is not None:
            try:  # a push failure must never break the trading loop
                self.notify(f"trex-desk {event}",
                            json.dumps(payload, default=str, sort_keys=True)[:300],
                            priority)
            except Exception:  # notifications are best-effort
                pass

    def _note_once(self, sid: str, what: str, *, key: str = "", **payload: Any) -> None:
        """One event per (structure, what, key) per process: a condition that
        persists across ticks alerts once, a changed one (new key) again."""
        if (sid, what + key) not in self._noted:
            self._noted.add((sid, what + key))
            self._event(what, structure=sid, **payload)

    # -- registration --------------------------------------------------------

    def register(self, effect: SupervisedEffect) -> DeskSpec:
        """Own a supervised effect BEFORE it is sent (idempotent)."""
        if self._lock_handle is None:
            raise RuntimeLocked("register requires the runtime lock")
        sid = effect.structure.id
        spec = DeskSpec(intent_id=effect.intent_id, account_id=effect.account_id,
                        structure=effect.structure,
                        entry_order_ref=supervised_order_ref(effect.intent_id),
                        registered_at=self._now())
        path = self.paths.spec(sid)
        if path.exists():
            existing = DeskSpec.model_validate(json.loads(path.read_bytes()))
            if (existing.intent_id, existing.structure) != (spec.intent_id, spec.structure):
                raise ValueError(f"{sid}: already registered for {existing.intent_id}")
            return existing
        self.paths.specs().mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(canonical_bytes(spec.model_dump(mode="json", by_alias=True)))
        os.replace(tmp, path)
        specs = self.specs()
        book = self._book(specs)
        self._save(book)
        self._event("registered", structure=sid, intent=effect.intent_id)
        self.ib.prepare([effect.structure])
        return spec

    # -- one tick ------------------------------------------------------------

    def tick(self) -> None:
        if self._lock_handle is None:
            raise RuntimeLocked("tick requires the runtime lock")
        try:
            self._tick()
        except Exception as error:
            # the error class only: broker errors can carry account details.
            # Re-raised: the loop propagates and the unit restarts (unchanged).
            self._write_health(type(error).__name__)
            raise
        self._write_health(None)

    def _tick(self) -> None:
        now = self._now()
        specs = self.specs()
        book = self._book(specs)
        if specs:
            self.ib.prepare([s.structure for s in specs.values()])
        live = {str(getattr(t.order, "orderRef", "") or ""): t for t in self.ib._ib.openTrades()}
        snap = self._snapshot(specs, now)
        halt = self.paths.halt().exists()
        flatten = self.paths.flatten().exists()
        for sid, spec in specs.items():
            st = book.structures[sid]
            if st.status is Status.CLOSED:
                continue
            try:
                self._structure_tick(spec, st, snap, live, halt=halt, flatten=flatten, now=now)
            finally:
                self._save(book)
        book.heartbeat = now
        self._save(book)

    def beat(self) -> None:
        """Heartbeat only (outside the session: no decisions, no orders).
        Also refreshes ``monitor.json`` without claiming a good tick."""
        if self._lock_handle is None:
            raise RuntimeLocked("beat requires the runtime lock")
        book = self._book(self.specs())
        book.heartbeat = self._now()
        self._save(book)
        self._write_health(None, tick_ran=False)

    def _snapshot(self, specs: Mapping[str, DeskSpec], now: datetime) -> Snapshot:
        structures = [s.structure for s in specs.values()]
        snap = self.ib.snapshot(structures, now)
        spots: Mapping[str, Decimal] = {}
        underlyings = sorted({s.underlying for s in structures})
        if self.spots is not None and underlyings:
            spots = self.spots(underlyings, now)
        dividends: dict[str, DividendCalendar] = {}
        short_call_mids: dict[str, Decimal | None] = {}
        for spec in specs.values():
            s = spec.structure
            if self.dividends is not None:
                div = declared_dividend(self.dividends(s.underlying, now.date()), now.date(),
                                        last_hold_session(s.first_expiry))
                if div is not None:
                    dividends[s.underlying] = div
            for i, leg in enumerate(s.legs):
                if leg.action == "SELL" and leg.right == "C":
                    quote = self.ib.leg_quote(s.id, i)
                    mid = None if quote is None else cents((quote[0] + quote[1]) / 2)
                    short_call_mids[short_call_key(s.id, leg.strike, leg.expiry)] = mid
        return replace(snap, spots=dict(spots), dividends=dividends,
                       short_call_mids=short_call_mids)

    def _trade_for(self, spec: DeskSpec, st: StructureState, live: Mapping[str, Any]) -> Any:
        sid = spec.structure.id
        if st.status in (Status.PLANNED, Status.ENTER_WORKING):
            trade = live.get(spec.entry_order_ref)
        else:
            trade = live.get(desk_order_ref(sid))
        if trade is not None:
            self.orders[sid] = trade
            return trade
        held = self.orders.get(sid)
        if held is None:
            return None
        order_id = str(getattr(held.order, "orderId", ""))
        expected = st.entry_order if st.status in (Status.PLANNED, Status.ENTER_WORKING) \
            else st.exit_order
        return held if order_id == expected else None

    def _working(self, spec: DeskSpec, st: StructureState, trade: Any) -> WorkingOrder | None:
        role = adopted_role(spec.structure, st, str(trade.order.action))
        if role is None:
            self._note_once(spec.structure.id, "unknown_exposure",
                            order=str(getattr(trade.order, "orderId", "?")),
                            side=str(trade.order.action), status=st.status.value)
            return None
        info = self.ib.order_status(OrderRef(spec.structure.id, str(trade.order.action),
                                             int(trade.order.totalQuantity),
                                             Decimal(str(trade.order.lmtPrice)), trade))
        return WorkingOrder(role=role, filled=info.filled, avg_fill_price=info.avg_fill_price,
                            order_id=str(trade.order.orderId))

    def _structure_tick(self, spec: DeskSpec, st: StructureState, snap: Snapshot,
                        live: Mapping[str, Any], *, halt: bool, flatten: bool,
                        now: datetime) -> None:
        sid = spec.structure.id
        if st.status is Status.PLANNED:
            self._resolve_planned(spec, st, live, now)
            if st.status is not Status.ENTER_WORKING:
                return
        trade = self._trade_for(spec, st, live)
        if trade is None and st.status is Status.ENTER_WORKING:
            self._resolve_entry_without_trade(spec, st, now)
            if st.status is not Status.OPEN:
                return
        if trade is None and st.status is Status.EXIT_WORKING and st.exit_order:
            if not self._resolve_exit_without_trade(spec, st, now) or \
                    st.status is Status.CLOSED:
                return
        working = self._working(spec, st, trade) if trade is not None else None
        if trade is not None and working is None:
            return  # unknown exposure: noted, never acted on
        decision = step(spec.structure, st, snap, working)
        st.stop_ticks = decision.stop_ticks
        status = str(getattr(trade.orderStatus, "status", "")) if trade is not None else ""
        self._transitions(spec, st, status, now)
        if st.status is Status.CLOSED:
            return
        if flatten:
            self._flatten(spec, st, trade, snap, halt=halt, now=now)
            return
        action = decision.action
        if isinstance(action, NoAction):
            return
        if isinstance(action, AbortEntry):
            if trade is not None and st.status is Status.ENTER_WORKING:
                self._cancel(sid, trade, why=action.reason)
            return
        if isinstance(action, EntryOrder):
            self._note_once(sid, "entry_reprice_not_sent",
                            reason="a supervised permit binds one limit")
            return
        if isinstance(action, CloseOrder):
            self._close(spec, st, action, trade, halt=halt, now=now)

    # -- entry lane ----------------------------------------------------------

    def _resolve_planned(self, spec: DeskSpec, st: StructureState,
                         live: Mapping[str, Any], now: datetime) -> None:
        sid = spec.structure.id
        trade = live.get(spec.entry_order_ref)
        if trade is not None:
            st.entry_order = str(trade.order.orderId)
            st.to(Status.ENTER_WORKING, now)
            self.orders[sid] = trade
            self._event("entry_adopted", structure=sid, order=st.entry_order)
            return
        sup = self.supervised
        terminal = sup.terminal(spec.intent_id)
        reconciled = sup.reconciled(spec.intent_id)
        if reconciled.exists():
            verdict = json.loads(reconciled.read_bytes())
            if verdict.get("verdict") == "confirmed_not_submitted":
                self._close_unentered(st, "not_submitted", now, sid)
            elif verdict.get("verdict") == "confirmed_submitted" and verdict.get("broker_order_id"):
                st.entry_order = str(verdict["broker_order_id"])
                st.to(Status.ENTER_WORKING, now)
                self._event("entry_adopted", structure=sid, order=st.entry_order,
                            via="reconciliation")
            return
        if terminal.exists():
            receipt = json.loads(terminal.read_bytes())
            outcome = receipt.get("outcome")
            if outcome == "acknowledged":
                st.entry_order = str(receipt["broker_order_id"])
                st.to(Status.ENTER_WORKING, now)
                self._event("entry_adopted", structure=sid, order=st.entry_order, via="receipt")
            elif outcome == "rejected":
                self._close_unentered(st, "entry_rejected", now, sid)
            else:
                self._note_once(sid, "entry_uncertain_held", reason=receipt.get("reason"))
            return
        pending = sup.pending(spec.intent_id)
        if pending.exists():
            deadline = datetime.fromisoformat(json.loads(pending.read_bytes())["send_deadline"])
            if (now - deadline).total_seconds() >= 0:
                # send() refuses a passed deadline: this intent can never go out
                self._close_unentered(st, "not_sent_by_deadline", now, sid)

    def _close_unentered(self, st: StructureState, reason: str, now: datetime,
                         sid: str) -> None:
        st.to(Status.CLOSED, now)
        st.close_reason = reason
        self._event("closed", structure=sid, reason=reason)

    def _resolve_entry_without_trade(self, spec: DeskSpec, st: StructureState,
                                     now: datetime) -> None:
        """The entry order left the live view (filled or died, maybe while
        this process was down): only broker evidence moves the book."""
        sid = spec.structure.id
        evidence = self.ib.entry_fill_evidence(sid, st.entry_order)
        if evidence is None:
            self._note_once(sid, "entry_evidence_inconclusive", order=st.entry_order)
            return
        qty, avg = evidence
        if qty:
            drain(st, WorkingOrder(role="entry", filled=qty,
                                   avg_fill_price=avg or Decimal(0),
                                   order_id=str(st.entry_order)))
            st.to(Status.OPEN, now)
            self._event("entry_filled", structure=sid, filled=st.filled_qty,
                        avg=str(st.entry_fill), via="evidence")
        else:
            self._close_unentered(st, "entry_unfilled", now, sid)

    # -- transitions -----------------------------------------------------------

    def _transitions(self, spec: DeskSpec, st: StructureState, order_status: str,
                     now: datetime) -> None:
        sid = spec.structure.id
        done = order_status in DONE_STATES
        if st.status is Status.ENTER_WORKING:
            if st.filled_qty >= spec.structure.quantity or (done and st.filled_qty > 0):
                st.to(Status.OPEN, now)
                self._event("entry_filled", structure=sid, filled=st.filled_qty,
                            avg=str(st.entry_fill))
            elif done:
                self._close_unentered(st, "entry_unfilled", now, sid)
        elif st.status is Status.EXIT_WORKING and st.open_qty <= 0 and (done or not order_status):
            st.to(Status.CLOSED, now)
            st.close_reason = st.exit_reason or "flat"
            self._event("closed", structure=sid, reason=st.close_reason,
                        exit_fill=str(st.exit_fill))

    # -- exits -----------------------------------------------------------------

    def _leg_positions(self, spec: DeskSpec) -> dict[int, Decimal] | None:
        """The bound account's position in each leg (by conId); None when
        the broker's positions cannot be read."""
        sid = spec.structure.id
        try:
            held = {p.con_id: p.qty for p in self.ib.positions(spec.account_id)}
        except Exception as error:
            self._note_once(sid, "legs_unverifiable", key=type(error).__name__,
                            error=repr(error))
            return None
        return {con: held.get(con, Decimal(0)) for con in self.ib.leg_con_ids(sid)}

    def _mismatch(self, spec: DeskSpec, held: Mapping[int, Decimal],
                  qty: int) -> list[str]:
        """Legs whose position is not ``qty`` packages (BUY long, SELL short)."""
        out: list[str] = []
        for con_id, leg in zip(self.ib.leg_con_ids(spec.structure.id), spec.structure.legs,
                               strict=True):
            want = Decimal(qty * leg.ratio) * (1 if leg.action == "BUY" else -1)
            if held.get(con_id, Decimal(0)) != want:
                out.append(f"{con_id}:held={held.get(con_id, 0)}:expected={want}")
        return out

    def _legs_held(self, spec: DeskSpec, st: StructureState) -> bool:
        """The bound account holds exactly ``open_qty`` of every leg."""
        held = self._leg_positions(spec)
        if held is None:
            return False
        wrong = self._mismatch(spec, held, st.open_qty)
        if wrong:
            self._note_once(spec.structure.id, "legs_mismatch", key="|".join(wrong),
                            legs=wrong, open_qty=st.open_qty)
            return False
        return True

    def _resolve_exit_without_trade(self, spec: DeskSpec, st: StructureState,
                                    now: datetime) -> bool:
        """The exit order left the live view (filled or expired, maybe while
        this process was down). Record what the broker PROVES filled, then
        cross-check the legs. True: the book agrees with the broker (closed
        now, or a remainder the normal flow re-places). False: held, with an
        alert; nothing is sent.

        Legs flat beyond what today's executions of the exit order explain
        is ambiguous (a fill on an earlier day, or positions not loaded): an
        automatic close could abandon a real position, so it waits for the
        operator's ``RESOLVE-FLAT-<sid>`` file (the unexplained packages
        close UNPRICED)."""
        sid = spec.structure.id
        # IbkrTrex.entry_fill_evidence reads ANY order's executions today, in
        # debit orientation (BUY-leg prices minus SELL-leg prices)
        evidence = self.ib.entry_fill_evidence(sid, st.exit_order)
        executed = evidence[0] if evidence is not None else 0
        if evidence is not None and executed:
            avg = evidence[1]
            if drain(st, WorkingOrder(role="exit", filled=executed,
                                      avg_fill_price=avg or Decimal(0),
                                      order_id=str(st.exit_order))):
                self._event("exit_fill", structure=sid, filled=st.exit_filled_qty,
                            avg=str(st.exit_fill), via="evidence")
        held = self._leg_positions(spec)
        if held is None:
            return False
        wrong = self._mismatch(spec, held, st.open_qty)
        if not wrong:
            if st.open_qty <= 0:
                st.to(Status.CLOSED, now)
                st.close_reason = st.exit_reason or "flat"
                self._event("closed", structure=sid, reason=st.close_reason,
                            exit_fill=str(st.exit_fill), via="evidence")
            return True
        flat = all(q == 0 for q in held.values())
        if flat and st.open_qty > 0:  # flat beyond what today's executions explain
            resolve = self.paths.resolve_flat(sid)
            if resolve.exists():
                unpriced = st.open_qty
                st.exit_filled_qty += unpriced
                st.exit_unpriced_qty += unpriced
                st.to(Status.CLOSED, now)
                st.close_reason = "operator_confirmed_flat"
                os.replace(resolve, resolve.with_name(resolve.name + ".applied"))
                self._event("closed", structure=sid, reason=st.close_reason,
                            unpriced_packages=unpriced)
                return True
            self._note_once(sid, "exit_flat_unexplained", open_qty=st.open_qty,
                            exit_order=st.exit_order,
                            operator=f"verify flat at the broker, then touch {resolve.name}")
            return False
        self._note_once(sid, "legs_mismatch", key="|".join(wrong), legs=wrong,
                        open_qty=st.open_qty)
        return False

    def _owner_epoch(self) -> str | None:
        """This run dir's owner epoch from ``owner.json`` (the desk process
        writes it at startup); None when absent or unreadable."""
        try:
            doc = json.loads((self.paths.root / "owner.json").read_bytes())
        except (OSError, ValueError):
            return None
        epoch = doc.get("owner_epoch") if isinstance(doc, dict) else None
        return epoch if isinstance(epoch, str) else None

    def _open_tagged(self, order_ref: str) -> dict[str, str] | None:
        """The broker's OPEN orders carrying ``order_ref``, keyed by identity
        (permId, else the session orderId) with the best order id each; None
        when the open-order view could not be read (absence is unproven).

        Open orders ONLY: a same-day EXECUTED desk order is legal in a
        reprice cycle (the completed-order and execution views would flag
        the very remainder this lane is about to replace)."""
        try:
            trades = self.ib._ib.reqAllOpenOrders()
        except Exception:  # any failed read: absence is not proven
            return None
        found: dict[str, str] = {}
        for trade in trades:
            if str(getattr(trade.order, "orderRef", "") or "") != order_ref:
                continue
            perm = int(getattr(trade.order, "permId", 0) or 0)
            oid = int(getattr(trade.order, "orderId", 0) or 0)
            key = f"perm:{perm}" if perm else f"oid:{oid}"
            found[key] = str(oid) if oid else key
        return found

    def _place_close(self, spec: DeskSpec, st: StructureState, side: str, qty: int,
                     limit: Decimal, *, halt: bool = False) -> Any:
        """Send one close order, guarded. Every guard refuses BEFORE the
        order exists and is fail-closed-with-retry: nothing is sent, one
        deduped urgent ``exit_blocked`` push, the next tick re-enters the
        lane. An expired mandate NEVER blocks an exit (ruling 2b): the
        supervised mandate covers entries only, so it is not consulted."""
        s = spec.structure
        blockers = self._guard.paper_blockers(spec.account_id)
        if blockers:
            self._note_once(s.id, "exit_blocked", key=",".join(blockers),
                            blockers=blockers, reason=st.exit_reason)
            return None
        existing = self._open_tagged(desk_order_ref(s.id))
        if existing is None:
            self._note_once(s.id, "exit_blocked", key="duplicate_check_unreadable",
                            reason="duplicate_check_unreadable", exit_reason=st.exit_reason)
            return None
        if existing:
            self._note_once(s.id, "exit_blocked",
                            key="duplicate:" + ",".join(sorted(existing)),
                            reason="exit_tag_already_open", orders=sorted(existing.values()),
                            exit_reason=st.exit_reason)
            return None
        owner_epoch = self._owner_epoch()
        self._note_once(s.id, "exit_authority", owner_epoch=owner_epoch, pid=os.getpid(),
                        client_id=self.ib.client_id, port=self.ib.port,
                        account=spec.account_id, halt=halt)
        contract, order = self.ib._order(s, side, qty, limit)
        order.orderRef = desk_order_ref(s.id)
        order.account = spec.account_id
        trade = self.ib._ib.placeOrder(contract, order)
        self.orders[s.id] = trade
        st.exit_order = str(trade.order.orderId)
        st.exit_order_seen, st.exit_order_notional, st.exit_order_unpriced = 0, None, 0
        self._event("exit_order", structure=s.id, side=side, qty=qty, limit=str(limit),
                    order=st.exit_order, reason=st.exit_reason, owner_epoch=owner_epoch)
        return trade

    def _cancel(self, sid: str, trade: Any, *, why: str) -> bool:
        """Cancel and WAIT for the confirmation; True once confirmed."""
        if str(trade.orderStatus.status) in DONE_STATES:
            return True
        self.ib._ib.cancelOrder(trade.order)
        self._event("cancel", structure=sid, order=str(trade.order.orderId), why=why)
        for _ in range(CANCEL_CONFIRM_POLLS):
            self.ib.sleep(CANCEL_POLL_S)
            if str(trade.orderStatus.status) in DONE_STATES:
                return True
        self._event("cancel_unconfirmed", structure=sid, order=str(trade.order.orderId))
        return False

    def _close(self, spec: DeskSpec, st: StructureState, action: CloseOrder, trade: Any, *,
               halt: bool, now: datetime) -> None:
        sid = spec.structure.id
        if action.limit is None:
            self._note_once(sid, "exit_pending_no_quote", reason=action.reason.value)
            return
        if halt:
            self._note_once(sid, "halt_no_new_orders", reason=action.reason.value)
            return
        if st.status is Status.OPEN:
            if st.open_qty <= 0 or not self._legs_held(spec, st):
                return
            st.to(Status.EXIT_WORKING, now)
            st.exit_reason = action.reason.value
            self._event("exit_begin", structure=sid, reason=action.reason.value,
                        qty=st.open_qty)
            self._place_close(spec, st, action.side, st.open_qty, action.limit, halt=halt)
            return
        if st.status is not Status.EXIT_WORKING:
            return
        if trade is not None and str(trade.orderStatus.status) not in DONE_STATES:
            if Decimal(str(trade.order.lmtPrice)) == action.limit:
                return
            if not self._cancel(sid, trade, why="reprice"):
                return
            st.exit_cycles += 1
        if trade is not None:
            working = self._working(spec, st, trade)
            if working is None:
                return
            drain(st, working)
        if st.open_qty <= 0:
            return  # the final fills flattened it: the next tick closes the book
        if not self._legs_held(spec, st):
            return
        self._place_close(spec, st, action.side, st.open_qty, action.limit, halt=halt)

    def _flatten(self, spec: DeskSpec, st: StructureState, trade: Any, snap: Snapshot, *,
                 halt: bool, now: datetime) -> None:
        sid = spec.structure.id
        s = spec.structure
        if st.status is Status.ENTER_WORKING:
            if trade is not None:
                self._cancel(sid, trade, why="flatten")
            return
        quote = snap.quotes.get(sid)
        if quote is None:
            self._note_once(sid, "flatten_pending_no_quote")
            return
        if s.close_side == "SELL":
            limit = max(cents(quote.bid), Decimal("0.01"))
        else:
            width = s.width
            if width is None:
                return
            limit = min(max(cents(quote.ask), Decimal("0.01")), width)
        action = CloseOrder(ExitReason.FLATTEN, s.close_side, st.open_qty, limit)
        if st.status is Status.EXIT_WORKING:
            st.exit_reason = ExitReason.FLATTEN.value
        self._close(spec, st, action, trade, halt=halt, now=now)
