"""Phase 0 restore rehearsal: owner backup -> scratch restore -> recovery.

One supervised desk opens a real structure through the request inbox, and
the owner's own beat takes the scheduled after-session backup (a request the
operator dropped that evening sits unclaimed in it). Then the host loses
both live roots. The backup is verified and restored into a scratch
directory, and a FRESH owner recovers from it against the same broker (the
gateway fake, subclassed here: the broker outlives every desk process):

- the reconciled book equals the backup's book, and the broker's legs agree
  with it (the legs-held guard lets the time-stop exit out);
- the old owner epoch, mandate, permits and every request are archived,
  never active: the unclaimed evening request is never processed;
- entries are refused until a NEW mandate exists for the NEW epoch (the old
  mandate copied back is refused for its epoch); exits never wait for one.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.unit.trex_fakes import SupervisedGateway, position_row

from tree_options.time.sessions import shift_instant
from tree_options.trex.account import AccountSnapshot
from tree_options.trex.clock import ET
from tree_options.trex.desk_backup import restore, verify
from tree_options.trex.desk_runtime import DeskPaths, DeskRuntime, desk_order_ref
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised import MAX_MANDATE_TTL_S, SupervisedPaths, grant_mandate
from tree_options.trex.supervised_desk import SupervisedDesk, load_profile, run_loop
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    SupervisedEffect,
    supervised_order_ref,
)

pytest.importorskip("ib_async")

pytestmark = pytest.mark.soak

ACCOUNT = "DU1234567"
STRATEGY = "operational-canary/1"
OLD_EPOCH = "desk83-before"
NEW_EPOCH = "desk83-after"
F = "20261016"
FL = "20261023"
CON = {
    ("OPT", "SPY", F, 100.0, "P"): 100,
    ("OPT", "SPY", F, 95.0, "P"): 95,
    ("OPT", "SPY", FL, 100.0, "P"): 102,
    ("OPT", "SPY", FL, 95.0, "P"): 97,
    ("OPT", "QQQ", FL, 100.0, "P"): 300,
    ("OPT", "QQQ", FL, 95.0, "P"): 295,
}
QUOTED = (100, 95, 102, 97, 300, 295)
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
ENTRY_AT = datetime(2026, 10, 1, 10, 0, tzinfo=ET)
BACKUP_AT = datetime(2026, 10, 1, 16, 45, tzinfo=ET)  # the first beat after 16:30
RECOVER_AT = datetime(2026, 10, 2, 10, 0, tzinfo=ET)  # a hold day for canary-a
EXIT_AT = datetime(2026, 10, 9, 10, 0, tzinfo=ET)  # canary-a's time stop


def _vertical(
    sid: str, entry_date: date, exit_deadline: date, expiry: date, underlying: str = "SPY"
) -> LegStructure:
    return LegStructure(
        id=sid,
        underlying=underlying,
        kind="debit_vertical",
        legs=[
            {"right": "P", "action": "BUY", "strike": "100", "expiry": expiry},
            {"right": "P", "action": "SELL", "strike": "95", "expiry": expiry},
        ],
        quantity=1,
        entry_date=entry_date,
        exit_deadline=exit_deadline,
        limit="1.00",
        exits={"touch": False, "breach": False},
    )


CANARY_A = _vertical("canary-a", date(2026, 10, 1), date(2026, 10, 9), date(2026, 10, 16))
EVENING_Z = _vertical("canary-z", date(2026, 10, 2), date(2026, 10, 9), date(2026, 10, 23))


def _late(sid: str) -> LegStructure:
    """A post-restore entry on another underlying: the canary's flat-book
    rule (ruling 3b) holds canary-a's SPY, and canary-a's legs stay 1:1."""
    return _vertical(sid, date(2026, 10, 2), date(2026, 10, 14), date(2026, 10, 23), "QQQ")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Broker(SupervisedGateway):
    """The broker side: one instance for the whole rehearsal, because the
    broker outlives the host. Cancels confirm at once."""

    def __init__(self) -> None:
        super().__init__(CON, ACCOUNT)
        for con in QUOTED:  # every 100/95 package: 0.70 / 1.10, mid exactly 0.90
            strike_100 = con in (100, 102, 300)
            self.quote(con, 2.00 if strike_100 else 1.10, 2.20 if strike_100 else 1.30)

    def cancelOrder(self, order: Any) -> None:
        super().cancelOrder(order)
        for trade in self.trades:
            if trade.order is order:
                trade.orderStatus.status = "Cancelled"

    def fresh_quotes(self, now: datetime) -> None:
        for con in QUOTED:
            self.tickers[con].time = shift_instant(now, -5)  # type: ignore[attr-defined]


class ClockedIbkr(IbkrTrex):
    """The account snapshot stamped by the test clock."""

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


class Owner:
    """One desk process: runtime (holding the run-dir lock) + entry desk."""

    def __init__(
        self, ib: ClockedIbkr, paths: DeskPaths, sup: SupervisedPaths, clock: Clock, epoch: str
    ) -> None:
        self.paths, self.sup, self.clock, self.epoch = paths, sup, clock, epoch
        self.rt = DeskRuntime(ib, paths, supervised=sup, clock=clock)
        self.rt.acquire()
        self.desk = SupervisedDesk(
            ib,
            self.rt,
            IbkrSupervisedBroker(ib, clock=clock),
            supervised=sup,
            owner_epoch=epoch,
            clock=clock,
        )
        self.desk.write_owner()

    def request(self, structure: LegStructure, name: str) -> None:
        effect = SupervisedEffect(
            intent_id=name,
            account_id=ACCOUNT,
            structure=structure,
            side=structure.open_side,
            quantity=1,
            limit=Decimal("0.90"),
            order_ref=supervised_order_ref(name),
        )
        inbox = self.desk.requests_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema": "trex-desk-entry-request/1",
            "strategy_version": STRATEGY,
            "send_deadline": shift_instant(self.clock.now, 300).isoformat(),
            "requested_by": "operator-terminal",
            "effect": effect.model_dump(mode="json", by_alias=True),
        }
        (inbox / f"{name}.json").write_text(json.dumps(doc))

    def loop_once(self) -> None:
        assert run_loop(self.desk, interval_s=0, stop=lambda: False, max_ticks=1) == 0

    def result(self, name: str) -> dict[str, Any]:
        return json.loads((self.desk.requests_dir() / f"{name}.result.json").read_text())

    def grant(self, ttl: int) -> None:
        digest = load_profile(self.paths.root / "profile.json")[1]
        grant_mandate(
            self.sup,
            now=self.clock.now,
            account_id=ACCOUNT,
            owner_epoch=self.epoch,
            strategy_version=STRATEGY,
            profile_digest=digest,
            max_orders=1,
            ttl_seconds=ttl,
            granted_by="operator-terminal",
        )

    def events(self, name: str) -> list[dict[str, Any]]:
        rows = [json.loads(line) for line in self.paths.events().read_text().splitlines()]
        return [r for r in rows if r["event"] == name]

    def structures(self) -> dict[str, Any]:
        return json.loads(self.paths.book().read_text())["structures"]


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_backup_restore_and_recover_from_scratch(tmp_path: Path) -> None:
    clock = Clock(ENTRY_AT)
    broker = Broker()
    ib = ClockedIbkr(client_id=SUPERVISED_CLIENT_ID)
    ib.clock = clock
    ib._ib = broker
    live = tmp_path / "state"
    sup = SupervisedPaths(live / "supervised")
    sup.prepare()
    paths = DeskPaths(live / "desk-paper")
    paths.root.mkdir(parents=True)
    (paths.root / "profile.json").write_text(json.dumps(PROFILE))

    # ---- the old owner opens canary-a through the real inbox ---------------
    old = Owner(ib, paths, sup, clock, OLD_EPOCH)
    old.grant(MAX_MANDATE_TTL_S)  # still unexpired at recovery: only the epoch refuses it
    broker.fresh_quotes(clock.now)
    old.request(CANARY_A, "canary-a")
    old.loop_once()
    assert (old.result("canary-a")["status"], old.result("canary-a")["receipt"]["outcome"]) == (
        "sent",
        "acknowledged",
    )
    entry = broker.trades[-1]
    assert entry.order.orderRef == "trex:sup:canary-a"
    old.rt.tick()
    entry.orderStatus.status, entry.orderStatus.filled, entry.orderStatus.avgFillPrice = (
        "Filled",
        1,
        0.90,
    )
    broker.position_rows[:] = [
        position_row(100, 1, account=ACCOUNT, symbol="SPY"),
        position_row(95, -1, account=ACCOUNT, symbol="SPY"),
    ]
    old.rt.tick()
    assert old.structures()["canary-a"]["status"] == "open"

    # ---- after the session: an evening request, then the scheduled backup --
    clock.now = BACKUP_AT
    old.request(EVENING_Z, "canary-z")  # out of session: never claimed by this owner
    old.loop_once()  # out of session -> beat -> the owner's custody hook
    (taken,) = old.events("backup_taken")
    assert taken["trigger"] == "scheduled"
    backup = live / "desk-backups" / taken["backup"]
    manifest = verify(backup)
    assert manifest["owner_epoch"] == OLD_EPOCH
    backed_book = json.loads((backup / "run" / "book.json").read_text())
    assert backed_book["structures"]["canary-a"]["status"] == "open"

    # ---- the host loses both live roots -------------------------------------
    old.rt.release()
    shutil.rmtree(paths.root)
    shutil.rmtree(sup.root)

    # ---- restore into scratch: authority is archived, never active ----------
    scratch = tmp_path / "scratch"
    receipt = restore(backup, scratch, now=RECOVER_AT)
    archived = _files(scratch / "archive")
    assert {
        "run/owner.json",
        "run/requests/canary-a.claimed.json",
        "run/requests/canary-a.result.json",
        "run/requests/canary-z.json",
        "supervised/mandate.json",
    } <= archived
    assert any(p.startswith("supervised/permits/") for p in archived), "the spent permit"
    assert set(receipt["quarantined"]) == archived
    restored_run = DeskPaths(scratch / "run")
    restored_sup = SupervisedPaths(scratch / "supervised")
    active = _files(scratch / "run") | {f"sup:{p}" for p in _files(scratch / "supervised")}
    assert "owner.json" not in active, "no epoch survives a restore"
    assert not any(p.startswith(("requests/", "sup:permits/")) for p in active)
    assert "sup:mandate.json" not in active
    assert (
        json.loads((scratch / "archive" / "run" / "owner.json").read_text())["owner_epoch"]
        == OLD_EPOCH
    )

    # ---- a FRESH owner recovers from the restored book ----------------------
    clock.now = RECOVER_AT
    broker.fresh_quotes(clock.now)
    new = Owner(ib, restored_run, restored_sup, clock, NEW_EPOCH)
    assert new.rt._owner_epoch() == NEW_EPOCH
    new.rt.tick()  # the runtime's recovery path: book + broker, no orders
    assert new.structures() == backed_book["structures"], "the reconciled book is the backup"
    placed = len(broker.trades)

    # entries refuse without a mandate (the evening request is never seen)
    new.request(_late("canary-n1"), "canary-n1")
    new.loop_once()
    assert (new.result("canary-n1")["status"], new.result("canary-n1")["reason"]) == (
        "refused",
        "mandate_absent",
    )
    assert not (new.desk.requests_dir() / "canary-z.result.json").exists()

    # the OLD mandate copied back is still not authority: its epoch is gone
    shutil.copy(scratch / "archive" / "supervised" / "mandate.json", restored_sup.mandate())
    new.request(_late("canary-n2"), "canary-n2")
    new.loop_once()
    assert (new.result("canary-n2")["status"], new.result("canary-n2")["reason"]) == (
        "refused",
        "mandate_owner_mismatch",
    )
    restored_sup.mandate().unlink()
    assert len(broker.trades) == placed, "no entry reached the broker"

    # a NEW mandate for the NEW epoch: entries resume
    new.grant(600)
    broker.fresh_quotes(clock.now)
    new.request(_late("canary-n3"), "canary-n3")
    new.loop_once()
    resumed = new.result("canary-n3")
    assert resumed["status"] == "sent", resumed
    assert resumed["receipt"]["outcome"] == "acknowledged", resumed
    assert [str(t.order.orderRef) for t in broker.trades[placed:]] == ["trex:sup:canary-n3"]
    assert all(str(t.order.orderRef) != "trex:sup:canary-z" for t in broker.trades)

    # ---- the exit lane owns canary-a from the restored book -----------------
    clock.now = EXIT_AT
    broker.fresh_quotes(clock.now)
    new.rt.tick()
    exits = [t for t in broker.trades if t.order.orderRef == desk_order_ref("canary-a")]
    assert len(exits) == 1, "the restored book and the broker's legs agree"
    assert (exits[0].order.action, exits[0].order.totalQuantity, exits[0].order.account) == (
        "SELL",
        1,
        ACCOUNT,
    )
    (authority,) = [e for e in new.events("exit_authority") if e["structure"] == "canary-a"]
    assert authority["owner_epoch"] == NEW_EPOCH
    new.rt.release()
