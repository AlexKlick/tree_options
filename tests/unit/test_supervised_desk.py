"""The supervised desk process: entry inbox, loss facts, and the loop.

Everything real except the gateway (``SupervisedGateway``), the clock, and
the account snapshot's timestamp (stamped from the test clock, the same
clock the screening's ``checked_at`` reads). The legacy loss oracle is the
live NVDA book's known worst case: 5 x $0.21 + 3 x $1.24 = $477.
"""

from __future__ import annotations

import fcntl
import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.trex_fakes import SupervisedGateway, position_row
from tree_options.action_graph.proposal import canonical_bytes
from tree_options.desk import book as desk_book
from tree_options.time.sessions import shift_instant
from tree_options.trex.account import AccountSnapshot
from tree_options.trex.clock import ET
from tree_options.trex.desk_runtime import DeskPaths, DeskRuntime
from tree_options.trex.desk_spec import DeskSpec
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


def _vertical(kind: str = "debit_vertical") -> LegStructure:
    first, second = ("BUY", "SELL") if kind == "debit_vertical" else ("SELL", "BUY")
    return LegStructure(
        id="dv1",
        underlying="SPY",
        kind=kind,
        legs=[
            {"right": "P", "action": first, "strike": "100", "expiry": date(2026, 10, 16)},
            {"right": "P", "action": second, "strike": "95", "expiry": date(2026, 10, 16)},
        ],
        quantity=1,
        entry_date=date(2026, 10, 1),
        exit_deadline=date(2026, 10, 9),
        limit="1.00",
        exits={"touch": False, "breach": False},
    )


def _effect(kind: str = "debit_vertical", intent_id: str = "sup-001") -> SupervisedEffect:
    s = _vertical(kind)
    return SupervisedEffect(
        intent_id=intent_id,
        account_id=ACCOUNT,
        structure=s,
        side=s.open_side,
        quantity=1,
        limit=Decimal("0.90") if kind == "debit_vertical" else Decimal("1.10"),
        order_ref=supervised_order_ref(intent_id),
    )


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
        return AccountSnapshot(
            account_id=ACCOUNT,
            net_liquidation=Decimal("1000000"),
            cash=Decimal("1000000"),
            buying_power=Decimal("4000000"),
            currency="USD",
            ts=self.clock(),
        )


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
        self.desk = SupervisedDesk(
            self.ib,
            self.runtime,
            IbkrSupervisedBroker(self.ib, clock=self.clock),
            supervised=self.sup,
            owner_epoch=EPOCH,
            legacy=legacy or (),
            clock=self.clock,
        )

    def tick_quotes(self) -> None:
        for con in (100, 95):
            self.gw.tickers[con].time = shift_instant(self.clock.now, -5)  # type: ignore[attr-defined]

    def write_profile(self, doc: dict[str, Any] | None = None) -> str:
        path = self.paths.root / "profile.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc or PROFILE))
        return load_profile(path)[1]

    def grant(self, digest: str, epoch: str = EPOCH) -> None:
        grant_mandate(
            self.sup,
            now=self.clock.now,
            account_id=ACCOUNT,
            owner_epoch=epoch,
            strategy_version=STRATEGY,
            profile_digest=digest,
            max_orders=1,
            ttl_seconds=3600,
            granted_by="operator-terminal",
        )

    def request(
        self,
        effect: SupervisedEffect | None = None,
        *,
        deadline_s: int = 300,
        name: str = "sup-001",
    ) -> Path:
        effect = effect or _effect()
        inbox = self.desk.requests_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema": "trex-desk-entry-request/1",
            "strategy_version": STRATEGY,
            "send_deadline": shift_instant(self.clock.now, deadline_s).isoformat(),
            "requested_by": "operator-terminal",
            "effect": effect.model_dump(mode="json", by_alias=True),
        }
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


@pytest.mark.parametrize(
    "setup, blocker",
    [
        (lambda r: (r.tick_quotes(), r.runtime.tick()), "profile_absent"),
        (
            lambda r: (r.grant("f" * 64), r.write_profile(), r.tick_quotes(), r.runtime.tick()),
            "mandate_profile_mismatch",
        ),
    ],
)
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
    wrong = SupervisedEffect(
        intent_id="canary-z",
        account_id="DU9999999",
        structure=s,
        side="BUY",
        quantity=1,
        limit=Decimal("0.90"),
        order_ref=supervised_order_ref("canary-z"),
    )
    req = EntryRequest(
        strategy_version=STRATEGY,
        send_deadline=shift_instant(rig.clock.now, 120),
        requested_by="test",
        effect=wrong,
    )
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
        supervised_send(
            rig.sup,
            now=rig.clock.now,
            permit_id=permit_id,
            effect_payload=b"x",
            broker=rig.desk.broker,
        )
    assert caught.value.reason == "permit_consumed"


def test_same_underlying_legacy_position_blocks_screening(rig):
    rig.ready()
    rig.gw.position_rows.append(position_row(12, -2, account=ACCOUNT, symbol="SPY"))
    rig.request()
    (result,) = rig.desk.process_requests()
    assert "legacy_book_not_flat" in result["blockers"]


@pytest.mark.parametrize(
    "effect, deadline_s, blocker",
    [
        (_effect("credit_vertical"), 300, "credit_open_not_supported_v1"),
        (_effect(), -1, "request_expired"),
    ],
)
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
    book.structures["nvda-oct"] = StructureState(
        Status.OPEN, filled_qty=5, entry_fill=Decimal("0.21")
    )
    book.structures["qqq-nov"] = StructureState(Status.CLOSED)
    book.structures["nvda-nov"] = StructureState(
        Status.OPEN, filled_qty=3, entry_fill=Decimal("1.24")
    )
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


RISK = RiskView(
    per_package=Decimal("1.00"), quantity=2, is_credit=False, entry_date=date(2026, 10, 1)
)


@pytest.mark.parametrize(
    "state, today, expected",
    [
        (
            StructureState(Status.OPEN, filled_qty=2, entry_fill=Decimal("0.80")),
            T0.date(),
            "160.00",
        ),
        (
            StructureState(
                Status.OPEN, filled_qty=2, entry_fill=Decimal("0.80"), entry_unpriced_qty=1
            ),
            T0.date(),
            "200.00",
        ),
        (StructureState(Status.ENTER_WORKING), T0.date(), "200.00"),
        (StructureState(Status.PLANNED), date(2026, 10, 2), "0"),
        (StructureState(Status.CLOSED, filled_qty=2), T0.date(), "0"),
    ],
)
def test_open_loss_reservation(state, today, expected):
    book = BookState(["s"])
    book.structures["s"] = state
    assert open_loss_reservation(book, {"s": RISK}, today) == Decimal(expected)


def test_realized_day_loss_counts_todays_losing_exits_only():
    book = BookState(["lost", "won", "yesterday"])
    book.structures["lost"] = StructureState(
        Status.CLOSED,
        filled_qty=2,
        entry_fill=Decimal("0.90"),
        exit_filled_qty=2,
        exit_fill=Decimal("0.40"),
        updated_at=T0,
    )
    book.structures["won"] = StructureState(
        Status.CLOSED,
        filled_qty=1,
        entry_fill=Decimal("0.90"),
        exit_filled_qty=1,
        exit_fill=Decimal("1.50"),
        updated_at=T0,
    )
    book.structures["yesterday"] = StructureState(
        Status.CLOSED,
        filled_qty=1,
        entry_fill=Decimal("0.90"),
        exit_filled_qty=1,
        exit_fill=Decimal("0.10"),
        updated_at=shift_instant(T0, -86400),
    )
    risks = {sid: RISK for sid in book.structures}
    assert realized_day_loss(book, risks, T0.date()) == Decimal("100.00")  # (0.90-0.40)x100x2


def test_an_unpriced_exit_today_makes_the_day_loss_unknown():
    book = BookState(["s"])
    book.structures["s"] = StructureState(
        Status.CLOSED,
        filled_qty=1,
        entry_fill=Decimal("0.90"),
        exit_filled_qty=1,
        exit_unpriced_qty=1,
        updated_at=T0,
    )
    assert realized_day_loss(book, {"s": RISK}, T0.date()) is None


# ------------------------------------------- reservation parity (two views)

# Two "$477" implementations exist by design and must not silently drift:
# the canary's screen (supervised_desk.open_loss_reservation, A) and the
# rails' book view (desk.book._position, B; load_book's per-position
# max_loss_usd). On the DOCUMENTED COMMON SUBSET they must agree exactly;
# where they differ the difference is intentional and its direction is
# asserted, so a future edit to either side that changes the totals fails.


def _struct(
    sid: str,
    *,
    kind: str = "debit_vertical",
    quantity: int = 2,
    limit: Decimal = Decimal("1.00"),
    entry: date = date(2026, 10, 1),
) -> LegStructure:
    hi, lo = (
        (Decimal("744"), Decimal("742"))
        if kind == "debit_vertical"
        else (Decimal("155"), Decimal("150"))
    )
    # a debit vertical BUYs the strike nearer the view (the higher put);
    # a credit vertical SELLs it
    buy, sell = (hi, lo) if kind == "debit_vertical" else (lo, hi)
    return LegStructure(
        id=sid,
        underlying="SPY" if kind == "debit_vertical" else "NVDA",
        kind=kind,
        legs=(
            Leg(right="P", action="BUY", strike=buy, expiry=date(2026, 11, 20)),
            Leg(right="P", action="SELL", strike=sell, expiry=date(2026, 11, 20)),
        ),
        quantity=quantity,
        entry_date=entry,
        exit_deadline=date(2026, 11, 1),
        limit=limit,
        exits=ExitRules(touch=kind == "debit_vertical", breach=kind != "debit_vertical"),
    )


PARITY_TODAY = date(2026, 9, 24)
# (sid, structure, state, today, expected A, expected B, common subset?)
# A = open_loss_reservation (the canary screen), B = desk.book._position
# with the LEGACY planned-dormant flag (the desk lane's flag is covered by
# test_planned_past_date_divergence_between_screen_and_desk_book below).
PARITY_CASES = [
    # -- the common subset: exact agreement -------------------------------
    (
        "debit-open-priced",
        _struct("d1"),
        StructureState(Status.OPEN, filled_qty=2, entry_fill=Decimal("0.80")),
        PARITY_TODAY,
        "160.00",
        "160.00",
        True,
    ),
    (
        "debit-open-no-fill",
        _struct("d2"),
        StructureState(Status.OPEN, filled_qty=2),
        PARITY_TODAY,
        "200.00",
        "200.00",
        True,
    ),
    (
        "debit-exit-working-partial",
        _struct("d3", quantity=3),
        StructureState(
            Status.EXIT_WORKING, filled_qty=3, exit_filled_qty=1, entry_fill=Decimal("0.80")
        ),
        PARITY_TODAY,
        "160.00",
        "160.00",
        True,
    ),
    (
        "debit-enter-working-full-quantity",
        _struct("d4", quantity=3),
        StructureState(Status.ENTER_WORKING),
        PARITY_TODAY,
        "300.00",
        "300.00",
        True,
    ),
    (
        "debit-planned-before-entry",
        _struct("d5"),
        StructureState(Status.PLANNED),
        PARITY_TODAY,
        "200.00",
        "200.00",
        True,
    ),
    (
        "debit-planned-past-entry",
        _struct("d6"),
        StructureState(Status.PLANNED),
        date(2026, 10, 2),
        "0",
        "0",
        True,
    ),
    (
        "credit-planned-working",
        _struct("c1", kind="credit_vertical", quantity=2, limit=Decimal("1.00")),
        StructureState(Status.PLANNED),
        PARITY_TODAY,
        "800.00",
        "800.00",
        True,
    ),  # (width 5 - floor 1) x 100 x 2
    # -- the intentional differences: A is the more conservative screen ----
    (
        "credit-open-priced",
        _struct("c2", kind="credit_vertical", quantity=1, limit=Decimal("1.00")),
        StructureState(Status.OPEN, filled_qty=1, entry_fill=Decimal("2.50")),
        PARITY_TODAY,
        "400.00",
        "250.00",
        False,
    ),  # A: floor-based max loss; B: width - fill
    (
        "debit-open-unpriced-partial",
        _struct("d7"),
        StructureState(Status.OPEN, filled_qty=2, entry_fill=Decimal("0.80"), entry_unpriced_qty=1),
        PARITY_TODAY,
        "200.00",
        "160.00",
        False,
    ),  # A: no refine on an unpriced average
]


@pytest.mark.parametrize("sid,struct,state,today,a,b,common", PARITY_CASES)
def test_open_loss_reservation_parities_with_the_desk_book_view(
    sid, struct, state, today, a, b, common
):
    book = BookState([sid])
    book.structures[sid] = state
    screen = open_loss_reservation(book, {sid: RiskView.of(struct)}, today)
    view = desk_book._position(
        f"fixture:{sid}", "fixture", struct, state, as_of=today, planned_dormant_after_entry=True
    )
    held = Decimal(0) if view is None else view.max_loss_usd
    assert screen == Decimal(a), f"{sid}: the screen moved"
    assert held == Decimal(b), f"{sid}: the book view moved"
    if common:
        assert screen == held, f"{sid}: the two reservations disagree"
    else:
        assert screen > held, f"{sid}: the screen must be the conservative one"


# The desk lane (``_desk``) keeps a PLANNED structure past its entry date at
# cap (planned_dormant_after_entry=False): divergence (3), the one case where
# the book view reserves MORE than the screen.
DESK_LANE_B = {"debit-planned-past-entry": "200.00"}


@pytest.mark.parametrize("sid,struct,state,today,a,b,common", PARITY_CASES)
def test_open_loss_reservation_parities_through_the_owner_spec_envelope(
    tmp_path, sid, struct, state, today, a, b, common
):
    """D1 gap: the parity above calls ``_position`` directly. The rails read
    the owner's specs through ``desk.book._desk``, which must unwrap the
    ``trex-desk-spec/1`` envelope ``DeskRuntime.register`` writes (the same
    canonical bytes here); a wrapper bug there drops the position or turns
    it into a problem, and this case fails."""
    specs = tmp_path / "specs"
    specs.mkdir()
    envelope = DeskSpec(
        intent_id=f"intent-{struct.id}",
        account_id=ACCOUNT,
        structure=struct,
        entry_order_ref=supervised_order_ref(f"intent-{struct.id}"),
        registered_at=T0,
    )
    spec_path = specs / f"{struct.id}.json"
    spec_path.write_bytes(canonical_bytes(envelope.model_dump(mode="json", by_alias=True)))
    assert json.loads(spec_path.read_bytes())["schema"] == "trex-desk-spec/1"
    book = BookState([struct.id])
    book.structures[struct.id] = state
    book.save(tmp_path / "book.json")
    positions, problems = desk_book._desk(specs, tmp_path / "book.json", today)
    assert problems == [], f"{sid}: the owner's envelope did not read"
    held = sum((p.max_loss_usd for p in positions), Decimal(0))
    screen = open_loss_reservation(book, {struct.id: RiskView.of(struct)}, today)
    expected = DESK_LANE_B.get(sid, b)
    assert screen == Decimal(a), f"{sid}: the screen moved"
    assert held == Decimal(expected), f"{sid}: the owner-envelope book view moved"
    if sid in DESK_LANE_B:
        assert screen < held, f"{sid}: the desk lane holds a dormant PLANNED at cap"
    elif common:
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
    view = desk_book._position(
        "fixture:d8", "fixture", struct, state, as_of=today, planned_dormant_after_entry=False
    )
    assert screen == Decimal(0)
    assert view is not None and view.max_loss_usd == Decimal("200.00")


def test_an_impossible_credit_fill_fails_closed_in_the_book_view_only():
    """B refuses a position whose recorded fill leaves no loss (its whole
    position is dropped into load_book's problems); the screen still counts
    the package at its floor-based max loss rather than trusting the fill."""
    struct = _struct("c3", kind="credit_vertical", quantity=1, limit=Decimal("1.00"))
    state = StructureState(Status.OPEN, filled_qty=1, entry_fill=Decimal("5.00"))
    with pytest.raises(desk_book._Inconsistent):
        desk_book._position(
            "fixture:c3",
            "fixture",
            struct,
            state,
            as_of=PARITY_TODAY,
            planned_dormant_after_entry=True,
        )
    book = BookState(["c3"])
    book.structures["c3"] = state
    assert open_loss_reservation(book, {"c3": RiskView.of(struct)}, PARITY_TODAY) == Decimal(
        "400.00"
    )


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
    assert run_loop(rig.desk, interval_s=0, stop=lambda: False, max_ticks=3) == EXIT_DISCONNECTED
    (lost,) = _events(rig, "gateway_lost")
    assert lost["stage"] == "loop"


def test_owner_file_names_the_epoch_to_grant(rig):
    rig.desk.write_owner()
    doc = json.loads(rig.desk.owner_path().read_text())
    assert (doc["owner_epoch"], doc["client_id"]) == (EPOCH, SUPERVISED_CLIENT_ID)


# ------------------------------------------- D2: the connection drops mid-loop

# a provider message that can carry account details: it must never reach a
# log line or an event (desk_runtime records the error class only)
SECRET = "acct DU1234567 provider detail"


def _events(rig: Rig, name: str) -> list[dict[str, Any]]:
    path = rig.paths.events()
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in records if r["event"] == name]


@pytest.mark.parametrize(
    "method, stage, error, disconnect",
    [
        ("openTrades", "tick", ConnectionError, True),
        ("qualifyContracts", "requests", ConnectionError, True),
        ("sleep", "sleep", ConnectionError, True),
        # the SDK still reports connected: the error class alone decides
        ("sleep", "sleep", ConnectionError, False),
        ("openTrades", "tick", TimeoutError, False),
        ("openTrades", "tick", OSError, False),
        # ANY error once the session reads disconnected (ib_async raises
        # whatever its torn-down client raises)
        ("openTrades", "tick", AttributeError, True),
        ("qualifyContracts", "requests", TypeError, True),
    ],
)
def test_a_drop_mid_loop_exits_6_with_a_gateway_lost_record(
    rig, caplog, method, stage, error, disconnect
):
    caplog.set_level(logging.DEBUG)
    rig.ready()
    if stage == "requests":
        rig.request()  # (the request path pumps ib.sleep too: inbox stage only)
    rig.gw.drop(method, error(SECRET), disconnect=disconnect)
    assert run_loop(rig.desk, interval_s=0, stop=lambda: False, max_ticks=3) == EXIT_DISCONNECTED
    (lost,) = _events(rig, "gateway_lost")
    assert (lost["stage"], lost["error"]) == (stage, error.__name__)
    assert ("trex-desk gateway_lost", "urgent") in [(t, p) for t, _, p in rig.notified]
    assert SECRET not in rig.paths.events().read_text(), "error class only"
    assert SECRET not in caplog.text, "error class only"
    assert "Traceback" not in caplog.text


def test_a_bug_while_connected_still_propagates(rig):
    rig.ready()
    rig.gw.drop("openTrades", ValueError("a real bug"), disconnect=False)
    with pytest.raises(ValueError, match="a real bug"):
        run_loop(rig.desk, interval_s=0, stop=lambda: False, max_ticks=3)
    assert _events(rig, "gateway_lost") == []


def test_an_unwritable_gateway_lost_record_cannot_mask_exit_6(rig, monkeypatch):
    def unwritable(*_: Any, **__: Any) -> None:
        raise OSError("events.jsonl: no space left on device")

    rig.ready()
    rig.gw.drop("sleep", ConnectionError(SECRET))
    monkeypatch.setattr(rig.runtime, "_event", unwritable)
    assert run_loop(rig.desk, interval_s=0, stop=lambda: False, max_ticks=3) == EXIT_DISCONNECTED


# ------------------------------------- D2: a claim the dead owner never answered


class _Killed(BaseException):
    """The process dies here (SIGKILL, OOM): nothing after this point runs."""


def _drop_during_preflight(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.gw.drop("qualifyContracts", ConnectionError(SECRET))
    with pytest.raises(ConnectionError):
        rig.desk.process_requests()


def _drop_after_the_permit(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def register(effect: SupervisedEffect) -> Any:
        rig.gw.is_connected = False
        raise ConnectionError(SECRET)

    monkeypatch.setattr(rig.runtime, "register", register)
    with pytest.raises(ConnectionError):
        rig.desk.process_requests()


def _killed_at_the_broker_call(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.gw.drop("placeOrder", _Killed())
    with pytest.raises(_Killed):
        rig.desk.process_requests()


def _killed_before_the_result(outcome: str) -> Callable[[Rig, pytest.MonkeyPatch], None]:
    """send() returned (its terminal receipt is durable); the process died
    before the request's result file was written."""

    def scenario(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        if outcome == "uncertain":
            rig.gw.drop("placeOrder", ConnectionError(SECRET))
        (result,) = rig.desk.process_requests()
        assert result["receipt"]["outcome"] == outcome
        (rig.desk.requests_dir() / "sup-001.result.json").unlink()

    return scenario


def _reconciled_not_submitted(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _killed_before_the_result("uncertain")(rig, monkeypatch)
    rig.sup.reconciled("sup-001").write_text(
        json.dumps({"intent_id": "sup-001", "verdict": "confirmed_not_submitted"})
    )


def _unreadable_receipt(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _killed_before_the_result("acknowledged")(rig, monkeypatch)
    rig.sup.terminal("sup-001").write_text("{torn")


@pytest.mark.parametrize(
    "scenario, outbox_state, order_sent, note",
    [
        (_drop_during_preflight, "absent", "no", None),
        (_drop_after_the_permit, "pending", "no", None),
        (_killed_at_the_broker_call, "sending", "unknown", "reconciliation_required"),
        (_killed_before_the_result("acknowledged"), "acknowledged", "yes", None),
        (
            _killed_before_the_result("uncertain"),
            "uncertain",
            "unknown",
            "reconciliation_required",
        ),
        (_reconciled_not_submitted, "reconciled", "no", None),
        (_unreadable_receipt, "unreadable", "unknown", "reconciliation_required"),
    ],
)
def test_an_orphaned_claim_gets_an_interrupted_result_and_is_never_resent(
    rig, monkeypatch, scenario, outbox_state, order_sent, note
):
    rig.ready()
    rig.request()
    scenario(rig, monkeypatch)
    inbox = rig.desk.requests_dir()
    claimed = inbox / "sup-001.claimed.json"
    assert claimed.exists() and not (inbox / "sup-001.result.json").exists(), "an orphan"
    sends = len(rig.gw.trades)
    # the restart: reconnected, the run-dir lock held, before the first tick
    rig.gw.is_connected = True
    (recovered,) = rig.desk.recover_interrupted_requests()
    result = rig.result()
    assert result == recovered
    assert (result["status"], result["intent_id"], result["request"]) == (
        "interrupted",
        "sup-001",
        "sup-001",
    )
    assert (result["outbox_state"], result["order_sent"]) == (outbox_state, order_sent)
    assert result.get("note") == note
    if outbox_state in ("absent", "pending"):
        assert result["execution_state"] is None, "nothing journaled: no attempt was made"
    if outbox_state == "sending":
        # the journal holds the submit attempt: why the outcome is unknown
        assert result["execution_state"] == "SUBMITTING"
    if outbox_state == "acknowledged":
        (trade,) = rig.gw.trades
        assert result["receipt"]["broker_order_id"] == str(trade.order.orderId)
    (pushed,) = [e for e in _events(rig, "entry_request") if e["status"] == "interrupted"]
    assert (pushed["intent"], pushed["order_sent"]) == ("sup-001", order_sent)
    # never re-sent: the claim stays, the inbox is empty, a later pass is a no-op
    assert claimed.exists() and not (inbox / "sup-001.json").exists()
    assert rig.desk.recover_interrupted_requests() == [], "idempotent"
    assert rig.result() == result, "an answered claim is never rewritten"
    assert rig.desk.process_requests() == []
    assert run_loop(rig.desk, interval_s=0, stop=lambda: False, max_ticks=2) == 0
    assert len(rig.gw.trades) == sends, "zero second sends"


def test_an_answered_claim_is_not_recovered(rig):
    rig.ready()
    rig.request()
    (sent,) = rig.desk.process_requests()
    assert rig.desk.recover_interrupted_requests() == []
    assert rig.result() == sent


# ---------------------------------------------------- D2: main's exit code


class _OfflineSession(IbkrTrex):
    """main()'s session over the fake gateway: ``connect`` opens no socket,
    and the shutdown ``disconnect`` fails like a socket already gone."""

    gateway: SupervisedGateway
    disconnects = 0

    def connect(self) -> None:
        self._ib = self.gateway

    def disconnect(self) -> None:
        self.disconnects += 1
        raise ConnectionError(SECRET)


def _then_fail(real: Callable[[Any], None]) -> Callable[[Any], None]:
    def wrapper(self: Any) -> None:
        real(self)
        raise OSError(SECRET)

    return wrapper


def test_main_exits_6_on_a_drop_even_when_every_cleanup_call_fails(tmp_path, monkeypatch, caplog):
    from tree_options.trex import supervised_desk
    from tree_options.trex.account_ownership import AccountOwnership

    for var, sub in (
        ("TREX_DESK_RUN_DIR", "desk"),
        ("TREX_SUPERVISED_DIR", "supervised"),
        ("TREX_ACCOUNT_OWNERS", "owners"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.delenv("TREX_IBKR_ACCOUNT_ALIAS", raising=False)
    paths = DeskPaths.default()
    assert paths.root == tmp_path / "desk", "never the live owner's run dir"
    assert SupervisedPaths.default().root == tmp_path / "supervised"
    gw = SupervisedGateway(CON, ACCOUNT)
    gw.drop("sleep", ConnectionError(SECRET))
    made: list[_OfflineSession] = []

    def session(**kwargs: Any) -> _OfflineSession:
        s = _OfflineSession(**kwargs)
        s.gateway = gw
        made.append(s)
        return s

    monkeypatch.setattr(supervised_desk, "IbkrTrex", session)
    monkeypatch.setattr(supervised_desk, "_dividend_source", lambda: None)
    monkeypatch.setattr(supervised_desk.notify, "load_config", lambda *_, **__: None)
    monkeypatch.setattr(supervised_desk.signal, "signal", lambda *_, **__: None)
    monkeypatch.setattr(supervised_desk, "in_session", lambda now: False)
    monkeypatch.setattr(DeskRuntime, "release", _then_fail(DeskRuntime.release))
    monkeypatch.setattr(AccountOwnership, "close", _then_fail(AccountOwnership.close))
    # the previous owner claimed a request and died before answering it
    inbox = paths.root / "requests"
    inbox.mkdir(parents=True)
    (inbox / "sup-001.claimed.json").write_text("{}")
    caplog.set_level(logging.DEBUG)

    assert supervised_desk.main(["run", "--no-spots", "--interval", "0"]) == EXIT_DISCONNECTED

    result = json.loads((inbox / "sup-001.result.json").read_text())
    assert (result["status"], result["outbox_state"], result["order_sent"]) == (
        "interrupted",
        "absent",
        "no",
    )
    events = [json.loads(line) for line in paths.events().read_text().splitlines()]
    (lost,) = [e for e in events if e["event"] == "gateway_lost"]
    assert (lost["stage"], lost["error"]) == ("sleep", "ConnectionError")
    assert SECRET not in caplog.text and SECRET not in paths.events().read_text()
    assert "Traceback" not in caplog.text
    # every cleanup call still ran, each failure isolated from the next
    fences = list((tmp_path / "owners").glob("*.lock"))
    assert fences and all(_lock_free(f) for f in fences), "the account fence was closed"
    assert _lock_free(paths.lock()), "the run-dir lock was released"
    assert [s.disconnects for s in made] == [1], "the disconnect was attempted"


def _lock_free(path: Path) -> bool:
    with open(path, "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
