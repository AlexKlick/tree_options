"""GET /api/ideas: the owner's entry ideas (``trex-idea-list/1``, Wave 5).

The owner initiates every entry. This view lists what they may compose:

- **drafts**: every ``trex-entry-proposal-draft/1`` under
  ``<run dir>/proposal-drafts/`` (``desk.enter_supervised.draft_directory``:
  the miner's option drafts, any equity draft), each with the exact
  ``compose_entry`` args that name it (``candidate_id`` = the file stem, the
  name the desk's compose reads), its structure, its age and horizon, the
  proposal that already names its intent (from ``GET /api/proposals``), and
  advisory ``hints``; the miner's ``<candidate>.BLOCKED.json`` diagnostics
  under ``blocked``; a file that cannot be read as a draft under
  ``problems``, never dropped;
- **manual_equity**: the terms of a manual ticket (``compose_entry`` with
  ``manual_equity_ticket``): the candidate prefix, the program and strategy,
  the allowlist, the limit policies, the session cap, today's equity entry
  window, and the hints that would refuse a ticket now.

Hints are what the web can see without a broker: the stops
(``entries_paused``, ``halt``, ``flatten``), the owner's facts (risk
snapshot, cash, reconciliation, exit owner), the capital profile, the
regular session and the equity entry window, the session cap, the
candidate's horizon, the allowlist and the profile's per-trade caps. The
desk screens everything again, with the live quote, when it composes.

Read-only and broker-free: nothing here composes, approves or sends, and
no broker library is imported. A drafts directory that does not exist yet
is known and empty; one that cannot be listed (or a proposals directory
that cannot be) makes the drafts block ``unavailable`` with its reason.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from tree_options.action_graph.canary import (
    entry_session_blockers,
    equity_entry_window,
    equity_entry_window_blockers,
)
from tree_options.trex.admission import entry_send_blockers, owner_fact_blockers
from tree_options.trex.clock import ET, session_calendar
from tree_options.trex.contracts.acceptance import AssetKind
from tree_options.trex.contracts.canonical import model_document
from tree_options.trex.contracts.commands import ComposeEntryArgs
from tree_options.trex.contracts.defaults import EQUITY_SYMBOL_ALLOWLIST, MAX_ENTRIES_PER_SESSION
from tree_options.trex.contracts.effects import (
    EFFECT_V1_ADAPTER,
    SupervisedEffect,
    SupervisedEquityEffect,
    equity_max_loss_cents,
    equity_notional_cents,
)
from tree_options.trex.contracts.ideas import (
    IDEAS_PATH,
    LIMIT_POLICIES,
    LIVE_PROPOSAL_STATUSES,
    MANUAL_CANDIDATE_PREFIX,
    MANUAL_PROGRAM_ID,
    MANUAL_STRATEGY_VERSION,
    BlockedIdea,
    DraftIdeas,
    Idea,
    IdeaEquity,
    IdeaLeg,
    IdeaList,
    IdeaOption,
    IdeaProblem,
    IdeaProposalStatus,
    IdeaStatus,
    ManualEquityTerms,
)
from tree_options.trex.contracts.layout import Wave2RunDir, Wave2SupervisedDir
from tree_options.trex.contracts.vocabulary import Blocker
from tree_options.trex.contracts.wave3 import Wave3Reason
from tree_options.trex.equity_exits import in_regular_session
from tree_options.trex_web.commands_view import proposals_payload

log = logging.getLogger("trex_web.entry_ideas_view")

__all__ = ["IDEAS_PATH", "MAX_IDEAS", "attach", "build_idea_list", "ideas_payload"]

#: the newest drafts listed (by file time); the rest set ``truncated``
MAX_IDEAS: Final = 200
#: a draft is small; anything bigger is a problem row, never parsed
MAX_DRAFT_BYTES: Final = 256 * 1024
_BLOCKED_SUFFIX: Final = ".BLOCKED.json"
_NO_STORE: Final = {"Cache-Control": "no-store"}
_HUNDRED: Final = Decimal(100)

Clock = Callable[[], datetime]
PathSource = Callable[[], Path]


def _whole_seconds(earlier: datetime, later: datetime) -> int:
    """Whole seconds from ``earlier`` to ``later`` (never negative)."""
    return max(0, int((later - earlier).total_seconds()))


def _reasons(codes: Iterable[str]) -> tuple[Blocker | Wave3Reason, ...]:
    """Codes as vocabulary members, in order, once each. A code outside the
    vocabulary (a producer drifted) is logged, never shown as a member."""
    out: list[Blocker | Wave3Reason] = []
    for code in codes:
        member: Blocker | Wave3Reason
        try:
            member = Blocker(code)
        except ValueError:
            try:
                member = Wave3Reason(code)
            except ValueError:
                log.warning("ideas: hint %r is not in the vocabulary", code)
                continue
        if member not in out:
            out.append(member)
    return tuple(out)


def _cents(value: Decimal, rounding: str) -> int:
    return int((value * _HUNDRED).to_integral_value(rounding=rounding))


# -- the web's own facts (advisory) --------------------------------------------------


@dataclass(frozen=True)
class _Facts:
    """What every idea's hints share, read once per request."""

    common: tuple[str, ...]
    window: tuple[date, datetime, datetime] | None
    loss_cap_cents: int | None
    capital_cents: int | None

    @classmethod
    def read(cls, run_dir: Path, supervised_dir: Path, now: datetime) -> _Facts:
        codes: list[str] = []
        codes += entry_send_blockers(run_dir)
        loss_cap = capital = None
        profile_code, profile = _profile(run_dir)
        if profile_code is not None:
            codes.append(profile_code)
        elif profile is not None:
            if profile.max_loss_per_trade is not None:
                loss_cap = _cents(profile.max_loss_per_trade, ROUND_FLOOR)
            capital = _cents(profile.intended_capital, ROUND_FLOOR)
        try:
            codes += owner_fact_blockers(run_dir, now)
        except (OSError, ValueError) as error:
            log.warning("ideas: owner facts unreadable (%s)", type(error).__name__)
        if not in_regular_session(now):
            codes.append(Blocker.OUTSIDE_REGULAR_SESSION)
        session = now.astimezone(ET).date()
        codes += entry_session_blockers(_entries_this_session(supervised_dir, session))
        window = None
        if session_calendar().is_session(session):
            start, end = equity_entry_window(session)
            window = (session, start, end)
        return cls(tuple(codes), window, loss_cap, capital)

    def window_open(self, now: datetime) -> bool:
        return self.window is not None and not equity_entry_window_blockers(now)


def _profile(run_dir: Path) -> tuple[str | None, Any]:
    """(blocker, profile) read exactly as the desk reads ``profile.json``."""
    from tree_options.trex.supervised_desk import load_profile  # lazy: a heavy module

    try:
        profile, _digest = load_profile(run_dir / "profile.json")
    except FileNotFoundError:
        return Blocker.PROFILE_ABSENT, None
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return Blocker.PROFILE_INVALID, None
    if not profile.complete_for_review:
        return Blocker.PROFILE_LIMITS_MISSING, profile
    return None, profile


def _entries_this_session(supervised_dir: Path, session: date) -> int | None:
    from tree_options.trex.desk_entries import entries_this_session
    from tree_options.trex.supervised import SupervisedPaths

    return entries_this_session(SupervisedPaths(supervised_dir), session)


# -- proposals per intent --------------------------------------------------------------


@dataclass(frozen=True)
class _IntentProposals:
    status: IdeaProposalStatus
    proposal_id: str | None


def _by_intent(rows: Iterable[Mapping[str, Any]]) -> dict[str, _IntentProposals]:
    """Per intent, the proposal that matters: a consumed one (the intent is
    spent), else the newest live one, else the newest."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["intent_id"]), []).append(row)
    out: dict[str, _IntentProposals] = {}
    for intent, group in grouped.items():
        newest = sorted(group, key=lambda r: (r["proposed_at"], r["proposal_id"]), reverse=True)
        consumed = [r for r in newest if r["status"] == "consumed"]
        live = [r for r in newest if r["status"] in LIVE_PROPOSAL_STATUSES]
        pick = (consumed or live or newest)[0]
        out[intent] = _IntentProposals(pick["status"], pick["proposal_id"])
    return out


def _intent_spent(supervised_dir: Path, intent_id: str) -> bool:
    """The desk's own "never again" facts for an intent: a consumed
    mandate/2 or any supervised outbox entry (``desk_entries._intent_used``)."""
    from tree_options.trex.supervised import SupervisedPaths

    sup = SupervisedPaths(supervised_dir)
    paths = (
        Wave2SupervisedDir(supervised_dir).mandate_consumed(intent_id),
        sup.pending(intent_id),
        sup.sending(intent_id),
        sup.terminal(intent_id),
        sup.reconciled(intent_id),
    )
    return any(os.path.lexists(path) for path in paths)


# -- one draft ----------------------------------------------------------------------------


class _Unreadable(Exception):
    def __init__(self, reason: Blocker | Wave3Reason) -> None:
        super().__init__(reason)
        self.reason = reason


def _read_draft(path: Path) -> tuple[dict[str, Any], datetime]:
    """The draft object and its file time; ``_Unreadable`` names why not."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DRAFT_BYTES:
            raise _Unreadable(Blocker.DRAFT_UNREADABLE)
        with open(path, "rb") as stream:
            raw = stream.read(MAX_DRAFT_BYTES + 1)
    except OSError as error:
        raise _Unreadable(Blocker.DRAFT_UNREADABLE) from error
    if len(raw) > MAX_DRAFT_BYTES:
        raise _Unreadable(Blocker.DRAFT_UNREADABLE)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise _Unreadable(Blocker.CANDIDATE_INVALID) from error
    if not isinstance(document, dict):
        raise _Unreadable(Blocker.CANDIDATE_INVALID)
    return document, datetime.fromtimestamp(info.st_mtime, UTC)


def _summary(effect: SupervisedEffect | SupervisedEquityEffect) -> str:
    if isinstance(effect, SupervisedEquityEffect):
        policy = effect.exit_policy
        target = policy.target_price_cents
        upside = f"target {target / 100:.2f}" if target is not None else "no target"
        text = (
            f"Buy {effect.quantity} {effect.symbol} at a {effect.limit_cents / 100:.2f} limit; "
            f"stop {policy.stop_price_cents / 100:.2f}, {upside}; "
            f"out by {policy.exit_deadline_session.isoformat()}"
        )
    else:
        structure = effect.structure
        legs = "/".join(f"{leg.strike}{leg.right}" for leg in structure.legs)
        text = (
            f"{effect.side.capitalize()} {effect.quantity} {structure.underlying} "
            f"{structure.kind.replace('_', ' ')} {legs} at a {effect.limit} limit; "
            f"time stop {structure.exit_deadline.isoformat()}"
        )
    return text[:500]


def _structure(
    effect: SupervisedEffect | SupervisedEquityEffect,
) -> tuple[IdeaOption | None, IdeaEquity | None]:
    if isinstance(effect, SupervisedEquityEffect):
        policy = effect.exit_policy
        return None, IdeaEquity(
            quantity=effect.quantity,
            limit_cents=effect.limit_cents,
            stop_price_cents=policy.stop_price_cents,
            target_price_cents=policy.target_price_cents,
            exit_deadline_session=policy.exit_deadline_session,
        )
    structure = effect.structure
    legs = tuple(
        IdeaLeg(
            right=leg.right,
            action=leg.action,
            strike_cents=_cents(leg.strike, ROUND_HALF_UP),
            expiry=leg.expiry,
        )
        for leg in structure.legs
    )
    option = IdeaOption(
        kind=structure.kind,
        legs=legs,
        quantity=effect.quantity,
        limit_cents=max(1, _cents(effect.limit, ROUND_CEILING)),
        exit_deadline=structure.exit_deadline,
    )
    return option, None


def _idea(
    candidate: str,
    document: Mapping[str, Any],
    written_at: datetime,
    now: datetime,
    facts: _Facts,
    proposals: Mapping[str, _IntentProposals],
    supervised_dir: Path,
) -> Idea:
    """One draft as an idea (``ValueError``/``KeyError``/``TypeError``: invalid)."""
    from tree_options.desk.enter_supervised import DRAFT_SCHEMA

    if document.get("schema") != DRAFT_SCHEMA:
        raise ValueError("not a proposal draft")
    body = document["body"]
    effect = EFFECT_V1_ADAPTER.validate_python(body["effect"])
    valid_until = datetime.fromisoformat(str(document["valid_until"]))
    if valid_until.tzinfo is None:
        raise ValueError("valid_until is naive")
    review = body["review"]
    asset_kind: AssetKind
    if isinstance(effect, SupervisedEquityEffect):
        asset_kind = "equity"
        notional, max_loss = equity_notional_cents(effect), equity_max_loss_cents(effect)
        underlying = effect.symbol
    else:
        asset_kind = "option"
        notional, max_loss = review["notional_cents"], review["max_loss_cents"]
        underlying = effect.structure.underlying
    equity = asset_kind == "equity"
    raw_session = document.get("entry_session")
    entry_session = (
        date.fromisoformat(str(raw_session))
        if raw_session is not None
        else valid_until.astimezone(ET).date()
    )
    left = _whole_seconds(now, valid_until)
    expired = (now - valid_until).total_seconds() >= 0
    found = proposals.get(effect.intent_id)
    proposal_status: IdeaProposalStatus = "none" if found is None else found.status
    spent = proposal_status == "consumed" or _intent_spent(supervised_dir, effect.intent_id)
    status: IdeaStatus
    if spent:
        status = "used"
    elif proposal_status in LIVE_PROPOSAL_STATUSES:
        status = "proposed"
    elif expired:
        status = "expired"
    else:
        status = "available"
    hints: list[str] = list(facts.common)
    if expired:
        hints.append(Blocker.CANDIDATE_EXPIRED)
    if spent:
        hints.append(Blocker.INTENT_ALREADY_USED)
    if facts.loss_cap_cents is not None and max_loss > facts.loss_cap_cents:
        hints.append(Blocker.TRADE_LOSS_CAP_EXCEEDED)
    if equity:
        if not facts.window_open(now):
            hints.append(Blocker.ENTRY_WINDOW_CLOSED)
        if underlying not in EQUITY_SYMBOL_ALLOWLIST:
            hints.append(Blocker.EQUITY_SYMBOL_NOT_QUALIFIED)
        if facts.capital_cents is not None and notional > facts.capital_cents:
            hints.append(Blocker.EQUITY_NOTIONAL_EXCEEDS_INTENDED_CAPITAL)
    option, equity_block = _structure(effect)
    program_id = str(body["program_id"])
    return Idea(
        candidate_id=candidate,
        asset_kind=asset_kind,
        underlying=underlying,
        intent_id=effect.intent_id,
        program_id=program_id,
        strategy_version=str(body["strategy_version"]),
        summary=_summary(effect),
        option=option,
        equity=equity_block,
        max_loss_cents=max_loss,
        notional_cents=notional,
        written_at=written_at,
        age_s=_whole_seconds(written_at, now),
        entry_session=entry_session,
        valid_until=valid_until,
        seconds_to_expiry=0 if expired else max(1, left),
        status=status,
        proposal_status=proposal_status,
        proposal_id=None if found is None else found.proposal_id,
        hints=_reasons(hints),
        compose_args=ComposeEntryArgs(
            program_id=program_id, candidate_id=candidate, asset_kind=asset_kind
        ),
    )


def _blocked(candidate: str, document: Mapping[str, Any], written_at: datetime) -> BlockedIdea:
    if document.get("schema") != "supervised-preview-blocked/1":
        raise ValueError("not a blocked diagnostic")
    return BlockedIdea(
        candidate_id=candidate,
        kind=str(document["kind"]),
        reason=str(document["reason"]),
        written_at=written_at,
    )


# -- the drafts block ----------------------------------------------------------------------


def _unavailable(reason: Blocker | Wave3Reason) -> DraftIdeas:
    return DraftIdeas(
        status="unavailable",
        unavailable_reason=reason,
        items=None,
        blocked=None,
        problems=None,
        truncated=None,
    )


def _listing(folder: Path) -> list[tuple[float, str]] | None:
    """(file time, name) of the directory's entries; [] when it does not
    exist yet; None when it cannot be listed (a link counts as that)."""
    try:
        if folder.is_symlink():
            return None
        entries: list[tuple[float, str]] = []
        with os.scandir(folder) as scan:
            for entry in scan:
                if entry.name.startswith(".") or not entry.name.endswith(".json"):
                    continue  # the writers' temporary files
                try:
                    entries.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
                except OSError:
                    entries.append((0.0, entry.name))
        return entries
    except FileNotFoundError:
        return []
    except OSError:
        return None


def _drafts(run_dir: Path, supervised_dir: Path, now: datetime, facts: _Facts) -> DraftIdeas:
    from tree_options.desk.enter_supervised import draft_directory

    folder = draft_directory(run_dir)
    listing = _listing(folder)
    if listing is None:
        return _unavailable(Blocker.DRAFTS_DIR_UNREADABLE)
    try:
        with os.scandir(Wave2RunDir(run_dir).proposals()):
            pass
    except FileNotFoundError:
        pass  # nothing composed yet
    except OSError:
        return _unavailable(Wave3Reason.PROPOSALS_DIR_UNREADABLE)
    proposals = _by_intent(proposals_payload(run_dir, supervised_dir, now)["proposals"])
    listing.sort(key=lambda item: (item[0], item[1]), reverse=True)
    items: list[Idea] = []
    blocked: list[BlockedIdea] = []
    problems: list[IdeaProblem] = []
    for _, name in listing[:MAX_IDEAS]:
        is_blocked = name.endswith(_BLOCKED_SUFFIX)
        candidate = name.removesuffix(_BLOCKED_SUFFIX if is_blocked else ".json")
        try:
            document, written_at = _read_draft(folder / name)
            if is_blocked:
                blocked.append(_blocked(candidate, document, written_at))
            else:
                items.append(
                    _idea(candidate, document, written_at, now, facts, proposals, supervised_dir)
                )
        except _Unreadable as error:
            problems.append(IdeaProblem(file=name[:255], reason=error.reason))
        except (ValidationError, ValueError, KeyError, TypeError, AttributeError):
            problems.append(IdeaProblem(file=name[:255], reason=Blocker.CANDIDATE_INVALID))
    items.sort(key=lambda idea: (idea.valid_until, idea.candidate_id))
    blocked.sort(key=lambda b: b.candidate_id)
    problems.sort(key=lambda p: p.file)
    return DraftIdeas(
        status="ok",
        unavailable_reason=None,
        items=tuple(items),
        blocked=tuple(blocked),
        problems=tuple(problems),
        truncated=len(listing) > MAX_IDEAS,
    )


def _manual_terms(facts: _Facts, now: datetime) -> ManualEquityTerms:
    hints: list[str] = list(facts.common)
    if not facts.window_open(now):
        hints.append(Blocker.ENTRY_WINDOW_CLOSED)
    window = facts.window
    status: Literal["open", "closed", "no_session"] = "no_session"
    session: date | None = None
    opens: datetime | None = None
    closes: datetime | None = None
    if window is not None:
        session, opens, closes = window
        status = "open" if facts.window_open(now) else "closed"
    return ManualEquityTerms(
        candidate_prefix=MANUAL_CANDIDATE_PREFIX,
        program_id=MANUAL_PROGRAM_ID,
        strategy_version=MANUAL_STRATEGY_VERSION,
        symbols=tuple(sorted(EQUITY_SYMBOL_ALLOWLIST)),
        limit_policies=LIMIT_POLICIES,
        max_entries_per_session=MAX_ENTRIES_PER_SESSION,
        window_status=status,
        window_session=session,
        window_opens_at=opens,
        window_closes_at=closes,
        hints=_reasons(hints),
    )


def build_idea_list(run_dir: Path, supervised_dir: Path, now: datetime) -> IdeaList:
    """The validated ``trex-idea-list/1`` document at ``now``."""
    facts = _Facts.read(run_dir, supervised_dir, now)
    return IdeaList(
        now=now,
        drafts=_drafts(run_dir, supervised_dir, now, facts),
        manual_equity=_manual_terms(facts, now),
        execution_enabled=False,
    )


def ideas_payload(run_dir: Path, supervised_dir: Path, now: datetime) -> dict[str, Any]:
    """``GET /api/ideas``: the document as served (canonical JSON mode)."""
    return model_document(build_idea_list(run_dir, supervised_dir, now))


def attach(app: FastAPI, *, run_dir: PathSource, supervised_dir: PathSource, clock: Clock) -> None:
    """Mount ``GET /api/ideas``; the directories resolve per request."""

    @app.get(IDEAS_PATH)
    def get_ideas() -> JSONResponse:
        return JSONResponse(ideas_payload(run_dir(), supervised_dir(), clock()), headers=_NO_STORE)
