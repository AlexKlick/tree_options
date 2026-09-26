"""The trex entry runner — gated on a live monitor (arm-before-enter).

Refuses to place anything unless the exit machine is provably alive:
a fresh heartbeat in ``book.json`` AND the monitor's flock taken. The
book's EV lives in the exit discipline; owning positions without the
monitor is the one way to turn this plan into the hold-to-max profile
the operator's own research refuted.

Entry mechanics per structure: place at mid (capped), reprice every
``ENTRY_REPRICE_SECONDS`` at the fresh mid, cross toward the ask only
after the engine's escalation cycles, stop at the window end. Never
above the cap; no chase beyond the window.

Kill files (the monitor's run dir, same names):
  FLATTEN  cancel every working entry and enter nothing new. Only this
           runner CAN cancel its BUYs: IBKR lets only the placing
           clientId cancel an order (error 10147), and the monitor never
           sees them. A filled part goes OPEN for the monitor to flatten.
  HALT     place and reprice nothing (aborts at the window end still cancel)

Usage: python -m tree_options.trex.enter --plan plans/2026-09-18.toml
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from decimal import Decimal
from pathlib import Path

from tree_options.trex.clock import is_session, now_et
from tree_options.trex.engine import (
    AbortEntry,
    Action,
    NoAction,
    PlaceEntry,
    WorkingOrder,
    configure,
    decide,
    drain,
)
from tree_options.trex.ibkr import IbkrTrex, OrderRef, OrderStatusInfo
from tree_options.trex.monitor import (
    HEARTBEAT_FRESH_SECONDS,
    _engine_config,
    _run_dir,
)
from tree_options.trex.plan import PutSpread, load_legacy_plan
from tree_options.trex.state import ENTRY_LANE, BookState, Status

log = logging.getLogger("trex.enter")

ENTRY_REPRICE_SECONDS = 90
POLL_SECONDS = 15
_TERMINAL = ("Filled", "Cancelled", "ApiCancelled")
FLATTEN_REASON = "kill: FLATTEN"


class Enterer:
    def __init__(
        self,
        plan,
        ib: IbkrTrex,
        book: BookState,
        run_dir: Path,
        clock=None,
    ) -> None:
        self.plan = plan
        self.ib = ib
        self.book = book
        self.run_dir = run_dir
        self._clock = clock or now_et  # injectable for tests, mirrors Monitor
        self.orders: dict[str, OrderRef] = {}
        # Order-local fill checkpoints live ONLY in the persisted book
        # (entry_order_seen/_notional/_unpriced): the loop reloads book.json
        # every cycle, and an in-memory checkpoint that outlived a reload
        # silently lost fills (R2-01). engine.drain reconciles the broker's
        # cumulative report against the durable checkpoint idempotently, so
        # a failed save self-heals on the next cycle.
        self._unresolved_noted: set[str] = set()

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    def arm_gate(self) -> bool:
        """Heartbeat fresh AND the monitor's flock actually held."""
        if not self.book.armed_within(HEARTBEAT_FRESH_SECONDS):
            log.error("monitor heartbeat stale/absent — entry REFUSED (arm the monitor first)")
            return False
        lock_path = self.run_dir / "monitor.lock"
        if not lock_path.exists():
            log.error("no monitor lock file at %s — entry REFUSED", lock_path)
            return False
        probe = open(lock_path)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # someone holds it: the monitor is alive
        else:
            log.error("monitor lock is FREE — exit machine not running; entry REFUSED")
            return False
        finally:
            probe.close()

    def adopt_open_entries(self) -> None:
        for trade in self.ib.open_combo_trades():
            sid = self.ib.structure_for_bag(trade.contract)
            if sid is None or trade.order.action != "BUY":
                continue
            ref = OrderRef(
                sid,
                "BUY",
                int(trade.order.totalQuantity),
                Decimal(str(trade.order.lmtPrice)),
                trade,
            )
            self.orders[sid] = ref
            st = self.book.structures[sid]
            if st.status is Status.PLANNED:
                st.to(Status.ENTER_WORKING, now_et())
            oid = str(trade.order.orderId)
            if st.entry_order != oid:
                # an order the book never recorded: none of its fills are in it
                st.entry_order_seen, st.entry_order_notional = 0, None
                st.entry_order_unpriced = 0
            # a known order resumes from the persisted checkpoint (in the
            # book); downtime fills merge against it on the next drain
            st.entry_order = oid
            log.info("adopted working entry order for %s (oid %s)", sid, oid)
        self._save_book()

    def run(self) -> int:
        self.adopt_open_entries()
        while True:
            try:
                self._sync_book_from_disk()
                self._tick()
            except Exception:
                log.exception("tick failed — retrying next poll")
            if self._entry_pending():
                self.ib.sleep(POLL_SECONDS)
                continue
            # Completion is a DURABLE boundary (R3-01): a failed save on the
            # terminal transition leaves the disk in the entry lane while
            # memory says done. The loop may only end when the book on disk
            # agrees — resuming re-adopts the durable state and the broker's
            # cumulative report reconciles against the durable checkpoint.
            disk = BookState.load(
                self.run_dir / "book.json", list(self.book.structures)
            )
            pending = [
                sid
                for sid, st in disk.structures.items()
                if st.status in (Status.PLANNED, Status.ENTER_WORKING)
            ]
            if not pending:
                break
            self.book.structures = disk.structures
            log.warning(
                "entry lane unfinished on disk (%s) despite in-memory state; resuming",
                ",".join(pending),
            )
            self.ib.sleep(POLL_SECONDS)
        log.info("entry phase complete; book is with the monitor")
        return 0

    def _sync_book_from_disk(self) -> None:
        """Adopt the monitor's book writes each cycle.

        The monitor and the entry runner share ``book.json``; without this
        reload a whole-book save here could revert the monitor's exit
        states with a stale in-memory copy (and ``_save_book`` re-adopts
        them at every save, closing the gap between this read and a save).
        """
        disk = BookState.load(self.run_dir / "book.json", list(self.book.structures))
        self.book.structures = disk.structures

    def _save_book(self) -> None:
        """Whole-book save that never reverts the monitor's writes: any
        structure already past the entry lane ON DISK is the monitor's
        (only this runner moves one out, and its own not-yet-saved move
        still shows the entry lane on disk, so it is kept)."""
        self.book.save_owned(
            self.run_dir / "book.json", lambda _mine, disk: disk.status not in ENTRY_LANE
        )

    def _entry_pending(self) -> bool:
        return any(
            st.status in (Status.PLANNED, Status.ENTER_WORKING)
            for st in self.book.structures.values()
        )

    def _flatten_requested(self) -> bool:
        return (self.run_dir / "FLATTEN").exists()

    def _halt_requested(self) -> bool:
        return (self.run_dir / "HALT").exists()

    def _tick(self) -> None:
        if self._flatten_requested():
            # any hour: a kill must not wait for a session day
            self._flatten_entries()
            return
        now = self._clock()
        if not is_session(now):
            log.info("non-session day — nothing to enter")
            return
        snap = self.ib.snapshot(self.plan.structures, now)

        for spread in self.plan.structures:
            st = self.book.structures[spread.id]
            if st.status not in (Status.PLANNED, Status.ENTER_WORKING):
                continue
            action = decide(spread, st, snap)
            self._apply(spread, action)

        self._drain_fills()

    def _flatten_entries(self) -> None:
        """FLATTEN: cancel our working entries, enter nothing new."""
        for spread in self.plan.structures:
            if self.book.structures[spread.id].status in ENTRY_LANE:
                self._cancel_and_settle(
                    spread, close_reason=FLATTEN_REASON, event="entry_cancelled",
                    reason=FLATTEN_REASON,
                )
        self._save_book()

    def _cancel_and_settle(
        self, spread: PutSpread, *, close_reason: str, event: str, reason: str
    ) -> bool:
        """Stop an entry and settle it from what the BROKER says was filled:
        OPEN (the monitor's discipline, or its FLATTEN, sells it) or CLOSED.

        Returns False, leaving the structure ENTER_WORKING to retry next
        cycle, when that can't be known yet: a cancel not confirmed (a live
        BUY is never closed on paper), or, with no working order left to
        ask (a restart after it filled or died), broker evidence that is
        inconclusive. Fills merge cumulatively through the order checkpoint.
        """
        sid = spread.id
        st = self.book.structures[sid]
        if st.status is Status.ENTER_WORKING:
            ref = self.orders.get(sid)
            if ref is not None:
                if self.ib.order_status(ref).status not in _TERMINAL:
                    self.ib.cancel(ref)
                    for _ in range(6):
                        self.ib.sleep(0.5)
                        if self.ib.order_status(ref).status in _TERMINAL:
                            break
                info, _changed = self._merge_fills(sid, ref)
                if info.status not in _TERMINAL:
                    log.warning("%s: entry cancel not confirmed; retrying next cycle", sid)
                    return False
                # the ref is dropped before the caller's save: if that save
                # fails, run()'s durable completion check keeps the loop alive
                # and _reconcile recovers the fills from broker evidence (R3-01)
                self.orders.pop(sid, None)
            elif not self._reconcile(spread):
                return False
        # PLANNED: nothing working (never placed, or a confirmed cancel)
        if st.filled_qty > 0:
            st.to(Status.OPEN, now_et())
            log.warning("%s: entry stopped with %d filled; OPEN for the monitor", sid, st.filled_qty)
        else:
            st.to(Status.CLOSED, now_et())
            st.close_reason = close_reason
        self.book.event(
            self.events_path, event, structure=sid, reason=reason, filled=st.filled_qty
        )
        return True

    def _reconcile(self, spread: PutSpread) -> bool:
        """An ENTER_WORKING structure with no working order: its order
        filled or died while this runner was down (adoption only sees
        working orders). Merge what the broker can prove; if it can't,
        keep the entry working and say so once."""
        st = self.book.structures[spread.id]
        evidence = self.ib.entry_fill_evidence(spread.id, st.entry_order)
        if evidence is None:
            if spread.id not in self._unresolved_noted:
                self._unresolved_noted.add(spread.id)
                log.error(
                    "%s: no working entry order and its fills can't be proven; "
                    "keeping ENTER_WORKING (check the account by hand)",
                    spread.id,
                )
                self.book.event(
                    self.events_path, "entry_unresolved", structure=spread.id,
                    order=st.entry_order,
                )
            return False
        filled, avg = evidence
        self._merge_order_total(spread.id, filled, avg or Decimal(0), "reconciled",
                                st.entry_order or f"evidence-{spread.id}")
        return True

    def _apply(self, spread: PutSpread, action: Action) -> None:
        st = self.book.structures[spread.id]
        match action:
            case NoAction(reason):
                log.debug("%s: %s", spread.id, reason)
            case PlaceEntry() if self._halt_requested():
                log.warning("%s: HALT active — placing no entry order", spread.id)
            case PlaceEntry(limit=limit) if st.status is Status.PLANNED:
                self._place(spread, limit)
            case PlaceEntry(limit=limit):
                self._reprice(spread, limit)
            case AbortEntry(reason=reason):
                self._abort(spread, reason)

    def _place(self, spread: PutSpread, limit: Decimal) -> None:
        st = self.book.structures[spread.id]
        if st.status is Status.PLANNED:
            st.to(Status.ENTER_WORKING, now_et())
        # reprice path: already ENTER_WORKING, no transition needed
        # a partial fill may already be in the book — buy only the remainder
        remaining = spread.quantity - st.filled_qty
        if remaining <= 0:
            st.to(Status.OPEN, now_et())  # fully filled already
            self._save_book()
            return
        self._save_book()
        ref = self.ib.place_combo(spread, "BUY", remaining, limit)
        self.orders[spread.id] = ref
        st.entry_order = str(ref.trade.order.orderId)
        # new order: its checkpoint starts over (engine.drain also resets on
        # an order-id change; this keeps the book explicit at place time)
        st.entry_order_seen, st.entry_order_notional = 0, None
        st.entry_order_unpriced = 0
        self.book.event(
            self.events_path,
            "entry_order",
            structure=spread.id,
            qty=remaining,
            limit=str(limit),
            order=st.entry_order,
        )
        log.info("%s: BUY %d @ %s", spread.id, remaining, limit)
        self._save_book()

    def _reprice(self, spread: PutSpread, limit: Decimal) -> None:
        ref = self.orders.get(spread.id)
        if ref is None:
            return  # adopted/foreign edge: engine said working; nothing local to reprice
        if ref.limit == limit:
            return
        if ref.trade.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled"):
            return
        self.ib.cancel(ref)
        st = self.book.structures[spread.id]
        st.entry_cycles += 1
        self.book.event(
            self.events_path,
            "entry_reprice",
            structure=spread.id,
            limit=str(limit),
            cycle=st.entry_cycles,
        )
        self._place_after_cancel(spread, limit)

    def _place_after_cancel(self, spread: PutSpread, limit: Decimal) -> None:
        # Cancel is async at IBKR. Placing the replacement before the cancel
        # confirms risks TWO working BUY combos = double the book. Wait for
        # the confirmation; if it never comes, keep the old order and try
        # again next poll.
        for _ in range(6):
            self.ib.sleep(0.5)
            ref = self.orders.get(spread.id)
            if ref is None:
                break
            if ref.trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Filled"):
                break
        ref = self.orders.get(spread.id)
        if (
            ref is not None
            and ref.trade.orderStatus.status not in ("Cancelled", "ApiCancelled", "Filled")
        ):
            log.warning("%s: cancel not confirmed; keeping old order this cycle", spread.id)
            return
        if ref is not None:
            # the cancelled order's last fills first: the replacement buys
            # only what is still missing (and resets the order-local count)
            self._merge_fills(spread.id, ref)
        self._place(spread, limit)

    def _abort(self, spread: PutSpread, reason: str) -> None:
        """Window end / date passed: the same cancel-and-settle discipline as
        FLATTEN (confirmed cancel, cumulative fills, broker evidence)."""
        settled = self._cancel_and_settle(
            spread, close_reason=f"aborted: {reason}", event="entry_abort", reason=reason
        )
        self._save_book()
        if settled:
            st = self.book.structures[spread.id]
            log.info("%s: entry aborted (%s) filled_qty=%d", spread.id, reason, st.filled_qty)

    def _merge_fills(self, sid: str, ref: OrderRef) -> tuple[OrderStatusInfo, bool]:
        """Merge this order's latest cumulative fills into the book.
        Returns (status, changed)."""
        info = self.ib.order_status(ref)
        changed = self._merge_order_total(
            sid, info.filled, info.avg_fill_price, info.status,
            str(ref.trade.order.orderId))
        return info, changed

    def _merge_order_total(
        self, sid: str, filled: int, avg: Decimal, status: str, order_id: str
    ) -> bool:
        """Merge the order's cumulative fill report into the book through
        the engine's accounting law (``engine.drain``): quantity and price
        are reconciled separately (a fill can arrive before its average),
        the order's cumulative notional replaces only its own priced
        contribution, and fills without a reported price count as unpriced —
        ``entry_fill`` averages only priced packages, never a fabricated
        whole-position number (R2-02). The checkpoint lives in the book, so
        a restart (or the per-cycle reload) never adds recorded fills twice.

        Returns True when the book changed (R2-01: every mutation must be
        persisted before the next reload)."""
        st = self.book.structures[sid]
        before = (st.filled_qty, st.entry_fill, st.entry_order_seen,
                  st.entry_order_notional, st.entry_unpriced_qty)
        new = drain(st, WorkingOrder(role="entry", filled=filled, avg_fill_price=avg,
                                     order_id=order_id))
        after = (st.filled_qty, st.entry_fill, st.entry_order_seen,
                 st.entry_order_notional, st.entry_unpriced_qty)
        if new > 0:
            self.book.event(
                self.events_path,
                "entry_fill",
                structure=sid,
                filled=st.filled_qty,
                order_filled=filled,
                avg=str(st.entry_fill),
                status=status,
            )
        elif after != before:
            self.book.event(
                self.events_path, "entry_fill_revised", structure=sid,
                filled=st.filled_qty, order_filled=filled,
                avg=str(st.entry_fill), status=status,
            )
        return after != before

    def _drain_fills(self) -> None:
        mutated = False
        finished: list[str] = []  # refs dropped only after a durable save
        for spread in self.plan.structures:
            sid = spread.id
            st = self.book.structures[sid]
            ref = self.orders.get(sid)
            if ref is None or st.status is not Status.ENTER_WORKING:
                continue
            info, changed = self._merge_fills(sid, ref)
            if info.status in _TERMINAL:
                if st.filled_qty > 0:
                    st.to(Status.OPEN, now_et())
                    log.info("%s: OPEN with %d spreads @ %s", sid, st.filled_qty, st.entry_fill)
                    finished.append(sid)
                elif info.status == "Cancelled":
                    # our own reprice cancel — back to PLANNED for the next cycle
                    st.to(Status.PLANNED, now_et())
                    finished.append(sid)
                mutated = True  # a lane transition is durable state
            mutated = mutated or changed
        if mutated:
            # every accounting mutation is persisted before the loop's next
            # book reload (R2-01: the round-1 fix covered revisions only and
            # plain partial fills were wiped by the reload). A failed save
            # propagates to run()'s handler; the next cycle reloads the
            # durable book and the broker's cumulative report reconciles
            # against the durable checkpoint (a duplicate event is fine).
            self._save_book()
        # the terminal report's reference is dropped only once the terminal
        # state is on disk (R3-01): a reference discarded before publication
        # cannot be retried, and the memory-only loop condition could then
        # declare completion over a stale book.
        for sid in finished:
            self.orders.pop(sid, None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trex-enter")
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=72)
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="show decisions, place no orders")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    plan = load_legacy_plan(args.plan)  # put spreads only: multi-leg is the desk's
    run_dir = _run_dir(plan, args.state_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
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

    enterer = Enterer(plan, ib, book, run_dir)
    if args.dry_run:
        snap = ib.snapshot(plan.structures, now_et())
        for spread in plan.structures:
            st = book.structures[spread.id]
            log.info("%s %s -> %s", spread.id, st.status.value, decide(spread, st, snap))
        ib.disconnect()
        return 0

    if not enterer.arm_gate():
        ib.disconnect()
        return 3
    try:
        return enterer.run()
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
