"""The trex exit machine.

Runs as a systemd user service for the life of the book. It holds an flock
on ``monitor.lock`` and heartbeats into ``book.json`` — the entry runner
refuses to arm unless BOTH say the monitor is alive (arm-before-enter: the
plan's EV lives in the exit discipline, so the book is never owned without
its exit machine).

Scope: structures at/after OPEN. Entry states are the entry runner's
business; the monitor only cancels stale entries under the FLATTEN kill.

Kill files in the run directory:
  FLATTEN  exit every position now, marketable; cancel unfilled entries
  HALT     place no new orders (existing exits continue to completion)

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
from datetime import datetime
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.trex.clock import EntryWindow, is_session, now_et
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
from tree_options.trex.plan import PutSpread, TradePlan, cents, load_plan
from tree_options.trex.state import BookState, Status

log = logging.getLogger("trex.monitor")

POLL_SECONDS = 20
MAX_MARKS_HISTORY = 2000  # ~11h of 20s ticks; older samples drop off
HEARTBEAT_FRESH_SECONDS = 30
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
    ) -> None:
        self.plan = plan
        self.ib = ib
        self.book = book
        self.run_dir = run_dir
        self._clock = clock or now_et
        self.orders: dict[str, OrderRef] = {}  # structure_id -> working exit OrderRef
        # Order-local fill counts RESTART on every replacement, so the drain
        # merges increments (seen -> now) into the book's cumulative totals.
        # Comparing a local count against the cumulative once recorded a
        # phantom open qty and the refresh path re-sold contracts no longer
        # held (naked short) — see test_trex_monitor.TestCumulativeExitAccounting.
        self._order_seen: dict[str, int] = {}
        self._history: list[dict[str, str]] | None = None  # marks history, lazy-loaded

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

    def _write_marks(self, snap: Snapshot) -> None:
        """Persist the observation-only marks payload for the status panel."""
        payload = compute_marks(self.plan.structures, self.book, snap.quotes)
        payload["spots"] = {sym: str(px) for sym, px in snap.spots.items()}
        payload["ts"] = now_et().isoformat()
        payload["history"] = self._marks_history(payload["ts"], payload["total_unrealized"])
        tmp = self.run_dir / "marks.json.tmp"
        tmp.write_text(json.dumps(payload) + "\n")
        os.replace(tmp, self.run_dir / "marks.json")

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
        self.adopt_open_exits()
        while not self._all_closed():
            self._sync_book_from_disk()
            self.book.beat()
            self.book.save(self.run_dir / "book.json")
            try:
                self._tick()
            except Exception:
                log.exception("tick failed — retrying next poll")
            self.ib.sleep(POLL_SECONDS)
        self.book.beat()
        self.book.save(self.run_dir / "book.json")
        log.info("book fully closed; monitor exiting")
        return 0

    def adopt_open_exits(self) -> None:
        """Re-adopt our SELL combos still working at the broker after a restart."""
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
            self._order_seen[sid] = 0  # adopted: local fills since now are new
            self.book.structures[sid].exit_order = str(trade.order.orderId)
            log.info("adopted working exit order for %s (oid %s)", sid, trade.order.orderId)
        self.book.save(self.run_dir / "book.json")

    def _all_closed(self) -> bool:
        return all(st.status is Status.CLOSED for st in self.book.structures.values())

    def _tick(self) -> None:
        now = self._now()
        if not is_session(now) or now.time() < SESSION_OPEN or now.time() > SESSION_END:
            self._drain_orders()  # still absorb fills outside the session
            return

        # Drain fills BEFORE deciding: a fully-filled exit order must be
        # reflected in open_qty, or a refresh would re-place a sell on a
        # position that no longer exist (double sell = naked short).
        self._drain_orders()

        snap = self.ib.snapshot(self.plan.structures, now)
        self._write_marks(snap)
        flatten = self._flatten_requested()

        for spread in self.plan.structures:
            st = self.book.structures[spread.id]
            if st.status is Status.CLOSED:
                continue

            if flatten and st.status is not Status.EXIT_WORKING:
                if st.status is Status.ENTER_WORKING:
                    self._cancel_entry(spread, "kill: FLATTEN")
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
        self.book.save(self.run_dir / "book.json")
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
        self.book.structures[spread.id].exit_order = f"{ref.trade.order.orderId}"
        self.book.save(self.run_dir / "book.json")
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

    def _cancel_entry(self, spread: PutSpread, reason: str) -> None:
        """Kill-file entry cancel: cancel at the BROKER, not just on paper.

        A state-only close would leave a live BUY able to fill into a book
        nobody is watching; a late partial fill flips us to OPEN so the
        normal discipline still flattens it.
        """
        st = self.book.structures[spread.id]
        for trade in self.ib.open_combo_trades():
            sid = self.ib.structure_for_bag(trade.contract)
            if sid != spread.id or trade.order.action != "BUY":
                continue
            ref = OrderRef(
                sid,
                "BUY",
                int(trade.order.totalQuantity),
                Decimal(str(trade.order.lmtPrice)),
                trade,
            )
            self.ib.cancel(ref)
            for _ in range(6):
                self.ib.sleep(0.5)
                if trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Filled"):
                    break
            if trade.orderStatus.filled > st.filled_qty:
                st.filled_qty = int(trade.orderStatus.filled)
                st.entry_fill = Decimal(str(trade.orderStatus.avgFillPrice))
        if st.filled_qty > 0:
            st.to(Status.OPEN, self._now())
            log.warning(
                "%s: kill-file cancel but %d filled — OPEN, flatten follows",
                spread.id,
                st.filled_qty,
            )
            return
        st.to(Status.CLOSED, self._now())
        st.close_reason = reason
        self.book.save(self.run_dir / "book.json")
        self.book.event(self.events_path, "entry_cancelled", structure=spread.id, reason=reason)

    def _drain_orders(self) -> None:
        for spread in self.plan.structures:
            sid = spread.id
            st = self.book.structures[sid]
            ref = self.orders.get(sid)
            if ref is None:
                continue
            info = self.ib.order_status(ref)
            seen = self._order_seen.get(sid, 0)
            if info.filled > seen:
                # merge only the order-local increment into the cumulative
                # book, re-blending the average price across all fills
                new_fills = info.filled - seen
                new_cum = st.exit_filled_qty + new_fills
                if info.avg_fill_price:
                    if st.exit_fill is not None and st.exit_filled_qty > 0:
                        st.exit_fill = (
                            st.exit_fill * st.exit_filled_qty
                            + info.avg_fill_price * new_fills
                        ) / new_cum
                    else:
                        st.exit_fill = info.avg_fill_price
                st.exit_filled_qty = new_cum
                self._order_seen[sid] = info.filled
                self.book.event(
                    self.events_path,
                    "exit_fill",
                    structure=sid,
                    filled=st.exit_filled_qty,
                    order_filled=info.filled,
                    avg=str(st.exit_fill),
                    status=info.status,
                )
            if st.open_qty <= 0 and info.status in ("Filled", "Cancelled", "ApiCancelled"):
                st.to(Status.CLOSED, self._now())
                st.close_reason = st.exit_reason or "flat"
                self.book.save(self.run_dir / "book.json")
                self.book.event(
                    self.events_path, "closed", structure=sid, reason=st.close_reason
                )
                del self.orders[sid]
        self.book.save(self.run_dir / "book.json")


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

    plan = load_plan(args.plan)
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
    monitor = Monitor(plan, ib, book, run_dir)
    if args.dry_run:
        log.info("dry-run: decisions only")
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
