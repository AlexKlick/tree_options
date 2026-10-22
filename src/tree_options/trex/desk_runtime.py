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
- **Custody.** ``beat`` (single-threaded, under the lock: consistent) takes
  the ``trex.desk_backup`` snapshot on a ``BACKUP-REQUEST`` file or on the
  first beat after 16:30 ET of a session day; a failure is the
  ``backup_failed`` event, never an exception in the loop.

Assignment risk: ``assignment_plan`` is the canary's
``assignment_plan_verified`` input. A structure without a short call is
covered by the engine's expiry safety; one with a short call needs a fresh
declared-dividend feed and no projected (undeclared) ex-date inside its
hold window, because the engine's pre-ex-date rule needs the exact date.

Wave 2 (lane C, docs/product/WAVE2-CONTRACTS.md): the same owner exits
desk-owned long EQUITY positions.

- **Specs.** ``specs/<sid>.json`` holds ``trex-desk-spec/1`` (options; still
  what ``register`` writes for an option effect, byte for byte) or
  ``trex-desk-spec/2`` (``desk_spec.load_spec``): a v2 option spec runs as
  the option it describes; an equity spec (its id is the intent id) runs in
  the equity lane. ``specs()`` stays the OPTION view; ``equity_specs()`` is
  the equity one. An unknown schema fails the tick (exposure is uncertain).
- **Rules** come from the bound ``exit_policy`` through the pure
  ``trex.equity_exits`` (stop confirmed over consecutive new real-time
  ticks, target, forced exit on the deadline session at min(15:45, close
  minus 15 min), deadline passed). Every exit is a DAY limit SELL
  marketable at the real-time bid, tagged ``trex:desk:<intent_id>``, for
  the open whole shares only, repriced down (cancel, confirm, replace) when
  the bid falls below it. No resting broker stop exists.
- **Holdings check** before ANY sell: the broker's position in the bound
  account and conId must show at least the book's open shares (every live
  desk equity position on that contract summed); the sell is only the
  position's own open shares. Fewer shares, a fractional or non-STK row
  sends nothing and records ``reconciliation_required`` (urgent push); the
  next tick retries. More shares (an outside holding) are flagged the same
  way but never block the exit: reconciliation blocks entries only (C2.5).
  A restart that finds the sell gone (``_equity_exit_without_trade``)
  expects exactly the book plus the outside holding seen at the sell.
- **Containment.** A failure confined to one structure, or to the option
  lane's prepare/snapshot, is recorded (``structure_tick_failed``, urgent)
  and every other structure still runs: one bad spec never starves another
  position's exits. The degraded tick reports its error class in
  ``monitor.json`` and does not refresh the heartbeat (no new entry arms).
  CLOSED option structures are never prepared again.
- **PAUSE_ENTRIES** (``contracts.layout``) cancels the unfilled remainder
  of any working entry, option or equity; it never blocks an exit. HALT is
  unchanged (no new orders, exits included). A partial entry that ends
  (pause, rule, expiry) opens with exactly the shares filled.
- **Health.** While an equity position is held, ``monitor.json`` adds
  ``quote_guarded`` (symbols) and ``quote_blind`` (symbol: first instant
  without a fresh real-time quote in the regular session) for
  ``trex.exit_watch``; an option-only book keeps the nine-key schema.

Wave 2b (lane F, owner orchestration) adds two seams and nothing to the
exit logic:

- ``owned_specs()``: both spec views (options, equities) from ONE read of
  ``specs/``, for the owner's loss facts and books (``specs()`` stays the
  option view).
- **Connection hooks.** ``connect_hooks`` run on the first connected
  observation of this process (a restart after exit 6 is a reconnect) and
  on every later disconnected-to-connected edge, observed at the top of
  every ``tick`` and ``beat``: the owner refreshes its status files there
  (``trex.desk_books``). Each hook is failure-isolated; the edge is the
  ``gateway_connected`` event.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, overload

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.desk.dividends import DividendSnapshot, ex_dividends
from tree_options.trex.clock import ET, EntryWindow
from tree_options.trex.contracts.books import base_exec_id_of
from tree_options.trex.contracts.canonical import model_bytes
from tree_options.trex.contracts.desk_spec_v2 import EquityDeskSpecV2, desk_spec_v2_for
from tree_options.trex.contracts.effects import SupervisedEquityEffect
from tree_options.trex.contracts.layout import PAUSE_ENTRIES
from tree_options.trex.contracts.vocabulary import DeskEvent
from tree_options.trex.desk_backup import OwnerBackups, default_backup_root
from tree_options.trex.desk_spec import SPEC_SCHEMA as SPEC_SCHEMA
from tree_options.trex.desk_spec import DeskSpec as DeskSpec
from tree_options.trex.desk_spec import load_spec, spec_id
from tree_options.trex.engine import (
    AbortEntry,
    CloseOrder,
    DividendCalendar,
    EngineConfig,
    EntryOrder,
    ExitReason,
    NoAction,
    Role,
    Snapshot,
    WorkingOrder,
    adopted_role,
    configure,
    drain,
    last_hold_session,
    short_call_key,
    step,
)
from tree_options.trex.equity_exits import (
    EXIT_EVENTS,
    EXIT_QUOTE_BLIND,
    EquityAbort,
    EquityClose,
    EquityExitReason,
    EquityQuote,
    ExitQuoteWatch,
    decide_equity,
    in_regular_session,
    marketable_limit,
)
from tree_options.trex.ibkr import DONE_STATES, ORDER_REF_PREFIX, IbkrTrex, OrderRef
from tree_options.trex.plan import LegStructure, cents
from tree_options.trex.state import ENTRY_LANE, BookState, Status, StructureState
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised_ibkr import (
    IbkrSupervisedBroker,
    SupervisedEffect,
    supervised_order_ref,
)

log = logging.getLogger("trex.desk_runtime")

DESK_REF_PREFIX = ORDER_REF_PREFIX + "desk:"
#: the E6 entry window; supervised entries only ever cancel inside it
DEFAULT_ENTRY_WINDOW = EntryWindow(time(9, 50), time(11, 30))
HEARTBEAT_MAX_AGE_S = 30
CANCEL_CONFIRM_POLLS = 6
CANCEL_POLL_S = 0.5
_CANCELLED = frozenset({"Cancelled", "ApiCancelled", "Inactive"})
#: an equity quote older than this (or not real-time) leaves its symbol
#: blind in monitor.json: the stop and target exits cannot act on it
QUOTE_FRESH_S = 60
#: ib_async ``Ticker.marketDataType``: 1 real-time (2 frozen, 3 delayed, 4
#: delayed-frozen); only real-time quotes drive equity exits
REALTIME_MARKET_DATA = 1
#: errors that mean the gateway connection went (``supervised_desk``'s
#: DISCONNECT_ERRORS): never contained to one structure, the tick fails
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError)


def desk_order_ref(structure_id: str) -> str:
    return DESK_REF_PREFIX + structure_id


def _decimal(value: Any) -> Decimal | None:
    """A broker number (ib_async floats) exactly as reported; None for junk,
    NaN or infinity."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _shares(value: Any) -> int | None:
    """A whole, non-negative share count, else None (fractional, junk)."""
    number = _decimal(value)
    if number is None or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _cents_floor(value: Any) -> int | None:
    """A positive broker price (dollars) as integer cents, rounded DOWN (a
    SELL priced from it stays marketable); None when there is no price."""
    number = _decimal(value)
    if number is None or number <= 0:
        return None
    cents_ = int((number * 100).to_integral_value(rounding=ROUND_FLOOR))
    return cents_ if cents_ > 0 else None


def _qty_text(qty: Decimal) -> str:
    """A broker quantity for an event: whole numbers without a fraction."""
    return str(int(qty)) if qty == qty.to_integral_value() else str(qty.normalize())


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

    def pause_entries(self) -> Path:
        """Wave 2 entries-only pause: cancels working entries' unfilled
        remainders; exits, fills and reconciliation continue."""
        return self.root / PAUSE_ENTRIES

    def resolve_flat(self, structure_id: str) -> Path:
        """Operator confirmation that a structure is flat at the broker."""
        return self.root / f"RESOLVE-FLAT-{structure_id}"


class RuntimeLocked(RuntimeError):
    """Another desk runtime holds the run dir."""


def exit_owner_ready(paths: DeskPaths, now: datetime, max_age_s: int = HEARTBEAT_MAX_AGE_S) -> bool:
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


def assignment_plan(
    structure: LegStructure, snapshot: DividendSnapshot | None, as_of: date
) -> tuple[bool, str]:
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


def declared_dividend(
    snapshot: DividendSnapshot | None, as_of: date, hold_end: date
) -> DividendCalendar | None:
    """The next DECLARED ex-dividend in [as_of, hold_end] as the engine's input."""
    if snapshot is None:
        return None
    found = ex_dividends(snapshot, as_of=as_of, start=as_of, end=hold_end)
    for ex in sorted(found or (), key=lambda d: d.ex_date):
        if ex.status == "declared" and ex.cash_amount is not None and ex.cash_amount > 0:
            return DividendCalendar(
                ex_date=ex.ex_date,
                prev_session=last_hold_session(ex.ex_date),
                amount=ex.cash_amount,
            )
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
    # Wave 2 equities: an exit that cannot go out until someone looks
    DeskEvent.RECONCILIATION_REQUIRED.value: "urgent",
    "equity_contract_mismatch": "urgent",
    "equity_unqualified": "urgent",
    # one structure's tick failed (contained): its exits are not running
    "structure_tick_failed": "urgent",
    # Wave 2b/4: an equity close wanted out with no usable bid for 300 s
    # (equity_exits.ExitQuoteWatch; once per structure per ET session)
    EXIT_QUOTE_BLIND: "urgent",
    "backup_failed": "default",
    "entry_request": "default",
    "entry_filled": "default",
    "exit_begin": "default",
    "closed": "default",
}


class DeskRuntime:
    """Owns supervised structures from registration to CLOSED."""

    def __init__(
        self,
        ib: IbkrTrex,
        paths: DeskPaths,
        *,
        supervised: SupervisedPaths | None = None,
        spots: SpotSource | None = None,
        dividends: DividendSource | None = None,
        config: EngineConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        notify: NotifyFn | None = None,
        backup_root: Path | None = None,
    ) -> None:
        self.ib = ib
        self.paths = paths
        self.supervised = supervised or SupervisedPaths.default()
        self.backups = OwnerBackups(
            paths.root, self.supervised, backup_root or default_backup_root(paths.root)
        )
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
        # Wave 2 equities: intent id -> the qualified Stock (its market data
        # is held through IbkrTrex's shared per-conId subscription)
        self._equity_contracts: dict[str, Any] = {}
        self._tick_equities: dict[str, EquityDeskSpecV2] = {}
        self._quote_guarded: set[str] = set()
        self._quote_blind: dict[str, str] = {}  # symbol -> first blind instant
        # Wave 2b/4: an equity close pending without a usable bid raises
        # exit_quote_blind once per structure per ET session (in memory)
        self.exit_quotes = ExitQuoteWatch()
        # the first contained failure's class in the last tick, else None
        self._degraded: str | None = None
        # Wave 2b: run after a (re)connect is observed (observe_connection)
        self.connect_hooks: list[Callable[[], None]] = []
        self._seen_connected: bool | None = None  # None: not observed yet

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
            payload: dict[str, Any] = {
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
            if self._quote_guarded:  # only while an equity is held
                payload["quote_guarded"] = sorted(self._quote_guarded)
                payload["quote_blind"] = dict(self._quote_blind)
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
        """The OPTION specs (v1, and v2 option specs as the option they
        describe). Equity specs are ``equity_specs``; any unreadable or
        unknown spec raises (the run dir's exposure is uncertain)."""
        return self._all_specs()[0]

    def equity_specs(self) -> dict[str, EquityDeskSpecV2]:
        """The ``trex-desk-spec/2`` equity specs, by intent id."""
        return self._all_specs()[1]

    def owned_specs(self) -> tuple[dict[str, DeskSpec], dict[str, EquityDeskSpecV2]]:
        """(option specs, equity specs) from one read of ``specs/``: what
        the owner's loss facts and books count. Raises like ``specs``."""
        return self._all_specs()

    def _all_specs(self) -> tuple[dict[str, DeskSpec], dict[str, EquityDeskSpecV2]]:
        options: dict[str, DeskSpec] = {}
        equities: dict[str, EquityDeskSpecV2] = {}
        if not self.paths.specs().is_dir():
            return options, equities
        for path in sorted(self.paths.specs().glob("*.json")):
            spec = load_spec(json.loads(path.read_bytes()))
            sid = spec_id(spec)
            if path.stem != sid:
                raise ValueError(f"{path.name}: names structure {sid}")
            if isinstance(spec, DeskSpec):
                options[sid] = spec
            else:
                equities[sid] = spec
        return options, equities

    def _book(
        self, specs: Mapping[str, DeskSpec], equities: Mapping[str, EquityDeskSpecV2] | None = None
    ) -> BookState:
        return BookState.load(self.paths.book(), [*specs, *(equities or {})])

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
                self.notify(
                    f"trex-desk {event}",
                    json.dumps(payload, default=str, sort_keys=True)[:300],
                    priority,
                )
            except Exception:  # notifications are best-effort
                pass

    def _note_once(self, sid: str, what: str, *, key: str = "", **payload: Any) -> None:
        """One event per (structure, what, key) per process: a condition that
        persists across ticks alerts once, a changed one (new key) again."""
        if (sid, what + key) not in self._noted:
            self._noted.add((sid, what + key))
            self._event(what, structure=sid, **payload)

    # -- registration --------------------------------------------------------

    @overload
    def register(self, effect: SupervisedEffect) -> DeskSpec: ...

    @overload
    def register(self, effect: SupervisedEquityEffect) -> EquityDeskSpecV2: ...

    def register(
        self, effect: SupervisedEffect | SupervisedEquityEffect
    ) -> DeskSpec | EquityDeskSpecV2:
        """Own a supervised effect BEFORE it is sent (idempotent). An option
        effect writes its ``trex-desk-spec/1`` spec exactly as before; an
        equity effect its ``trex-desk-spec/2`` spec (``desk_spec_v2_for``)."""
        if self._lock_handle is None:
            raise RuntimeLocked("register requires the runtime lock")
        if isinstance(effect, SupervisedEquityEffect):
            return self._register_equity(effect)
        sid = effect.structure.id
        spec = DeskSpec(
            intent_id=effect.intent_id,
            account_id=effect.account_id,
            structure=effect.structure,
            entry_order_ref=supervised_order_ref(effect.intent_id),
            registered_at=self._now(),
        )
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

    def _register_equity(self, effect: SupervisedEquityEffect) -> EquityDeskSpecV2:
        sid = effect.intent_id
        spec = desk_spec_v2_for(effect, self._now())
        assert isinstance(spec, EquityDeskSpecV2)
        path = self.paths.spec(sid)
        if path.exists():
            existing = load_spec(json.loads(path.read_bytes()))
            if not isinstance(existing, EquityDeskSpecV2) or (
                existing.intent_id,
                existing.account_id,
                existing.entry_order_ref,
                existing.position,
            ) != (spec.intent_id, spec.account_id, spec.entry_order_ref, spec.position):
                raise ValueError(f"{sid}: already registered for {existing.intent_id}")
            return existing
        self.paths.specs().mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(model_bytes(spec))
        os.replace(tmp, path)
        options, equities = self._all_specs()
        self._save(self._book(options, equities))
        self._event("registered", structure=sid, intent=sid, asset_kind="equity")
        self._equity_contract(spec)  # qualify and subscribe now; a failure is noted
        return spec

    # -- one tick ------------------------------------------------------------

    def observe_connection(self) -> bool:
        """Whether the session is up; on the first connected observation of
        this process, and on each disconnected-to-connected edge after it,
        record ``gateway_connected`` and run ``connect_hooks`` (each one
        failure-isolated: a hook can never break the trading loop)."""
        try:
            up = bool(self.ib.connected)
        except Exception:  # an unreadable session state is not a live session
            up = False
        if up and self._seen_connected is not True:
            first = self._seen_connected is None
            self._seen_connected = True
            try:
                self._event("gateway_connected", first=first)
            except Exception:
                log.exception("gateway_connected event not recorded")
            for hook in list(self.connect_hooks):
                try:
                    hook()
                except Exception:
                    log.exception("connect hook failed (the owner loop is unaffected)")
        elif not up and self._seen_connected:
            self._seen_connected = False
        return up

    def tick(self) -> None:
        if self._lock_handle is None:
            raise RuntimeLocked("tick requires the runtime lock")
        self.observe_connection()
        try:
            self._tick()
        except Exception as error:
            # the error class only: broker errors can carry account details.
            # Re-raised: the loop propagates and the unit restarts (unchanged).
            self._write_health(type(error).__name__)
            raise
        # a structure's contained failure still counts as a failing tick
        self._write_health(self._degraded)

    def _tick(self) -> None:
        """One tick. A failure confined to one structure (or to the option
        lane's prepare/snapshot) is contained: recorded, and every other
        structure still runs, so one bad spec can never starve another
        position's exits. Such a degraded tick reports its error class in
        ``monitor.json`` and leaves the heartbeat alone (the arm gate stays
        shut for new entries). Spec loading, the live-order view and a lost
        gateway still fail the whole tick, as before."""
        now = self._now()
        specs, equities = self._all_specs()
        self._tick_equities = equities
        book = self._book(specs, equities)
        failed: list[str] = []
        active = {
            sid: spec
            for sid, spec in specs.items()
            if book.structures[sid].status is not Status.CLOSED
        }
        ready = self._prepare_options(active, failed)
        live = {str(getattr(t.order, "orderRef", "") or ""): t for t in self.ib._ib.openTrades()}
        halt = self.paths.halt().exists()
        flatten = self.paths.flatten().exists()
        paused = self.paths.pause_entries().exists()
        try:
            snap = self._snapshot(ready, now)
        except Exception as error:
            self._contain("options", error, failed)
            ready = {}  # no quotes: no option structure acts this tick
        for sid, spec in ready.items():
            st = book.structures[sid]
            if st.status is Status.CLOSED:
                continue
            try:
                self._structure_tick(
                    spec, st, snap, live, halt=halt, flatten=flatten, paused=paused, now=now
                )
            except Exception as error:
                self._contain(sid, error, failed)
            finally:
                self._save(book)
        for sid, equity in equities.items():
            st = book.structures[sid]
            try:
                if st.status is not Status.CLOSED:
                    try:
                        self._equity_tick(
                            equity,
                            st,
                            book,
                            live,
                            halt=halt,
                            flatten=flatten,
                            paused=paused,
                            now=now,
                        )
                    finally:
                        self._save(book)
                if st.status is Status.CLOSED:
                    self._equity_release(sid)
            except Exception as error:
                self._contain(sid, error, failed)
        try:
            self._equity_watch(equities, book, now)
        except Exception as error:
            self._contain("equity_watch", error, failed)
        self._degraded = failed[0] if failed else None
        if not failed:
            book.heartbeat = now
        self._save(book)

    def _prepare_options(
        self, active: Mapping[str, DeskSpec], failed: list[str]
    ) -> dict[str, DeskSpec]:
        """Qualify and subscribe the non-CLOSED option structures (a CLOSED
        one is never prepared again: past its expiry IBKR cannot qualify its
        legs). One batch as before; when the batch fails, each structure on
        its own, so a structure that cannot be prepared is contained and
        skipped this tick while the others run."""
        if not active:
            return {}
        try:
            self.ib.prepare([s.structure for s in active.values()])
            return dict(active)
        except Exception as error:
            if self._connection_lost(error):
                raise
        ready: dict[str, DeskSpec] = {}
        for sid, spec in active.items():
            try:
                if not self.ib.leg_con_ids(sid):
                    self.ib.prepare([spec.structure])
                ready[sid] = spec
            except Exception as error:
                self._contain(sid, error, failed)
        return ready

    def _connection_lost(self, error: Exception) -> bool:
        """A connection-class error, or any error once the session reads
        disconnected: the loop's ``gateway_lost`` (never contained)."""
        if isinstance(error, _CONNECTION_ERRORS):
            return True
        try:
            return not self.ib.connected
        except Exception:  # an unreadable session state is not a live session
            return True

    def _contain(self, scope: str, error: Exception, failed: list[str]) -> None:
        """Record a failure confined to ``scope`` (a structure id, or a lane)
        and let the tick go on; a lost connection re-raises. The error
        class only: broker errors can carry account details."""
        if self._connection_lost(error):
            raise error
        name = type(error).__name__
        failed.append(name)
        log.error("desk tick: %s failed (%s); the other structures still ran", scope, name)
        self._note_once(scope, "structure_tick_failed", key=name, error=name)

    def beat(self) -> None:
        """Heartbeat only (outside the session: no decisions, no orders).
        Also refreshes ``monitor.json`` without claiming a good tick."""
        if self._lock_handle is None:
            raise RuntimeLocked("beat requires the runtime lock")
        self.observe_connection()
        book = self._book(*self._all_specs())
        book.heartbeat = self._now()
        self._save(book)
        self._write_health(None, tick_ran=False)
        self._custody()

    def _custody(self) -> None:
        """Phase 0 owner custody (``trex.desk_backup``): the snapshot is
        consistent because this runs under the lock, between beats. Never
        raises into the owner loop: a failed backup is an event."""
        try:
            outcome = self.backups.on_beat(self._now(), self._owner_epoch())
            if outcome is not None:
                self._event(outcome.pop("event"), **outcome)
        except Exception:
            log.exception("desk custody hook failed (the owner loop is unaffected)")

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
                div = declared_dividend(
                    self.dividends(s.underlying, now.date()),
                    now.date(),
                    last_hold_session(s.first_expiry),
                )
                if div is not None:
                    dividends[s.underlying] = div
            for i, leg in enumerate(s.legs):
                if leg.action == "SELL" and leg.right == "C":
                    quote = self.ib.leg_quote(s.id, i)
                    mid = None if quote is None else cents((quote[0] + quote[1]) / 2)
                    short_call_mids[short_call_key(s.id, leg.strike, leg.expiry)] = mid
        return replace(
            snap, spots=dict(spots), dividends=dividends, short_call_mids=short_call_mids
        )

    def _trade_for(self, spec: DeskSpec, st: StructureState, live: Mapping[str, Any]) -> Any:
        return self._trade_by(spec.structure.id, spec.entry_order_ref, st, live)

    def _trade_by(
        self, sid: str, entry_order_ref: str, st: StructureState, live: Mapping[str, Any]
    ) -> Any:
        """The structure's current order: the live one carrying its entry
        tag (entry lane) or desk tag, else the last one this process saw
        when it is still the book's entry/exit order."""
        if st.status in (Status.PLANNED, Status.ENTER_WORKING):
            trade = live.get(entry_order_ref)
        else:
            trade = live.get(desk_order_ref(sid))
        if trade is not None:
            self.orders[sid] = trade
            return trade
        held = self.orders.get(sid)
        if held is None:
            return None
        order_id = str(getattr(held.order, "orderId", ""))
        expected = (
            st.entry_order if st.status in (Status.PLANNED, Status.ENTER_WORKING) else st.exit_order
        )
        return held if order_id == expected else None

    def _working(self, spec: DeskSpec, st: StructureState, trade: Any) -> WorkingOrder | None:
        role = adopted_role(spec.structure, st, str(trade.order.action))
        if role is None:
            self._note_once(
                spec.structure.id,
                "unknown_exposure",
                order=str(getattr(trade.order, "orderId", "?")),
                side=str(trade.order.action),
                status=st.status.value,
            )
            return None
        info = self.ib.order_status(
            OrderRef(
                spec.structure.id,
                str(trade.order.action),
                int(trade.order.totalQuantity),
                Decimal(str(trade.order.lmtPrice)),
                trade,
            )
        )
        return WorkingOrder(
            role=role,
            filled=info.filled,
            avg_fill_price=info.avg_fill_price,
            order_id=str(trade.order.orderId),
        )

    def _structure_tick(
        self,
        spec: DeskSpec,
        st: StructureState,
        snap: Snapshot,
        live: Mapping[str, Any],
        *,
        halt: bool,
        flatten: bool,
        now: datetime,
        paused: bool = False,
    ) -> None:
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
            if not self._resolve_exit_without_trade(spec, st, now) or st.status is Status.CLOSED:
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
        if paused and st.status is Status.ENTER_WORKING and trade is not None:
            # entries-only pause: the unfilled remainder goes; exits never wait
            self._cancel(sid, trade, why="entries_paused")
            return
        action = decision.action
        if isinstance(action, NoAction):
            return
        if isinstance(action, AbortEntry):
            if trade is not None and st.status is Status.ENTER_WORKING:
                self._cancel(sid, trade, why=action.reason)
            return
        if isinstance(action, EntryOrder):
            self._note_once(
                sid, "entry_reprice_not_sent", reason="a supervised permit binds one limit"
            )
            return
        if isinstance(action, CloseOrder):
            self._close(spec, st, action, trade, halt=halt, now=now)

    # -- entry lane ----------------------------------------------------------

    def _resolve_planned(
        self, spec: DeskSpec, st: StructureState, live: Mapping[str, Any], now: datetime
    ) -> None:
        self._resolve_planned_by(
            spec.structure.id, spec.intent_id, spec.entry_order_ref, st, live, now
        )

    def _resolve_planned_by(
        self,
        sid: str,
        intent_id: str,
        entry_order_ref: str,
        st: StructureState,
        live: Mapping[str, Any],
        now: datetime,
    ) -> None:
        """A PLANNED structure (option or equity) from the broker's live
        tagged order and the supervised outbox of its intent."""
        trade = live.get(entry_order_ref)
        if trade is not None:
            st.entry_order = str(trade.order.orderId)
            st.to(Status.ENTER_WORKING, now)
            self.orders[sid] = trade
            self._event("entry_adopted", structure=sid, order=st.entry_order)
            return
        sup = self.supervised
        terminal = sup.terminal(intent_id)
        reconciled = sup.reconciled(intent_id)
        if reconciled.exists():
            verdict = json.loads(reconciled.read_bytes())
            if verdict.get("verdict") == "confirmed_not_submitted":
                self._close_unentered(st, "not_submitted", now, sid)
            elif verdict.get("verdict") == "confirmed_submitted" and verdict.get("broker_order_id"):
                st.entry_order = str(verdict["broker_order_id"])
                st.to(Status.ENTER_WORKING, now)
                self._event(
                    "entry_adopted", structure=sid, order=st.entry_order, via="reconciliation"
                )
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
        pending = sup.pending(intent_id)
        if pending.exists():
            deadline = datetime.fromisoformat(json.loads(pending.read_bytes())["send_deadline"])
            if (now - deadline).total_seconds() >= 0:
                # send() refuses a passed deadline: this intent can never go out
                self._close_unentered(st, "not_sent_by_deadline", now, sid)

    def _close_unentered(self, st: StructureState, reason: str, now: datetime, sid: str) -> None:
        st.to(Status.CLOSED, now)
        st.close_reason = reason
        self._event("closed", structure=sid, reason=reason)

    def _resolve_entry_without_trade(
        self, spec: DeskSpec, st: StructureState, now: datetime
    ) -> None:
        """The entry order left the live view (filled or died, maybe while
        this process was down): only broker evidence moves the book."""
        sid = spec.structure.id
        evidence = self.ib.entry_fill_evidence(sid, st.entry_order)
        if evidence is None:
            self._note_once(sid, "entry_evidence_inconclusive", order=st.entry_order)
            return
        qty, avg = evidence
        if qty:
            drain(
                st,
                WorkingOrder(
                    role="entry",
                    filled=qty,
                    avg_fill_price=avg or Decimal(0),
                    order_id=str(st.entry_order),
                ),
            )
            st.to(Status.OPEN, now)
            self._event(
                "entry_filled",
                structure=sid,
                filled=st.filled_qty,
                avg=str(st.entry_fill),
                via="evidence",
            )
        else:
            self._close_unentered(st, "entry_unfilled", now, sid)

    # -- transitions -----------------------------------------------------------

    def _transitions(
        self, spec: DeskSpec, st: StructureState, order_status: str, now: datetime
    ) -> None:
        self._transitions_by(spec.structure.id, spec.structure.quantity, st, order_status, now)

    def _transitions_by(
        self, sid: str, quantity: int, st: StructureState, order_status: str, now: datetime
    ) -> None:
        """Filled entry -> OPEN (a dead partial opens with what filled);
        unfilled dead entry -> CLOSED; flat exit -> CLOSED."""
        done = order_status in DONE_STATES
        if st.status is Status.ENTER_WORKING:
            if st.filled_qty >= quantity or (done and st.filled_qty > 0):
                st.to(Status.OPEN, now)
                self._event(
                    "entry_filled", structure=sid, filled=st.filled_qty, avg=str(st.entry_fill)
                )
            elif done:
                self._close_unentered(st, "entry_unfilled", now, sid)
        elif st.status is Status.EXIT_WORKING and st.open_qty <= 0 and (done or not order_status):
            st.to(Status.CLOSED, now)
            st.close_reason = st.exit_reason or "flat"
            self._event(
                "closed", structure=sid, reason=st.close_reason, exit_fill=str(st.exit_fill)
            )

    # -- exits -----------------------------------------------------------------

    def _leg_positions(self, spec: DeskSpec) -> dict[int, Decimal] | None:
        """The bound account's position in each leg (by conId); None when
        the broker's positions cannot be read."""
        sid = spec.structure.id
        try:
            held = {p.con_id: p.qty for p in self.ib.positions(spec.account_id)}
        except Exception as error:
            self._note_once(sid, "legs_unverifiable", key=type(error).__name__, error=repr(error))
            return None
        return {con: held.get(con, Decimal(0)) for con in self.ib.leg_con_ids(sid)}

    def _mismatch(self, spec: DeskSpec, held: Mapping[int, Decimal], qty: int) -> list[str]:
        """Legs whose position is not ``qty`` packages (BUY long, SELL short)."""
        out: list[str] = []
        for con_id, leg in zip(
            self.ib.leg_con_ids(spec.structure.id), spec.structure.legs, strict=True
        ):
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
            self._note_once(
                spec.structure.id,
                "legs_mismatch",
                key="|".join(wrong),
                legs=wrong,
                open_qty=st.open_qty,
            )
            return False
        return True

    def _resolve_exit_without_trade(
        self, spec: DeskSpec, st: StructureState, now: datetime
    ) -> bool:
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
            if drain(
                st,
                WorkingOrder(
                    role="exit",
                    filled=executed,
                    avg_fill_price=avg or Decimal(0),
                    order_id=str(st.exit_order),
                ),
            ):
                self._event(
                    "exit_fill",
                    structure=sid,
                    filled=st.exit_filled_qty,
                    avg=str(st.exit_fill),
                    via="evidence",
                )
        held = self._leg_positions(spec)
        if held is None:
            return False
        wrong = self._mismatch(spec, held, st.open_qty)
        if not wrong:
            if st.open_qty <= 0:
                st.to(Status.CLOSED, now)
                st.close_reason = st.exit_reason or "flat"
                self._event(
                    "closed",
                    structure=sid,
                    reason=st.close_reason,
                    exit_fill=str(st.exit_fill),
                    via="evidence",
                )
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
                self._event(
                    "closed", structure=sid, reason=st.close_reason, unpriced_packages=unpriced
                )
                return True
            self._note_once(
                sid,
                "exit_flat_unexplained",
                open_qty=st.open_qty,
                exit_order=st.exit_order,
                operator=f"verify flat at the broker, then touch {resolve.name}",
            )
            return False
        self._note_once(sid, "legs_mismatch", key="|".join(wrong), legs=wrong, open_qty=st.open_qty)
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

    def _exit_cleared(
        self, sid: str, account_id: str, st: StructureState, *, halt: bool
    ) -> tuple[bool, str | None]:
        """The exit guards every close order (option or equity) passes
        before it exists: the supervised paper session, and no OTHER open
        order carrying the desk tag (an unreadable view refuses). Passing is
        the ``exit_authority`` evidence; (cleared, owner epoch)."""
        blockers = self._guard.paper_blockers(account_id)
        if blockers:
            self._note_once(
                sid,
                "exit_blocked",
                key=",".join(blockers),
                blockers=blockers,
                reason=st.exit_reason,
            )
            return False, None
        existing = self._open_tagged(desk_order_ref(sid))
        if existing is None:
            self._note_once(
                sid,
                "exit_blocked",
                key="duplicate_check_unreadable",
                reason="duplicate_check_unreadable",
                exit_reason=st.exit_reason,
            )
            return False, None
        if existing:
            self._note_once(
                sid,
                "exit_blocked",
                key="duplicate:" + ",".join(sorted(existing)),
                reason="exit_tag_already_open",
                orders=sorted(existing.values()),
                exit_reason=st.exit_reason,
            )
            return False, None
        owner_epoch = self._owner_epoch()
        self._note_once(
            sid,
            "exit_authority",
            owner_epoch=owner_epoch,
            pid=os.getpid(),
            client_id=self.ib.client_id,
            port=self.ib.port,
            account=account_id,
            halt=halt,
        )
        return True, owner_epoch

    def _place_close(
        self,
        spec: DeskSpec,
        st: StructureState,
        side: str,
        qty: int,
        limit: Decimal,
        *,
        halt: bool = False,
    ) -> Any:
        """Send one close order, guarded. Every guard refuses BEFORE the
        order exists and is fail-closed-with-retry: nothing is sent, one
        deduped urgent ``exit_blocked`` push, the next tick re-enters the
        lane. An expired mandate NEVER blocks an exit (ruling 2b): the
        supervised mandate covers entries only, so it is not consulted."""
        s = spec.structure
        cleared, owner_epoch = self._exit_cleared(s.id, spec.account_id, st, halt=halt)
        if not cleared:
            return None
        contract, order = self.ib._order(s, side, qty, limit)
        order.orderRef = desk_order_ref(s.id)
        order.account = spec.account_id
        trade = self.ib._ib.placeOrder(contract, order)
        self.orders[s.id] = trade
        st.exit_order = str(trade.order.orderId)
        st.exit_order_seen, st.exit_order_notional, st.exit_order_unpriced = 0, None, 0
        self._event(
            "exit_order",
            structure=s.id,
            side=side,
            qty=qty,
            limit=str(limit),
            order=st.exit_order,
            reason=st.exit_reason,
            owner_epoch=owner_epoch,
        )
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

    def _close(
        self,
        spec: DeskSpec,
        st: StructureState,
        action: CloseOrder,
        trade: Any,
        *,
        halt: bool,
        now: datetime,
    ) -> None:
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
            self._event("exit_begin", structure=sid, reason=action.reason.value, qty=st.open_qty)
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

    def _flatten(
        self,
        spec: DeskSpec,
        st: StructureState,
        trade: Any,
        snap: Snapshot,
        *,
        halt: bool,
        now: datetime,
    ) -> None:
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

    # -- Wave 2: equity positions ---------------------------------------------

    def _equity_tick(
        self,
        spec: EquityDeskSpecV2,
        st: StructureState,
        book: BookState,
        live: Mapping[str, Any],
        *,
        halt: bool,
        flatten: bool,
        paused: bool,
        now: datetime,
    ) -> None:
        """One equity position, one tick: resolve the entry, drain the
        working order's fills, record transitions, then act on the rules."""
        sid = spec.intent_id
        if self._equity_contract(spec) is None:
            return  # unqualified or a different conId: noted, nothing acted on
        if st.status is Status.PLANNED:
            self._resolve_planned_by(sid, spec.intent_id, spec.entry_order_ref, st, live, now)
            if st.status is not Status.ENTER_WORKING:
                return
        trade = self._trade_by(sid, spec.entry_order_ref, st, live)
        if trade is None and st.status is Status.ENTER_WORKING:
            self._equity_entry_without_trade(spec, st, book, now)
            if st.status is not Status.OPEN:
                return
        if trade is None and st.status is Status.EXIT_WORKING and st.exit_order:
            if not self._equity_exit_without_trade(spec, st, book, now):
                return
            if st.status is Status.CLOSED:
                return
        if trade is not None:
            working = self._equity_working(spec, st, trade)
            if working is None:
                return  # unknown exposure: noted, never acted on
            drain(st, working)
        status = str(getattr(trade.orderStatus, "status", "")) if trade is not None else ""
        self._transitions_by(sid, spec.position.quantity, st, status, now)
        if st.status is Status.CLOSED:
            return
        quote = self._equity_quote(spec)
        decision = decide_equity(spec.position.exit_policy, st, quote, now)
        st.stop_ticks, st.stop_tick_at = decision.stop_ticks, decision.stop_tick_at
        entry_working = (
            st.status is Status.ENTER_WORKING and trade is not None and status not in DONE_STATES
        )
        if flatten:
            if entry_working:
                self._cancel(sid, trade, why="flatten")
            elif st.status in (Status.OPEN, Status.EXIT_WORKING):
                if st.status is Status.EXIT_WORKING:
                    st.exit_reason = EquityExitReason.FLATTEN.value
                flat = EquityClose(EquityExitReason.FLATTEN, st.open_qty, marketable_limit(quote))
                self._equity_close(spec, st, flat, trade, book, halt=halt, now=now)
            return
        if paused and entry_working:
            # entries-only pause: the unfilled remainder goes; exits never wait
            self._cancel(sid, trade, why="entries_paused")
            return
        action = decision.action
        if isinstance(action, EquityAbort):
            if entry_working:
                self._cancel(sid, trade, why=action.reason)
            return
        if isinstance(action, EquityClose):
            self._equity_close(spec, st, action, trade, book, halt=halt, now=now)

    def _equity_contract(self, spec: EquityDeskSpecV2) -> Any:
        """The position's qualified ``Stock`` (SMART, USD), subscribed to
        market data once per process; None (noted) when IBKR cannot qualify
        it or resolves the symbol to another conId than the bound one."""
        sid = spec.intent_id
        held = self._equity_contracts.get(sid)
        if held is not None:
            return held
        from ib_async import Stock  # lazy: optional dependency group

        pos = spec.position
        stock = Stock(pos.symbol, "SMART", "USD")
        try:
            self.ib._ib.qualifyContracts(stock)
        except Exception as error:  # the class only: broker errors carry details
            self._note_once(
                sid, "equity_unqualified", key=type(error).__name__, error=type(error).__name__
            )
            return None
        con_id = int(getattr(stock, "conId", 0) or 0)
        if con_id != pos.con_id:
            self._note_once(
                sid,
                "equity_contract_mismatch",
                key=str(con_id),
                symbol=pos.symbol,
                qualified=con_id,
                bound=pos.con_id,
            )
            return None
        self.ib._subscribe(stock)
        self._equity_contracts[sid] = stock
        return stock

    def _equity_release(self, sid: str) -> None:
        """Drop a CLOSED position's claim on its market-data stream (and its
        exit_quote_blind clock)."""
        self.exit_quotes.forget(sid)
        stock = self._equity_contracts.pop(sid, None)
        if stock is not None:
            self.ib._unsubscribe(int(stock.conId))

    def _equity_quote(self, spec: EquityDeskSpecV2) -> EquityQuote | None:
        """The stock's latest NBBO from the shared stream: cents (rounded
        down), real-time or not (``Ticker.marketDataType``), and its tick
        instant; None without a stream."""
        sub = self.ib._md.get(spec.position.con_id)
        if sub is None:
            return None
        ticker = sub.ticker
        at = getattr(ticker, "time", None)
        if not isinstance(at, datetime) or at.tzinfo is None:
            at = None
        return EquityQuote(
            bid_cents=_cents_floor(getattr(ticker, "bid", None)),
            ask_cents=_cents_floor(getattr(ticker, "ask", None)),
            realtime=getattr(ticker, "marketDataType", None) == REALTIME_MARKET_DATA,
            at=at,
        )

    def _equity_working(
        self, spec: EquityDeskSpecV2, st: StructureState, trade: Any
    ) -> WorkingOrder | None:
        """The lane a live stock order belongs to (a BUY in the entry lane is
        the entry, a SELL of an open position the exit) with its cumulative
        fills; None (noted) for anything else: unknown exposure."""
        sid = spec.intent_id
        side = str(getattr(trade.order, "action", ""))
        con_id = int(getattr(trade.contract, "conId", 0) or 0)
        role: Role | None = None
        if side == "BUY" and st.status in ENTRY_LANE:
            role = "entry"
        elif side == "SELL" and st.status in (Status.OPEN, Status.EXIT_WORKING):
            role = "exit"
        order_id = str(getattr(trade.order, "orderId", "?"))
        if role is None or con_id != spec.position.con_id:
            self._note_once(
                sid,
                "unknown_exposure",
                order=order_id,
                side=side,
                status=st.status.value,
                con_id=con_id,
            )
            return None
        filled = _shares(getattr(trade.orderStatus, "filled", 0) or 0)
        if filled is None:
            self._note_once(
                sid,
                DeskEvent.RECONCILIATION_REQUIRED.value,
                key=f"order_fill:{order_id}",
                why="order_fill_not_whole_shares",
                order=order_id,
            )
            return None
        avg = _decimal(getattr(trade.orderStatus, "avgFillPrice", 0)) if filled else None
        return WorkingOrder(
            role=role,
            filled=filled,
            avg_fill_price=avg if avg is not None and avg > 0 else Decimal(0),
            order_id=order_id,
        )

    def _equity_executions(
        self, spec: EquityDeskSpecV2, order_id: str | None
    ) -> tuple[int, Decimal | None] | None:
        """What today's executions PROVE of ``order_id`` (this client, the
        bound conId): (shares, average price), a corrected execution counted
        once at its latest version (same base exec id, highest last
        segment); (0, None) when there are none; None when the view cannot
        be read or does not add up to whole shares (never guessed)."""
        if order_id is None or not order_id.isdigit():
            return 0, None
        oid = int(order_id)
        try:
            fills = list(self.ib._ib.fills())
        except Exception:  # any failed read: nothing is proven
            return None
        latest: dict[str, tuple[int, Any]] = {}
        for fill in fills:
            ex = fill.execution
            if getattr(ex, "orderId", None) != oid:
                continue
            if getattr(ex, "clientId", None) != self.ib.client_id:
                continue
            if int(getattr(fill.contract, "conId", 0) or 0) != spec.position.con_id:
                continue
            exec_id = str(getattr(ex, "execId", "") or "")
            try:
                version = int(exec_id.rpartition(".")[2], 16)
            except ValueError:
                return None
            base = base_exec_id_of(exec_id)
            if base not in latest or version > latest[base][0]:
                latest[base] = (version, ex)
        qty = 0
        notional = Decimal(0)
        for _, ex in latest.values():
            shares = _shares(getattr(ex, "shares", None))
            price = _decimal(getattr(ex, "price", None))
            if shares is None or price is None or price <= 0:
                return None
            qty += shares
            notional += price * shares
        return (qty, notional / qty) if qty else (0, None)

    def _equity_book_qty(self, spec: EquityDeskSpecV2, st: StructureState, book: BookState) -> int:
        """Shares the book says the account holds in the position's
        contract: its open shares plus every other live desk equity
        position's on the same (account, conId)."""
        total = max(st.open_qty, 0)
        key = (spec.account_id, spec.position.con_id)
        for other_sid, other in self._tick_equities.items():
            if other_sid == spec.intent_id or (other.account_id, other.position.con_id) != key:
                continue
            other_st = book.structures.get(other_sid)
            if other_st is not None and other_st.status is not Status.CLOSED:
                total += max(other_st.open_qty, 0)
        return total

    def _reconcile(
        self,
        spec: EquityDeskSpecV2,
        *,
        why: str,
        broker_qty: Decimal,
        book_qty: int,
        outside_qty: int = 0,
    ) -> None:
        extra = {"outside_qty": outside_qty} if outside_qty else {}
        self._note_once(
            spec.intent_id,
            DeskEvent.RECONCILIATION_REQUIRED.value,
            key=f"{why}:{_qty_text(broker_qty)}/{book_qty}",
            why=why,
            symbol=spec.position.symbol,
            con_id=spec.position.con_id,
            broker_qty=_qty_text(broker_qty),
            book_qty=book_qty,
            **extra,
        )

    def _equity_held(self, spec: EquityDeskSpecV2, book_qty: int) -> int | None:
        """The bound account's whole shares in the position's conId; None
        when unreadable (``holdings_unverifiable``) or not representable (a
        fractional or non-STK row: ``reconciliation_required``)."""
        try:
            rows = self.ib.positions(spec.account_id)
        except Exception as error:  # the class only: broker errors carry details
            self._note_once(
                spec.intent_id,
                "holdings_unverifiable",
                key=type(error).__name__,
                error=type(error).__name__,
            )
            return None
        mine = [r for r in rows if r.con_id == spec.position.con_id]
        total = sum((r.qty for r in mine), Decimal(0))
        if any(r.sec_type != "STK" for r in mine):
            self._reconcile(spec, why="not_a_stock_row", broker_qty=total, book_qty=book_qty)
            return None
        if total != total.to_integral_value():
            self._reconcile(spec, why="fractional_position", broker_qty=total, book_qty=book_qty)
            return None
        return int(total)

    def _holdings_ok(self, spec: EquityDeskSpecV2, st: StructureState, book: BookState) -> bool:
        """The holdings check every sell passes: the broker shows at least
        every share the book says the desk holds in the contract (never
        sell what is not shown held). Shares beyond them are an outside
        holding: flagged (``reconciliation_required``, ``holdings_excess``)
        but never a block, since reconciliation stops entries only (C2.5).
        The sell is the position's open shares, so the excess stays. The
        excess is remembered (``exit_outside_qty``) for a restart that finds
        the sell gone."""
        want = self._equity_book_qty(spec, st, book)
        held = self._equity_held(spec, want)
        if held is None:
            return False
        if held < want:
            self._reconcile(spec, why="holdings_mismatch", broker_qty=Decimal(held), book_qty=want)
            return False
        if held > want:
            self._reconcile(spec, why="holdings_excess", broker_qty=Decimal(held), book_qty=want)
        st.exit_outside_qty = held - want
        return True

    def _equity_entry_without_trade(
        self, spec: EquityDeskSpecV2, st: StructureState, book: BookState, now: datetime
    ) -> None:
        """The entry left the live view (filled or died, maybe while this
        process was down): only broker evidence moves the book."""
        sid = spec.intent_id
        evidence = self._equity_executions(spec, st.entry_order)
        if evidence is None:
            self._note_once(sid, "entry_evidence_inconclusive", order=st.entry_order)
            return
        qty, avg = evidence
        if qty:  # executions prove the fill: the position is open
            drain(
                st,
                WorkingOrder(
                    role="entry",
                    filled=qty,
                    avg_fill_price=avg or Decimal(0),
                    order_id=str(st.entry_order),
                ),
            )
        else:  # no execution today: only matching holdings prove it ended
            want = self._equity_book_qty(spec, st, book)
            held = self._equity_held(spec, want)
            if held is None or held != want:
                self._note_once(sid, "entry_evidence_inconclusive", order=st.entry_order)
                return
        if st.filled_qty > 0:
            st.to(Status.OPEN, now)
            self._event(
                "entry_filled",
                structure=sid,
                filled=st.filled_qty,
                avg=str(st.entry_fill),
                via="evidence",
            )
        else:
            self._close_unentered(st, "entry_unfilled", now, sid)

    def _equity_exit_without_trade(
        self, spec: EquityDeskSpecV2, st: StructureState, book: BookState, now: datetime
    ) -> bool:
        """The exit left the live view (filled or expired, maybe while this
        process was down). Record what the broker PROVES filled, then the
        holdings check. True: the book agrees with the broker (closed now,
        or a remainder the normal flow re-sells). False: held, noted.

        The broker must show the book's shares plus exactly the outside
        holding seen when the sell went out (``exit_outside_qty``): a sell
        from an earlier session can have filled unseen (today's executions
        do not reach it), and an outside holding that moved meanwhile could
        hide that fill, so re-selling could sell outside shares. Shares flat
        beyond what today's executions explain wait for the operator's
        ``RESOLVE-FLAT-<sid>`` file, as for options."""
        sid = spec.intent_id
        evidence = self._equity_executions(spec, st.exit_order)
        if evidence is None:
            self._note_once(sid, "exit_evidence_inconclusive", order=st.exit_order)
            return False
        executed, avg = evidence
        if executed and drain(
            st,
            WorkingOrder(
                role="exit",
                filled=executed,
                avg_fill_price=avg or Decimal(0),
                order_id=str(st.exit_order),
            ),
        ):
            self._event(
                "exit_fill",
                structure=sid,
                filled=st.exit_filled_qty,
                avg=str(st.exit_fill),
                via="evidence",
            )
        want = self._equity_book_qty(spec, st, book)
        held = self._equity_held(spec, want)
        if held is None:
            return False
        outside = st.exit_outside_qty
        if held == want + outside:
            if st.open_qty <= 0:
                st.to(Status.CLOSED, now)
                st.close_reason = st.exit_reason or "flat"
                self._event(
                    "closed",
                    structure=sid,
                    reason=st.close_reason,
                    exit_fill=str(st.exit_fill),
                    via="evidence",
                )
            return True
        if st.open_qty > 0 and held == want + outside - st.open_qty:
            resolve = self.paths.resolve_flat(sid)
            if resolve.exists():
                unpriced = st.open_qty
                st.exit_filled_qty += unpriced
                st.exit_unpriced_qty += unpriced
                st.to(Status.CLOSED, now)
                st.close_reason = "operator_confirmed_flat"
                os.replace(resolve, resolve.with_name(resolve.name + ".applied"))
                self._event(
                    "closed", structure=sid, reason=st.close_reason, unpriced_shares=unpriced
                )
                return True
            self._note_once(
                sid,
                "exit_flat_unexplained",
                open_qty=st.open_qty,
                exit_order=st.exit_order,
                operator=f"verify flat at the broker, then touch {resolve.name}",
            )
            return False
        self._reconcile(
            spec,
            why="holdings_mismatch",
            broker_qty=Decimal(held),
            book_qty=want,
            outside_qty=outside,
        )
        return False

    def _equity_close(
        self,
        spec: EquityDeskSpecV2,
        st: StructureState,
        action: EquityClose,
        trade: Any,
        book: BookState,
        *,
        halt: bool,
        now: datetime,
    ) -> None:
        """Start or keep an equity exit: SELL the open shares at the bid,
        repriced (cancel, confirm, merge the final fills, replace the
        remainder) only when the bid falls below the working limit. A close
        that stays unpriced for ``EXIT_QUOTE_BLIND_AFTER_S`` records (and
        pushes, urgent) ``exit_quote_blind`` once per structure per session."""
        sid = spec.intent_id
        alert = self.exit_quotes.observe(sid, action, now)
        if alert is not None:
            self._event(
                EXIT_QUOTE_BLIND, structure=sid, symbol=spec.position.symbol, **alert.payload()
            )
        limit = action.limit_cents
        if limit is None:
            self._note_once(sid, "exit_pending_no_quote", reason=action.reason.value)
            return
        if halt:
            self._note_once(sid, "halt_no_new_orders", reason=action.reason.value)
            return
        if st.status is Status.OPEN:
            if st.open_qty <= 0 or not self._holdings_ok(spec, st, book):
                return
            st.to(Status.EXIT_WORKING, now)
            st.exit_reason = action.reason.value
            self._event("exit_begin", structure=sid, reason=action.reason.value, qty=st.open_qty)
            rule = EXIT_EVENTS.get(action.reason)
            if rule is not None:
                policy = spec.position.exit_policy
                self._event(
                    rule.value,
                    structure=sid,
                    symbol=spec.position.symbol,
                    qty=st.open_qty,
                    bid_cents=limit,
                    stop_cents=policy.stop_price_cents,
                    target_cents=policy.target_price_cents,
                    stop_ticks=st.stop_ticks,
                    deadline_session=policy.exit_deadline_session.isoformat(),
                )
            self._place_equity_sell(spec, st, st.open_qty, limit, halt=halt)
            return
        if st.status is not Status.EXIT_WORKING:
            return
        if trade is not None and str(trade.orderStatus.status) not in DONE_STATES:
            working_limit = _cents_floor(getattr(trade.order, "lmtPrice", None))
            if working_limit is not None and working_limit <= limit:
                return  # at or below the bid: still marketable
            if not self._cancel(sid, trade, why="reprice"):
                return
            st.exit_cycles += 1
        if trade is not None:
            working = self._equity_working(spec, st, trade)
            if working is None:
                return
            drain(st, working)
        if st.open_qty <= 0:
            return  # the final fills flattened it: the next tick closes the book
        if not self._holdings_ok(spec, st, book):
            return
        self._place_equity_sell(spec, st, st.open_qty, limit, halt=halt)

    def _place_equity_sell(
        self,
        spec: EquityDeskSpecV2,
        st: StructureState,
        qty: int,
        limit_cents: int,
        *,
        halt: bool,
    ) -> Any:
        """Send one guarded SELL: whole shares, DAY limit at ``limit_cents``,
        regular hours only, ``trex:desk:<intent_id>``, the bound account."""
        sid = spec.intent_id
        stock = self._equity_contracts.get(sid)
        if stock is None or qty <= 0:
            return None
        cleared, owner_epoch = self._exit_cleared(sid, spec.account_id, st, halt=halt)
        if not cleared:
            return None
        from ib_async import LimitOrder  # lazy: optional dependency group

        order = LimitOrder(
            "SELL",
            qty,
            float(Decimal(limit_cents) / 100),  # the wire's float, from exact cents
            tif="DAY",
            outsideRth=False,
            transmit=True,
            orderRef=desk_order_ref(sid),
            account=spec.account_id,
        )
        trade = self.ib._ib.placeOrder(stock, order)
        self.orders[sid] = trade
        st.exit_order = str(trade.order.orderId)
        st.exit_order_seen, st.exit_order_notional, st.exit_order_unpriced = 0, None, 0
        self._event(
            "exit_order",
            structure=sid,
            side="SELL",
            qty=qty,
            limit_cents=limit_cents,
            order=st.exit_order,
            reason=st.exit_reason,
            owner_epoch=owner_epoch,
        )
        return trade

    def _equity_watch(
        self, equities: Mapping[str, EquityDeskSpecV2], book: BookState, now: datetime
    ) -> None:
        """What ``monitor.json`` tells trex.exit_watch about equity exits:
        the symbols guarded (shares held) and those without a fresh
        real-time quote inside the regular session, since when."""
        guarded: set[str] = set()
        fresh: set[str] = set()
        for sid, spec in equities.items():
            st = book.structures.get(sid)
            if st is None or st.status in (Status.PLANNED, Status.CLOSED) or st.open_qty <= 0:
                continue
            symbol = spec.position.symbol
            guarded.add(symbol)
            quote = self._equity_quote(spec)
            if (
                quote is not None
                and quote.realtime
                and quote.bid_cents is not None
                and quote.at is not None
                and now.timestamp() - quote.at.timestamp() <= QUOTE_FRESH_S
            ):
                fresh.add(symbol)
        self._quote_guarded = guarded
        if not in_regular_session(now):
            self._quote_blind = {}
            return
        stamp = now.isoformat()
        self._quote_blind = {s: self._quote_blind.get(s, stamp) for s in sorted(guarded - fresh)}
