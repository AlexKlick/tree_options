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
    configure,
    decide,
)
from tree_options.trex.ibkr import IbkrTrex, OrderRef
from tree_options.trex.monitor import (
    HEARTBEAT_FRESH_SECONDS,
    _engine_config,
    _run_dir,
)
from tree_options.trex.plan import PutSpread, load_plan
from tree_options.trex.state import BookState, Status

log = logging.getLogger("trex.enter")

ENTRY_REPRICE_SECONDS = 90
POLL_SECONDS = 15


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
        # Order-local fill counts restart on replacement; drain merges
        # increments into the book's cumulative filled_qty (C1 regression).
        self._order_seen: dict[str, int] = {}
        # per-order cumulative notional (avg * filled) for notional blends
        self._order_notional: dict[str, Decimal] = {}

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
            st.entry_order = str(trade.order.orderId)
            log.info("adopted working entry order for %s (oid %s)", sid, trade.order.orderId)
        self.book.save(self.run_dir / "book.json")

    def run(self) -> int:
        self.adopt_open_entries()
        while self._entry_pending():
            try:
                self._sync_book_from_disk()
                self._tick()
            except Exception:
                log.exception("tick failed — retrying next poll")
            self.ib.sleep(POLL_SECONDS)
        log.info("entry phase complete; book is with the monitor")
        return 0

    def _sync_book_from_disk(self) -> None:
        """Adopt the monitor's book writes each cycle.

        The monitor and the entry runner share ``book.json``; without this
        reload a whole-book save here could revert a monitor's FLATTEN
        cancel (re-placing a killed BUY = double book) or its exit states
        with a stale in-memory copy. Monitor-owned exit-side fields ride
        along untouched: this runner never writes them.
        """
        disk = BookState.load(self.run_dir / "book.json", list(self.book.structures))
        self.book.structures = disk.structures

    def _entry_pending(self) -> bool:
        return any(
            st.status in (Status.PLANNED, Status.ENTER_WORKING)
            for st in self.book.structures.values()
        )

    def _tick(self) -> None:
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

    def _apply(self, spread: PutSpread, action: Action) -> None:
        st = self.book.structures[spread.id]
        match action:
            case NoAction(reason):
                log.debug("%s: %s", spread.id, reason)
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
            self.book.save(self.run_dir / "book.json")
            return
        self.book.save(self.run_dir / "book.json")
        ref = self.ib.place_combo(spread, "BUY", remaining, limit)
        self.orders[spread.id] = ref
        self._order_seen[spread.id] = 0  # new order: local count starts over
        self._order_notional[spread.id] = Decimal(0)
        st.entry_order = str(ref.trade.order.orderId)
        self.book.event(
            self.events_path,
            "entry_order",
            structure=spread.id,
            qty=remaining,
            limit=str(limit),
            order=st.entry_order,
        )
        log.info("%s: BUY %d @ %s", spread.id, remaining, limit)
        self.book.save(self.run_dir / "book.json")

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
        self._place(spread, limit)

    def _abort(self, spread: PutSpread, reason: str) -> None:
        st = self.book.structures[spread.id]
        ref = self.orders.pop(spread.id, None)
        if ref is not None:
            self.ib.cancel(ref)
            # IBKR cancels are async; a fill can land right up to the
            # confirmation. Decide OPEN vs CLOSED from the final fill count.
            info = None
            for _ in range(6):
                self.ib.sleep(0.5)
                info = self.ib.order_status(ref)
                if info.status in ("Filled", "Cancelled", "ApiCancelled"):
                    break
            if info is not None and info.filled > st.filled_qty:
                st.filled_qty = info.filled
                st.entry_fill = info.avg_fill_price
        if st.filled_qty > 0:
            st.to(Status.OPEN, now_et())  # partial/late fill: monitor's lane now
            log.warning(
                "%s: aborted entry but %d filled — OPEN, monitor will flatten on discipline",
                spread.id,
                st.filled_qty,
            )
        else:
            st.to(Status.CLOSED, now_et())
            st.close_reason = f"aborted: {reason}"
        self.book.event(self.events_path, "entry_abort", structure=spread.id, reason=reason)
        self.book.save(self.run_dir / "book.json")
        log.info("%s: entry aborted (%s) filled_qty=%d", spread.id, reason, st.filled_qty)

    def _drain_fills(self) -> None:
        for spread in self.plan.structures:
            sid = spread.id
            st = self.book.structures[sid]
            ref = self.orders.get(sid)
            if ref is None or st.status is not Status.ENTER_WORKING:
                continue
            info = self.ib.order_status(ref)
            seen = self._order_seen.get(sid, 0)
            if info.filled > seen:
                # merge the order-local increment, blending by NOTIONAL:
                # the order's cumulative average times only its new fills
                # would re-price the earlier fills (Codex-M2 #2)
                new_fills = info.filled - seen
                new_cum = st.filled_qty + new_fills
                prev_notional = self._order_notional.get(sid, Decimal(0))
                if info.avg_fill_price:
                    inc_notional = info.avg_fill_price * info.filled - prev_notional
                    if st.entry_fill is not None and st.filled_qty > 0:
                        st.entry_fill = (
                            st.entry_fill * st.filled_qty + inc_notional
                        ) / new_cum
                    else:
                        st.entry_fill = (
                            inc_notional / new_fills if new_fills else info.avg_fill_price
                        )
                    self._order_notional[sid] = info.avg_fill_price * info.filled
                st.filled_qty = new_cum
                self._order_seen[sid] = info.filled
                self.book.event(
                    self.events_path,
                    "entry_fill",
                    structure=sid,
                    filled=st.filled_qty,
                    order_filled=info.filled,
                    avg=str(st.entry_fill),
                    status=info.status,
                )
            if info.status in ("Filled", "Cancelled", "ApiCancelled"):
                if st.filled_qty > 0:
                    st.to(Status.OPEN, now_et())
                    log.info("%s: OPEN with %d spreads @ %s", sid, st.filled_qty, st.entry_fill)
                    self.orders.pop(sid, None)
                elif info.status == "Cancelled":
                    # our own reprice cancel — back to PLANNED for the next cycle
                    st.to(Status.PLANNED, now_et())
                    self.orders.pop(sid, None)
                self.book.save(self.run_dir / "book.json")


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

    plan = load_plan(args.plan)
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
