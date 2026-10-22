"""Owner-side books: broker facts into the ledger, and the owner's status files.

Runs INSIDE the supervised owner (``trex.supervised_desk``, clientId 83),
over the one broker session, and is the only writer of these files
(WAVE2-CONTRACTS.md C2.4-C2.6, ``contracts.layout``):

| File | When | Producer |
| --- | --- | --- |
| ``ledger/executions.jsonl`` | every step: each distinct broker report once | ``ledger.append_executions`` |
| ``ledger/events.jsonl`` | every step: new fills, corrections, late commissions | ``ledger.fill_events`` / ``append_ledger`` |
| ``ledger/holdings.json`` | when the ledger moved, and with the status files | ``ledger.write_holdings`` (``trex-holdings/1``) |
| ``ledger/reconciliation.json`` | every ``STATUS_INTERVAL_S``, at once after a (re)connect or on demand | ``reconcile.reconcile_ledger`` |
| ``ledger/risk-snapshot.json`` | with the reconciliation (same broker read) | ``risk_snapshot.build_risk_snapshot`` |

Rules:

- **Append-only and idempotent.** Replayed broker facts (the same exec id,
  a commission report that arrives late on a copy of the execution) are
  no-op appends; a correction re-report books its adjustment once.
- **History.** IBKR reports only the current session's executions, so the
  fold reads ``executions.jsonl`` (every report this desk ever recorded),
  never just today's view.
- **Attribution** comes from the proposal each sent intent's per-trade
  mandate names (``mandates/<intent_id>.consumed.json`` -> its
  ``proposal_id``) and from the desk specs (exits are tagged by structure).
  A consumed mandate whose proposal cannot be read stops booking for the
  step (``attribution_incomplete``): a fill booked under the wrong owner can
  never be re-booked, so it waits instead.
- **Fail closed.** An unreadable execution view, an execution the contracts
  cannot represent, a conflicting broker report or ledger, an unreadable
  position view or settled cash: the reconciliation is ``unavailable`` (or
  ``reconciliation_required`` when the views read and disagree) and the risk
  snapshot ``unavailable``. Both block ENTRIES only (``trex.admission``);
  the exit lane never reads them.
- **Never raises** into the owner loop for a books problem: every problem is
  a short code in the step's outcome and a ``books_problem`` event (the
  class, never a broker message). A lost session is the loop's business.

Status transitions are desk events (``reconciliation_clean`` /
``reconciliation_required``, the latter also for ``unavailable``) and a
``reconciliation`` ledger marker binding the document's hash.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.mandates import SupervisedMandateV2
from tree_options.trex.contracts.proposals import EntryProposal, proposal_bytes
from tree_options.trex.contracts.risk import Reconciliation, RiskSnapshot
from tree_options.trex.contracts.vocabulary import DeskEvent
from tree_options.trex.fills import FillConflict, fold_executions
from tree_options.trex.ledger import (
    AnyDeskSpec,
    AttributionIndex,
    LedgerConflict,
    LedgerFold,
    append_executions,
    append_ledger,
    fill_events,
    fold_ledger,
    read_executions,
    read_ledger,
    symbols_from_executions,
    write_holdings,
)
from tree_options.trex.reconcile import reconcile_ledger, reconciliation_event, write_reconciliation
from tree_options.trex.risk_snapshot import (
    SETTLED_CASH_TAG,
    build_risk_snapshot,
    write_risk_snapshot,
)
from tree_options.trex.supervised import SupervisedPaths

log = logging.getLogger("trex.desk_books")

__all__ = [
    "BOOKS_PROBLEM_EVENT",
    "STATUS_INTERVAL_S",
    "BooksStep",
    "DeskBooks",
    "approved_proposals",
]

#: the reconciliation and risk snapshot are rewritten at least this often
#: (both go stale for admission after 120 s: two chances per window)
STATUS_INTERVAL_S = 60
#: the desk event a books problem is recorded as (class codes only)
BOOKS_PROBLEM_EVENT = "books_problem"
_CONSUMED = ".consumed.json"

EventFn = Callable[..., None]
SpecsFn = Callable[[], Iterable[AnyDeskSpec]]


@dataclass(frozen=True)
class BooksStep:
    """What one books step did."""

    account_id: str | None
    executions_recorded: int = 0
    events_appended: int = 0
    published: bool = False
    reconciliation: str | None = None
    risk_snapshot: str | None = None
    problems: tuple[str, ...] = ()


@dataclass
class _Ingest:
    fold: LedgerFold | None = None
    appended: int = 0
    recorded: int = 0
    trusted: bool = True  # the execution view read completely this step
    problems: list[str] = field(default_factory=list)


def approved_proposals(
    run_dir: Path, supervised: SupervisedPaths
) -> tuple[list[EntryProposal], list[str]]:
    """The proposal behind every CONSUMED per-trade mandate (every intent
    that may have reached the broker), and the intent ids whose proposal
    could not be read (their fills must not be booked yet)."""
    layout = Wave2RunDir(run_dir)
    directory = supervised.entry_mandates()
    proposals: list[EntryProposal] = []
    missing: list[str] = []
    if not directory.is_dir():
        return proposals, missing
    for path in sorted(directory.glob("*" + _CONSUMED)):
        intent_id = path.name.removesuffix(_CONSUMED)
        try:
            mandate = SupervisedMandateV2.model_validate_json(path.read_bytes())
            raw = layout.proposal(mandate.proposal_id).read_bytes()
            proposal = EntryProposal.model_validate_json(raw)
        except (OSError, ValidationError, ValueError):
            missing.append(intent_id)
            continue
        if proposal_bytes(proposal) != raw or proposal.effect.intent_id != mandate.intent_id:
            missing.append(intent_id)
            continue
        proposals.append(proposal)
    return proposals, missing


class DeskBooks:
    """The owner's books over one broker session (``IbkrTrex``)."""

    def __init__(
        self,
        ib: Any,
        run_dir: Path,
        supervised: SupervisedPaths,
        *,
        specs: SpecsFn,
        clock: Callable[[], datetime],
        event: EventFn | None = None,
        account_id: str | None = None,
        interval_s: int = STATUS_INTERVAL_S,
    ) -> None:
        self.ib = ib
        self.layout = Wave2RunDir(Path(run_dir))
        self.supervised = supervised
        self.specs = specs
        self.clock = clock
        self.event = event
        self.interval_s = interval_s
        self._account = account_id
        self._force = True  # the first step after start publishes at once
        self._last_published: datetime | None = None
        self._last_status: str | None = None
        self._fold: LedgerFold | None = None
        self._noted: set[str] = set()
        self.last: BooksStep | None = None

    # -- the account ------------------------------------------------------------

    def account_id(self) -> str | None:
        """The one paper account these books keep: the configured one, else
        the session's sole managed account; None when ambiguous or unread."""
        if self._account is not None:
            return self._account
        try:
            accounts = sorted({str(a) for a in self.ib._ib.managedAccounts()})
        except Exception:  # an unreadable account list binds nothing
            return None
        if len(accounts) == 1:
            self._account = accounts[0]
        return self._account

    # -- cadence ----------------------------------------------------------------

    def request_refresh(self) -> None:
        """Publish the status files at the next step (the reconnect hook)."""
        self._force = True

    def due(self, now: datetime) -> bool:
        if self._force or self._last_published is None:
            return True
        age = (now - self._last_published).total_seconds()
        return age < 0 or age >= self.interval_s

    @property
    def fold(self) -> LedgerFold | None:
        """The ledger as of the last successful ingest (None: not trusted)."""
        return self._fold

    # -- the step ---------------------------------------------------------------

    def step(self, now: datetime | None = None, *, force: bool = False) -> BooksStep:
        """Ingest broker facts; publish the status files when due (or forced)."""
        now = (now or self.clock()).astimezone(UTC)
        account = self.account_id()
        if account is None:
            outcome = BooksStep(account_id=None, problems=("account_unbound",))
            self._note_problems(outcome.problems)
            self.last = outcome
            return outcome
        ingest = self._ingest(account, now)
        published = force or self.due(now)
        reconciliation: Reconciliation | None = None
        snapshot: RiskSnapshot | None = None
        if published:
            reconciliation, snapshot = self._publish(account, now, ingest)
            self._force = False
            self._last_published = now
        elif ingest.appended:
            self._write_holdings(account, now, ingest)
        outcome = BooksStep(
            account_id=account,
            executions_recorded=ingest.recorded,
            events_appended=ingest.appended,
            published=published,
            reconciliation=None if reconciliation is None else reconciliation.status,
            risk_snapshot=None if snapshot is None else snapshot.status,
            problems=tuple(ingest.problems),
        )
        self._note_problems(outcome.problems)
        self.last = outcome
        return outcome

    # -- ingest -------------------------------------------------------------------

    def _ingest(self, account: str, now: datetime) -> _Ingest:
        out = _Ingest()
        view = self.ib.executions(account)
        if view.error is not None:
            out.trusted = False
            out.problems.append(f"executions_unavailable:{view.error}")
        elif view.unrepresentable:
            out.trusted = False
            out.problems.extend(f"execution_{code}" for code in view.unrepresentable)
        try:
            if view.rows:
                out.recorded = append_executions(self.layout.broker_executions(), view.rows)
            history = read_executions(self.layout.broker_executions())
            book = fold_executions(history)
            proposals, missing = approved_proposals(self.layout.root, self.supervised)
            existing = read_ledger(self.layout.ledger_events())
            if missing:
                out.problems.append("attribution_incomplete")
                out.trusted = False
                self._fold = fold_ledger(existing)
                out.fold = self._fold
                return out
            index = AttributionIndex.build(proposals, self.specs())
            fresh = fill_events(book, index, recorded_at=now, existing=existing)
            events = existing
            if fresh:
                result = append_ledger(self.layout.ledger_events(), fresh)
                out.appended = len(result.appended)
                events = result.events
            self._fold = fold_ledger(events)
            out.fold = self._fold
        except (FillConflict, LedgerConflict) as error:
            out.trusted = False
            out.problems.append(f"books_conflict:{type(error).__name__}")
            self._fold = None
        except (OSError, ValueError) as error:
            out.trusted = False
            out.problems.append(f"books_unreadable:{type(error).__name__}")
            self._fold = None
        return out

    def _symbols(self) -> dict[int, str]:
        return symbols_from_executions(read_executions(self.layout.broker_executions()))

    def _write_holdings(self, account: str, now: datetime, ingest: _Ingest) -> None:
        if ingest.fold is None:
            return
        try:
            write_holdings(
                self.layout, ingest.fold, account_id=account, symbols=self._symbols(), as_of=now
            )
        except (LedgerConflict, OSError, ValueError) as error:
            ingest.problems.append(f"holdings_unwritten:{type(error).__name__}")

    # -- the status files ---------------------------------------------------------

    def _publish(
        self, account: str, now: datetime, ingest: _Ingest
    ) -> tuple[Reconciliation, RiskSnapshot]:
        problems = ingest.problems
        portfolio = self.ib.portfolio(as_of=now, account=account)
        rows = portfolio.rows if portfolio.complete else None
        if portfolio.error is not None:
            problems.append(f"positions_unavailable:{portfolio.error}")
        elif portfolio.unrepresentable:
            problems.extend(f"position_{code}" for code in portfolio.unrepresentable)
        try:  # SettledCash, else TotalCashValue (the preflight's rule)
            cash = self.ib.account_cash(account)
        except Exception as error:  # unreadable account values prove nothing
            if not self._session_up():
                raise
            problems.append(f"settled_cash_unavailable:{type(error).__name__}")
            cash = None
        fold = ingest.fold
        if fold is None or not ingest.trusted or rows is None:
            reconciliation = Reconciliation(account_id=account, status="unavailable", as_of=now)
        else:
            reconciliation = reconcile_ledger(fold, account_id=account, as_of=now, broker_rows=rows)
        snapshot: RiskSnapshot
        if fold is None or not ingest.trusted:
            snapshot = RiskSnapshot(
                account_id=account,
                as_of=now,
                status="unavailable",
                open_loss_cents=None,
                day_realized_loss_cents=None,
                cash_cents=None,
                equity_gross_cents=None,
            )
        else:
            try:
                specs = list(self.specs())
            except (OSError, ValueError, KeyError, TypeError) as error:
                problems.append(f"specs_unreadable:{type(error).__name__}")
                specs = None
            if specs is None:
                snapshot = RiskSnapshot(
                    account_id=account,
                    as_of=now,
                    status="unavailable",
                    open_loss_cents=None,
                    day_realized_loss_cents=None,
                    cash_cents=None,
                    equity_gross_cents=None,
                )
            else:
                build = build_risk_snapshot(
                    account_id=account,
                    as_of=now,
                    fold=fold,
                    specs=specs,
                    broker_rows=rows,
                    settled_cash_cents=None if cash is None else cash.cents,
                    cash_source=SETTLED_CASH_TAG if cash is None else cash.source,
                )
                snapshot = build.snapshot
                problems.extend(f"risk:{p}" for p in build.problems if p)
        try:
            write_reconciliation(self.layout, reconciliation)
            write_risk_snapshot(self.layout, snapshot)
        except OSError as error:
            problems.append(f"status_unwritten:{type(error).__name__}")
        self._write_holdings(account, now, ingest)
        self._transition(reconciliation, now)
        return reconciliation, snapshot

    def _transition(self, reconciliation: Reconciliation, now: datetime) -> None:
        """One desk event and one ledger marker per status change."""
        status = reconciliation.status
        if status == self._last_status:
            return
        self._last_status = status
        try:
            append_ledger(
                self.layout.ledger_events(), [reconciliation_event(reconciliation, recorded_at=now)]
            )
        except (LedgerConflict, OSError, ValueError) as error:
            log.error("reconciliation marker not booked: %s", type(error).__name__)
        name = (
            DeskEvent.RECONCILIATION_CLEAN
            if status == "clean"
            else DeskEvent.RECONCILIATION_REQUIRED
        )
        self._emit(
            name.value,
            source="books",
            status=status,
            mismatches=[m.model_dump(mode="json") for m in reconciliation.mismatches],
        )

    # -- reporting ----------------------------------------------------------------

    def _session_up(self) -> bool:
        try:
            return bool(self.ib.connected)
        except Exception:  # an unreadable session state is not a live session
            return False

    def _note_problems(self, problems: Iterable[str]) -> None:
        """One ``books_problem`` event per distinct problem code per process."""
        for code in problems:
            if code in self._noted:
                continue
            self._noted.add(code)
            log.warning("books: %s", code)
            self._emit(BOOKS_PROBLEM_EVENT, problem=code)

    def _emit(self, name: str, **payload: Any) -> None:
        if self.event is None:
            return
        try:
            self.event(name, **payload)
        except Exception:  # a record failure never breaks the owner loop
            log.exception("books event %s not recorded", name)
