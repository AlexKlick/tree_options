"""Wave 5: ``trex-idea-list/1`` (``GET /api/ideas``), the owner's entry ideas.

The owner initiates every entry ("manual plus qualified ideas", the
2026-10-14 plan). Two sources feed one ``compose_entry`` command:

- **qualified ideas**: the ``trex-entry-proposal-draft/1`` files under
  ``<run dir>/proposal-drafts/`` (the miner's option drafts written by
  ``desk.enter_supervised``; any equity draft built by
  ``desk_entries.equity_draft``). Each item repeats the exact
  ``compose_args`` its compose command sends;
- **a manual equity ticket**: ``compose_entry`` with
  ``manual_equity_ticket`` (``contracts.commands``); ``manual_equity``
  carries the terms the ticket form needs.

Composing never approves or sends: the desk writes a proposal, and the
approval stays the owner's separate act on it. The view is advisory
(``hints``); the desk screens everything again. Read-only, broker-free.

The null rule is Wave 3's (``WAVE3-CONTRACTS.md`` C3.2): a block is known
(no null figure) or ``unavailable`` with a reason (every figure null); a
nullable field that is "not applicable" is fixed by a sibling enum and
validated. Reasons are ``Blocker`` or ``Wave3Reason`` members.

Schemas and pure helpers only: no file or network I/O, no broker library,
no ``timedelta``.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.contracts.acceptance import AssetKind, FalseLiteral
from tree_options.trex.contracts.canonical import PathSafeId, Sha256Hex, Symbol, UTCInstant
from tree_options.trex.contracts.commands import (
    MANUAL_CANDIDATE_PREFIX,
    MANUAL_MAX_HOLD_SESSIONS,
    MANUAL_STRATEGY_VERSION,
    ComposeEntryArgs,
    ManualEquityTicket,
    ManualLimit,
    manual_intent_id,
)
from tree_options.trex.contracts.wave3 import UnavailableReason

__all__ = [
    "IDEAS_PATH",
    "IDEA_LIST_SCHEMA",
    "LIMIT_POLICIES",
    "LIVE_PROPOSAL_STATUSES",
    "MANUAL_CANDIDATE_PREFIX",
    "MANUAL_MAX_HOLD_SESSIONS",
    "MANUAL_PROGRAM_ID",
    "MANUAL_STRATEGY_VERSION",
    "BlockedIdea",
    "DraftIdeas",
    "Idea",
    "IdeaEquity",
    "IdeaLeg",
    "IdeaList",
    "IdeaOption",
    "IdeaProblem",
    "IdeaProposalStatus",
    "IdeaStatus",
    "ManualEquityTerms",
    "ManualEquityTicket",
    "ManualLimit",
    "manual_intent_id",
]

IDEA_LIST_SCHEMA: Final = "trex-idea-list/1"
IDEAS_PATH: Final = "/api/ideas"
#: The program of every manual ticket: the desk binds exactly this one and
#: refuses a manual ticket naming any other (``candidate_program_mismatch``).
MANUAL_PROGRAM_ID: Final = "owner-manual"
#: ``limit_policy`` choices of a manual ticket (``limit_cents`` = an
#: explicit ``{"limit_cents": n}``).
LIMIT_POLICIES: Final = ("ask", "mid", "limit_cents")

#: ``available``: composable as far as the web can tell; ``proposed``: a
#: live (pending or approved) proposal exists for its intent; ``used``: its
#: intent was sent (or its mandate consumed) and can never be sent again;
#: ``expired``: past ``valid_until``.
IdeaStatus = Literal["available", "proposed", "used", "expired"]
#: The status ``GET /api/proposals`` gives the idea's relevant proposal
#: (the consumed one, else the newest live one, else the newest), or
#: ``none`` when no proposal names its intent.
IdeaProposalStatus = Literal[
    "none", "pending", "approved", "consumed", "rejected", "expired", "unknown"
]
LIVE_PROPOSAL_STATUSES: Final = frozenset({"pending", "approved"})

NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
HumanText = Annotated[str, Field(min_length=1, max_length=500)]
FileName = Annotated[str, Field(min_length=1, max_length=255)]
#: A miner's diagnostic code (``credit_open_not_supported_v1``, ...).
DiagnosticCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class IdeaLeg(StrictModel):
    """One option leg; the strike in integer cents."""

    right: Literal["C", "P"]
    action: Literal["BUY", "SELL"]
    strike_cents: PositiveInt
    expiry: date


class IdeaOption(StrictModel):
    """An option idea's structure: ``limit_cents`` is the package limit per
    share (0.90 -> 90), ``exit_deadline`` the structure's time stop."""

    kind: IdStr
    legs: tuple[IdeaLeg, ...] = Field(min_length=1)
    quantity: PositiveInt
    limit_cents: PositiveInt
    exit_deadline: date


class IdeaEquity(StrictModel):
    """An equity idea's order and exit policy. ``target_price_cents`` null:
    the ticket has no target (the desk exits by stop or deadline)."""

    quantity: PositiveInt
    limit_cents: PositiveInt
    stop_price_cents: PositiveInt
    target_price_cents: PositiveInt | None
    exit_deadline_session: date


class Idea(StrictModel):
    """One draft the owner may compose.

    ``option`` / ``equity``: set exactly for its ``asset_kind``.
    ``written_at``: the draft file's modification time; ``age_s`` at
    ``now``. ``entry_session``: the draft's, else the ET date of
    ``valid_until``. ``seconds_to_expiry``: whole seconds to ``valid_until``
    at ``now`` (0 once expired). ``proposal_id``: set exactly when
    ``proposal_status`` is not ``none``. ``hints``: advisory blockers the
    web can see (the desk is authoritative). ``compose_args``: what the
    ``compose_entry`` command repeats, verbatim."""

    candidate_id: PathSafeId
    asset_kind: AssetKind
    underlying: Symbol
    intent_id: PathSafeId
    program_id: IdStr
    strategy_version: IdStr
    summary: HumanText
    option: IdeaOption | None
    equity: IdeaEquity | None
    max_loss_cents: PositiveInt
    notional_cents: PositiveInt
    written_at: UTCInstant
    age_s: NonNegativeInt
    entry_session: date
    valid_until: UTCInstant
    seconds_to_expiry: NonNegativeInt
    status: IdeaStatus
    proposal_status: IdeaProposalStatus
    proposal_id: Sha256Hex | None
    hints: tuple[UnavailableReason, ...]
    compose_args: ComposeEntryArgs

    @model_validator(mode="after")
    def _shape(self) -> Idea:
        if (self.option is not None) != (self.asset_kind == "option"):
            raise ValueError("option is set exactly for an option idea")
        if (self.equity is not None) != (self.asset_kind == "equity"):
            raise ValueError("equity is set exactly for an equity idea")
        if (self.proposal_id is None) != (self.proposal_status == "none"):
            raise ValueError("proposal_id is set exactly when a proposal names the intent")
        args = self.compose_args
        if (args.candidate_id, args.asset_kind, args.program_id) != (
            self.candidate_id,
            self.asset_kind,
            self.program_id,
        ):
            raise ValueError("compose_args repeat this idea's candidate, kind and program")
        if args.manual_equity_ticket is not None:
            raise ValueError("a drafted idea composes without a manual ticket")
        live = self.proposal_status in LIVE_PROPOSAL_STATUSES
        if self.proposal_status == "consumed" and self.status != "used":
            raise ValueError("a consumed proposal means the idea is used")
        if self.status == "proposed" and not live:
            raise ValueError("a proposed idea has a live proposal")
        if live and self.status not in ("proposed", "used"):
            raise ValueError("an idea with a live proposal is proposed (or used)")
        if self.status == "expired" and self.seconds_to_expiry != 0:
            raise ValueError("an expired idea has no time left")
        if self.status == "available" and self.seconds_to_expiry == 0:
            raise ValueError("an available idea has time left")
        return self


class BlockedIdea(StrictModel):
    """A ``<candidate>.BLOCKED.json`` diagnostic: a deal the miner could not
    draft (e.g. ``credit_open_not_supported_v1``). Never composable."""

    candidate_id: PathSafeId
    kind: IdStr
    reason: DiagnosticCode
    written_at: UTCInstant


class IdeaProblem(StrictModel):
    """A drafts-directory entry that could not be read as an idea; never
    dropped silently."""

    file: FileName
    reason: UnavailableReason


class DraftIdeas(StrictModel):
    """The drafts directory. A missing directory is known and empty
    (nothing drafted yet); one that cannot be listed is unavailable."""

    status: Literal["ok", "unavailable"]
    unavailable_reason: UnavailableReason | None
    items: tuple[Idea, ...] | None
    blocked: tuple[BlockedIdea, ...] | None
    problems: tuple[IdeaProblem, ...] | None
    truncated: bool | None

    @model_validator(mode="after")
    def _shape(self) -> DraftIdeas:
        figures = (self.items, self.blocked, self.problems, self.truncated)
        if (self.status == "unavailable") != (self.unavailable_reason is not None):
            raise ValueError("unavailable_reason is set exactly when the status is unavailable")
        if self.status == "unavailable" and any(f is not None for f in figures):
            raise ValueError("an unavailable block carries no figures")
        if self.status == "ok" and any(f is None for f in figures):
            raise ValueError("a known block has no null figure")
        return self


class ManualEquityTerms(StrictModel):
    """What the manual ticket form needs: the candidate prefix, the program
    and strategy the desk binds, the symbols it trades (the allowlist), the
    limit policies, the session cap, today's equity entry window and the
    hints the web can see now (advisory).

    ``window_status``: ``no_session`` when today is not a session (then the
    window fields are null), else ``open`` / ``closed`` at ``now``.

    Review amendment (additive, 2026-10-21): ``max_hold_sessions`` is the
    hold cap (``MANUAL_MAX_HOLD_SESSIONS``) and ``max_exit_deadline_session``
    the latest exit deadline the desk accepts for a ticket entered today
    (that many sessions after ``window_session`` on the repo calendar, or its
    last session when it ends sooner): null when today is not a session.
    The view always states the cap; a document served before the amendment
    has neither field (both parse as None)."""

    candidate_prefix: Literal["manual:"]
    program_id: IdStr
    strategy_version: IdStr
    symbols: tuple[Symbol, ...]
    limit_policies: tuple[Literal["ask", "mid", "limit_cents"], ...]
    max_entries_per_session: PositiveInt
    window_status: Literal["open", "closed", "no_session"]
    window_session: date | None
    window_opens_at: UTCInstant | None
    window_closes_at: UTCInstant | None
    hints: tuple[UnavailableReason, ...]
    max_hold_sessions: PositiveInt | None = None
    max_exit_deadline_session: date | None = None

    @model_validator(mode="after")
    def _window(self) -> ManualEquityTerms:
        window = (self.window_session, self.window_opens_at, self.window_closes_at)
        if (self.window_status == "no_session") != all(f is None for f in window):
            raise ValueError("the window is set exactly when today is a session")
        if any(f is None for f in window) and not all(f is None for f in window):
            raise ValueError("the window fields are set together")
        if self.window_opens_at is not None and self.window_closes_at is not None:
            if self.window_opens_at >= self.window_closes_at:
                raise ValueError("the window opens before it closes")
        latest = self.max_exit_deadline_session
        if latest is not None:
            if self.max_hold_sessions is None:
                raise ValueError("the latest exit deadline comes with its hold cap")
            if self.window_session is None or latest < self.window_session:
                raise ValueError("the latest exit deadline is a session on or after today's")
        return self


class IdeaList(StrictModel):
    """``GET /api/ideas``: the drafts and the manual ticket terms.
    ``execution_enabled`` is the literal ``false``: the web never executes."""

    schema_version: Literal["trex-idea-list/1"] = Field(default="trex-idea-list/1", alias="schema")
    now: UTCInstant
    drafts: DraftIdeas
    manual_equity: ManualEquityTerms
    execution_enabled: FalseLiteral
