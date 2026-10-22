"""The owner's entry flows: compose, approve, reject (Wave 2b lane F).

The integration seam between the merged Wave 2 parts, run by the single
supervised owner (``trex.supervised_desk``) for the ``compose_entry``,
``approve_entry`` and ``reject_entry`` commands (``trex.desk_commands``).
PAPER ONLY: the effects bind IBKR paper accounts, the mandate is
``ibkr-paper``, and nothing here adds a live path.

**Compose** (one candidate -> one ``trex-entry-proposal/1``):

1. inside the regular session; no ``PAUSE_ENTRIES`` / ``HALT``
   (``admission.entry_send_blockers``); the operator profile reads;
2. the candidate is a ``trex-entry-proposal-draft/1`` in
   ``proposal-drafts/<candidate_id>.json`` (lane A writes the miner's option
   drafts; ``equity_draft`` builds an equity one), of the command's program
   and asset kind, rebound to the desk's account, inside its horizon;
3. fresh books (``DeskBooks.step(force=True)``), then the owner's facts
   (``admission.owner_fact_blockers``: risk snapshot, cash, reconciliation,
   exit owner);
4. the stock/option wire preflight (``IbkrSupervisedBroker.preflight``:
   allowlist, qualification, real-time quote, cash) and the kind-aware
   screening (``supervised_canary.collect_entry_screening``) with this
   desk's entries this session and its pending-entry reservation;
5. the proposal (``enter_supervised.complete_draft`` with the live quote,
   screening digest, owner epoch and profile digest) is written once, in its
   canonical bytes, to ``proposals/<proposal_id>.json``.

**Approve** (exactly one send per proposal):

1. the proposal exists, is canonical and not rejected (``proposal_unknown``
   / ``proposal_rejected``);
2. a proposal that was already sent answers the stored result of the
   approval that sent it (nothing is re-sent); one whose approval was spent
   without a send refuses ``mandate_already_consumed``;
3. ``contracts.approval_blockers`` against the LIVE owner epoch and profile
   digest; the kill files; fresh books and the owner's facts; preflight and
   screening again (the facts may have moved since compose);
4. ``supervised.grant_entry_mandate`` (or the live, unconsumed mandate an
   earlier approval of the same proposal left when a kill file stopped it
   before its permit); the kill files again; intent; permit (consumes the
   mandate); ``DeskRuntime.register``; the kill files once more; ``send``.

**Reject**: a ``proposals/<proposal_id>.rejected.json`` tombstone, and an
unconsumed per-trade mandate for that proposal is retired. A sent proposal
cannot be rejected (``mandate_already_consumed``).

**Pending-entry reservation.** An entry sent but not (fully) filled is not
in the broker-priced risk snapshot yet. Its unfilled part (the spec's
quantity minus what the ledger booked for the intent) is reserved at its
max loss (equities: the contract arithmetic at the entry limit; options:
the defined max loss) and its cash, so two approvals back to back cannot
both pass when only one fits. The option screen counts open loss from the
desk book (``SupervisedDesk.loss_facts``), which reserves every entry-lane
structure at full size already.

**Entries per session** (both kinds): intents the supervised outbox shows
sent on the session's ET date, a broker rejection or a reconciliation
proving no submission excepted; unreadable -> unknown, which blocks.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tree_options.execution import OrderIntent
from tree_options.time.sessions import shift_instant
from tree_options.trex import supervised as supervised_core
from tree_options.trex.admission import entry_send_blockers, owner_fact_blockers
from tree_options.trex.clock import ET, session_calendar
from tree_options.trex.contracts.canonical import model_document
from tree_options.trex.contracts.commands import (
    AnyCommand,
    ApproveEntryCommand,
    CommandResult,
    ComposeEntryCommand,
    RejectEntryCommand,
)
from tree_options.trex.contracts.defaults import EQUITY_GAP_RESERVE_BPS, PROPOSAL_MAX_TTL_S
from tree_options.trex.contracts.effects import (
    EFFECT_V1_ADAPTER,
    SupervisedEffect,
    SupervisedEquityEffect,
    effect_v1_bytes,
    effect_v1_sha256,
    equity_max_loss_cents,
    equity_notional_cents,
)
from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.mandates import SupervisedMandateV2, approval_blockers
from tree_options.trex.contracts.proposals import (
    PROPOSAL_SCHEMA,
    EntryProposal,
    Quote,
    proposal_bytes,
    proposal_sha256,
)
from tree_options.trex.contracts.vocabulary import Blocker, DeskEvent
from tree_options.trex.desk_commands import (
    ANSWERED_BY,
    Outcome,
    Status,
    actor_label,
    applied,
    failed,
    refused,
    rename_noreplace,
)
from tree_options.trex.desk_runtime import assignment_plan, exit_owner_ready
from tree_options.trex.desk_spec import DeskSpec
from tree_options.trex.equity_exits import in_regular_session
from tree_options.trex.ibkr import market_data_type_name
from tree_options.trex.ledger import LedgerFold
from tree_options.trex.risk_snapshot import option_open_loss_cents
from tree_options.trex.state import ENTRY_LANE, BookState
from tree_options.trex.supervised import (
    ApprovalRefused,
    SupervisedIntent,
    SupervisedPaths,
    SupervisedRefused,
)
from tree_options.trex.supervised_canary import (
    CanaryScreening,
    EquityScreening,
    EquityScreeningInputs,
    OperatorCanaryInputs,
    collect_entry_screening,
)

if TYPE_CHECKING:
    from tree_options.trex.contracts.desk_spec_v2 import EquityDeskSpecV2
    from tree_options.trex.supervised_desk import SupervisedDesk

__all__ = [
    "DEFAULT_SEND_TTL_S",
    "REJECTION_SCHEMA",
    "OwnerEntries",
    "Reservation",
    "entries_this_session",
    "equity_draft",
    "equity_loss_cents",
    "pending_reservation",
]

#: a composed proposal may be approved and sent for this long (capped by the
#: candidate's horizon and the session close; the contract allows 900 s)
DEFAULT_SEND_TTL_S = 600
REJECTION_SCHEMA = "trex-entry-rejection/1"
MAX_FILE_BYTES = 256 * 1024
_PATH_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
#: outbox file suffixes of the supervised core (``SupervisedPaths``)
_TERMINAL, _SENDING, _RECONCILED = ".terminal.json", ".sending.json", ".reconciled.json"
_OPTION_MULTIPLIER = 100
_CENTS = Decimal(100)

Effect = SupervisedEffect | SupervisedEquityEffect


# ------------------------------------------------------------- shared facts


def entries_this_session(supervised: SupervisedPaths, session: date) -> int | None:
    """Entries SENT on the ET date ``session`` (both kinds): every outbox
    intent whose send started that day (``sending`` claim or terminal
    receipt), except a broker rejection or a reconciled
    ``confirmed_not_submitted``. None when the outbox cannot be read."""
    outbox = supervised.outbox_dir()
    if not outbox.is_dir():
        return 0
    counted: set[str] = set()
    try:
        for path in outbox.iterdir():
            name = path.name
            if name.endswith(_TERMINAL):
                doc = json.loads(path.read_bytes())
                if doc.get("outcome") == "rejected":
                    continue
                at = datetime.fromisoformat(str(doc["at"]))
                intent = name.removesuffix(_TERMINAL)
            elif name.endswith(_SENDING):
                doc = json.loads(path.read_bytes())
                at = datetime.fromisoformat(str(doc["claimed_at"]))
                intent = name.removesuffix(_SENDING)
            else:
                continue
            if at.tzinfo is None or at.astimezone(ET).date() != session:
                continue
            counted.add(intent)
        for intent in sorted(counted):
            reconciled = supervised.reconciled(intent)
            if reconciled.exists():
                verdict = json.loads(reconciled.read_bytes()).get("verdict")
                if verdict == "confirmed_not_submitted":
                    counted.discard(intent)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return len(counted)


@dataclass(frozen=True)
class Reservation:
    """What sent-but-unfilled entries hold back from screening (cents)."""

    loss_cents: int = 0
    cash_cents: int = 0
    intents: tuple[str, ...] = ()


def equity_loss_cents(
    quantity: int,
    price_cents: int,
    stop_cents: int,
    gap_reserve_bps: int = EQUITY_GAP_RESERVE_BPS,
) -> int:
    """The contract's equity max loss (``equity_max_loss_cents``) for
    ``quantity`` shares at ``price_cents``: the stop loss, or the gap
    reserve (rounded up) when larger."""
    stop_loss = quantity * (price_cents - stop_cents)
    gap = -(-quantity * price_cents * gap_reserve_bps // 10_000)
    return max(stop_loss, gap, 0)


def _ledger_quantity(fold: LedgerFold | None, intent_id: str, con_ids: set[int]) -> int | None:
    """Units the ledger booked for the intent (shares, or option packages =
    the largest leg); None when the ledger is not trusted."""
    if fold is None:
        return None
    moved = [
        abs(lot.quantity)
        for lot in fold.lots
        if lot.attribution.intent_id == intent_id and (not con_ids or lot.con_id in con_ids)
    ]
    return max(moved, default=0)


def pending_reservation(
    book: BookState,
    options: Mapping[str, DeskSpec],
    equities: Mapping[str, EquityDeskSpecV2],
    fold: LedgerFold | None,
    *,
    gap_reserve_bps: int = EQUITY_GAP_RESERVE_BPS,
) -> Reservation:
    """The unfilled part of every entry-lane structure (PLANNED or
    ENTER_WORKING) at its max loss and cash. Without a trusted ledger the
    whole entry is reserved."""
    loss = cash = 0
    intents: list[str] = []
    for sid, equity in sorted(equities.items()):
        st = book.structures.get(sid)
        if st is None or st.status not in ENTRY_LANE:
            continue
        pos = equity.position
        booked = _ledger_quantity(fold, equity.intent_id, {pos.con_id}) or 0
        unfilled = max(pos.quantity - booked, 0)
        if not unfilled:
            continue
        intents.append(equity.intent_id)
        loss += equity_loss_cents(
            unfilled, pos.entry_limit_cents, pos.exit_policy.stop_price_cents, gap_reserve_bps
        )
        cash += unfilled * pos.entry_limit_cents
    for sid, option in sorted(options.items()):
        st = book.structures.get(sid)
        if st is None or st.status not in ENTRY_LANE:
            continue
        structure = option.structure
        booked = _ledger_quantity(fold, option.intent_id, set()) or 0
        unfilled = max(structure.quantity - booked, 0)
        if not unfilled:
            continue
        intents.append(option.intent_id)
        loss += option_open_loss_cents(structure, unfilled)
        if not structure.is_credit:
            dollars = structure.limit * _OPTION_MULTIPLIER * unfilled
            cash += int((dollars * _CENTS).to_integral_value(rounding=ROUND_CEILING))
    return Reservation(loss_cents=loss, cash_cents=cash, intents=tuple(intents))


def equity_draft(
    effect: SupervisedEquityEffect,
    *,
    program_id: str,
    strategy_version: str,
    valid_until: datetime,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """A ``trex-entry-proposal-draft/1`` for an equity candidate (the shape
    lane A's option drafts have): every live binding null, the reviewed
    notional and max loss from the contract arithmetic. The owner composes
    it into a proposal with live facts."""
    from tree_options.desk.enter_supervised import DRAFT_SCHEMA, LIVE_BINDINGS

    if valid_until.tzinfo is None:
        raise ValueError("valid_until must be timezone-aware")
    return {
        "schema": DRAFT_SCHEMA,
        "draft_of": PROPOSAL_SCHEMA,
        "candidate_id": candidate_id or effect.intent_id,
        "kind": "equity",
        "valid_until": valid_until.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "execution_enabled": False,
        "missing": list(LIVE_BINDINGS),
        "body": {
            "schema": PROPOSAL_SCHEMA,
            "effect": model_document(effect),
            "effect_sha256": effect_v1_sha256(effect),
            "strategy_version": strategy_version,
            "program_id": program_id,
            "sleeve_ref": None,
            "proposed_at": None,
            "send_deadline": None,
            "review": {
                "quote": None,
                "max_loss_cents": equity_max_loss_cents(effect),
                "notional_cents": equity_notional_cents(effect),
                "screening_sha256": None,
                "owner_epoch": None,
                "profile_digest": None,
            },
        },
    }


def _read_bounded(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError(f"{path.name} is a link")
    with open(path, "rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} is too large")
    return raw


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class _Screened:
    blockers: tuple[str, ...]
    screening_sha256: str
    package_sha256: str
    quote: Quote | None
    entries: int | None
    reservation: Reservation


# ------------------------------------------------------------- the flows


class OwnerEntries:
    """``EntryFlows`` for one supervised desk (see the module docstring)."""

    def __init__(self, desk: SupervisedDesk) -> None:
        self.desk = desk

    # -- shorthand --------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self.desk.paths.root

    @property
    def layout(self) -> Wave2RunDir:
        return Wave2RunDir(self.root)

    @property
    def sup(self) -> SupervisedPaths:
        return self.desk.supervised

    def _event(self, name: str, **payload: Any) -> None:
        self.desk.runtime._event(name, **payload)

    # -- reads ------------------------------------------------------------------------

    def proposal(self, proposal_id: str) -> EntryProposal | None:
        """The stored proposal: canonical bytes under its own id, else None."""
        path = self.layout.proposal(proposal_id)
        try:
            raw = _read_bounded(path)
            proposal = EntryProposal.model_validate_json(raw)
        except (OSError, ValidationError, ValueError):
            return None
        if proposal.proposal_id != proposal_id or proposal_bytes(proposal) != raw:
            return None
        return proposal

    def _profile(self) -> tuple[Any, str] | Outcome:
        from tree_options.trex.supervised_desk import load_profile

        path = self.desk.profile_path()
        if not path.exists():
            return refused("profile_absent")
        try:
            return load_profile(path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            return refused("profile_invalid", type(error).__name__)

    def _gates(self, now: datetime) -> Outcome | None:
        if not in_regular_session(now):
            return refused("outside_regular_session")
        stopped = entry_send_blockers(self.root)
        if stopped:
            return refused(stopped[0], blockers=list(stopped))
        return None

    def _intent_used(self, effect: Effect) -> str | None:
        """Why the effect's intent can never be sent again (None: fresh)."""
        iid = effect.intent_id
        sup = self.sup
        for path in (sup.pending(iid), sup.sending(iid), sup.terminal(iid), sup.reconciled(iid)):
            if path.exists():
                return f"outbox:{path.name}"
        if sup.entry_mandate_consumed(iid).exists():
            return "mandate_consumed"
        return self._registration_conflict(effect)

    def _registration_conflict(self, effect: Effect) -> str | None:
        """A spec already registered under the effect's spec id for ANOTHER
        intent (the runtime would refuse it after the permit is spent)."""
        sid = (
            effect.intent_id if isinstance(effect, SupervisedEquityEffect) else effect.structure.id
        )
        path = self.desk.paths.spec(sid)
        if not path.exists():
            return None
        try:
            doc = json.loads(_read_bounded(path))
        except (OSError, ValueError):
            return f"spec_unreadable:{sid}"
        owner = doc.get("intent_id") if isinstance(doc, dict) else None
        return None if owner == effect.intent_id else f"spec_registered:{sid}"

    # -- screening ------------------------------------------------------------------

    def _reservation(self) -> Reservation:
        options, equities = self.desk.runtime.owned_specs()
        book = BookState.load(self.desk.paths.book(), [*options, *equities])
        return pending_reservation(book, options, equities, self.desk.books.fold)

    def _screen(self, effect: Effect, profile: Any, account: str, now: datetime) -> _Screened:
        """Preflight (the wire's own blockers first), then E's screening
        with the session count and the pending-entry reservation."""
        desk = self.desk
        payload = effect_v1_bytes(effect)
        session = now.astimezone(ET).date()
        entries = entries_this_session(self.sup, session)
        unreadable: tuple[str, ...] = ()
        try:
            reservation = self._reservation()
        except (OSError, ValueError, KeyError, TypeError):
            reservation = Reservation()
            unreadable = ("desk_book_unreadable",)
        preflight = tuple(desk.broker.preflight(payload))
        exit_ready = exit_owner_ready(desk.paths, now)
        screening: CanaryScreening | EquityScreening
        quote: Quote | None = None
        if isinstance(effect, SupervisedEquityEffect):
            held = desk.ib.equity_quote(effect.symbol)
            quote = None if held is None else held.to_contract()
            risk = self._reserved_risk(account, reservation)
            screening = collect_entry_screening(
                effect,
                profile=profile,
                mandate_account_id=account,
                entries_this_session=entries,
                equity_inputs=EquityScreeningInputs(
                    quote=quote,
                    risk=risk,
                    owner_healthy=bool(desk.ib.connected),
                    protective_exit_ready=exit_ready,
                ),
                clock=desk.clock,
            )
            package = effect_v1_sha256(effect)
        else:
            structure = effect.structure
            dividends = (
                desk.dividends(structure.underlying, now.astimezone(ET).date())
                if desk.dividends
                else None
            )
            plan_ok, _ = assignment_plan(structure, dividends, now.astimezone(ET).date())
            open_loss, day_loss = desk.loss_facts(now.astimezone(ET).date())
            option_screening = collect_entry_screening(
                effect,
                profile=profile,
                mandate_account_id=account,
                entries_this_session=entries,
                broker=desk.broker,
                option_inputs=OperatorCanaryInputs(
                    owner_epoch=desk.owner_epoch,
                    owner_healthy=bool(desk.ib.connected),
                    assignment_plan_verified=plan_ok,
                    protective_exit_ready=exit_ready,
                    current_open_loss=open_loss,
                    realized_daily_loss=day_loss,
                ),
                clock=desk.clock,
            )
            assert isinstance(option_screening, CanaryScreening)  # the option arm
            screening = option_screening
            facts = option_screening.facts
            package = facts.intent_sha256 if facts is not None else effect_v1_sha256(effect)
            quote = self._option_quote(effect, option_screening)
        blockers = tuple(dict.fromkeys([*preflight, *unreadable, *screening.blockers]))
        return _Screened(
            blockers=blockers,
            screening_sha256=screening.screening_sha256,
            package_sha256=package,
            quote=quote,
            entries=entries,
            reservation=reservation,
        )

    def _reserved_risk(self, account: str, reservation: Reservation) -> Any:
        """The books' risk snapshot with the pending entries added: their
        max loss to the open loss and their cash out of settled cash."""
        from tree_options.trex.admission import read_risk_snapshot

        snapshot = read_risk_snapshot(self.root)
        if snapshot is None or snapshot.status != "complete" or snapshot.account_id != account:
            return snapshot
        assert snapshot.open_loss_cents is not None and snapshot.cash_cents is not None
        return snapshot.model_copy(
            update={
                "open_loss_cents": snapshot.open_loss_cents + reservation.loss_cents,
                "cash_cents": snapshot.cash_cents - reservation.cash_cents,
            }
        )

    def _option_quote(self, effect: SupervisedEffect, screening: CanaryScreening) -> Quote | None:
        """The package quote the owner reviews (debit orientation, cents: the
        bid floored at 0, the ask rounded up), observed at the oldest leg
        tick (the screening's own observation)."""
        ib = self.desk.ib
        quote = ib.package_quote(effect.structure.id)
        observed = screening.evidence.get("quote_observed_at")
        if quote is None or not observed:
            return None
        bid = max(int((quote.bid * _CENTS).to_integral_value(rounding=ROUND_FLOOR)), 0)
        ask = int((quote.ask * _CENTS).to_integral_value(rounding=ROUND_CEILING))
        if ask <= 0 or bid > ask:
            return None
        kinds = set()
        for con_id in ib.leg_con_ids(effect.structure.id):
            ticker = getattr(ib._md.get(con_id), "ticker", None)
            kinds.add(market_data_type_name(getattr(ticker, "marketDataType", None)))
        kind = kinds.pop() if len(kinds) == 1 else "unknown"
        return Quote(
            bid_cents=bid,
            ask_cents=ask,
            market_data_type=kind,
            as_of=datetime.fromisoformat(str(observed)),
        )

    # -- compose ----------------------------------------------------------------------

    def _candidate(
        self, command: ComposeEntryCommand, account: str
    ) -> tuple[Effect, dict[str, Any], datetime] | Outcome:
        from tree_options.desk.enter_supervised import DRAFT_SCHEMA, draft_directory

        args = command.args
        if not _PATH_SAFE.fullmatch(args.candidate_id):
            return refused("candidate_unknown", "the candidate id is not a file name")
        folder = draft_directory(self.root)
        blocked = folder / f"{args.candidate_id}.BLOCKED.json"
        path = folder / f"{args.candidate_id}.json"
        if not path.exists():
            if blocked.exists():
                return refused("candidate_blocked")
            return refused("candidate_unknown")
        try:
            draft = json.loads(_read_bounded(path))
            if not isinstance(draft, dict) or draft.get("schema") != DRAFT_SCHEMA:
                raise ValueError("not a proposal draft")
            body = draft["body"]
            effect = EFFECT_V1_ADAPTER.validate_python(body["effect"])
            valid_until = datetime.fromisoformat(str(draft["valid_until"]))
            if valid_until.tzinfo is None:
                raise ValueError("valid_until is naive")
        except (OSError, ValueError, KeyError, TypeError) as error:
            return refused("candidate_invalid", type(error).__name__)
        if body.get("program_id") != args.program_id:
            return refused("candidate_program_mismatch")
        kind = "equity" if isinstance(effect, SupervisedEquityEffect) else "option"
        if kind != args.asset_kind:
            return refused("candidate_kind_mismatch", f"the candidate is an {kind} entry")
        if effect.account_id != account:
            # drafts carry an annotation account; the desk binds its own
            try:
                effect = type(effect).model_validate(
                    {**model_document(effect), "account_id": account}
                )
            except ValidationError as error:
                return refused("candidate_invalid", type(error).__name__)
        review = dict(body.get("review") or {})
        if isinstance(effect, SupervisedEquityEffect):
            review["notional_cents"] = equity_notional_cents(effect)
            review["max_loss_cents"] = equity_max_loss_cents(effect)
        rebound = {
            **draft,
            "body": {
                **body,
                "effect": model_document(effect),
                "effect_sha256": effect_v1_sha256(effect),
                "sleeve_ref": args.sleeve_ref,
                "review": review,
            },
        }
        return effect, rebound, valid_until

    def compose(self, command: ComposeEntryCommand, now: datetime) -> Outcome:
        from tree_options.desk.enter_supervised import complete_draft

        desk = self.desk
        gated = self._gates(now)
        if gated is not None:
            return gated
        profiled = self._profile()
        if isinstance(profiled, Outcome):
            return profiled
        profile, digest = profiled
        account = desk.books.account_id()
        if account is None:
            return refused("account_unbound")
        candidate = self._candidate(command, account)
        if isinstance(candidate, Outcome):
            return candidate
        effect, draft, valid_until = candidate
        used = self._intent_used(effect)
        if used is not None:
            return refused("intent_already_used", used, intent_id=effect.intent_id)
        if (now - valid_until).total_seconds() >= 0:
            return refused("candidate_expired", valid_until=_iso(valid_until))
        desk.books.step(now, force=True)
        facts = owner_fact_blockers(self.root, now, account_id=account)
        if facts:
            return refused(facts[0], blockers=list(facts))
        screened = self._screen(effect, profile, account, now)
        reviewed = self._screening_facts(screened)
        if screened.blockers:
            return refused(screened.blockers[0], blockers=list(screened.blockers), **reviewed)
        if screened.quote is None:
            return refused("quote_unavailable", **reviewed)
        proposed_at = desk.clock()  # after every observation
        close = session_calendar().session_close(proposed_at.astimezone(ET).date())
        ttl = min(DEFAULT_SEND_TTL_S, PROPOSAL_MAX_TTL_S)
        deadline = min(shift_instant(proposed_at, ttl), valid_until, close)
        if (deadline - proposed_at).total_seconds() <= 0:
            return refused("candidate_expired", valid_until=_iso(valid_until))
        try:
            proposal = complete_draft(
                draft,
                proposed_at=proposed_at,
                send_deadline=deadline,
                quote=screened.quote,
                screening_sha256=screened.screening_sha256,
                owner_epoch=desk.owner_epoch,
                profile_digest=digest,
            )
        except (ValueError, KeyError, TypeError) as error:
            return refused("candidate_invalid", type(error).__name__, **reviewed)
        self._write_proposal(proposal)
        self._event(
            DeskEvent.PROPOSAL_COMPOSED.value,
            proposal_id=proposal.proposal_id,
            intent=effect.intent_id,
            candidate=command.args.candidate_id,
            program=command.args.program_id,
            asset_kind=command.args.asset_kind,
            actor=actor_label(command.issued_by),
            send_deadline=_iso(proposal.send_deadline),
        )
        return applied(
            proposal_id=proposal.proposal_id,
            proposal_sha256=proposal_sha256(proposal),
            effect_sha256=proposal.effect_sha256,
            intent_id=effect.intent_id,
            asset_kind=command.args.asset_kind,
            owner_epoch=desk.owner_epoch,
            profile_digest=digest,
            send_deadline=_iso(proposal.send_deadline),
            max_loss_cents=proposal.review.max_loss_cents,
            notional_cents=proposal.review.notional_cents,
            **reviewed,
        )

    @staticmethod
    def _screening_facts(screened: _Screened) -> dict[str, Any]:
        return {
            "screening_sha256": screened.screening_sha256,
            "entries_this_session": screened.entries,
            "reserved_loss_cents": screened.reservation.loss_cents,
            "reserved_cash_cents": screened.reservation.cash_cents,
            "reserved_intents": list(screened.reservation.intents),
        }

    def _write_proposal(self, proposal: EntryProposal) -> None:
        """``proposals/<id>.json`` once, in its canonical bytes (the id is
        the content hash, so an existing file is this proposal)."""
        target = self.layout.proposal(proposal.proposal_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as stream:
            stream.write(proposal_bytes(proposal))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            rename_noreplace(tmp, target)
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)

    # -- approve ----------------------------------------------------------------------

    def _sent_result(self, proposal_id: str) -> CommandResult | None:
        """The stored answer of the approval that SENT this proposal."""
        folder = self.layout.commands()
        if not folder.is_dir():
            return None
        for path in sorted(folder.glob("*.result.json")):
            try:
                result = CommandResult.model_validate_json(_read_bounded(path))
            except (OSError, ValidationError, ValueError):
                continue
            facts = result.result or {}
            if (
                result.kind == "approve_entry"
                and facts.get("answered_by") == ANSWERED_BY
                and facts.get("proposal_id") == proposal_id
                and facts.get("sent") is True
            ):
                return result
        return None

    def _live_mandate(
        self, proposal: EntryProposal, now: datetime, digest: str
    ) -> SupervisedMandateV2 | None:
        """An unconsumed mandate an earlier approval of THIS proposal left
        (a kill file stopped it before its permit) that can still mint."""
        path = self.sup.entry_mandate(proposal.effect.intent_id)
        try:
            mandate = SupervisedMandateV2.model_validate_json(_read_bounded(path))
        except (OSError, ValidationError, ValueError):
            return None
        if (
            mandate.proposal_id != proposal.proposal_id
            or mandate.effect_sha256 != proposal.effect_sha256
            or mandate.owner_epoch != self.desk.owner_epoch
            or mandate.profile_digest != digest
            or mandate.expired_at(now)
        ):
            return None
        return mandate

    def approve(self, command: ApproveEntryCommand, now: datetime) -> Outcome:
        desk = self.desk
        args = command.args
        proposal = self.proposal(args.proposal_id)
        if proposal is None:
            return refused(Blocker.PROPOSAL_UNKNOWN, proposal_id=args.proposal_id)
        effect = proposal.effect
        iid = effect.intent_id
        base: dict[str, Any] = {"proposal_id": proposal.proposal_id, "intent_id": iid}
        if self.layout.proposal_rejected(proposal.proposal_id).exists():
            return refused("proposal_rejected", **base)
        if self.sup.entry_mandate_consumed(iid).exists() or self._outbox_entered(iid):
            sent = self._sent_result(proposal.proposal_id)
            if sent is None:
                return refused(
                    Blocker.MANDATE_ALREADY_CONSUMED,
                    "the approval was spent; compose a new proposal",
                    **base,
                    **desk._outbox_report(iid),
                )
            first = dict(sent.result or {})
            first.pop("answered_by", None)
            first.update(sent=False, duplicate_of=sent.idempotency_key)
            status: Status = "failed" if sent.status == "failed" else "applied"
            return Outcome(
                status,
                sent.reason,
                f"already sent by approval {sent.idempotency_key}; nothing re-sent",
                first,
            )
        gated = self._gates(now)
        if gated is not None:
            return Outcome(gated.status, gated.reason, gated.detail, {**gated.facts, **base})
        profiled = self._profile()
        if isinstance(profiled, Outcome):
            return profiled
        profile, digest = profiled
        approval = approval_blockers(
            proposal, command, now=now, owner_epoch=desk.owner_epoch, profile_digest=digest
        )
        if approval:
            return refused(approval[0], blockers=list(approval), **base)
        conflict = self._registration_conflict(effect)
        if conflict is not None:
            return refused("intent_already_used", conflict, **base)
        account = desk.books.account_id()
        if account is None:
            return refused("account_unbound", **base)
        desk.books.step(now, force=True)
        facts = owner_fact_blockers(self.root, now, account_id=effect.account_id)
        if facts:
            return refused(facts[0], blockers=list(facts), **base)
        screened = self._screen(effect, profile, account, now)
        reviewed = {**base, **self._screening_facts(screened)}
        if screened.blockers:
            return refused(screened.blockers[0], blockers=list(screened.blockers), **reviewed)
        try:
            mandate = self._live_mandate(proposal, now, digest)
            if mandate is None:
                mandate = supervised_core.grant_entry_mandate(
                    self.sup,
                    now=now,
                    proposal=proposal,
                    command=command,
                    owner_epoch=desk.owner_epoch,
                    profile_digest=digest,
                )
        except ApprovalRefused as error:
            return refused(error.reason, error.detail, blockers=list(error.blockers), **reviewed)
        except SupervisedRefused as error:
            return refused(error.reason, error.detail, **reviewed)
        reviewed["mandate_approval_id"] = mandate.approval_id
        stopped = entry_send_blockers(self.root)
        if stopped:
            # the mandate stays unconsumed: a later approval of this
            # proposal (after a resume) may still use it once
            return refused(stopped[0], "stopped before the permit", **reviewed)
        return self._send(proposal, screened, digest, command, reviewed)

    def _outbox_entered(self, intent_id: str) -> bool:
        sup = self.sup
        return any(
            path.exists()
            for path in (sup.sending(intent_id), sup.terminal(intent_id), sup.reconciled(intent_id))
        )

    def _send(
        self,
        proposal: EntryProposal,
        screened: _Screened,
        digest: str,
        command: ApproveEntryCommand,
        reviewed: dict[str, Any],
    ) -> Outcome:
        desk = self.desk
        effect = proposal.effect
        payload = effect_v1_bytes(effect)
        now = desk.clock()  # the observations above may have taken time
        if (now - proposal.send_deadline).total_seconds() >= 0:
            return refused(Blocker.APPROVAL_EXPIRED, "the send deadline passed", **reviewed)
        intent = SupervisedIntent(
            intent=self._order_intent(proposal, now),
            package_intent_sha256=screened.package_sha256,
            created_at=now,
            send_deadline=proposal.send_deadline,
        )
        try:
            supervised_core.record_intent(self.sup, intent)
            permit = supervised_core.issue_permit(
                self.sup,
                now=now,
                account_id=effect.account_id,
                owner_epoch=desk.owner_epoch,
                intent=intent,
                canary_blockers=screened.blockers,
                effect_payload=payload,
                screening_sha256=screened.screening_sha256,
                profile_digest=digest,
            )
        except ApprovalRefused as error:
            return refused(error.reason, error.detail, blockers=list(error.blockers), **reviewed)
        except SupervisedRefused as error:
            return refused(error.reason, error.detail, **reviewed)
        reviewed["permit_id"] = permit.permit_id
        desk.runtime.register(effect)
        stopped = entry_send_blockers(self.root)
        if stopped:
            return refused(
                stopped[0], "stopped after the permit: the approval is spent", **reviewed
            )
        try:
            receipt = supervised_core.send(
                self.sup,
                now=now,
                permit_id=permit.permit_id,
                effect_payload=payload,
                broker=desk.broker,
            )
        except SupervisedRefused as error:
            return refused(error.reason, error.detail, **reviewed)
        outcome = str(receipt.get("outcome"))
        facts = {
            **reviewed,
            "sent": True,
            "outcome": outcome,
            "broker_order_id": receipt.get("broker_order_id"),
            "send_reason": receipt.get("reason") or receipt.get("reason_code"),
        }
        self._event(
            DeskEvent.ENTRY_APPROVED.value,
            proposal_id=proposal.proposal_id,
            intent=effect.intent_id,
            actor=actor_label(command.issued_by),
            command=command.idempotency_key,
            permit=reviewed["permit_id"],
            outcome=outcome,
        )
        if outcome == "acknowledged":
            return applied(**facts)
        if outcome == "rejected":
            return failed("broker_rejected", str(receipt.get("reason_code") or ""), **facts)
        return failed("send_uncertain", str(receipt.get("reason") or ""), **facts)

    @staticmethod
    def _order_intent(proposal: EntryProposal, now: datetime) -> OrderIntent:
        effect = proposal.effect
        if isinstance(effect, SupervisedEquityEffect):
            return OrderIntent(
                intent_id=effect.intent_id,
                contract_id=f"STK:{effect.con_id}",
                side="BUY",
                position_effect="OPEN_LONG",
                quantity=effect.quantity,
                order_type="LIMIT",
                limit_price=Decimal(effect.limit_cents) / _CENTS,
                execution_style="single",
                intent_created_at=now,
                source=proposal.strategy_version,
                source_sequence_id=f"desk-{effect.intent_id}",
            )
        return OrderIntent(
            intent_id=effect.intent_id,
            contract_id=f"BAG:{effect.structure.id}",
            side="BUY",
            position_effect="OPEN_LONG",
            quantity=effect.quantity,
            order_type="LIMIT",
            limit_price=effect.limit,
            execution_style="package",
            package_id=effect.structure.id,
            intent_created_at=now,
            source=proposal.strategy_version,
            source_sequence_id=f"desk-{effect.intent_id}",
        )

    # -- reject ------------------------------------------------------------------------

    def reject(self, command: RejectEntryCommand, now: datetime) -> Outcome:
        args = command.args
        proposal = self.proposal(args.proposal_id)
        if proposal is None:
            return refused(Blocker.PROPOSAL_UNKNOWN, proposal_id=args.proposal_id)
        iid = proposal.effect.intent_id
        base = {"proposal_id": proposal.proposal_id, "intent_id": iid}
        if self._outbox_entered(iid) or self._consumed_for(proposal):
            return refused(Blocker.MANDATE_ALREADY_CONSUMED, "the entry was already sent", **base)
        tombstone = self.layout.proposal_rejected(proposal.proposal_id)
        record = {
            "schema": REJECTION_SCHEMA,
            "proposal_id": proposal.proposal_id,
            "intent_id": iid,
            "rejected_by": actor_label(command.issued_by),
            "reason": args.reason,
            "command": command.idempotency_key,
            "at": _iso(now),
        }
        tmp = tombstone.with_name(f".{tombstone.name}.{os.getpid()}.tmp")
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
        try:
            rename_noreplace(tmp, tombstone)
            changed = True
        except FileExistsError:
            changed = False
        finally:
            tmp.unlink(missing_ok=True)
        retired = self._retire_pending(proposal, now)
        self._event(
            DeskEvent.ENTRY_REJECTED.value,
            proposal_id=proposal.proposal_id,
            intent=iid,
            actor=actor_label(command.issued_by),
            command=command.idempotency_key,
            retired_mandate=retired,
        )
        return applied(**base, changed=changed, retired_mandate=retired)

    def _consumed_for(self, proposal: EntryProposal) -> bool:
        path = self.sup.entry_mandate_consumed(proposal.effect.intent_id)
        try:
            mandate = SupervisedMandateV2.model_validate_json(_read_bounded(path))
        except (OSError, ValidationError, ValueError):
            return path.exists()  # unreadable but present: treat as spent
        return mandate.proposal_id == proposal.proposal_id

    def _retire_pending(self, proposal: EntryProposal, now: datetime) -> str | None:
        """Retire the unconsumed per-trade mandate of this proposal (if any)
        under the supervised state lock; its approval id."""
        path = self.sup.entry_mandate(proposal.effect.intent_id)
        if not path.exists():
            return None
        # lane E's lock and retirement record (supervised.py exposes no public
        # retire yet; WAVE2b integration note)
        with supervised_core._locked(self.sup):
            try:
                mandate = SupervisedMandateV2.model_validate_json(_read_bounded(path))
            except (OSError, ValidationError, ValueError):
                return None
            if mandate.proposal_id != proposal.proposal_id:
                return None
            supervised_core._retire_entry_mandate(self.sup, mandate, now=now, reason="rejected")
        return mandate.approval_id

    # -- recovery ------------------------------------------------------------------------

    def interrupted(self, command: AnyCommand) -> dict[str, Any]:
        """What is known of an entry command the owner died in."""
        if not isinstance(command, ApproveEntryCommand):
            return {}
        proposal = self.proposal(command.args.proposal_id)
        if proposal is None:
            return {"proposal_id": command.args.proposal_id}
        iid = proposal.effect.intent_id
        return {
            "proposal_id": proposal.proposal_id,
            "intent_id": iid,
            **self.desk._outbox_report(iid),
        }
