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
- **Evidence is never replaced.** The claim and every result are made with
  a no-replace rename. A request for an intent that already has a claim or
  a result is never processed: it moves aside to
  ``<intent_id>.duplicate-<n>.json`` and is refused in
  ``<intent_id>.duplicate-<n>.result.json``.
- **The loop.** Inside the session: one ``DeskRuntime.tick``, then the
  inbox. Outside it: heartbeat only. A lost gateway connection, seen at
  the loop top or raised mid tick/inbox/sleep, is one ``gateway_lost``
  event (error class only) and exit 6 (systemd restarts; the arm gate goes
  stale); shutdown failures cannot mask that code.
- **Restart.** A request the previous owner claimed but never answered
  gets an ``interrupted`` result reporting what the supervised outbox
  proves about its order (``order_sent`` yes / no / unknown, unknown
  meaning reconciliation required). It is never re-sent.

``owner.json`` in the run dir names this process's owner epoch: the operator
grants the mandate for exactly that epoch (``python -m
tree_options.trex.supervised grant --owner-epoch ...``), so a restarted
process needs a fresh grant. The capital profile is the operator-authored
``profile.json`` in the run dir; its digest must equal the mandate's.

v1 supports debit kinds only: the execution records represent BUY-to-open
(``OrderIntent``), and a credit package opens with a SELL.

Wave 2b (lane F, owner orchestration): the same process is the supervised
paper desk the owner drives by commands. PAPER ONLY; the owner approves
every entry; exits stay automatic and the runtime's.

- **Each loop iteration**: the runtime's tick (in session) or beat, then the
  books (``trex.desk_books``: executions and late commission reports into
  the append-only ledger, the ``trex-holdings/1`` projection, and the
  reconciliation and risk snapshot every 60 s and at once after a
  (re)connect), then the commands (``trex.desk_commands``: every kind,
  kill switches included, is applied HERE; the web only queues), then the
  v1 entry inbox (in session). A books or command problem never stops the
  exits; a lost gateway is still exit 6.
- **Entry commands** (``trex.desk_entries``): compose -> preflight +
  screening -> ``proposals/<id>.json``; approve -> approval binding, kill
  files, fresh owner facts, screening with the session's entry count and the
  pending-entry reservation -> per-trade mandate -> exactly one send.
- **At start** (``start``): every unconsumed per-trade mandate of another
  owner epoch is retired (``approvals_invalidated``; proposals composed
  under it no longer match the live epoch), and a command the previous
  owner claimed but never answered is answered ``failed``, never re-applied.
- **Loss facts** count the desk's EQUITY positions beside the options:
  entries at full size, open shares at the contract's max-loss arithmetic
  at their entry limit, and today's realized equity losses.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import functools
import hashlib
import json
import logging
import os
import re
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
from tree_options.execution import ExecutionLifecycleError, OrderIntent
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex import notify
from tree_options.trex.admission import entry_send_blockers
from tree_options.trex.clock import ET, is_session
from tree_options.trex.contracts.desk_spec_v2 import EquityDeskSpecV2
from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.proposals import EntryProposal
from tree_options.trex.contracts.vocabulary import DeskEvent
from tree_options.trex.desk_books import DeskBooks
from tree_options.trex.desk_commands import CommandQueue
from tree_options.trex.desk_entries import OwnerEntries, equity_loss_cents
from tree_options.trex.desk_runtime import (
    DeskPaths,
    DeskRuntime,
    DividendSource,
    RuntimeLocked,
    assignment_plan,
    exit_owner_ready,
)
from tree_options.trex.ibkr import GATEWAY_PAPER_PORT, IbkrTrex
from tree_options.trex.ledger import AnyDeskSpec
from tree_options.trex.plan import LegStructure, PutSpread, load_plan
from tree_options.trex.state import ENTRY_LANE, BookState, Status
from tree_options.trex.supervised import (
    SupervisedIntent,
    SupervisedPaths,
    SupervisedRefused,
    active_mandate,
    invalidate_entry_mandates,
    issue_permit,
    project_intent,
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
#: what the run loop treats as a lost gateway even while the SDK still
#: reports connected (ConnectionError and TimeoutError are OSErrors; named
#: for the reader). Anything raised once the session reads disconnected
#: counts too; anything else while connected is a bug and propagates.
DISCONNECT_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError)
#: an interrupted request whose order the outbox cannot prove either way
_UNKNOWN: Mapping[str, str] = {"order_sent": "unknown", "note": "reconciliation_required"}
#: what the desk writes beside a request (claims, results, a duplicate set
#: aside and its refusal, a collision answer): never itself a request
_BOOKKEEPING = re.compile(r"\.(?:claimed|result|duplicate-\d+)\.json$")
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def request_name(path: Path) -> str | None:
    """The request name of an inbox file ``<name>.json``; None for the
    desk's own bookkeeping files and anything that is not ``*.json``."""
    if not path.name.endswith(".json") or _BOOKKEEPING.search(path.name):
        return None
    return path.name.removesuffix(".json")


@functools.cache
def _native_renameat2() -> Callable[..., int] | None:
    """glibc's ``renameat2``, or None where the C library lacks it."""
    try:
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return None
    fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    fn.restype = ctypes.c_int
    return fn


def rename_noreplace(src: Path, dst: Path) -> None:
    """Rename ``src`` to ``dst`` unless ``dst`` exists, in one step.

    ``renameat2(RENAME_NOREPLACE)``: FileExistsError leaves both names
    untouched, FileNotFoundError means ``src`` is gone, and no instant shows
    both names or neither. Where the kernel or filesystem lacks the flag:
    a hard link (which never replaces either) then unlinking ``src``. A
    crash between those two leaves both names; the inbox then refuses the
    request as a duplicate of its own claim, so it is still never
    processed twice."""
    native = _native_renameat2()
    if native is not None:
        if native(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise OSError(code, os.strerror(code), str(src), None, str(dst))
    os.link(src, dst)
    os.unlink(src)


class EntryRequest(StrictModel):
    schema_version: Literal["trex-desk-entry-request/1"] = Field(
        default="trex-desk-entry-request/1", alias="schema"
    )
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
        profile_id=str(doc["profile_id"]),
        revision=int(doc["revision"]),
        intended_capital=intended,
        risk_style=str(doc["risk_style"]),
        goals=tuple(doc["goals"]),
        allowed_strategy_versions=tuple(doc["allowed_strategy_versions"]),
        max_loss_per_trade=money("max_loss_per_trade"),
        max_open_loss=money("max_open_loss"),
        max_daily_loss=money("max_daily_loss"),
        horizon_days=doc.get("horizon_days"),
    )
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
        return cls(
            structure.max_loss_per_package(),
            structure.quantity,
            structure.is_credit,
            structure.entry_date,
        )


def open_loss_reservation(book: BookState, risks: Mapping[str, RiskView], today: date) -> Decimal:
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


def realized_day_loss(
    book: BookState, risks: Mapping[str, RiskView], today: date
) -> Decimal | None:
    """Losses realized by exits recorded today; None when any is unpriced."""
    loss = Decimal(0)
    for sid, risk in risks.items():
        st = book.structures.get(sid)
        if st is None or not st.exit_filled_qty or st.updated_at is None:
            continue
        if st.updated_at.astimezone(ET).date() != today:
            continue
        if (
            st.entry_fill is None
            or st.exit_fill is None
            or st.entry_unpriced_qty
            or st.exit_unpriced_qty
        ):
            return None
        pnl = st.entry_fill - st.exit_fill if risk.is_credit else st.exit_fill - st.entry_fill
        loss += max(Decimal(0), -pnl) * 100 * st.exit_filled_qty
    return loss


def equity_open_loss_reservation(
    book: BookState, equities: Mapping[str, EquityDeskSpecV2]
) -> Decimal:
    """What the desk's equity positions can still lose, in dollars: an entry
    (PLANNED or working) at its full quantity, an open position at its open
    shares, both at the entry LIMIT (never below a buy's fill) with the
    contract's max-loss arithmetic; CLOSED holds nothing."""
    cents = 0
    for sid, spec in equities.items():
        st = book.structures.get(sid)
        if st is None or st.status is Status.CLOSED:
            continue
        pos = spec.position
        qty = pos.quantity if st.status in ENTRY_LANE else max(st.open_qty, 0)
        cents += equity_loss_cents(qty, pos.entry_limit_cents, pos.exit_policy.stop_price_cents)
    return Decimal(cents) / 100


def equity_realized_day_loss(
    book: BookState, equities: Mapping[str, EquityDeskSpecV2], today: date
) -> Decimal | None:
    """Equity losses realized by exits recorded today (per share, x1); None
    when any of them is unpriced."""
    loss = Decimal(0)
    for sid in equities:
        st = book.structures.get(sid)
        if st is None or not st.exit_filled_qty or st.updated_at is None:
            continue
        if st.updated_at.astimezone(ET).date() != today:
            continue
        if (
            st.entry_fill is None
            or st.exit_fill is None
            or st.entry_unpriced_qty
            or st.exit_unpriced_qty
        ):
            return None
        loss += max(Decimal(0), st.entry_fill - st.exit_fill) * st.exit_filled_qty
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

    def __init__(
        self,
        ib: IbkrTrex,
        runtime: DeskRuntime,
        broker: IbkrSupervisedBroker,
        *,
        supervised: SupervisedPaths,
        owner_epoch: str,
        dividends: DividendSource | None = None,
        legacy: Sequence[LegacyBook] = (),
        clock: Callable[[], datetime] | None = None,
        account_id: str | None = None,
    ) -> None:
        self.ib = ib
        self.runtime = runtime
        self.broker = broker
        self.supervised = supervised
        self.owner_epoch = owner_epoch
        self.dividends = dividends
        self.legacy = tuple(legacy)
        self.clock = clock or (lambda: datetime.now(ET))
        # Wave 2b: the books, the entry flows and the command consumer
        self.books = DeskBooks(
            ib,
            runtime.paths.root,
            supervised,
            specs=self._desk_specs,
            clock=self.clock,
            event=runtime._event,
            account_id=account_id,
        )
        runtime.connect_hooks.append(self.books.request_refresh)
        self.entries = OwnerEntries(self)
        self.commands = CommandQueue(
            runtime.paths.root,
            flows=self.entries,
            clock=self.clock,
            owner_epoch=owner_epoch,
            event=runtime._event,
            connected=lambda: _connected(self),
        )
        self._books_noted: set[str] = set()

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
        doc = {
            "owner_epoch": self.owner_epoch,
            "client_id": self.ib.client_id,
            "pid": os.getpid(),
            "started_at": self.clock().isoformat(),
        }
        tmp = self.owner_path().with_name("owner.json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.owner_path())

    # -- loss facts ------------------------------------------------------------

    def loss_facts(self, today: date) -> tuple[Decimal | None, Decimal | None]:
        """(open-loss reservation, realized day loss) over the E5 book (its
        option structures AND its equity positions) and every legacy book;
        None for either when any book cannot be read."""
        views: list[tuple[BookState, dict[str, RiskView]]] = []
        try:
            options, equities = self.runtime.owned_specs()
            e5_risks = {sid: RiskView.of(s.structure) for sid, s in options.items()}
            e5_book = BookState.load(self.paths.book(), [*e5_risks, *equities])
            views.append((e5_book, e5_risks))
            views.extend(book.load() for book in self.legacy)
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
        open_loss: Decimal | None = equity_open_loss_reservation(e5_book, equities)
        day_loss: Decimal | None = equity_realized_day_loss(e5_book, equities, today)
        for book, risks in views:
            open_loss = (
                None if open_loss is None else open_loss + open_loss_reservation(book, risks, today)
            )
            realized = realized_day_loss(book, risks, today)
            day_loss = None if day_loss is None or realized is None else day_loss + realized
        return open_loss, day_loss

    def _desk_specs(self) -> list[AnyDeskSpec]:
        """Every desk spec (both kinds): what the books attribute and price by."""
        options, equities = self.runtime.owned_specs()
        return [*options.values(), *equities.values()]

    # -- Wave 2b: start, books, commands ------------------------------------------

    def start(self) -> dict[str, Any]:
        """The owner's start (lock held, owner.json written): retire every
        unconsumed per-trade mandate of an earlier owner epoch
        (``approvals_invalidated``, also naming the pending proposals composed
        under another epoch, which can no longer be approved), then answer
        the commands a previous owner claimed but never answered."""
        if self.runtime._lock_handle is None:
            raise RuntimeLocked("start requires the runtime lock")
        now = self.clock()
        retired = invalidate_entry_mandates(self.supervised, now=now, owner_epoch=self.owner_epoch)
        stale = self._stale_proposals(now)
        self.runtime._event(
            DeskEvent.APPROVALS_INVALIDATED.value,
            owner_epoch=self.owner_epoch,
            retired_mandates=retired,
            stale_proposals=stale,
        )
        recovered = self.commands.recover_interrupted()
        self.books.request_refresh()
        return {
            "retired_mandates": retired,
            "stale_proposals": stale,
            "recovered_commands": [r.idempotency_key for r in recovered],
        }

    def _stale_proposals(self, now: datetime) -> list[str]:
        """Pending proposals (not rejected, not past their deadline) composed
        under another owner epoch: no approval can match them any more."""
        layout = Wave2RunDir(self.paths.root)
        folder = layout.proposals()
        if not folder.is_dir():
            return []
        stale: list[str] = []
        for path in sorted(folder.glob("*.json")):
            if path.name.endswith(".rejected.json"):
                continue
            try:
                proposal = EntryProposal.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                continue
            if layout.proposal_rejected(proposal.proposal_id).exists():
                continue
            if (now - proposal.send_deadline).total_seconds() >= 0:
                continue
            if proposal.review.owner_epoch != self.owner_epoch:
                stale.append(proposal.proposal_id)
        return stale

    def books_step(self, now: datetime | None = None) -> None:
        """One books step; a problem is recorded (class only), never raised
        into the loop while the session is up (exits must keep running)."""
        try:
            self.books.step(now)
        except Exception as error:
            if isinstance(error, DISCONNECT_ERRORS) or not _connected(self):
                raise
            name = type(error).__name__
            log.error("books step failed (%s); exits are unaffected", name)
            if name not in self._books_noted:
                self._books_noted.add(name)
                self.runtime._event("books_failed", error=name)

    def process_commands(self) -> list[Any]:
        """Consume ``commands/`` (``trex.desk_commands``)."""
        return self.commands.process()

    # -- the inbox -------------------------------------------------------------

    def process_requests(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        inbox = self.requests_dir()
        if not inbox.is_dir():
            return results
        for path in sorted(inbox.glob("*.json")):
            name = request_name(path)
            if name is None:
                continue
            claimed = self.claim_path(name)
            if claimed.exists() or self.result_path(name).exists():
                refused = self._set_aside(path, name)
                if refused is not None:
                    results.append(refused)
                continue
            try:
                rename_noreplace(path, claimed)  # the claim: processed exactly once
            except FileNotFoundError:
                continue
            except FileExistsError:  # claimed meanwhile: never take it over
                refused = self._set_aside(path, name)
                if refused is not None:
                    results.append(refused)
                continue
            result = self._handle(claimed, name)
            out = self._publish(name, result)
            extra = {} if out == self.result_path(name) else {"result_file": out.name}
            self.runtime._event(
                "entry_request", intent=result.get("intent_id"), status=result["status"], **extra
            )
            results.append(result)
        return results

    def claim_path(self, name: str) -> Path:
        return self.requests_dir() / f"{name}.claimed.json"

    def result_path(self, name: str) -> Path:
        return self.requests_dir() / f"{name}.result.json"

    def _set_aside(self, path: Path, name: str) -> dict[str, Any] | None:
        """A request for an intent that already has a claim or a result.

        It is never processed and never replaces that evidence: it moves to
        the first free ``<name>.duplicate-<n>.json`` (one no-replace rename)
        and its refusal is ``<name>.duplicate-<n>.result.json``. None when
        the request vanished meanwhile."""
        existing = sorted(
            p.name for p in (self.claim_path(name), self.result_path(name)) if p.exists()
        )
        n = 0
        while True:
            n += 1
            moved = path.with_name(f"{name}.duplicate-{n}.json")
            if self.result_path(f"{name}.duplicate-{n}").exists():
                continue
            try:
                rename_noreplace(path, moved)
            except FileExistsError:
                continue
            except FileNotFoundError:
                return None
            break
        result: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "at": self.clock().isoformat(),
            "request": name,
            "status": "refused",
            "reason": "duplicate_request",
            "detail": "the intent already has a claim or a result; neither is replaced",
            "existing": existing,
            "moved_to": moved.name,
        }
        self._publish(f"{name}.duplicate-{n}", result)
        self.runtime._event(
            "entry_request",
            request=name,
            status="refused",
            reason="duplicate_request",
            moved_to=moved.name,
        )
        return result

    def _publish(self, stem: str, result: Mapping[str, Any]) -> Path:
        """Write ``<stem>.result.json``, never replacing a file.

        A private tmp, fsynced, then one no-replace rename. If that name is
        taken (written meanwhile by someone else), the answer goes to the
        first free ``<stem>.collision-<n>.result.json`` and the existing
        file stays as it is. Returns the path written."""
        inbox = self.requests_dir()
        tmp = inbox / f".{stem}.result.json.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            n = 0
            while True:
                out = inbox / (
                    f"{stem}.result.json" if n == 0 else f"{stem}.collision-{n}.result.json"
                )
                try:
                    rename_noreplace(tmp, out)
                except FileExistsError:
                    n += 1
                    continue
                if n:
                    log.error("%s.result.json existed: answer written to %s", stem, out.name)
                return out
        finally:
            tmp.unlink(missing_ok=True)

    def recover_interrupted_requests(self) -> list[dict[str, Any]]:
        """Answer every request a previous owner claimed and never answered.

        A claim (``<id>.claimed.json``) with no result means that owner died
        or lost the gateway mid-request. The request is NEVER re-sent: the
        claim stays put, and the result (status ``interrupted``) reports
        what the supervised outbox proves about its order. Lock holder only:
        a live owner's in-flight claim is not an orphan."""
        if self.runtime._lock_handle is None:
            raise RuntimeLocked("recovery requires the runtime lock")
        recovered: list[dict[str, Any]] = []
        inbox = self.requests_dir()
        if not inbox.is_dir():
            return recovered
        for claimed in sorted(inbox.glob("*.claimed.json")):
            name = claimed.name.removesuffix(".claimed.json")
            if self.result_path(name).exists():
                continue
            # process_requests sends only under intent_id == the file name
            result: dict[str, Any] = {
                "schema": RESULT_SCHEMA,
                "at": self.clock().isoformat(),
                "request": name,
                "intent_id": name,
                "status": "interrupted",
                "reason": "owner_stopped_before_result",
                **self._outbox_report(name),
            }
            self._publish(name, result)
            self.runtime._event(
                "entry_request",
                intent=name,
                status="interrupted",
                order_sent=result["order_sent"],
            )
            recovered.append(result)
        return recovered

    def _outbox_report(self, intent_id: str) -> dict[str, Any]:
        """What the supervised outbox PROVES about one intent's order.

        ``order_sent`` is "no" only where ``send`` cannot have reached the
        broker: no outbox entry, a still-pending intent (``send`` renames
        pending -> sending durably BEFORE its broker call) or a
        ``confirmed_not_submitted`` reconciliation. "yes" is a broker
        receipt (acknowledged or rejected) or ``confirmed_submitted``.
        Everything else, an unreadable outbox included, is "unknown" with
        ``reconciliation_required``: never a guess."""
        sup = self.supervised
        report: dict[str, Any]
        try:
            if sup.reconciled(intent_id).exists():
                doc = json.loads(sup.reconciled(intent_id).read_bytes())
                verdict = doc.get("verdict")
                report = {"outbox_state": "reconciled", "verdict": verdict}
                if verdict == "confirmed_not_submitted":
                    report["order_sent"] = "no"
                elif verdict == "confirmed_submitted":
                    report["order_sent"] = "yes"
                    report["broker_order_id"] = doc.get("broker_order_id")
                else:
                    report.update(_UNKNOWN)
            elif sup.terminal(intent_id).exists():
                receipt = json.loads(sup.terminal(intent_id).read_bytes())
                outcome = str(receipt.get("outcome"))
                report = {"outbox_state": outcome, "receipt": receipt}
                if outcome in ("acknowledged", "rejected"):
                    report["order_sent"] = "yes"
                else:
                    report.update(_UNKNOWN)
            elif sup.sending(intent_id).exists():
                report = {"outbox_state": "sending", **_UNKNOWN}
            elif sup.pending(intent_id).exists():
                report = {"outbox_state": "pending", "order_sent": "no"}
            else:
                report = {"outbox_state": "absent", "order_sent": "no"}
        except (OSError, ValueError, AttributeError):
            report = {"outbox_state": "unreadable", **_UNKNOWN}
        # the journal's projection, as context only: order_sent never reads it
        try:
            report["execution_state"] = project_intent(sup, intent_id).state.value
        except (
            SupervisedRefused,
            ExecutionLifecycleError,
            OSError,
            ValueError,
            AttributeError,
            KeyError,
            TypeError,
        ):
            report["execution_state"] = None
        return report

    def _handle(self, claimed: Path, name: str) -> dict[str, Any]:
        now = self.clock()
        base: dict[str, Any] = {"schema": RESULT_SCHEMA, "at": now.isoformat(), "request": name}
        try:
            request = EntryRequest.model_validate(json.loads(claimed.read_bytes()))
        except (ValueError, OSError) as error:
            return {**base, "status": "invalid", "reason": repr(error)}
        if request.effect.intent_id != name:
            return {**base, "status": "invalid", "reason": "file name is not the intent id"}
        try:
            return {**base, "intent_id": name, **self.enter(request, now)}
        except SupervisedRefused as refused:
            return {
                **base,
                "intent_id": name,
                "status": "refused",
                "reason": refused.reason,
                "detail": refused.detail,
            }

    def enter(self, request: EntryRequest, now: datetime) -> dict[str, Any]:
        """Kill files -> mandate -> preflight -> screening -> kill files ->
        intent -> permit -> register -> send. Every refusal happens before
        the send. ``PAUSE_ENTRIES`` / ``HALT`` in the run dir refuse before
        any authority is spent, and again after the screening (a stop
        placed while it read the broker); exits never pass through here."""
        effect = request.effect
        structure = effect.structure
        stopped = entry_send_blockers(self.paths.root)
        if stopped:
            return {"status": "blocked", "blockers": list(stopped)}
        if effect.side != "BUY":
            return {"status": "blocked", "blockers": ["credit_open_not_supported_v1"]}
        if (now - request.send_deadline).total_seconds() >= 0:
            return {"status": "blocked", "blockers": ["request_expired"]}
        if not self.profile_path().exists():
            return {"status": "blocked", "blockers": ["profile_absent"]}
        profile, digest = load_profile(self.profile_path())
        mandate = active_mandate(
            self.supervised,
            now=now,
            account_id=effect.account_id,
            owner_epoch=self.owner_epoch,
            strategy_version=request.strategy_version,
        )
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
            owner_epoch=self.owner_epoch,
            owner_healthy=self.ib.connected,
            assignment_plan_verified=plan_ok,
            protective_exit_ready=exit_owner_ready(self.paths, now),
            current_open_loss=open_loss,
            realized_daily_loss=day_loss,
        )
        screening = collect_canary_screening(
            self.broker,
            effect,
            profile=profile,
            mandate_account_id=mandate.account_id,
            inputs=inputs,
            clock=self.clock,
        )
        facts = {
            "screening_sha256": screening.screening_sha256,
            "assignment_plan": plan_why,
            "open_loss": None if open_loss is None else str(open_loss),
            "day_loss": None if day_loss is None else str(day_loss),
        }
        if not screening.clear or screening.facts is None:
            return {"status": "blocked", "blockers": list(screening.blockers), **facts}
        stopped = entry_send_blockers(self.paths.root)
        if stopped:
            return {"status": "blocked", "blockers": list(stopped), **facts}
        intent = SupervisedIntent(
            intent=OrderIntent(
                intent_id=effect.intent_id,
                contract_id=f"BAG:{structure.id}",
                side="BUY",
                position_effect="OPEN_LONG",
                quantity=effect.quantity,
                order_type="LIMIT",
                limit_price=effect.limit,
                execution_style="package",
                package_id=structure.id,
                intent_created_at=now,
                source=request.strategy_version,
                source_sequence_id=f"desk-{effect.intent_id}",
            ),
            package_intent_sha256=screening.facts.intent_sha256,
            created_at=now,
            send_deadline=request.send_deadline,
        )
        record_intent(self.supervised, intent)
        # Account-id pre-check: refuse BEFORE spending a permit. The mandate
        # already passed (the screening gate verified it); if the effect's
        # account diverged from that mandate (operator typo, replay, etc.)
        # we leak neither a permit nor a spec file.
        if mandate.account_id != effect.account_id:
            return {
                "status": "blocked",
                "blockers": ["register_account_mismatch"],
                "account_id": effect.account_id,
                "mandate_account_id": mandate.account_id,
                **facts,
            }
        permit = issue_permit(
            self.supervised,
            now=now,
            account_id=effect.account_id,
            owner_epoch=self.owner_epoch,
            intent=intent,
            canary_blockers=screening.blockers,
            effect_payload=payload,
            screening_sha256=screening.screening_sha256,
        )
        self.runtime.register(effect)
        receipt = send(
            self.supervised,
            now=now,
            permit_id=permit.permit_id,
            effect_payload=payload,
            broker=self.broker,
        )
        return {"status": "sent", "permit_id": permit.permit_id, "receipt": receipt, **facts}


# ---------------------------------------------------------------- the loop


def in_session(now: datetime) -> bool:
    local = now.astimezone(ET)
    return is_session(local) and SESSION_OPEN <= local.time() <= SESSION_END


def run_loop(
    desk: SupervisedDesk,
    *,
    interval_s: float,
    stop: Callable[[], bool],
    max_ticks: int | None = None,
) -> int:
    """Tick until ``stop()``; 6 when the gateway connection is lost.

    The loss is caught where it surfaces: at the loop top, or as an error
    out of the tick, the books, the commands, the inbox, the beat or the
    sleep (``DISCONNECT_ERRORS``, or anything once the session reads
    disconnected). Either way one ``gateway_lost`` event and exit 6; any
    other error propagates. A request or command claimed when the gateway
    went is answered at the next start, never re-sent.

    Order per iteration: tick (in session) or beat, books, commands, then
    the v1 inbox (in session). Commands run outside the session too: a
    kill switch never waits for the open (the entry kinds refuse there)."""
    ticks = 0
    while not stop():
        if not _connected(desk):
            return _gateway_lost(desk, "loop")
        stage = "clock"
        try:
            now = desk.clock()
            session = in_session(now)
            if session:
                # tick FIRST: the arm gate (exit_owner_ready) reads this beat,
                # so the first request after a restart is not refused for a
                # stale one
                stage = "tick"
                desk.runtime.tick()
            else:
                stage = "beat"
                desk.runtime.beat()
            stage = "books"
            desk.books_step()
            stage = "commands"
            desk.process_commands()
            if session:
                stage = "requests"
                desk.process_requests()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            stage = "sleep"
            desk.ib.sleep(interval_s)
        except Exception as error:
            if isinstance(error, DISCONNECT_ERRORS) or not _connected(desk):
                return _gateway_lost(desk, stage, error)
            raise
    return 0


def _connected(desk: SupervisedDesk) -> bool:
    try:
        return bool(desk.ib.connected)
    except Exception:  # an unreadable session state is not a live session
        return False


def _gateway_lost(desk: SupervisedDesk, stage: str, error: BaseException | None = None) -> int:
    """Record the loss and return the restart code. The error CLASS only
    (as desk_runtime does): broker errors can carry account details. The
    record is best-effort, so a failed write cannot turn exit 6 into a
    crash."""
    detail: dict[str, str] = {"stage": stage}
    if error is not None:
        detail["error"] = type(error).__name__
    log.error("gateway lost (%s)", ", ".join(f"{k}={v}" for k, v in sorted(detail.items())))
    try:
        desk.runtime._event("gateway_lost", **detail)
    except Exception as failure:
        log.error("gateway_lost event not recorded: %s", type(failure).__name__)
    return EXIT_DISCONNECTED


def _dividend_source() -> DividendSource:
    from tree_options.desk.dividends import load_snapshot
    from tree_options.trex.clock import session_calendar

    cal = session_calendar()
    return lambda symbol, as_of: load_snapshot(symbol, as_of, cal)


def qualify_owned_account(
    desk: SupervisedDesk,
    fences: Sequence[Any],
    *,
    account_id: str,
    account_alias: str | None,
    output: Path,
) -> dict[str, Any]:
    """One explicit read-only assessment using this owner's held account fence."""
    from tree_options.trex.ibkr_paper_qualification import qualify_read_only

    if not account_alias or account_alias.casefold() == account_id.casefold():
        raise ValueError("qualification requires a private configured account alias")
    matching = [fence for fence in fences if fence.alias == account_alias and fence.held]
    if len(matching) != 1:
        raise ValueError("qualification requires exactly one held alias fence")
    return qualify_read_only(
        desk, matching[0], account_id=account_id, account_alias=account_alias, output=output
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tree_options.trex.supervised_desk")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="the supervised desk process (clientId 83)")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=GATEWAY_PAPER_PORT)
    run.add_argument("--interval", type=float, default=5.0)
    run.add_argument(
        "--qualification-receipt",
        type=Path,
        default=None,
        help="explicit one-shot read-only receipt output directory (separate from owner state)",
    )
    run.add_argument(
        "--qualification-account",
        default=None,
        help="exact existing paper account for the optional owner-process qualification",
    )
    run.add_argument(
        "--legacy-plan",
        type=Path,
        action="append",
        default=[],
        help="a legacy trex plan whose book counts toward open/day loss",
    )
    run.add_argument(
        "--no-spots",
        action="store_true",
        help="no Polygon spot feed (touch/breach exits cannot fire)",
    )
    digest = sub.add_parser(
        "profile-digest", help="print the digest a mandate must bind (no broker contact)"
    )
    digest.add_argument(
        "--path", type=Path, default=None, help="profile.json (default: the desk run dir's)"
    )
    args = ap.parse_args(argv)
    if args.command == "profile-digest":
        path = args.path or DeskPaths.default().root / "profile.json"
        try:
            profile, value = load_profile(path)
        except (OSError, ValueError, KeyError) as error:
            print(f"refused: invalid_profile {error!r}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "profile_id": profile.profile_id,
                    "revision": profile.revision,
                    "profile_digest": value,
                },
                indent=2,
            )
        )
        return 0
    if bool(args.qualification_receipt) != bool(args.qualification_account):
        ap.error("--qualification-receipt and --qualification-account must be supplied together")
    if args.qualification_receipt and not os.environ.get("TREX_IBKR_ACCOUNT_ALIAS"):
        ap.error("qualification requires TREX_IBKR_ACCOUNT_ALIAS on the existing owner")
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
    runtime = DeskRuntime(
        ib, paths, supervised=supervised, spots=spots, dividends=dividends, notify=notify_fn
    )
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
        _shutdown(account_fences, runtime, ib)
        raise
    desk = SupervisedDesk(
        ib,
        runtime,
        IbkrSupervisedBroker(ib),
        supervised=supervised,
        owner_epoch=epoch,
        dividends=dividends,
        legacy=[
            LegacyBook(p, Path(os.environ.get("TREX_STATE", Path.home() / ".local/state/trex")))
            for p in args.legacy_plan
        ],
    )
    desk.write_owner()
    log.info("supervised desk up: owner epoch %s (grant the mandate for it)", epoch)
    try:
        started = desk.start()
        log.info(
            "approvals invalidated: %d mandate(s), %d stale proposal(s); %d command(s) recovered",
            len(started["retired_mandates"]),
            len(started["stale_proposals"]),
            len(started["recovered_commands"]),
        )
    except Exception as error:
        # the exit owner must still come up; entries stay refused (each
        # approval is checked against the live epoch anyway)
        log.error("owner start bookkeeping failed: %s", type(error).__name__)
    stopping = False

    def _stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        try:
            desk.recover_interrupted_requests()
        except Exception as error:
            # reporting only: the exit owner must still come up. The claims
            # stay unanswered (and are never re-sent); the next start retries.
            log.error("interrupted-request recovery failed: %s", type(error).__name__)
        if args.qualification_receipt is not None:
            try:
                receipt = qualify_owned_account(
                    desk,
                    account_fences,
                    account_id=args.qualification_account,
                    account_alias=alias,
                    output=args.qualification_receipt,
                )
                log.info("IBKR read-only qualification: %s", receipt["verdict"])
            except Exception as error:
                # Qualification cannot take exit ownership down; no raw provider
                # exception/account information is written to service logs.
                log.warning("IBKR read-only qualification unavailable: %s", type(error).__name__)
        return run_loop(desk, interval_s=args.interval, stop=lambda: stopping)
    finally:
        _shutdown(account_fences, runtime, ib)


def _shutdown(fences: Sequence[Any], runtime: DeskRuntime, ib: IbkrTrex) -> None:
    """Close the account fences, release the run dir, disconnect; each step
    isolated, so a failure (a socket already gone, a lock file vanished)
    neither skips the next step nor masks the loop's exit code (6 after a
    lost gateway) or its exception. The error class only."""
    steps: list[tuple[str, Callable[[], None]]] = [
        *(("account fence close", fence.close) for fence in fences),
        ("runtime release", runtime.release),
        ("gateway disconnect", ib.disconnect),
    ]
    for what, step in steps:
        try:
            step()
        except Exception as error:
            log.warning("%s failed: %s", what, type(error).__name__)


if __name__ == "__main__":
    sys.exit(main())
