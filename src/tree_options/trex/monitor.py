"""The trex exit machine.

Runs as a systemd user service for the life of the book. It holds an flock
on ``monitor.lock`` and heartbeats into ``book.json`` — the entry runner
refuses to arm unless BOTH say the monitor is alive (arm-before-enter: the
plan's EV lives in the exit discipline, so the book is never owned without
its exit machine).

Scope: structures at/after OPEN. Entry states are the entry runner's
business, under FLATTEN too: IBKR lets only the placing clientId cancel an
order (error 10147) and the monitor (71) never even sees the entry
runner's (72) BUYs, so enter.py cancels its own working entries and hands
any filled part over as OPEN, which the monitor then flattens.

Kill files in the run directory:
  FLATTEN  exit every position now, marketable; enter.py cancels unfilled
           entries and enters nothing new
  HALT     place no new orders (existing exits continue to completion)

Spots come from trex.spot's SpotFeed (the Polygon snapshot, fetched off
this loop, session-bounded), for touch-guarded (OPEN) underlyings only.
``monitor.json`` reports them for the exit watchdog: ``touch_guarded``,
``spot_ok`` (accepted this tick) and ``spot_blind`` (none accepted, and
since when, counted only inside trex.spot.touch_window).

Usage: python -m tree_options.trex.monitor --plan plans/2026-09-18.toml
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.trex import history
from tree_options.trex.account import write_account
from tree_options.trex.clock import (
    ET,
    EntryWindow,
    calendar_horizon_warn,
    calendar_last_session,
    is_session,
    now_et,
)
from tree_options.trex.engine import (
    AbortEntry,
    Action,
    ComboQuote,
    EngineConfig,
    ExitOrder,
    ExitReason,
    NoAction,
    PlaceEntry,
    configure,
    decide,
)
from tree_options.trex.ibkr import IbkrTrex, OrderRef, Snapshot
from tree_options.trex.plan import PutSpread, TradePlan, cents, load_legacy_plan
from tree_options.trex.spot import SpotFeed, polygon_fetcher, touch_window
from tree_options.trex.state import ENTRY_LANE, BookState, Status

log = logging.getLogger("trex.monitor")

POLL_SECONDS = 20
MAX_MARKS_HISTORY = 2000  # ~11h of 20s ticks; older samples drop off
MAX_MARKS_HISTORY_LINES = 25_000  # marks_history.jsonl cap (~21 session-days)
ACCOUNT_EVERY = 3  # account.json cadence (polls)
HEARTBEAT_FRESH_SECONDS = 30
EXIT_BROKER_LOST = 6  # main()'s exit codes: 2 lock, 4 connect, 5 qualify
SESSION_OPEN = dtime(9, 30)
SESSION_END = dtime(16, 15)


_CENT = Decimal("0.01")


def compute_marks(
    spreads: list[PutSpread],
    book: BookState,
    quotes: Mapping[str, ComboQuote | None],
) -> dict[str, Any]:
    """Mark-to-mid observation of filled structures.

    Serialized to ``marks.json`` in the run dir every tick so the
    broker-free status panel can render live P&L without importing
    ``ib_async``. Observation only — never an input to any decision.
    """
    rows: dict[str, dict[str, object]] = {}
    total = Decimal("0")
    for spread in spreads:
        st = book.structures[spread.id]
        if st.filled_qty <= 0 or st.entry_fill is None:
            continue
        row: dict[str, object] = {"qty": st.filled_qty, "entry": str(st.entry_fill)}
        quote = quotes.get(spread.id)
        if quote is None:
            row["mark"] = None
        else:
            mid = (quote.bid + quote.ask) / 2
            unrealized = (mid - st.entry_fill) * st.filled_qty * 100
            total += unrealized
            row.update(
                bid=str(quote.bid),
                ask=str(quote.ask),
                mark=str(mid.quantize(_CENT)),
                unrealized=str(unrealized.quantize(_CENT)),
            )
        rows[spread.id] = row
    return {"structures": rows, "total_unrealized": str(total.quantize(_CENT))}


def _engine_config(plan: TradePlan) -> EngineConfig:
    return EngineConfig(
        entry_window=EntryWindow(
            dtime.fromisoformat(plan.entry_window_start),
            dtime.fromisoformat(plan.entry_window_end),
        )
    )


def _run_dir(plan: TradePlan, base: Path | None) -> Path:
    root = base or Path(os.environ.get("TREX_STATE", Path.home() / ".local/state/trex"))
    return root / plan.id


class Monitor:
    def __init__(
        self,
        plan: TradePlan,
        ib: IbkrTrex,
        book: BookState,
        run_dir: Path,
        clock: Callable[[], datetime] | None = None,
        spots: SpotFeed | None = None,
    ) -> None:
        self.plan = plan
        self.ib = ib
        self.book = book
        self.run_dir = run_dir
        self._clock = clock or now_et
        # None: the snapshot's own spots stand (test doubles); main() wires
        # the Polygon feed, the only touch source (see trex.spot)
        self.spots = spots
        self.orders: dict[str, OrderRef] = {}  # structure_id -> working exit OrderRef
        # Order-local fill counts RESTART on every replacement, so the drain
        # merges increments (seen -> now) into the book's cumulative totals.
        # Comparing a local count against the cumulative once recorded a
        # phantom open qty and the refresh path re-sold contracts no longer
        # held (naked short) — see test_trex_monitor.TestCumulativeExitAccounting.
        self._order_seen: dict[str, int] = {}
        # per-order cumulative notional (avg * filled), so blends merge
        # NOTIONAL increments - an order's cumulative average times only
        # its new fills would re-price the old ones (Codex-M2 #2)
        self._order_notional: dict[str, Decimal] = {}
        self._account_writes = 0
        self._history: list[dict[str, str]] | None = None  # marks history, lazy-loaded
        self._marks_history_lines: int | None = None  # jsonl line count, lazy
        self._marks_history_repaired = False
        self._tick_failures = 0  # consecutive, for monitor.json
        self._last_tick_ok_at: float | None = None
        self._started_at: float | None = None
        # touch-guarded underlyings with no accepted spot -> since when
        self._spot_blind_since: dict[str, datetime] = {}
        # ... and those with one this tick -> its as_of
        self._spot_ok: dict[str, datetime] = {}
        self._flatten_wait_noted: set[str] = set()

    def _now(self) -> datetime:
        return self._clock()

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    # -- kill files --------------------------------------------------------

    def _sync_book_from_disk(self) -> None:
        """Adopt the entry runner's book writes each cycle.

        The monitor and the entry runner share ``book.json``; without this
        reload the monitor's periodic save would clobber enter's
        OPEN/ENTER_WORKING states with its stale in-memory copy, and
        entries placed after arming would never be supervised — the exit
        machine only acts on statuses it can see. Entry-side fields are
        enter's to write; the monitor owns exit-side fields, which enter
        never touches, so taking disk structures wholesale is safe.
        """
        disk = BookState.load(self.run_dir / "book.json", list(self.book.structures))
        self.book.structures = disk.structures

    def _save_book(self) -> None:
        """Whole-book save that never reverts the entry runner's writes:
        entry-lane structures (state.ENTRY_LANE) are taken from disk, since
        the monitor never writes them and enter.py may have settled one
        (e.g. a FLATTEN cancel) since this loop's sync."""
        self.book.save_owned(
            self.run_dir / "book.json", lambda mine, _disk: mine.status in ENTRY_LANE
        )

    def _write_marks(self, snap: Snapshot) -> dict[str, Any]:
        """Persist the observation-only marks payload for the status panel."""
        payload = compute_marks(self.plan.structures, self.book, snap.quotes)
        payload["spots"] = {sym: str(px) for sym, px in snap.spots.items()}
        payload["spot_sources"] = {sym: r.to_json() for sym, r in snap.spot_sources.items()}
        payload["ts"] = now_et().isoformat()
        payload["history"] = self._marks_history(payload["ts"], payload["total_unrealized"])
        tmp = self.run_dir / "marks.json.tmp"
        tmp.write_text(json.dumps(payload) + "\n")
        os.replace(tmp, self.run_dir / "marks.json")
        return payload

    def _marks_history_cycle(self, payload: dict[str, Any] | None) -> None:
        """Append one marks_history.jsonl line with contemporaneous fields.

        OWN failure boundary, called at the END of the tick (after exit
        decisions): a history failure costs one sample, never an exit
        decision. Per-structure rows carry the book's open_qty / entry
        basis / realized-to-date AT SAMPLE TIME, and unrealized is
        OPEN-basis (remaining contracts) - the filled-basis marks.json
        total is a different, separately-labeled number. ``payload=None``
        writes a quote-less terminal line (a fill that closed a structure
        outside session hours must not leave the last sample stale).
        """
        try:
            path = self.run_dir / "marks_history.jsonl"
            if not self._marks_history_repaired:
                history.repair_torn_tail(path)
                self._marks_history_repaired = True
            if self._marks_history_lines is None:
                self._marks_history_lines = history.count_lines(path)
            structures: dict[str, dict[str, Any]] = {}
            open_ct = 0
            quoted_ct = 0
            total_open_unrealized = Decimal("0.00")
            for spread in self.plan.structures:
                st = self.book.structures[spread.id]
                # live exposure rows only - except on the quote-less
                # terminal path, where a just-closed structure's final
                # state must be recorded (its previous sample is now stale)
                if st.open_qty <= 0 and not (payload is None and st.filled_qty > 0):
                    continue
                open_ct += 1
                row_raw = (payload or {}).get("structures", {}).get(spread.id)
                row = row_raw if isinstance(row_raw, dict) else {}
                mark = row.get("mark")
                entry = st.entry_fill
                unrealized_open: Decimal | None = None
                if mark is not None and entry is not None:
                    quoted_ct += 1
                    try:
                        unrealized_open = (Decimal(str(mark)) - entry) * st.open_qty * 100
                        total_open_unrealized += unrealized_open
                    except Exception:
                        unrealized_open = None
                realized = Decimal("0.00")
                if (
                    st.entry_fill is not None
                    and st.exit_fill is not None
                    and st.exit_filled_qty > 0
                ):
                    realized = (st.exit_fill - st.entry_fill) * st.exit_filled_qty * 100
                structures[spread.id] = {
                    "mark": mark,  # already a money-string or None
                    "unrealized": (
                        str(unrealized_open) if unrealized_open is not None else None
                    ),
                    "open_qty": st.open_qty,
                    "entry": str(entry) if entry is not None else None,
                    "realized_to_date": str(realized),
                }
            line: dict[str, Any] = {
                "ts": (payload or {}).get("ts") or now_et().isoformat(),
                "total_unrealized": (payload or {}).get("total_unrealized"),
                "total_unrealized_open": str(total_open_unrealized),
                "structures": structures,
                "quote_coverage": {"open": open_ct, "quoted": quoted_ct},
            }
            history.append_line(path, line)
            self._marks_history_lines = (self._marks_history_lines or 0) + 1
            if self._marks_history_lines > MAX_MARKS_HISTORY_LINES:
                history.rotate_halving(path, MAX_MARKS_HISTORY_LINES)
                self._marks_history_lines = history.count_lines(path)
        except Exception:
            log.exception("marks history append failed (exit machine unaffected)")
            # a torn partial write must not poison the next append: forget
            # the repair/count caches so the next cycle repairs + recounts
            self._marks_history_repaired = False
            self._marks_history_lines = None

    def _marks_history(self, ts: str, total: str) -> list[dict[str, str]]:
        """Bounded (ts, total-unrealized) series, resumed across restarts."""
        if self._history is None:
            self._history = []
            prior = self.run_dir / "marks.json"
            if prior.exists():
                try:
                    resumed = json.loads(prior.read_text()).get("history")
                    if isinstance(resumed, list):
                        self._history = [h for h in resumed if isinstance(h, dict)]
                except (OSError, json.JSONDecodeError):
                    pass  # unreadable prior marks: start a fresh history
        self._history.append({"ts": ts, "total": total})
        del self._history[:-MAX_MARKS_HISTORY]
        return self._history

    def _flatten_requested(self) -> bool:
        return (self.run_dir / "FLATTEN").exists()

    def _halt_requested(self) -> bool:
        return (self.run_dir / "HALT").exists()

    # -- main loop ---------------------------------------------------------

    def run(self) -> int:
        log.info("monitor armed for plan %s (%d structures)", self.plan.id, len(self.plan.structures))
        self._start_health()
        self.adopt_open_exits()
        while not self._all_closed():
            self._sync_book_from_disk()
            self.book.beat()
            self._save_book()
            tick_error: str | None = None
            try:
                self._tick()
            except Exception as exc:
                tick_error = type(exc).__name__
                log.exception("tick failed — retrying next poll")
            # in run(), NOT in _tick: the tick exits early outside session
            # hours and account.json would freeze overnight (C9)
            try:
                self._account_cycle()
            except Exception:
                log.exception("account cycle failed — retrying next poll")
            self._write_health(tick_error)
            # a drop inside the tick is caught above; ib_async never raises
            # it again, so exit and let systemd restart us behind
            # ExecStartPre --wait-api (Codex 2026-09-23)
            if not self.ib.connected:
                log.error("lost the IB Gateway connection — exiting for a clean restart")
                return EXIT_BROKER_LOST
            self.ib.sleep(POLL_SECONDS)
        self.book.beat()
        self._save_book()
        log.info("book fully closed; monitor exiting")
        return 0

    def _start_health(self) -> None:
        """Mark this run and carry the last good tick across restarts: a
        monitor killed mid-tick and restarted by systemd must not look
        freshly healthy, and its loop may never reach its own write."""
        try:
            prior = json.loads((self.run_dir / "monitor.json").read_text())
            ok_at = prior.get("last_tick_ok_at")
            if isinstance(ok_at, (int, float)):
                self._last_tick_ok_at = float(ok_at)
            # a crash-looping monitor must not restart the blind clock
            # (clamped to today's open on the next session tick)
            blind = prior.get("spot_blind")
            for sym, since in (blind.items() if isinstance(blind, dict) else ()):
                try:
                    parsed = datetime.fromisoformat(since)
                except (TypeError, ValueError):
                    continue
                if parsed.tzinfo is not None:
                    self._spot_blind_since[str(sym)] = parsed
        except (OSError, ValueError, AttributeError):
            pass  # no (readable) prior health: nothing to carry
        self._started_at = self._now().timestamp()
        self._write_health(None, tick_ran=False)

    def _write_health(self, tick_error: str | None, *, tick_ran: bool = True) -> None:
        """``monitor.json`` for the exit-machine watchdog (trex.exit_watch):
        the tick outcome, which the heartbeat can't show (a monitor whose
        every tick fails still beats). Error class only, never the message:
        broker errors can carry account details. Failure-isolated."""
        now = self._now().timestamp()
        if tick_ran and tick_error is None:
            self._tick_failures = 0
            self._last_tick_ok_at = now
        elif tick_ran:
            self._tick_failures += 1
        try:
            payload = {
                "at": now,
                "started_at": self._started_at,
                "pid": os.getpid(),
                "connected": bool(self.ib.connected),
                "tick_failures": self._tick_failures,
                "last_tick_ok_at": self._last_tick_ok_at,
                "last_error": tick_error,
                "touch_guarded": sorted(self._touch_guarded()),
                "spot_ok": {sym: at.isoformat() for sym, at in sorted(self._spot_ok.items())},
                "spot_blind": {
                    sym: since.isoformat() for sym, since in sorted(self._spot_blind_since.items())
                },
                **self._calendar_health(),
            }
            path = self.run_dir / "monitor.json"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, path)
        except Exception:
            log.exception("monitor health write failed (exit machine unaffected)")

    def _calendar_health(self) -> dict[str, Any]:
        """How far the session calendar reaches: past its last session
        every tick is a non-session (no exits). Own failure boundary."""
        try:
            return {
                "calendar_last_session": calendar_last_session().isoformat(),
                "calendar_horizon_warn": calendar_horizon_warn(self._now().date()),
            }
        except Exception:
            log.exception("calendar horizon check failed")
            return {"calendar_last_session": None, "calendar_horizon_warn": True}

    def _touch_guarded(self) -> set[str]:
        """Underlyings whose exit relies on the touch: OPEN structures (an
        exiting one is already being sold; entries are enter.py's)."""
        return {
            s.underlying
            for s in self.plan.structures
            if self.book.structures[s.id].status is Status.OPEN
        }

    def _with_spots(self, snap: Snapshot, now: datetime) -> Snapshot:
        """The feed's accepted spots for touch-guarded underlyings only (no
        spot for anything else is ever needed). Non-blocking: the feed
        fetches in the background and returns what has arrived."""
        if self.spots is None:
            return snap
        readings = self.spots.readings(sorted(self._touch_guarded()), now)
        return replace(
            snap, spots={sym: r.px for sym, r in readings.items()}, spot_sources=readings
        )

    def _track_spot_blind(self, snap: Snapshot, now: datetime) -> None:
        """Which touch-guarded underlyings have an accepted spot, and since
        when each has not. Blindness counts only inside the touch window
        (the delayed feed's first bar after the open, to the calendar
        close, early closes included); a since carried from an earlier
        session (restart) counts from today's window start, never before."""
        guarded = sorted(self._touch_guarded())
        self._spot_ok = {
            sym: snap.spot_sources[sym].as_of if sym in snap.spot_sources else now
            for sym in guarded
            if sym in snap.spots
        }
        window = touch_window(now)
        if window is None or not window[0] <= now.timestamp() <= window[1]:
            self._spot_blind_since = {}
            return
        floor = datetime.fromtimestamp(window[0], ET)
        blind: dict[str, datetime] = {}
        for sym in guarded:
            if sym in snap.spots:
                continue
            prior = self._spot_blind_since.get(sym)
            blind[sym] = max(prior, floor) if prior is not None else now
        self._spot_blind_since = blind

    def _account_cycle(self) -> None:
        """Persist account equity every ACCOUNT_EVERY cycles, failure-isolated.

        The web lane reads the freshest account.json across run dirs; a
        broker hiccup here must never touch the exit machine.
        """
        if self._account_writes % ACCOUNT_EVERY == 0:
            try:
                snap = self.ib.account_snapshot()
                if snap is not None:
                    write_account(self.run_dir / "account.json", snap)
            except Exception:
                log.exception("account snapshot failed (continuing)")
        self._account_writes += 1

    def adopt_open_exits(self) -> None:
        """Re-adopt our SELL combos still working at the broker after a restart.

        Idempotent via the persisted order checkpoint (Codex-M2 #8): when
        the working order id matches the book's, already-recorded fills
        form the baseline (downtime fills still merge); an UNKNOWN order
        id adopts the broker's current cumulative state as baseline so
        nothing is re-counted either way.
        """
        for trade in self.ib.open_combo_trades():
            sid = self.ib.structure_for_bag(trade.contract)
            if sid is None or trade.order.action != "SELL":
                continue
            ref = OrderRef(
                sid,
                "SELL",
                int(trade.order.totalQuantity),
                Decimal(str(trade.order.lmtPrice)),
                trade,
            )
            self.orders[sid] = ref
            st = self.book.structures[sid]
            order_id = str(trade.order.orderId)
            info = self.ib.order_status(ref)
            if st.exit_order == order_id:
                # known order: resume from the persisted checkpoint; fills
                # since then (downtime) merge on the next drain
                self._order_seen[sid] = st.exit_order_seen
                self._order_notional[sid] = st.exit_order_notional or Decimal(0)
            else:
                # unknown order (replaced while we were down): baseline the
                # broker's current execution state, disclose the adoption
                self._order_seen[sid] = info.filled
                self._order_notional[sid] = info.avg_fill_price * info.filled
                self.book.event(
                    self.events_path,
                    "exit_adopt_baseline",
                    structure=sid,
                    order=order_id,
                    filled=info.filled,
                    note="unknown order adopted at broker state; prior fills attributed to book history",
                )
            st.exit_order = order_id
            log.info("adopted working exit order for %s (oid %s)", sid, order_id)
        self._save_book()

    def _all_closed(self) -> bool:
        return all(st.status is Status.CLOSED for st in self.book.structures.values())

    def _tick(self) -> None:
        now = self._now()
        if not is_session(now) or now.time() < SESSION_OPEN or now.time() > SESSION_END:
            # no touch decisions off-session, so nothing is blind either
            self._spot_blind_since = {}
            self._spot_ok = {}
            # still absorb fills outside the session; a book-changing fill
            # writes a quote-less terminal history line (marks need quotes,
            # the position change must not be lost to a stale last sample)
            if self._drain_orders():
                self._marks_history_cycle(None)
            return

        # Drain fills BEFORE deciding: a fully-filled exit order must be
        # reflected in open_qty, or a refresh would re-place a sell on a
        # position that no longer exist (double sell = naked short).
        self._drain_orders()

        snap = self._with_spots(self.ib.snapshot(self.plan.structures, now), now)
        self._track_spot_blind(snap, now)
        marks_payload = self._write_marks(snap)
        flatten = self._flatten_requested()

        for spread in self.plan.structures:
            st = self.book.structures[spread.id]
            if st.status is Status.CLOSED:
                continue

            if flatten and st.status is not Status.EXIT_WORKING:
                if st.status is Status.ENTER_WORKING:
                    self._flatten_waits_for_entry_runner(spread)
                elif st.filled_qty > st.exit_filled_qty:
                    quote = snap.quotes.get(spread.id)
                    if quote is not None:  # no quote: retry next tick
                        self._begin_exit(spread, ExitReason.FLATTEN, cents(quote.bid))
                continue

            if st.status in (Status.PLANNED, Status.ENTER_WORKING):
                continue  # entry runner's lane

            action = decide(spread, st, snap)
            self._apply(spread, action)

        self._drain_orders()
        # history LAST: its failure boundary is its own, after every
        # order decision this tick (see _marks_history_cycle)
        self._marks_history_cycle(marks_payload)

    def _apply(self, spread: PutSpread, action: Action) -> None:
        st = self.book.structures[spread.id]
        match action:
            case NoAction(reason):
                log.debug("%s: %s", spread.id, reason)
            case ExitOrder(reason=reason, limit=limit) if st.status is Status.OPEN:
                if limit is None:
                    st.touch_ts = st.touch_ts or self._now()
                    log.info("%s: %s exit pending (no quote yet)", spread.id, reason.value)
                    return
                self._begin_exit(spread, reason, limit)
            case ExitOrder(limit=limit) if st.status is Status.EXIT_WORKING:
                self._refresh_exit(spread, limit)
            case PlaceEntry() | AbortEntry():
                log.error("monitor asked to act on entry state — out of scope, ignored")
            case _:
                log.warning("unhandled action %r", action)

    def _begin_exit(self, spread: PutSpread, reason: ExitReason, limit: Decimal) -> None:
        st = self.book.structures[spread.id]
        qty = st.open_qty
        if qty <= 0:
            return
        st.to(Status.EXIT_WORKING, self._now())
        st.exit_reason = reason.value
        st.touch_ts = st.touch_ts or self._now()
        self._save_book()
        self.book.event(
            self.events_path, "exit_begin", structure=spread.id, reason=reason.value, qty=qty
        )
        self._place_exit(spread, qty, limit)

    def _place_exit(self, spread: PutSpread, qty: int, limit: Decimal) -> None:
        if self._halt_requested():
            log.warning("%s: HALT active — not placing exit order", spread.id)
            return
        ref = self.ib.place_combo(spread, "SELL", qty, limit)
        self.orders[spread.id] = ref
        self._order_seen[spread.id] = 0  # new order: local count starts over
        self._order_notional[spread.id] = Decimal(0)  # and its notional too
        self.book.structures[spread.id].exit_order = f"{ref.trade.order.orderId}"
        self._save_book()
        self.book.event(
            self.events_path,
            "exit_order",
            structure=spread.id,
            qty=qty,
            limit=str(limit),
            order=self.book.structures[spread.id].exit_order,
        )

    def _refresh_exit(self, spread: PutSpread, limit: Decimal | None) -> None:
        if limit is None:
            return
        st = self.book.structures[spread.id]
        ref = self.orders.get(spread.id)
        if ref is None:
            self._place_exit(spread, st.open_qty, limit)  # restart recovery
            return
        if ref.trade.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled"):
            if ref.limit == limit:
                return
            # Reprice: cancel, WAIT for the confirmation (an unconfirmed
            # cancel plus a new SELL = two sell orders on one position =
            # naked short exposure), then replace at the new limit.
            self.ib.cancel(ref)
            st.exit_cycles += 1
            for _ in range(6):
                self.ib.sleep(0.5)
                if ref.trade.orderStatus.status in (
                    "Cancelled",
                    "ApiCancelled",
                    "Filled",
                ):
                    break
            if ref.trade.orderStatus.status not in (
                "Cancelled",
                "ApiCancelled",
                "Filled",
            ):
                log.warning("%s: exit cancel not confirmed; keeping old order", spread.id)
                return
            self._place_exit(spread, st.open_qty, limit)
        else:
            # prior order done but position remains — re-place the remainder
            if st.open_qty > 0:
                st.exit_cycles += 1
                self._place_exit(spread, st.open_qty, limit)

    def _flatten_waits_for_entry_runner(self, spread: PutSpread) -> None:
        """FLATTEN on a working entry: enter.py's to cancel, not ours.

        IBKR lets only the placing clientId cancel an order (error 10147),
        and this session never even sees enter.py's BUYs. The old path
        cancelled nothing and closed the structure on paper, so enter.py
        then abandoned a live BUY. The structure stays ENTER_WORKING (the
        exit watchdog keeps counting it as exposure) until enter.py cancels
        it: CLOSED, or OPEN with any filled part, which the FLATTEN branch
        then sells. If enter.py is not running, restart it: it adopts its
        working BUYs on start and cancels them on its first cycle.
        """
        if spread.id in self._flatten_wait_noted:
            return
        self._flatten_wait_noted.add(spread.id)
        log.warning("%s: FLATTEN - waiting for the entry runner to cancel its BUY", spread.id)
        self.book.event(self.events_path, "flatten_waits_for_entry_runner", structure=spread.id)

    def _drain_orders(self) -> bool:
        """Merge order-local fill increments into the book. Returns True
        when anything changed (a fill merged or a structure closed)."""
        changed = False
        for spread in self.plan.structures:
            sid = spread.id
            st = self.book.structures[sid]
            ref = self.orders.get(sid)
            if ref is None:
                continue
            info = self.ib.order_status(ref)
            seen = self._order_seen.get(sid, 0)
            if info.filled > seen:
                # merge the order-local increment into the cumulative book,
                # blending by NOTIONAL: the order's cumulative average times
                # only its new fills would re-price the earlier fills
                new_fills = info.filled - seen
                new_cum = st.exit_filled_qty + new_fills
                prev_notional = self._order_notional.get(sid, Decimal(0))
                if info.avg_fill_price:
                    inc_notional = info.avg_fill_price * info.filled - prev_notional
                    if st.exit_fill is not None and st.exit_filled_qty > 0:
                        st.exit_fill = (
                            st.exit_fill * st.exit_filled_qty + inc_notional
                        ) / new_cum
                    else:
                        st.exit_fill = (
                            inc_notional / new_fills if new_fills else info.avg_fill_price
                        )
                    self._order_notional[sid] = info.avg_fill_price * info.filled
                st.exit_filled_qty = new_cum
                self._order_seen[sid] = info.filled
                changed = True
                self.book.event(
                    self.events_path,
                    "exit_fill",
                    structure=sid,
                    filled=st.exit_filled_qty,
                    order_filled=info.filled,
                    avg=str(st.exit_fill),
                    status=info.status,
                )
            # persist the order checkpoint whenever it moved (adoption
            # idempotency; Codex-M2 #8)
            st.exit_order_seen = self._order_seen.get(sid, 0)
            st.exit_order_notional = self._order_notional.get(sid)
            if st.open_qty <= 0 and info.status in ("Filled", "Cancelled", "ApiCancelled"):
                st.to(Status.CLOSED, self._now())
                st.close_reason = st.exit_reason or "flat"
                self._save_book()
                self.book.event(
                    self.events_path, "closed", structure=sid, reason=st.close_reason
                )
                del self.orders[sid]
                changed = True
        self._save_book()
        return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trex-monitor")
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=71)
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="log decisions, place no orders")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    plan = load_legacy_plan(args.plan)  # put spreads only: multi-leg is the desk's
    run_dir = _run_dir(plan, args.state_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    lock_path = run_dir / "monitor.lock"
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another monitor holds %s — refusing double monitor", lock_path)
        return 2

    configure(_engine_config(plan))
    book = BookState.load(run_dir / "book.json", [s.id for s in plan.structures])

    ib = IbkrTrex(host=args.host, port=args.port, client_id=args.client_id)
    try:
        ib.connect()
    except Exception as exc:
        log.error(
            "cannot reach IB Gateway at %s:%s (%s) — start deploy/trex/docker-compose.yml first",
            args.host,
            args.port,
            type(exc).__name__,
        )
        return 4
    try:
        ib.prepare(plan.structures)
    except Exception as exc:
        log.error("contract qualification failed: %s", exc)
        return 5
    # the touch exit's spot: the (15 min delayed) Polygon snapshot, fetched
    # off the exit loop (trex.spot); IBKR stock prices are not a source
    monitor = Monitor(plan, ib, book, run_dir, spots=SpotFeed(polygon_fetcher()))
    if args.dry_run:
        log.info("dry-run: decisions only (no spot yet: the feed fetches in the background)")
        # one decision sweep, no orders
        snap = ib.snapshot(plan.structures, now_et())
        for spread in plan.structures:
            st = book.structures[spread.id]
            log.info("%s %s -> %s", spread.id, st.status.value, decide(spread, st, snap))
        return 0
    try:
        return monitor.run()
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
