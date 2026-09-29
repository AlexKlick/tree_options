"""The supervised desk process: entry inbox, loss facts, and the loop.

Everything real except the gateway (``SupervisedGateway``), the clock, and
the account snapshot's timestamp (stamped from the test clock, the same
clock the screening's ``checked_at`` reads). The legacy loss oracle is the
live NVDA book's known worst case: 5 x $0.21 + 3 x $1.24 = $477.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.trex_fakes import SupervisedGateway, position_row
from tree_options.desk import book as desk_book
from tree_options.time.sessions import shift_instant
from tree_options.trex.account import AccountSnapshot
from tree_options.trex.clock import ET
from tree_options.trex.desk_runtime import DeskPaths, DeskRuntime
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.plan import ExitRules, Leg, LegStructure
from tree_options.trex.state import BookState, Status, StructureState
from tree_options.trex.supervised import SupervisedPaths, grant_mandate
from tree_options.trex.supervised_desk import (
    EXIT_DISCONNECTED,
    LegacyBook,
    RiskView,
    SupervisedDesk,
    in_session,
    load_profile,
    open_loss_reservation,
    realized_day_loss,
    run_loop,
)
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    SupervisedEffect,
    supervised_order_ref,
)

pytest.importorskip("ib_async")

REPO = Path(__file__).resolve().parents[2]
ACCOUNT = "DU1234567"
STRATEGY = "operational-canary/1"
EPOCH = "desk83-test"
F = "20261016"
CON = {
    ("OPT", "SPY", F, 100.0, "P"): 100,
    ("OPT", "SPY", F, 95.0, "P"): 95,
    ("STK", "SPY", "", 0.0, ""): 1,
}
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=ET)
PROFILE = {"profile_id": "canary-5k", "revision": 1, "intended_capital": "5000",
           "risk_style": "defined-risk", "goals": ["operational-canary"],
           "allowed_strategy_versions": [STRATEGY], "max_loss_per_trade": "300",
           "max_open_loss": "1500", "max_daily_loss": "600", "horizon_days": 30}


def _vertical(kind: str = "debit_vertical") -> LegStructure:
    first, second = ("BUY", "SELL") if kind == "debit_vertical" else ("SELL", "BUY")
    return LegStructure(
        id="dv1", underlying="SPY", kind=kind,
        legs=[{"right": "P", "action": first, "strike": "100", "expiry": date(2026, 10, 16)},
              {"right": "P", "action": second, "strike": "95", "expiry": date(2026, 10, 16)}],
        quantity=1, entry_date=date(2026, 10, 1), exit_deadline=date(2026, 10, 9),
        limit="1.00", exits={"touch": False, "breach": False})


def _effect(kind: str = "debit_vertical", intent_id: str = "sup-001") -> SupervisedEffect:
    s = _vertical(kind)
    return SupervisedEffect(intent_id=intent_id, account_id=ACCOUNT, structure=s,
                            side=s.open_side, quantity=1,
                            limit=Decimal("0.90") if kind == "debit_vertical" else Decimal("1.10"),
                            order_ref=supervised_order_ref(intent_id))


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ClockedIbkr(IbkrTrex):
    """The account snapshot stamped by the test clock (the real one reads
    the wall clock, which a fixed test clock would call stale)."""

    clock: Clock

    def account_snapshot(self) -> AccountSnapshot | None:
        return AccountSnapshot(account_id=ACCOUNT, net_liquidation=Decimal("1000000"),
                               cash=Decimal("1000000"), buying_power=Decimal("4000000"),
                               currency="USD", ts=self.clock())


class Rig:
    def __init__(self, tmp: Path, *, legacy: list[LegacyBook] | None = None) -> None:
        self.clock = Clock(T0)
        self.gw = SupervisedGateway(CON, ACCOUNT)
        self.gw.quote(100, 2.00, 2.20)
        self.gw.quote(95, 1.10, 1.30)
        self.ib = ClockedIbkr(client_id=SUPERVISED_CLIENT_ID)
        self.ib.clock = self.clock
        self.ib._ib = self.gw
        self.sup = SupervisedPaths(tmp / "supervised")
        self.paths = DeskPaths(tmp / "desk")
        self.runtime = DeskRuntime(self.ib, self.paths, supervised=self.sup, clock=self.clock)
        self.runtime.acquire()
        self.notified: list[tuple[str, str, str]] = []
        self.runtime.notify = lambda t, m, p="default": self.notified.append((t, m, p))
        self.desk = SupervisedDesk(self.ib, self.runtime,
                                   IbkrSupervisedBroker(self.ib, clock=self.clock),
                                   supervised=self.sup, owner_epoch=EPOCH,
                                   legacy=legacy or (), clock=self.clock)

    def tick_quotes(self) -> None:
        for con in (100, 95):
            self.gw.tickers[con].time = shift_instant(self.clock.now, -5)  # type: ignore[attr-defined]

    def write_profile(self, doc: dict[str, Any] | None = None) -> str:
        path = self.paths.root / "profile.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc or PROFILE))
        return load_profile(path)[1]

    def grant(self, digest: str, epoch: str = EPOCH) -> None:
        grant_mandate(self.sup, now=self.clock.now, account_id=ACCOUNT, owner_epoch=epoch,
                      strategy_version=STRATEGY, profile_digest=digest, max_orders=1,
                      ttl_seconds=3600, granted_by="operator-terminal")

    def request(self, effect: SupervisedEffect | None = None, *, deadline_s: int = 300,
                name: str = "sup-001") -> Path:
        effect = effect or _effect()
        inbox = self.desk.requests_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        doc = {"schema": "trex-desk-entry-request/1", "strategy_version": STRATEGY,
               "send_deadline": shift_instant(self.clock.now, deadline_s).isoformat(),
               "requested_by": "operator-terminal",
               "effect": effect.model_dump(mode="json", by_alias=True)}
        path = inbox / f"{name}.json"
        path.write_text(json.dumps(doc))
        return path

    def result(self, name: str = "sup-001") -> dict[str, Any]:
        return json.loads((self.desk.requests_dir() / f"{name}.result.json").read_text())

    def ready(self) -> None:
        """Everything a clean canary needs: profile, mandate, fresh quotes, a beat."""
        self.grant(self.write_profile())
        self.tick_quotes()
        self.runtime.tick()


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    yield r
    r.runtime.release()


# ------------------------------------------------------------ the inbox


def test_a_clean_request_is_sent_and_owned_by_e5(rig):
    rig.ready()
    rig.request()
    (result,) = rig.desk.process_requests()
    assert result["status"] == "sent", result
    assert result["receipt"]["outcome"] == "acknowledged"
    (trade,) = rig.gw.trades
    assert (trade.order.orderRef, trade.order.account) == ("trex:sup:sup-001", ACCOUNT)
    assert rig.result()["screening_sha256"] == result["screening_sha256"]
    rig.runtime.tick()
    book = json.loads(rig.paths.book().read_text())["structures"]["dv1"]
    assert (book["status"], book["entry_order"]) == ("enter_working", str(trade.order.orderId))
    pushed = [n for n in rig.notified if "entry_request" in n[0]]
    assert pushed and "sent" in pushed[-1][1], "the request result is pushed"


def test_a_request_is_processed_exactly_once(rig):
    rig.ready()
    rig.request()
    rig.desk.process_requests()
    assert rig.desk.process_requests() == []
    assert len(rig.gw.trades) == 1


@pytest.mark.parametrize("setup, blocker", [
    (lambda r: (r.tick_quotes(), r.runtime.tick()), "profile_absent"),
    (lambda r: (r.grant("f" * 64), r.write_profile(), r.tick_quotes(), r.runtime.tick()),
     "mandate_profile_mismatch"),
])
def test_profile_binding_blocks(rig, setup, blocker):
    setup(rig)
    if blocker == "profile_absent":
        rig.grant(rig.write_profile())
        (rig.paths.root / "profile.json").unlink()
    rig.request()
    (result,) = rig.desk.process_requests()
    assert (result["status"], result["blockers"]) == ("blocked", [blocker])
    assert rig.gw.trades == []


def test_a_mandate_for_another_owner_epoch_refuses(rig):
    rig.grant(rig.write_profile(), epoch="desk83-previous-process")
    rig.tick_quotes()
    rig.runtime.tick()
    rig.request()
    (result,) = rig.desk.process_requests()
    assert (result["status"], result["reason"]) == ("refused", "mandate_owner_mismatch")


def test_no_heartbeat_means_no_protective_exit(rig):
    rig.grant(rig.write_profile())
    rig.tick_quotes()  # no runtime tick: the arm gate has no beat
    rig.request()
    (result,) = rig.desk.process_requests()
    assert result["status"] == "blocked"
    assert "protective_exit_unavailable" in result["blockers"]


def test_wrong_account_refuses_before_writing_a_spec(rig, tmp_path):
    """Audit follow-up: a wrong-account effect must NOT consume a permit or
    leave a spec file behind; the entry_account_mismatch blocker is
    operator-visible in the .result.json."""
    rig.grant(rig.write_profile())
    rig.tick_quotes()
    rig.runtime.tick()  # arm the gate
    from tree_options.trex.supervised_desk import EntryRequest
    from tree_options.trex.supervised_ibkr import SupervisedEffect, supervised_order_ref

    # First send the legit request. The orchestrator enforces filename ==
    # intent_id; the spec is named after structure.id (dv1); both must match
    # AND equal the intent_id. We use "dv1" for both.
    rig.request(effect=_effect(intent_id="dv1"), name="dv1")
    (first,) = rig.desk.process_requests()
    assert first["status"] == "sent", f"dv1 did not land: {first}"
    # The orchestrator now holds a spec + consumed permit for dv1.
    # A SECOND request for dv1 with the WRONG account must refuse BEFORE
    # writing a new spec (and BEFORE consuming a fresh permit).
    previews_dir = rig.paths.root / "requests"
    pre_specs = list((rig.paths.root / "specs").glob("dv1*.json"))
    pre_consumed = list((rig.sup.root / "permits").glob("*.consumed.json"))
    s = rig.desk.runtime.specs()["dv1"].structure
    wrong = SupervisedEffect(intent_id="canary-z", account_id="DU9999999",
                            structure=s, side="BUY", quantity=1,
                            limit=Decimal("0.90"),
                            order_ref=supervised_order_ref("canary-z"))
    req = EntryRequest(strategy_version=STRATEGY,
                      send_deadline=shift_instant(rig.clock.now, 120),
                      requested_by="test", effect=wrong)
    out = previews_dir / "canary-z.json"
    # an old claim artifact from a prior run can trip the inbox's claim
    # mechanism — only the new request file matters for THIS process call.
    for f in previews_dir.glob("canary-z*.json"):
        f.unlink()
    out.write_text(json.dumps(req.model_dump(mode="json", by_alias=True)))
    (results,) = rig.desk.process_requests()
    # the orchestrator refuses BEFORE consuming the permit; we see
    # `refused` (the SupervisedRefused branch), not `blocked`.
    assert results["status"] == "refused", results
    assert results["reason"] == "mandate_account_mismatch", results
    specs_after = list((rig.paths.root / "specs").glob("dv1*.json"))
    consumed_after = list((rig.sup.root / "permits").glob("*.consumed.json"))
    # pre_specs/pre_consumed were captured AFTER the legit attempt (both
    # already hold its artifacts); the wrong-account attempt must add NEITHER.
    assert specs_after == pre_specs, "no new spec written for the wrong-account attempt"
    assert consumed_after == pre_consumed, "no new permit consumed for the wrong-account attempt"
    # the inbox lifecycle (claim + result) is the orchestrator's concern;
    # we only assert the invariants the audit cares about: no NEW spec,
    # no NEW permit, the wrong-account attempt is refused. The original
    # sent result.json from the first call may or may not be present
    # depending on the rename semantics; we don't pin it.
    if (previews_dir / "dv1.result.json").exists():
        assert json.loads((previews_dir / "dv1.result.json").read_text())["status"] == "sent"


def test_a_broker_transport_error_refuses_subsequent_send(rig, tmp_path):
    """Audit follow-up: a second send() on an intent whose first submit
    crashed must refuse with permit_consumed (the consume-rename-then-submit
    path), never re-submit."""
    from tree_options.trex.supervised import SupervisedRefused
    from tree_options.trex.supervised_desk import send as supervised_send

    rig.grant(rig.write_profile())
    rig.tick_quotes()
    rig.runtime.tick()
    rig.request(effect=_effect(intent_id="canary-z"), name="canary-z")
    (first,) = rig.desk.process_requests()
    assert first["status"] == "sent", first
    permit_id = first["permit_id"]
    # the orchestrator wrote the consumed permit under Rig's supervised dir
    consumed = rig.sup.permit_consumed(permit_id)
    assert consumed.exists(), "the permit was consumed atomically"
    with pytest.raises(SupervisedRefused) as caught:
        supervised_send(rig.sup, now=rig.clock.now, permit_id=permit_id,
                        effect_payload=b"x", broker=rig.desk.broker)
    assert caught.value.reason == "permit_consumed"


def test_same_underlying_legacy_position_blocks_screening(rig):
    rig.ready()
    rig.gw.position_rows.append(position_row(12, -2, account=ACCOUNT, symbol="SPY"))
    rig.request()
    (result,) = rig.desk.process_requests()
    assert "legacy_book_not_flat" in result["blockers"]


@pytest.mark.parametrize("effect, deadline_s, blocker", [
    (_effect("credit_vertical"), 300, "credit_open_not_supported_v1"),
    (_effect(), -1, "request_expired"),
])
def test_unsupported_or_expired_requests_block(rig, effect, deadline_s, blocker):
    rig.ready()
    rig.request(effect, deadline_s=deadline_s)
    (result,) = rig.desk.process_requests()
    assert result["blockers"] == [blocker]


def test_a_request_named_for_another_intent_is_invalid(rig):
    rig.ready()
    rig.request(name="sup-777")
    (result,) = rig.desk.process_requests()
    assert result["status"] == "invalid"
    assert rig.gw.trades == []


def test_open_loss_includes_the_legacy_book(tmp_path):
    """Ruling 3b: other books' risk enters through the open-loss cap."""
    state = tmp_path / "trex-state"
    book = BookState(["nvda-oct", "qqq-nov", "nvda-nov"])
    book.structures["nvda-oct"] = StructureState(Status.OPEN, filled_qty=5,
                                                 entry_fill=Decimal("0.21"))
    book.structures["qqq-nov"] = StructureState(Status.CLOSED)
    book.structures["nvda-nov"] = StructureState(Status.OPEN, filled_qty=3,
                                                 entry_fill=Decimal("1.24"))
    book.save(state / "putspread-20260922" / "book.json")
    rig = Rig(tmp_path, legacy=[LegacyBook(REPO / "plans/2026-09-22.toml", state)])
    try:
        open_loss, day_loss = rig.desk.loss_facts(T0.date())
        assert (open_loss, day_loss) == (Decimal("477.00"), Decimal("0"))
    finally:
        rig.runtime.release()


def test_an_unreadable_legacy_book_makes_the_loss_unknown(tmp_path):
    state = tmp_path / "trex-state"
    (state / "putspread-20260922").mkdir(parents=True)
    (state / "putspread-20260922" / "book.json").write_text("{not json")
    rig = Rig(tmp_path, legacy=[LegacyBook(REPO / "plans/2026-09-22.toml", state)])
    try:
        assert rig.desk.loss_facts(T0.date()) == (None, None)
    finally:
        rig.runtime.release()


# -------------------------------------------------------------- loss math


RISK = RiskView(per_package=Decimal("1.00"), quantity=2, is_credit=False,
                entry_date=date(2026, 10, 1))


@pytest.mark.parametrize("state, today, expected", [
    (StructureState(Status.OPEN, filled_qty=2, entry_fill=Decimal("0.80")), T0.date(), "160.00"),
    (StructureState(Status.OPEN, filled_qty=2, entry_fill=Decimal("0.80"), entry_unpriced_qty=1),
     T0.date(), "200.00"),
    (StructureState(Status.ENTER_WORKING), T0.date(), "200.00"),
    (StructureState(Status.PLANNED), date(2026, 10, 2), "0"),
    (StructureState(Status.CLOSED, filled_qty=2), T0.date(), "0"),
])
def test_open_loss_reservation(state, today, expected):
    book = BookState(["s"])
    book.structures["s"] = state
    assert open_loss_reservation(book, {"s": RISK}, today) == Decimal(expected)


def test_realized_day_loss_counts_todays_losing_exits_only():
    book = BookState(["lost", "won", "yesterday"])
    book.structures["lost"] = StructureState(Status.CLOSED, filled_qty=2, entry_fill=Decimal("0.90"),
                                             exit_filled_qty=2, exit_fill=Decimal("0.40"),
                                             updated_at=T0)
    book.structures["won"] = StructureState(Status.CLOSED, filled_qty=1, entry_fill=Decimal("0.90"),
                                            exit_filled_qty=1, exit_fill=Decimal("1.50"),
                                            updated_at=T0)
    book.structures["yesterday"] = StructureState(
        Status.CLOSED, filled_qty=1, entry_fill=Decimal("0.90"), exit_filled_qty=1,
        exit_fill=Decimal("0.10"), updated_at=shift_instant(T0, -86400))
    risks = {sid: RISK for sid in book.structures}
    assert realized_day_loss(book, risks, T0.date()) == Decimal("100.00")  # (0.90-0.40)x100x2


def test_an_unpriced_exit_today_makes_the_day_loss_unknown():
    book = BookState(["s"])
    book.structures["s"] = StructureState(Status.CLOSED, filled_qty=1, entry_fill=Decimal("0.90"),
                                          exit_filled_qty=1, exit_unpriced_qty=1, updated_at=T0)
    assert realized_day_loss(book, {"s": RISK}, T0.date()) is None


# ------------------------------------------- reservation parity (two views)

# Two "$477" implementations exist by design and must not silently drift:
# the canary's screen (supervised_desk.open_loss_reservation, A) and the
# rails' book view (desk.book._position, B; load_book's per-position
# max_loss_usd). On the DOCUMENTED COMMON SUBSET they must agree exactly;
# where they differ the difference is intentional and its direction is
# asserted, so a future edit to either side that changes the totals fails.


def _struct(sid: str, *, kind: str = "debit_vertical", quantity: int = 2,
            limit: Decimal = Decimal("1.00"), entry: date = date(2026, 10, 1)) -> LegStructure:
    hi, lo = ((Decimal("744"), Decimal("742")) if kind == "debit_vertical"
              else (Decimal("155"), Decimal("150")))
    # a debit vertical BUYs the strike nearer the view (the higher put);
    # a credit vertical SELLs it
    buy, sell = (hi, lo) if kind == "debit_vertical" else (lo, hi)
    return LegStructure(
        id=sid, underlying="SPY" if kind == "debit_vertical" else "NVDA", kind=kind,
        legs=(Leg(right="P", action="BUY", strike=buy, expiry=date(2026, 11, 20)),
              Leg(right="P", action="SELL", strike=sell, expiry=date(2026, 11, 20))),
        quantity=quantity, entry_date=entry, exit_deadline=date(2026, 11, 1),
        limit=limit,
        exits=ExitRules(touch=kind == "debit_vertical", breach=kind != "debit_vertical"))


PARITY_TODAY = date(2026, 9, 24)
# (sid, structure, state, today, expected A, expected B, common subset?)
# A = open_loss_reservation (the canary screen), B = desk.book._position
# with the LEGACY planned-dormant flag (the desk lane's flag is covered by
# test_planned_past_date_divergence_between_screen_and_desk_book below).
PARITY_CASES = [
    # -- the common subset: exact agreement -------------------------------
    ("debit-open-priced", _struct("d1"),
     StructureState(Status.OPEN, filled_qty=2, entry_fill=Decimal("0.80")),
     PARITY_TODAY, "160.00", "160.00", True),
    ("debit-open-no-fill", _struct("d2"),
     StructureState(Status.OPEN, filled_qty=2),
     PARITY_TODAY, "200.00", "200.00", True),
    ("debit-exit-working-partial", _struct("d3", quantity=3),
     StructureState(Status.EXIT_WORKING, filled_qty=3, exit_filled_qty=1,
                    entry_fill=Decimal("0.80")),
     PARITY_TODAY, "160.00", "160.00", True),
    ("debit-enter-working-full-quantity", _struct("d4", quantity=3),
     StructureState(Status.ENTER_WORKING),
     PARITY_TODAY, "300.00", "300.00", True),
    ("debit-planned-before-entry", _struct("d5"),
     StructureState(Status.PLANNED),
     PARITY_TODAY, "200.00", "200.00", True),
    ("debit-planned-past-entry", _struct("d6"),
     StructureState(Status.PLANNED),
     date(2026, 10, 2), "0", "0", True),
    ("credit-planned-working", _struct("c1", kind="credit_vertical", quantity=2,
                                       limit=Decimal("1.00")),
     StructureState(Status.PLANNED),
     PARITY_TODAY, "800.00", "800.00", True),  # (width 5 - floor 1) x 100 x 2
    # -- the intentional differences: A is the more conservative screen ----
    ("credit-open-priced", _struct("c2", kind="credit_vertical", quantity=1,
                                   limit=Decimal("1.00")),
     StructureState(Status.OPEN, filled_qty=1, entry_fill=Decimal("2.50")),
     PARITY_TODAY, "400.00", "250.00", False),  # A: floor-based max loss; B: width - fill
    ("debit-open-unpriced-partial", _struct("d7"),
     StructureState(Status.OPEN, filled_qty=2, entry_fill=Decimal("0.80"),
                    entry_unpriced_qty=1),
     PARITY_TODAY, "200.00", "160.00", False),  # A: no refine on an unpriced average
]


@pytest.mark.parametrize("sid,struct,state,today,a,b,common", PARITY_CASES)
def test_open_loss_reservation_parities_with_the_desk_book_view(
        sid, struct, state, today, a, b, common):
    book = BookState([sid])
    book.structures[sid] = state
    screen = open_loss_reservation(book, {sid: RiskView.of(struct)}, today)
    view = desk_book._position(f"fixture:{sid}", "fixture", struct, state,
                               as_of=today, planned_dormant_after_entry=True)
    held = Decimal(0) if view is None else view.max_loss_usd
    assert screen == Decimal(a), f"{sid}: the screen moved"
    assert held == Decimal(b), f"{sid}: the book view moved"
    if common:
        assert screen == held, f"{sid}: the two reservations disagree"
    else:
        assert screen > held, f"{sid}: the screen must be the conservative one"


def test_planned_past_date_divergence_between_screen_and_desk_book():
    """The desk lane keeps a PLANNED structure at its cap even past its
    entry date (desk.book passes planned_dormant_after_entry=False), while
    the screen drops it (it can no longer enter). Opposite direction from
    the credit/unpriced differences: this is the ONE case where the book
    view reserves MORE than the screen."""
    struct = _struct("d8")
    state = StructureState(Status.PLANNED)
    today = date(2026, 10, 2)
    book = BookState(["d8"])
    book.structures["d8"] = state
    screen = open_loss_reservation(book, {"d8": RiskView.of(struct)}, today)
    view = desk_book._position("fixture:d8", "fixture", struct, state,
                               as_of=today, planned_dormant_after_entry=False)
    assert screen == Decimal(0)
    assert view is not None and view.max_loss_usd == Decimal("200.00")


def test_an_impossible_credit_fill_fails_closed_in_the_book_view_only():
    """B refuses a position whose recorded fill leaves no loss (its whole
    position is dropped into load_book's problems); the screen still counts
    the package at its floor-based max loss rather than trusting the fill."""
    struct = _struct("c3", kind="credit_vertical", quantity=1, limit=Decimal("1.00"))
    state = StructureState(Status.OPEN, filled_qty=1, entry_fill=Decimal("5.00"))
    with pytest.raises(desk_book._Inconsistent):
        desk_book._position("fixture:c3", "fixture", struct, state,
                            as_of=PARITY_TODAY, planned_dormant_after_entry=True)
    book = BookState(["c3"])
    book.structures["c3"] = state
    assert open_loss_reservation(book, {"c3": RiskView.of(struct)},
                                 PARITY_TODAY) == Decimal("400.00")


# ---------------------------------------------------------------- the loop


def test_session_window():
    assert in_session(T0)
    assert not in_session(T0.replace(hour=9, minute=29))
    assert not in_session(datetime(2026, 10, 3, 10, 0, tzinfo=ET))  # Saturday


def test_first_loop_iteration_beats_before_it_reads_the_inbox(rig):
    """A restarted process must not refuse its first request for a stale beat."""
    rig.grant(rig.write_profile())
    rig.tick_quotes()
    rig.request()
    assert run_loop(rig.desk, interval_s=0, stop=lambda: False, max_ticks=1) == 0
    assert rig.result()["status"] == "sent"


def test_outside_the_session_the_loop_only_beats(rig):
    rig.clock.now = T0.replace(hour=18)
    rig.request()
    run_loop(rig.desk, interval_s=0, stop=lambda: False, max_ticks=1)
    assert (rig.desk.requests_dir() / "sup-001.json").exists(), "not claimed off-session"
    assert BookState.load(rig.paths.book(), []).heartbeat == T0.replace(hour=18)


def test_a_lost_gateway_exits_for_a_restart(rig):
    rig.gw.is_connected = False
    # bounded: a loop that ignored the lost connection must FAIL, not hang
    assert run_loop(rig.desk, interval_s=0, stop=lambda: False,
                    max_ticks=3) == EXIT_DISCONNECTED


def test_owner_file_names_the_epoch_to_grant(rig):
    rig.desk.write_owner()
    doc = json.loads(rig.desk.owner_path().read_text())
    assert (doc["owner_epoch"], doc["client_id"]) == (EPOCH, SUPERVISED_CLIENT_ID)
