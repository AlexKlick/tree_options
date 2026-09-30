"""The supervised desk process: entry orchestration + the E5 loop.

One process, one IBKR session (clientId ``SUPERVISED_CLIENT_ID``), because
IBKR refuses a second connection with the same clientId and only the placing
client can see and cancel an order. Everything that touches the broker for
supervised trades therefore runs HERE:

- **Entry inbox.** An entry request (``requests/<intent_id>.json``, schema
  ``trex-desk-entry-request/1``: the ``SupervisedEffect``, the strategy
  version and a send deadline) is claimed by an atomic rename and taken
  through ``enter``: mandate (bound to this process's owner epoch AND the
  operator's capital-profile digest) -> adapter preflight -> canary screening
  (with this process's own facts: owner health, ``exit_owner_ready``, the
  assignment plan, and the reconciled open/day loss of the E5 book plus the
  legacy books) -> intent -> permit -> ``DeskRuntime.register`` -> ``send``.
  The outcome is ``requests/<intent_id>.result.json``; nothing is retried.
- **The loop.** Inside the session: process the inbox, then one
  ``DeskRuntime.tick``. Outside it: heartbeat only. A lost gateway
  connection exits 6 (systemd restarts; the arm gate goes stale).

``owner.json`` in the run dir names this process's owner epoch: the operator
grants the mandate for exactly that epoch (``python -m
tree_options.trex.supervised grant --owner-epoch ...``), so a restarted
process needs a fresh grant. The capital profile is the operator-authored
``profile.json`` in the run dir; its digest must equal the mandate's.

v1 supports debit kinds only: the execution records represent BUY-to-open
(``OrderIntent``), and a credit package opens with a SELL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import time as wall
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from tree_options.action_graph.capital import CapitalProfile
from tree_options.action_graph.proposal import canonical_bytes
from tree_options.execution import OrderIntent
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex import notify
from tree_options.trex.clock import ET, is_session
from tree_options.trex.desk_runtime import (
    DeskPaths,
    DeskRuntime,
    DividendSource,
    assignment_plan,
    exit_owner_ready,
)
from tree_options.trex.ibkr import GATEWAY_PAPER_PORT, IbkrTrex
from tree_options.trex.plan import LegStructure, PutSpread, load_plan
from tree_options.trex.state import ENTRY_LANE, BookState, Status
from tree_options.trex.supervised import (
    SupervisedIntent,
    SupervisedPaths,
    SupervisedRefused,
    active_mandate,
    issue_permit,
    record_intent,
    send,
)
from tree_options.trex.supervised_canary import (
    OperatorCanaryInputs,
    collect_canary_screening,
)
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    SupervisedEffect,
    effect_bytes,
)

log = logging.getLogger("trex.supervised_desk")

REQUEST_SCHEMA = "trex-desk-entry-request/1"
RESULT_SCHEMA = "trex-desk-entry-result/1"
SESSION_OPEN = time(9, 30)
SESSION_END = time(16, 15)
EXIT_DISCONNECTED = 6


class EntryRequest(StrictModel):
    schema_version: Literal["trex-desk-entry-request/1"] = Field(
        default="trex-desk-entry-request/1", alias="schema")
    strategy_version: IdStr
    send_deadline: datetime
    requested_by: IdStr
    effect: SupervisedEffect


def load_profile(path: Path) -> tuple[CapitalProfile, str]:
    """The operator's capital profile and the digest a mandate must bind."""
    doc = json.loads(path.read_bytes())
    digest = hashlib.sha256(canonical_bytes(doc)).hexdigest()

    def money(name: str) -> Decimal | None:
        value = doc.get(name)
        return None if value is None else Decimal(str(value))

    intended = money("intended_capital")
    if intended is None:
        raise ValueError("intended_capital required")
    profile = CapitalProfile(
        profile_id=str(doc["profile_id"]), revision=int(doc["revision"]),
        intended_capital=intended, risk_style=str(doc["risk_style"]),
        goals=tuple(doc["goals"]),
        allowed_strategy_versions=tuple(doc["allowed_strategy_versions"]),
        max_loss_per_trade=money("max_loss_per_trade"), max_open_loss=money("max_open_loss"),
        max_daily_loss=money("max_daily_loss"), horizon_days=doc.get("horizon_days"))
    return profile, digest


# ------------------------------------------------------------- loss facts


@dataclass(frozen=True)
class RiskView:
    """What a structure can lose, per package before x100."""

    per_package: Decimal
    quantity: int
    is_credit: bool
    entry_date: date

    @classmethod
    def of(cls, structure: PutSpread | LegStructure) -> RiskView:
        if isinstance(structure, PutSpread):
            return cls(structure.limit_cap, structure.quantity, False, structure.entry_date)
        return cls(structure.max_loss_per_package(), structure.quantity, structure.is_credit,
                   structure.entry_date)


def open_loss_reservation(book: BookState, risks: Mapping[str, RiskView],
                          today: date) -> Decimal:
    """Worst-case loss still at risk: open packages at the debit paid (or the
    cap/floor when unpriced), entries at their full size; a PLANNED entry
    whose date has passed can no longer enter.

    A SECOND reservation exists — the rails' book view
    (``desk.book._position`` behind ``load_book``) — and the two agree on
    their common subset (pinned by ``test_supervised_desk.py``'s parity
    cases: debit kinds priced or unpriced, ENTER_WORKING at full quantity,
    planned-before-date at cap, planned-past-date dropped). The intentional
    differences, all making THIS screen the more conservative one except
    the last: (1) a priced credit kind stays at its floor-based max loss
    here while the book view refines to width - fill; (2) an entry average
    spanning unpriced packages is NOT trusted here (cap), while the book
    view prices at the recorded average; (3) opposite direction: a desk
    PLANNED structure past its entry date is dropped here (it can no
    longer enter) but held at cap by the desk book view; (4) a state the
    book view proves incoherent (e.g. a fill leaving no loss) raises there
    and fail-closes into load_book's problems, while here it still counts
    at the cap."""
    total = Decimal(0)
    for sid, risk in risks.items():
        st = book.structures.get(sid)
        if st is None or st.status is Status.CLOSED:
            continue
        if st.status in ENTRY_LANE:
            if st.status is Status.PLANNED and risk.entry_date < today:
                continue
            total += risk.per_package * 100 * risk.quantity
            continue
        per = risk.per_package
        if not risk.is_credit and st.entry_fill is not None and not st.entry_unpriced_qty:
            per = st.entry_fill
        total += per * 100 * max(st.open_qty, 0)
    return total


def realized_day_loss(book: BookState, risks: Mapping[str, RiskView],
                      today: date) -> Decimal | None:
    """Losses realized by exits recorded today; None when any is unpriced."""
    loss = Decimal(0)
    for sid, risk in risks.items():
        st = book.structures.get(sid)
        if st is None or not st.exit_filled_qty or st.updated_at is None:
            continue
        if st.updated_at.astimezone(ET).date() != today:
            continue
        if (st.entry_fill is None or st.exit_fill is None or st.entry_unpriced_qty
                or st.exit_unpriced_qty):
            return None
        pnl = st.entry_fill - st.exit_fill if risk.is_credit else st.exit_fill - st.entry_fill
        loss += max(Decimal(0), -pnl) * 100 * st.exit_filled_qty
    return loss


@dataclass(frozen=True)
class LegacyBook:
    """A legacy trex plan whose book counts toward the open/day loss (3b)."""

    plan_path: Path
    state_root: Path

    def load(self) -> tuple[BookState, dict[str, RiskView]]:
        plan = load_plan(self.plan_path)
        structures: list[PutSpread | LegStructure] = [*plan.structures, *plan.leg_structures]
        risks = {s.id: RiskView.of(s) for s in structures}
        book_path = self.state_root / plan.id / "book.json"
        return BookState.load(book_path, list(risks)), risks


# ------------------------------------------------------------ the desk


class SupervisedDesk:
    """Entry orchestration for one supervised desk process."""

    def __init__(self, ib: IbkrTrex, runtime: DeskRuntime, broker: IbkrSupervisedBroker, *,
                 supervised: SupervisedPaths, owner_epoch: str,
                 dividends: DividendSource | None = None,
                 legacy: Sequence[LegacyBook] = (),
                 clock: Callable[[], datetime] | None = None) -> None:
        self.ib = ib
        self.runtime = runtime
        self.broker = broker
        self.supervised = supervised
        self.owner_epoch = owner_epoch
        self.dividends = dividends
        self.legacy = tuple(legacy)
        self.clock = clock or (lambda: datetime.now(ET))

    @property
    def paths(self) -> DeskPaths:
        return self.runtime.paths

    def requests_dir(self) -> Path:
        return self.paths.root / "requests"

    def profile_path(self) -> Path:
        return self.paths.root / "profile.json"

    def owner_path(self) -> Path:
        return self.paths.root / "owner.json"

    def write_owner(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        doc = {"owner_epoch": self.owner_epoch, "client_id": self.ib.client_id,
               "pid": os.getpid(), "started_at": self.clock().isoformat()}
        tmp = self.owner_path().with_name("owner.json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.owner_path())

    # -- loss facts ------------------------------------------------------------

    def loss_facts(self, today: date) -> tuple[Decimal | None, Decimal | None]:
        """(open-loss reservation, realized day loss) over the E5 book and
        every legacy book; None for either when any book cannot be read."""
        views: list[tuple[BookState, dict[str, RiskView]]] = []
        try:
            specs = self.runtime.specs()
            e5_risks = {sid: RiskView.of(s.structure) for sid, s in specs.items()}
            views.append((BookState.load(self.paths.book(), list(e5_risks)), e5_risks))
            views.extend(book.load() for book in self.legacy)
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
        open_loss: Decimal | None = Decimal(0)
        day_loss: Decimal | None = Decimal(0)
        for book, risks in views:
            open_loss = (None if open_loss is None
                         else open_loss + open_loss_reservation(book, risks, today))
            realized = realized_day_loss(book, risks, today)
            day_loss = None if day_loss is None or realized is None else day_loss + realized
        return open_loss, day_loss

    # -- the inbox -------------------------------------------------------------

    def process_requests(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        inbox = self.requests_dir()
        if not inbox.is_dir():
            return results
        for path in sorted(inbox.glob("*.json")):
            if path.name.endswith((".claimed.json", ".result.json")):
                continue
            claimed = path.with_name(path.name.removesuffix(".json") + ".claimed.json")
            try:
                os.rename(path, claimed)  # the claim: processed exactly once
            except FileNotFoundError:
                continue
            result = self._handle(claimed, path.name.removesuffix(".json"))
            out = path.with_name(path.name.removesuffix(".json") + ".result.json")
            tmp = out.with_name(out.name + ".tmp")
            tmp.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
            os.replace(tmp, out)
            self.runtime._event("entry_request", intent=result.get("intent_id"),
                                status=result["status"])
            results.append(result)
        return results

    def _handle(self, claimed: Path, name: str) -> dict[str, Any]:
        now = self.clock()
        base: dict[str, Any] = {"schema": RESULT_SCHEMA, "at": now.isoformat(),
                                "request": name}
        try:
            request = EntryRequest.model_validate(json.loads(claimed.read_bytes()))
        except (ValueError, OSError) as error:
            return {**base, "status": "invalid", "reason": repr(error)}
        if request.effect.intent_id != name:
            return {**base, "status": "invalid", "reason": "file name is not the intent id"}
        try:
            return {**base, "intent_id": name, **self.enter(request, now)}
        except SupervisedRefused as refused:
            return {**base, "intent_id": name, "status": "refused",
                    "reason": refused.reason, "detail": refused.detail}

    def enter(self, request: EntryRequest, now: datetime) -> dict[str, Any]:
        """Mandate -> preflight -> screening -> intent -> permit -> register
        -> send. Every refusal happens before the send."""
        effect = request.effect
        structure = effect.structure
        if effect.side != "BUY":
            return {"status": "blocked", "blockers": ["credit_open_not_supported_v1"]}
        if (now - request.send_deadline).total_seconds() >= 0:
            return {"status": "blocked", "blockers": ["request_expired"]}
        if not self.profile_path().exists():
            return {"status": "blocked", "blockers": ["profile_absent"]}
        profile, digest = load_profile(self.profile_path())
        mandate = active_mandate(self.supervised, now=now, account_id=effect.account_id,
                                 owner_epoch=self.owner_epoch,
                                 strategy_version=request.strategy_version)
        if mandate.profile_digest != digest:
            return {"status": "blocked", "blockers": ["mandate_profile_mismatch"]}
        payload = effect_bytes(effect)
        preflight = self.broker.preflight(payload)
        if preflight:
            return {"status": "blocked", "blockers": preflight}
        snapshot = self.dividends(structure.underlying, now.date()) if self.dividends else None
        plan_ok, plan_why = assignment_plan(structure, snapshot, now.date())
        open_loss, day_loss = self.loss_facts(now.astimezone(ET).date())
        inputs = OperatorCanaryInputs(
            owner_epoch=self.owner_epoch, owner_healthy=self.ib.connected,
            assignment_plan_verified=plan_ok,
            protective_exit_ready=exit_owner_ready(self.paths, now),
            current_open_loss=open_loss, realized_daily_loss=day_loss)
        screening = collect_canary_screening(self.broker, effect, profile=profile,
                                             mandate_account_id=mandate.account_id,
                                             inputs=inputs, clock=self.clock)
        facts = {"screening_sha256": screening.screening_sha256,
                 "assignment_plan": plan_why,
                 "open_loss": None if open_loss is None else str(open_loss),
                 "day_loss": None if day_loss is None else str(day_loss)}
        if not screening.clear or screening.facts is None:
            return {"status": "blocked", "blockers": list(screening.blockers), **facts}
        intent = SupervisedIntent(
            intent=OrderIntent(
                intent_id=effect.intent_id, contract_id=f"BAG:{structure.id}", side="BUY",
                position_effect="OPEN_LONG", quantity=effect.quantity, order_type="LIMIT",
                limit_price=effect.limit, execution_style="package", package_id=structure.id,
                intent_created_at=now, source=request.strategy_version,
                source_sequence_id=f"desk-{effect.intent_id}"),
            package_intent_sha256=screening.facts.intent_sha256,
            created_at=now, send_deadline=request.send_deadline)
        record_intent(self.supervised, intent)
        # Account-id pre-check: refuse BEFORE spending a permit. The mandate
        # already passed (the screening gate verified it); if the effect's
        # account diverged from that mandate (operator typo, replay, etc.)
        # we leak neither a permit nor a spec file.
        if mandate.account_id != effect.account_id:
            return {"status": "blocked", "blockers": ["register_account_mismatch"],
                    "account_id": effect.account_id,
                    "mandate_account_id": mandate.account_id, **facts}
        permit = issue_permit(self.supervised, now=now, account_id=effect.account_id,
                              owner_epoch=self.owner_epoch, intent=intent,
                              canary_blockers=screening.blockers, effect_payload=payload,
                              screening_sha256=screening.screening_sha256)
        self.runtime.register(effect)
        receipt = send(self.supervised, now=now, permit_id=permit.permit_id,
                       effect_payload=payload, broker=self.broker)
        return {"status": "sent", "permit_id": permit.permit_id, "receipt": receipt, **facts}


# ---------------------------------------------------------------- the loop


def in_session(now: datetime) -> bool:
    local = now.astimezone(ET)
    return is_session(local) and SESSION_OPEN <= local.time() <= SESSION_END


def run_loop(desk: SupervisedDesk, *, interval_s: float,
             stop: Callable[[], bool], max_ticks: int | None = None) -> int:
    """Tick until ``stop()``; 6 when the gateway connection is lost."""
    ticks = 0
    while not stop():
        if not desk.ib.connected:
            desk.runtime._event("gateway_lost")
            return EXIT_DISCONNECTED
        now = desk.clock()
        if in_session(now):
            # tick FIRST: the arm gate (exit_owner_ready) reads this beat, so
            # the first request after a restart is not refused for a stale one
            desk.runtime.tick()
            desk.process_requests()
        else:
            desk.runtime.beat()
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        desk.ib.sleep(interval_s)
    return 0


def _dividend_source() -> DividendSource:
    from tree_options.desk.dividends import load_snapshot
    from tree_options.trex.clock import session_calendar

    cal = session_calendar()
    return lambda symbol, as_of: load_snapshot(symbol, as_of, cal)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tree_options.trex.supervised_desk")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="the supervised desk process (clientId 83)")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=GATEWAY_PAPER_PORT)
    run.add_argument("--interval", type=float, default=5.0)
    run.add_argument("--legacy-plan", type=Path, action="append", default=[],
                     help="a legacy trex plan whose book counts toward open/day loss")
    run.add_argument("--no-spots", action="store_true",
                     help="no Polygon spot feed (touch/breach exits cannot fire)")
    digest = sub.add_parser("profile-digest",
                            help="print the digest a mandate must bind (no broker contact)")
    digest.add_argument("--path", type=Path, default=None,
                        help="profile.json (default: the desk run dir's)")
    args = ap.parse_args(argv)
    if args.command == "profile-digest":
        path = args.path or DeskPaths.default().root / "profile.json"
        try:
            profile, value = load_profile(path)
        except (OSError, ValueError, KeyError) as error:
            print(f"refused: invalid_profile {error!r}", file=sys.stderr)
            return 2
        print(json.dumps({"profile_id": profile.profile_id, "revision": profile.revision,
                          "profile_digest": value}, indent=2))
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    ib = IbkrTrex(host=args.host, port=args.port, client_id=SUPERVISED_CLIENT_ID)
    try:
        ib.connect()
    except Exception as error:  # the unit's ExecStartPre waits for the API first
        log.error("gateway connect failed: %r", error)
        return EXIT_DISCONNECTED
    spots = None
    if not args.no_spots:
        from tree_options.trex.spot import SpotFeed, polygon_fetcher

        feed = SpotFeed(polygon_fetcher())

        def spots(symbols: Sequence[str], now: datetime) -> dict[str, Decimal]:
            return {sym: r.px for sym, r in feed.readings(list(symbols), now).items()}

    dividends = _dividend_source()
    notify_fn: Callable[[str, str, str], None] | None = None
    notify_cfg = notify.load_config()
    if notify_cfg is not None:
        def _push(title: str, message: str, priority: str = "default") -> None:
            notify.send(notify_cfg, title, message, priority)

        notify_fn = _push
    paths = DeskPaths.default()
    supervised = SupervisedPaths.default()
    runtime = DeskRuntime(ib, paths, supervised=supervised, spots=spots,
                          dividends=dividends, notify=notify_fn)
    runtime.acquire()
    epoch = f"desk{SUPERVISED_CLIENT_ID}-{int(wall.time()):x}"
    # One process-lifetime alias fence across execution providers. Existing
    # managed IBKR paper accounts remain the broker binding; an operator may
    # give a single account a common alias when another runtime uses that alias.
    from tree_options.trex.account_ownership import AccountOwnership, ownership_root

    account_fences: list[AccountOwnership] = []
    try:
        accounts = [str(a) for a in ib._ib.managedAccounts()]
        alias = os.environ.get("TREX_IBKR_ACCOUNT_ALIAS")
        if alias and len(accounts) != 1:
            raise ValueError("an account alias requires exactly one managed account")
        for account in accounts:
            fence = AccountOwnership(ownership_root(), alias or account, epoch=epoch)
            fence.acquire()
            account_fences.append(fence)
    except Exception:
        for fence in account_fences:
            fence.close()
        runtime.release()
        ib.disconnect()
        raise
    desk = SupervisedDesk(ib, runtime, IbkrSupervisedBroker(ib), supervised=supervised,
                          owner_epoch=epoch, dividends=dividends,
                          legacy=[LegacyBook(p, Path(os.environ.get(
                              "TREX_STATE", Path.home() / ".local/state/trex")))
                              for p in args.legacy_plan])
    desk.write_owner()
    log.info("supervised desk up: owner epoch %s (grant the mandate for it)", epoch)
    stopping = False

    def _stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        return run_loop(desk, interval_s=args.interval, stop=lambda: stopping)
    finally:
        for fence in account_fences:
            fence.close()
        runtime.release()
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
