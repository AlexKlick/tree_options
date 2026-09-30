"""B2 soak: one scripted multi-session run of the supervised desk.

The invariants are individually pinned by tests/unit/test_desk_runtime.py and
tests/unit/test_supervised_desk.py; this module drives ONE desk — one gateway,
one run dir, one supervised outbox — through all of them in sequence, across
five trading days and three process restarts, then asserts the END state:
the book closed cleanly, no lane ever placed a second broker order for a
structure, the event log is an ordered coherent history, and the supervised
outbox ends in terminal states that agree with the broker views.

The days (the clock is fake; the fakes run in-process, so the whole run takes
seconds, not days):

- 2026-10-01  canary-a enters through the REAL request inbox (mandate ->
  screening -> permit -> register -> send), fills at a 0.90 debit and opens.
- 2026-10-09  canary-a's time stop fires: the exit is tagged ``trex:desk:``
  with an explicit account, the runtime is KILLED mid-exit, the exit fills
  while it is down, and a fresh runtime closes the book from the broker's
  executions. canary-b (2 packages, registered through the runtime path —
  the request inbox pins quantity 1) then enters; its exit PARTIALLY fills
  before a second kill, and the remainder fills while down: the SAME order
  finishes, nothing is re-sent.
- 2026-10-13  canary-b closes from the evidence. canary-c's entry ack times
  out (uncertain): the package is held — a fresh request for the same
  package is refused — then reconciliation confirms not_submitted, the book
  closes not_submitted and the package becomes admissible again (probed at
  ``record_intent``: the runtime's spec file pins the structure to its first
  intent, so a same-id re-send is a separate operator concern). canary-d
  enters through the inbox.
- 2026-10-14  HALT: the due exit is not placed; resumed, the time stop goes
  out and closes. canary-e (a later expiry, its own leg contracts) enters
  through the inbox.
- 2026-10-15  canary-e's exit vanishes flat with no executions to explain
  it: held for the operator (alert once, nothing sent), then RESOLVE-FLAT
  closes the packages unpriced.

Quotes never move, so no exit is ever repriced: every lane places exactly
one broker order for the whole run.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.unit.trex_fakes import SupervisedGateway, fill_row, position_row

from tree_options.execution import OrderIntent, SubmitAttempt
from tree_options.time.sessions import shift_instant
from tree_options.trex.account import AccountSnapshot
from tree_options.trex.clock import ET
from tree_options.trex.desk_runtime import DeskPaths, DeskRuntime, desk_order_ref
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised import (
    SupervisedIntent,
    SupervisedPaths,
    grant_mandate,
    reconcile_intent,
    record_intent,
)
from tree_options.trex.supervised_desk import SupervisedDesk, load_profile, run_loop
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    SupervisedEffect,
    effect_bytes,
    supervised_order_ref,
)

pytest.importorskip("ib_async")

pytestmark = pytest.mark.soak

ACCOUNT = "DU1234567"
STRATEGY = "operational-canary/1"
EPOCH = "desk83-soak"
F = "20261016"
FL = "20261023"  # canary-e's later expiry: its own leg contracts
CON = {
    ("OPT", "SPY", F, 100.0, "P"): 100,
    ("OPT", "SPY", F, 95.0, "P"): 95,
    ("OPT", "SPY", FL, 100.0, "P"): 102,
    ("OPT", "SPY", FL, 95.0, "P"): 97,
}
PROFILE = {
    "profile_id": "canary-5k",
    "revision": 1,
    "intended_capital": "5000",
    "risk_style": "defined-risk",
    "goals": ["operational-canary"],
    "allowed_strategy_versions": [STRATEGY],
    "max_loss_per_trade": "300",
    "max_open_loss": "1500",
    "max_daily_loss": "600",
    "horizon_days": 30,
}

#: every leg is a 100/95 put vertical (package mid exactly 0.90: the exit
#: limit equals the working order's, so nothing is ever repriced)
EXPIRY = date(2026, 10, 16)
EXPIRY_LATE = date(2026, 10, 23)
A_ENTRY_AT = datetime(2026, 10, 1, 10, 0, tzinfo=ET)
A_EXIT_AT = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
B_ENTRY_AT = datetime(2026, 10, 9, 10, 30, tzinfo=ET)
B_EXIT_AT = datetime(2026, 10, 13, 10, 0, tzinfo=ET)
C_REQUEST_AT = datetime(2026, 10, 13, 10, 30, tzinfo=ET)
D_REQUEST_AT = datetime(2026, 10, 13, 11, 0, tzinfo=ET)
D_HALT_AT = datetime(2026, 10, 14, 10, 0, tzinfo=ET)
E_REQUEST_AT = datetime(2026, 10, 14, 10, 30, tzinfo=ET)
E_EXIT_AT = datetime(2026, 10, 15, 10, 0, tzinfo=ET)
E_RESOLVE_AT = datetime(2026, 10, 15, 10, 50, tzinfo=ET)


def _vertical(
    sid: str, *, qty: int = 1, entry_date: date, exit_deadline: date, expiry: date = EXPIRY
) -> LegStructure:
    return LegStructure(
        id=sid,
        underlying="SPY",
        kind="debit_vertical",
        legs=[
            {"right": "P", "action": "BUY", "strike": "100", "expiry": expiry},
            {"right": "P", "action": "SELL", "strike": "95", "expiry": expiry},
        ],
        quantity=qty,
        entry_date=entry_date,
        exit_deadline=exit_deadline,
        limit="1.00",
        exits={"touch": False, "breach": False},
    )


def _effect(structure: LegStructure, intent_id: str) -> SupervisedEffect:
    return SupervisedEffect(
        intent_id=intent_id,
        account_id=ACCOUNT,
        structure=structure,
        side=structure.open_side,
        quantity=structure.quantity,
        limit=Decimal("0.90"),
        order_ref=supervised_order_ref(intent_id),
    )


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ClockedIbkr(IbkrTrex):
    """The account snapshot stamped by the test clock (the real one reads the
    wall clock, which a fixed test clock would call stale)."""

    clock: Clock

    def account_snapshot(self) -> AccountSnapshot | None:
        return AccountSnapshot(
            account_id=ACCOUNT,
            net_liquidation=Decimal("1000000"),
            cash=Decimal("1000000"),
            buying_power=Decimal("4000000"),
            currency="USD",
            ts=self.clock(),
        )


class Soak:
    """One supervised desk: gateway, adapter, runtime, desk process, dirs.

    A restart replaces the runtime AND the desk process (fresh in-memory
    state) over the SAME run dir and supervised outbox, like a systemd
    restart of the unit."""

    def __init__(self, tmp: Path) -> None:
        self.clock = Clock(A_ENTRY_AT)
        self.gw = SupervisedGateway(CON, ACCOUNT)
        for con, bid, ask in (
            (100, 2.00, 2.20),
            (95, 1.10, 1.30),
            (102, 2.00, 2.20),
            (97, 1.10, 1.30),
        ):
            self.gw.quote(con, bid, ask)  # package 0.70 / 1.10, mid exactly 0.90
        self.ib = ClockedIbkr(client_id=SUPERVISED_CLIENT_ID)
        self.ib.clock = self.clock
        self.ib._ib = self.gw
        self.sup = SupervisedPaths(tmp / "supervised")
        self.sup.prepare()
        self.paths = DeskPaths(tmp / "desk")
        self.notified: list[tuple[str, str, str]] = []
        self.vanished: list[str] = []  # orderRefs the gateway's views lost
        self.broker = IbkrSupervisedBroker(self.ib, clock=self.clock)
        self.rt = self._new_runtime()
        self.desk = self._new_desk()
        profile = self.paths.root / "profile.json"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(json.dumps(PROFILE))
        self.profile_digest = load_profile(profile)[1]

    # -- process lifecycle ---------------------------------------------------

    def _new_runtime(self) -> DeskRuntime:
        rt = DeskRuntime(self.ib, self.paths, supervised=self.sup, clock=self.clock)
        rt.acquire()
        rt.notify = lambda title, message, priority="default": self.notified.append(
            (title, message, priority)
        )
        return rt

    def _new_desk(self) -> SupervisedDesk:
        return SupervisedDesk(
            self.ib, self.rt, self.broker, supervised=self.sup, owner_epoch=EPOCH, clock=self.clock
        )

    def restart(self) -> None:
        self.rt.release()
        self.rt = self._new_runtime()
        self.desk = self._new_desk()

    def kill_runtime(self) -> None:
        self.rt.release()  # the process dies; book, specs and events stay on disk

    # -- request-day helpers ---------------------------------------------------

    def grant(self) -> None:
        grant_mandate(
            self.sup,
            now=self.clock.now,
            account_id=ACCOUNT,
            owner_epoch=EPOCH,
            strategy_version=STRATEGY,
            profile_digest=self.profile_digest,
            max_orders=3,
            ttl_seconds=600,
            granted_by="operator-terminal",
        )

    def tick_quotes(self) -> None:
        for con in (100, 95, 102, 97):
            self.gw.tickers[con].time = shift_instant(self.clock.now, -5)  # type: ignore[attr-defined]

    def arm(self, at: datetime) -> None:
        """A new request day: clock, fresh mandate, fresh quotes, a beat."""
        self.clock.now = at
        self.grant()
        self.tick_quotes()
        self.rt.tick()

    def request(self, structure: LegStructure, name: str) -> Path:
        effect = _effect(structure, name)
        inbox = self.desk.requests_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema": "trex-desk-entry-request/1",
            "strategy_version": STRATEGY,
            "send_deadline": shift_instant(self.clock.now, 300).isoformat(),
            "requested_by": "operator-terminal",
            "effect": effect.model_dump(mode="json", by_alias=True),
        }
        path = inbox / f"{name}.json"
        path.write_text(json.dumps(doc))
        return path

    def result(self, name: str) -> dict[str, Any]:
        return json.loads((self.desk.requests_dir() / f"{name}.result.json").read_text())

    # -- desk helpers ------------------------------------------------------------

    def send_entry(self, effect: SupervisedEffect) -> Any:
        """The runtime's own entry send (no inbox): register + submit."""
        attempt = SubmitAttempt(
            record_id=f"sup-send-{effect.intent_id}",
            intent_id=effect.intent_id,
            send_attempt_at=self.clock.now,
            source="supervised",
            source_sequence_id=effect.intent_id,
        )
        self.broker.submit(attempt, effect_bytes(effect))
        return self.gw.trades[-1]

    def fill(self, trade: Any, qty: int, price: float) -> None:
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = qty
        trade.orderStatus.avgFillPrice = price

    def hold_legs(self, qty: int = 1, cons: tuple[int, int] = (100, 95)) -> None:
        """The account holds ``qty`` packages: long the BUY leg, short the SELL."""
        self.gw.position_rows[:] = [
            position_row(cons[0], qty, account=ACCOUNT, symbol="SPY"),
            position_row(cons[1], -qty, account=ACCOUNT, symbol="SPY"),
        ]

    def book(self) -> dict[str, Any]:
        return json.loads(self.paths.book().read_text())["structures"]

    def event_names(self) -> list[str]:
        return [json.loads(line)["event"] for line in self.event_lines()]

    def event_lines(self) -> list[str]:
        return self.paths.events().read_text().splitlines()

    def refs(self) -> list[str]:
        return [str(t.order.orderRef) for t in self.gw.trades]

    def all_refs(self) -> list[str]:
        return self.refs() + self.vanished

    def assert_one_order_per_lane(self) -> None:
        counts = Counter(self.all_refs())
        assert max(counts.values()) == 1, counts


@pytest.fixture()
def soak(tmp_path: Path) -> Soak:
    rig = Soak(tmp_path)
    yield rig
    rig.rt.release()


# ---------------------------------------------------------------- the soak


def test_one_desk_across_sessions_restarts_and_operator_verdicts(soak: Soak) -> None:
    # ---- Day 1: canary-a through the real request inbox -------------------
    soak.arm(A_ENTRY_AT)  # mandate, fresh quotes, a heartbeat beat
    soak.request(
        _vertical("canary-a", entry_date=date(2026, 10, 1), exit_deadline=date(2026, 10, 9)),
        "canary-a",
    )
    assert run_loop(soak.desk, interval_s=0, stop=lambda: False, max_ticks=1) == 0
    sent = soak.result("canary-a")
    assert (sent["status"], sent["receipt"]["outcome"]) == ("sent", "acknowledged")
    entry_a = soak.gw.trades[-1]
    assert (entry_a.order.orderRef, entry_a.order.account) == ("trex:sup:canary-a", ACCOUNT)
    soak.rt.tick()  # the runtime adopts the live tagged order it finds
    assert soak.book()["canary-a"]["status"] == "enter_working"
    soak.fill(entry_a, 1, 0.90)
    soak.hold_legs()
    soak.rt.tick()
    book_a = soak.book()["canary-a"]
    assert (book_a["status"], book_a["filled_qty"], book_a["entry_fill"]) == ("open", 1, "0.9")

    # ---- Day 7 (10-09): the exit fires, the runtime dies mid-exit ----------
    soak.clock.now = A_EXIT_AT
    soak.rt.tick()
    book_a = soak.book()["canary-a"]
    assert (book_a["status"], book_a["exit_reason"]) == ("exit_working", "time_stop")
    exit_a = soak.gw.trades[-1]
    assert exit_a.order.orderRef == desk_order_ref("canary-a") == "trex:desk:canary-a"
    assert (exit_a.order.account, exit_a.order.action, exit_a.order.lmtPrice) == (
        ACCOUNT,
        "SELL",
        0.90,
    )
    soak.kill_runtime()
    # while down: the exit fills (long sold 2.05, short bought 1.20 = 0.85)
    oid = exit_a.order.orderId
    soak.fill(exit_a, 1, 0.85)
    soak.gw.fill_rows[:] = [
        fill_row(100, 1, 2.05, oid, SUPERVISED_CLIENT_ID),
        fill_row(95, 1, 1.20, oid, SUPERVISED_CLIENT_ID),
    ]
    soak.gw.position_rows.clear()
    soak.restart()
    soak.rt.tick()
    book_a = soak.book()["canary-a"]
    assert (book_a["status"], book_a["close_reason"], book_a["exit_fill"]) == (
        "closed",
        "time_stop",
        "0.85",
    )
    soak.assert_one_order_per_lane()

    # ---- same day: canary-b (2 packages) via the runtime's own send --------
    soak.clock.now = B_ENTRY_AT
    structure_b = _vertical(
        "canary-b", qty=2, entry_date=date(2026, 10, 9), exit_deadline=date(2026, 10, 13)
    )
    effect_b = _effect(structure_b, "canary-b")
    soak.rt.register(effect_b)
    entry_b = soak.send_entry(effect_b)
    assert entry_b.order.orderRef == "trex:sup:canary-b"
    soak.rt.tick()
    assert soak.book()["canary-b"]["status"] == "enter_working"
    soak.fill(entry_b, 2, 0.90)
    soak.hold_legs(2)
    soak.rt.tick()
    assert (soak.book()["canary-b"]["status"], soak.book()["canary-b"]["filled_qty"]) == ("open", 2)

    # ---- Day 11 (10-13): partial exit, kill, the remainder fills while down
    soak.clock.now = B_EXIT_AT
    soak.rt.tick()
    exit_b = soak.gw.trades[-1]
    assert (exit_b.order.orderRef, exit_b.order.totalQuantity) == ("trex:desk:canary-b", 2)
    assert soak.book()["canary-b"]["status"] == "exit_working"
    exit_b.orderStatus.filled, exit_b.orderStatus.avgFillPrice = 1, 0.90  # half done
    soak.hold_legs(1)
    soak.rt.tick()  # the partial fill is drained into the book BEFORE the kill
    book_b = soak.book()["canary-b"]
    assert (book_b["status"], book_b["exit_filled_qty"]) == ("exit_working", 1)
    assert book_b["filled_qty"] - book_b["exit_filled_qty"] == 1  # one package still held
    placed = len(soak.gw.trades)
    soak.kill_runtime()
    # while down the SAME order finishes: today's executions of it are two
    oid = exit_b.order.orderId
    soak.fill(exit_b, 2, 0.90)
    soak.gw.fill_rows[:] = [
        fill_row(100, 2, 2.10, oid, SUPERVISED_CLIENT_ID),
        fill_row(95, 2, 1.20, oid, SUPERVISED_CLIENT_ID),
    ]
    soak.gw.position_rows.clear()
    soak.restart()
    soak.rt.tick()
    book_b = soak.book()["canary-b"]
    assert (book_b["status"], book_b["close_reason"], book_b["exit_filled_qty"]) == (
        "closed",
        "time_stop",
        2,
    )
    assert Decimal(book_b["exit_fill"]) == Decimal("0.90")
    assert len(soak.gw.trades) == placed, "a filling exit is never re-sent"
    soak.assert_one_order_per_lane()

    # ---- same day: canary-c's send is uncertain, then reconciled -----------
    soak.arm(C_REQUEST_AT)
    structure_c = _vertical(
        "canary-c", entry_date=date(2026, 10, 13), exit_deadline=date(2026, 10, 14)
    )
    soak.gw.status_script = ["PendingSubmit"]  # the gateway never acknowledges
    soak.request(structure_c, "canary-c")
    (uncertain,) = soak.desk.process_requests()
    assert uncertain["status"] == "sent"
    assert (uncertain["receipt"]["outcome"], uncertain["receipt"]["reason"]) == (
        "uncertain",
        "ack_timeout",
    )
    unsure = soak.gw.trades[-1]
    assert unsure.order.orderRef == "trex:sup:canary-c"
    # the truth: the order never reached the broker (the gateway's views show
    # nothing for the tag) — the desk cannot know that until reconciliation
    soak.gw.trades.remove(unsure)
    soak.vanished.append("trex:sup:canary-c")
    soak.gw.status_script = ["Submitted"]
    soak.rt.tick()
    assert soak.book()["canary-c"]["status"] == "planned"  # never guessed
    assert "entry_uncertain_held" in soak.event_names()
    # while the effect is held, a fresh request for the SAME package refuses
    placed = len(soak.gw.trades)
    soak.request(structure_c, "canary-c2")
    (refused,) = soak.desk.process_requests()
    assert (refused["status"], refused["reason"]) == ("refused", "package_already_in_flight")
    assert len(soak.gw.trades) == placed
    # after the settle window the broker evidence clears the package
    soak.clock.now = shift_instant(C_REQUEST_AT, 240)
    verdict = reconcile_intent(
        soak.sup, now=soak.clock.now, intent_id="canary-c", broker=soak.broker
    )
    assert verdict["verdict"] == "confirmed_not_submitted"
    soak.rt.tick()
    assert (soak.book()["canary-c"]["status"], soak.book()["canary-c"]["close_reason"]) == (
        "closed",
        "not_submitted",
    )
    # ... and the package is admissible again: a fresh intent records
    sha = json.loads(soak.sup.terminal("canary-c").read_text())["package_intent_sha256"]
    record_intent(
        soak.sup,
        SupervisedIntent(
            intent=OrderIntent(
                intent_id="canary-c2",
                contract_id="BAG:canary-c",
                side="BUY",
                position_effect="OPEN_LONG",
                quantity=1,
                order_type="LIMIT",
                limit_price=Decimal("0.90"),
                execution_style="package",
                package_id="canary-c",
                intent_created_at=soak.clock.now,
                source=STRATEGY,
                source_sequence_id="desk-canary-c2",
            ),
            package_intent_sha256=sha,
            created_at=soak.clock.now,
            send_deadline=shift_instant(soak.clock.now, 300),
        ),
    )
    assert soak.sup.pending("canary-c2").exists()
    soak.assert_one_order_per_lane()

    # ---- canary-d enters, then Day 12 is a HALT and a resume ---------------
    soak.arm(D_REQUEST_AT)
    soak.request(
        _vertical("canary-d", entry_date=date(2026, 10, 13), exit_deadline=date(2026, 10, 14)),
        "canary-d",
    )
    (sent_d,) = soak.desk.process_requests()
    assert (sent_d["status"], sent_d["receipt"]["outcome"]) == ("sent", "acknowledged")
    entry_d = soak.gw.trades[-1]
    soak.rt.tick()
    soak.fill(entry_d, 1, 0.90)
    soak.hold_legs()
    soak.rt.tick()
    assert soak.book()["canary-d"]["status"] == "open"
    soak.clock.now = D_HALT_AT
    soak.paths.halt().touch()
    placed = len(soak.gw.trades)
    soak.rt.tick()
    assert soak.book()["canary-d"]["status"] == "open"  # the HALT holds the exit
    assert len(soak.gw.trades) == placed
    assert "halt_no_new_orders" in soak.event_names()
    soak.paths.halt().unlink()  # resume
    soak.rt.tick()
    exit_d = soak.gw.trades[-1]
    assert exit_d.order.orderRef == "trex:desk:canary-d"
    assert soak.book()["canary-d"]["status"] == "exit_working"
    soak.fill(exit_d, 1, 0.85)
    soak.gw.position_rows.clear()
    soak.rt.tick()
    book_d = soak.book()["canary-d"]
    assert (book_d["status"], book_d["close_reason"], book_d["exit_fill"]) == (
        "closed",
        "time_stop",
        "0.85",
    )
    soak.assert_one_order_per_lane()

    # ---- canary-e (a later expiry, its own legs): an exit that vanishes flat
    soak.arm(E_REQUEST_AT)
    soak.request(
        _vertical(
            "canary-e",
            entry_date=date(2026, 10, 14),
            exit_deadline=date(2026, 10, 15),
            expiry=EXPIRY_LATE,
        ),
        "canary-e",
    )
    (sent_e,) = soak.desk.process_requests()
    assert sent_e["status"] == "sent"
    entry_e = soak.gw.trades[-1]
    soak.rt.tick()
    soak.fill(entry_e, 1, 0.90)
    soak.hold_legs(cons=(102, 97))
    soak.rt.tick()
    assert soak.book()["canary-e"]["status"] == "open"
    soak.clock.now = E_EXIT_AT
    soak.rt.tick()
    exit_e = soak.gw.trades[-1]
    assert exit_e.order.orderRef == "trex:desk:canary-e"
    soak.kill_runtime()
    # while down the exit left the view and the legs went flat, with NO
    # executions today that explain it: an automatic close could abandon a
    # real position, so the desk waits for the operator
    exit_e.orderStatus.status = "Filled"
    soak.gw.fill_rows[:] = []
    soak.gw.position_rows.clear()
    soak.restart()
    placed = len(soak.gw.trades)
    soak.rt.tick()
    soak.rt.tick()
    assert soak.book()["canary-e"]["status"] == "exit_working"
    assert soak.event_names().count("exit_flat_unexplained") == 1  # alerts once
    assert len(soak.gw.trades) == placed, "nothing is sent into a flat account"
    urgent = [n for n in soak.notified if n[2] == "urgent"]
    assert any("exit_flat_unexplained" in n[0] for n in urgent)
    soak.clock.now = E_RESOLVE_AT
    soak.paths.resolve_flat("canary-e").touch()
    soak.rt.tick()
    book_e = soak.book()["canary-e"]
    assert (book_e["status"], book_e["close_reason"], book_e["exit_unpriced_qty"]) == (
        "closed",
        "operator_confirmed_flat",
        1,
    )
    assert not soak.paths.resolve_flat("canary-e").exists()
    assert soak.paths.resolve_flat("canary-e").with_name("RESOLVE-FLAT-canary-e.applied").exists()

    # ------------------------------------------------ the end-state invariants
    book = soak.book()
    assert set(book) == {"canary-a", "canary-b", "canary-c", "canary-d", "canary-e"}
    states = {sid: (st["status"], st["close_reason"]) for sid, st in book.items()}
    assert states == {
        "canary-a": ("closed", "time_stop"),
        "canary-b": ("closed", "time_stop"),
        "canary-c": ("closed", "not_submitted"),
        "canary-d": ("closed", "time_stop"),
        "canary-e": ("closed", "operator_confirmed_flat"),
    }
    assert all(st.get("exit_unpriced_qty", 0) == 0 for sid, st in book.items() if sid != "canary-e")

    # the event log is a coherent ordered history
    records = [json.loads(line) for line in soak.event_lines()]
    stamps = [datetime.fromisoformat(str(r["ts"])) for r in records]
    assert stamps == sorted(stamps), "event time never runs backwards"
    allowed = {
        "registered",
        "entry_request",
        "entry_adopted",
        "entry_filled",
        "entry_uncertain_held",
        "entry_reprice_not_sent",
        "exit_begin",
        "exit_order",
        "exit_fill",
        "closed",
        "halt_no_new_orders",
        "exit_flat_unexplained",
        "exit_authority",
    }
    names = [str(r["event"]) for r in records]
    assert set(names) <= allowed, sorted(set(names) - allowed)
    assert "unknown_exposure" not in names
    assert "legs_mismatch" not in names
    assert names.count("closed") == 5
    closes = {r["structure"]: r["reason"] for r in records if r["event"] == "closed"}
    assert closes == {
        "canary-a": "time_stop",
        "canary-b": "time_stop",
        "canary-c": "not_submitted",
        "canary-d": "time_stop",
        "canary-e": "operator_confirmed_flat",
    }

    # one broker order per lane, per structure, for the whole run
    counts = Counter(soak.all_refs())
    assert max(counts.values()) == 1, counts
    assert set(counts) == {
        "trex:sup:canary-a",
        "trex:desk:canary-a",
        "trex:sup:canary-b",
        "trex:desk:canary-b",
        "trex:sup:canary-c",  # the uncertain send: never re-sent, never adopted
        "trex:sup:canary-d",
        "trex:desk:canary-d",
        "trex:sup:canary-e",
        "trex:desk:canary-e",
    }
    for trade in soak.gw.trades:
        if str(trade.order.orderRef).startswith("trex:desk:"):
            assert (trade.order.account, trade.order.action) == (ACCOUNT, "SELL")

    # the supervised outbox ends in terminal states that agree with the broker
    for intent in ("canary-a", "canary-d", "canary-e"):
        receipt = json.loads(soak.sup.terminal(intent).read_text())
        assert receipt["outcome"] == "acknowledged", receipt
        assert receipt["broker_order_id"] == soak.book()[intent]["entry_order"]
    held = json.loads(soak.sup.terminal("canary-c").read_text())
    assert (held["outcome"], held["reason"]) == ("uncertain", "ack_timeout")
    verdict = json.loads(soak.sup.reconciled("canary-c").read_text())
    assert verdict["verdict"] == "confirmed_not_submitted"
    permits = soak.sup.root / "permits"
    assert list(permits.glob("*.issued.json")) == []  # every permit was consumed once
    assert len(list(permits.glob("*.consumed.json"))) == 4


# ------------------------------------------------------- the Stage A drill


def test_entry_to_time_stop_exit_end_to_end_drill(soak: Soak) -> None:
    """Stage A drill: ONE session day, the whole protected path — inbox
    entry to a time-stop exit — with the exit guards and the health file
    exercised exactly as production runs them (write_owner first, run_loop
    for the request, runtime ticks for the exits)."""
    from tree_options.trex import exit_watch

    drill = "drill-1"
    soak.desk.write_owner()  # main() writes owner.json before the first pass

    # ---- arm and enter through the REAL inbox -----------------------------
    soak.arm(A_ENTRY_AT)  # mandate, fresh quotes, a heartbeat tick
    soak.request(
        _vertical(drill, entry_date=date(2026, 10, 1), exit_deadline=date(2026, 10, 5)), drill
    )
    assert run_loop(soak.desk, interval_s=0, stop=lambda: False, max_ticks=1) == 0
    sent = soak.result(drill)
    assert (sent["status"], sent["receipt"]["outcome"]) == ("sent", "acknowledged")
    entry = soak.gw.trades[-1]
    assert (entry.order.orderRef, entry.order.account) == ("trex:sup:drill-1", ACCOUNT)
    soak.rt.tick()
    assert soak.book()[drill]["status"] == "enter_working"
    soak.fill(entry, 1, 0.90)
    soak.hold_legs()
    soak.rt.tick()
    assert (soak.book()[drill]["status"], soak.book()[drill]["entry_fill"]) == ("open", "0.9")

    # ---- monitor.json is present and classifies ok while the book is guarded
    health = json.loads(soak.paths.health().read_text())
    books = exit_watch.scan_books(soak.paths.root.parent)
    assert [b.plan for b in books] == [soak.paths.root.name]
    obs = exit_watch.ExitObs(
        now=health["at"] + 5,
        books=books,
        gateway_status=None,
        gateway_since=None,
        market=True,
        unit_state=None,
    )
    status, _, detail = exit_watch.classify(obs)
    assert (status, detail) == ("ok", f"{soak.paths.root.name}: heartbeat 5s ago")

    # ---- the deadline day, past the 09:45 time stop: the exit goes out -----
    soak.clock.now = datetime(2026, 10, 5, 10, 0, tzinfo=ET)
    soak.rt.tick()
    book = soak.book()[drill]
    assert (book["status"], book["exit_reason"]) == ("exit_working", "time_stop")
    exit_trade = soak.gw.trades[-1]
    assert exit_trade.order.orderRef == desk_order_ref(drill) == "trex:desk:drill-1"
    assert exit_trade.order.account == ACCOUNT
    assert (exit_trade.order.action, exit_trade.order.totalQuantity) == ("SELL", 1)
    soak.fill(exit_trade, 1, 0.85)
    # the runtime dies; while it is down the exit fills (long sold 2.05,
    # short bought 1.20 = 0.85 a package) and the legs go flat
    oid = exit_trade.order.orderId
    soak.gw.fill_rows[:] = [
        fill_row(100, 1, 2.05, oid, SUPERVISED_CLIENT_ID),
        fill_row(95, 1, 1.20, oid, SUPERVISED_CLIENT_ID),
    ]
    soak.gw.position_rows.clear()
    soak.kill_runtime()
    soak.restart()
    soak.rt.tick()
    book = soak.book()[drill]
    assert (book["status"], book["close_reason"], book["exit_fill"]) == (
        "closed",
        "time_stop",
        "0.85",
    )

    # ---- the FULL chain, in order, through the real event log --------------
    # (entry_reprice_not_sent is the pinned supervised-entry discipline: the
    # engine always asks to reprice an unfilled entry; the permit binds one
    # limit, so the runtime never sends it)
    records = [json.loads(line) for line in soak.event_lines()]
    mine = [r["event"] for r in records if r.get("structure") == drill or r.get("intent") == drill]
    assert mine == [
        "registered",
        "entry_request",
        "entry_adopted",
        "entry_reprice_not_sent",
        "entry_filled",
        "exit_begin",
        "exit_authority",
        "exit_order",
        "exit_fill",
        "closed",
    ]
    chain = [
        "entry_request",
        "entry_filled",
        "exit_begin",
        "exit_authority",
        "exit_order",
        "exit_fill",
        "closed",
    ]
    scan = iter(mine)
    assert [e for e in chain if e in scan] == chain, "the approved chain, in order"
    begin = next(r for r in records if r["event"] == "exit_begin")
    assert begin["reason"] == "time_stop"
    authority = next(r for r in records if r["event"] == "exit_authority")
    assert authority["owner_epoch"] == EPOCH  # owner.json wired into the evidence
    assert (authority["account"], authority["halt"]) == (ACCOUNT, False)
    assert "exit_blocked" not in mine  # nothing refused on the clean path
    assert all("exit_authority" not in n[0] for n in soak.notified)  # jsonl only

    # ---- exactly one entry order and one exit order at the broker ----------
    assert Counter(soak.refs()) == {"trex:sup:drill-1": 1, "trex:desk:drill-1": 1}

    # ---- closed: the watchdog has nothing left to guard ---------------------
    assert exit_watch.scan_books(soak.paths.root.parent) == []
    obs = exit_watch.ExitObs(
        now=soak.clock.now.timestamp(),
        books=[],
        gateway_status=None,
        gateway_since=None,
        market=True,
        unit_state=None,
    )
    assert exit_watch.classify(obs)[0] == "idle"
