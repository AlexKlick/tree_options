"""Fail-closed review of an operator-authored IBKR paper canary.

This is a pure screening function. It cannot issue a mandate, consume a permit,
reserve risk, or send an order. The eventual broker owner must independently
recheck these facts at the send boundary.

Wave 2 adds the equity arm (``review_equity_entry``): one whole-share,
long-only, cash-only stock entry, screened with the frozen contract defaults
and the contract's own equity risk arithmetic (integer cents). The option
screen (``review_canary_package``) is unchanged; the per-session entry cap
(``entry_session_blockers``) is shared by both asset kinds.

Equity entries are screened only inside the regular-session entry window
(``equity_entry_window_blockers``, blocker ``entry_window_closed``): from
``EQUITY_ENTRY_OPEN_DELAY_S`` (300 s, 5 min) after an NYSE session's open to
``EQUITY_ENTRY_CLOSE_CUTOFF_S`` (1800 s, 30 min) before its close, both ends
inclusive, per the repo's trex calendar (``trex.clock.session_calendar``).
Early closes are honored: a 13:00 close ends the window at 12:30 ET. Any
other instant (pre-market, the last half hour, after the close, a weekend or
holiday) blocks. Options keep their own screen and have no such window here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256

from tree_options.action_graph.capital import CapitalProfile, _money
from tree_options.action_graph.proposal import canonical_bytes
from tree_options.time.sessions import require_utc, shift_instant
from tree_options.trex.clock import ET, session_calendar
from tree_options.trex.contracts.defaults import (
    EQUITY_GAP_RESERVE_BPS,
    EQUITY_SYMBOL_ALLOWLIST,
    MAX_ENTRIES_PER_SESSION,
)
from tree_options.trex.contracts.effects import (
    SupervisedEquityEffect,
    equity_max_loss_cents,
    equity_notional_cents,
)
from tree_options.trex.contracts.vocabulary import Blocker
from tree_options.trex.plan import LegStructure

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
# Ages in whole seconds (the calendar guard bans timedelta arithmetic outside time/).
ACCOUNT_MAX_AGE_S = 60
QUOTE_MAX_AGE_S = 30


@dataclass(frozen=True)
class CanaryFacts:
    """Fresh observations and checked geometry for exactly one paper package."""

    intent_sha256: str
    observed_account_id: str
    mandate_account_id: str
    paper_gateway_verified: bool
    account_observed_at: datetime
    quote_observed_at: datetime
    checked_at: datetime
    owner_epoch: str
    owner_healthy: bool
    legacy_positions: int
    legacy_working_orders: int
    package_quantity: int
    contract_verified: bool
    quote_complete: bool
    defined_risk_verified: bool
    assignment_plan_verified: bool
    protective_exit_ready: bool
    worst_case_loss: Decimal
    temporary_assignment_exposure: Decimal
    broker_margin_change: Decimal | None
    current_open_loss: Decimal | None
    realized_daily_loss: Decimal | None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.intent_sha256):
            raise ValueError("intent_sha256 must be a lowercase SHA-256 digest")
        if not self.observed_account_id or not self.mandate_account_id or not self.owner_epoch:
            raise ValueError("account and owner identity required")
        if self.legacy_positions < 0 or self.legacy_working_orders < 0:
            raise ValueError("legacy counts must be non-negative")
        _money(self.worst_case_loss, "worst_case_loss")
        _money(self.temporary_assignment_exposure, "temporary_assignment_exposure", allow_zero=True)
        for name in ("broker_margin_change", "current_open_loss", "realized_daily_loss"):
            value = getattr(self, name)
            if value is not None:
                _money(value, name, allow_zero=True)
        for name in ("account_observed_at", "quote_observed_at", "checked_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")


def review_canary(profile: CapitalProfile, facts: CanaryFacts) -> tuple[str, ...]:
    """Return blockers, never an authorization or inferred broker state."""

    blockers: list[str] = []
    if (
        not profile.complete_for_review
        or "operational-canary/1" not in profile.allowed_strategy_versions
    ):
        blockers.append("profile_or_canary_scope_missing")
    if not facts.paper_gateway_verified or facts.observed_account_id != facts.mandate_account_id:
        blockers.append("paper_account_mismatch_or_unverified")
    for name, observed, limit in (
        ("account", facts.account_observed_at, ACCOUNT_MAX_AGE_S),
        ("quote", facts.quote_observed_at, QUOTE_MAX_AGE_S),
    ):
        age = (facts.checked_at - observed).total_seconds()
        if age < 0 or age > limit:
            blockers.append(f"{name}_stale_or_future")
    if not facts.owner_healthy:
        blockers.append("broker_owner_unhealthy")
    if facts.legacy_positions or facts.legacy_working_orders:
        blockers.append("legacy_book_not_flat")
    if facts.package_quantity != 1:
        blockers.append("canary_quantity_not_one")
    if not facts.contract_verified or not facts.quote_complete:
        blockers.append("contract_or_quote_unverified")
    if not facts.defined_risk_verified or not facts.assignment_plan_verified:
        blockers.append("package_or_assignment_risk_unverified")
    if not facts.protective_exit_ready:
        blockers.append("protective_exit_unavailable")
    if facts.worst_case_loss > profile.intended_capital:
        blockers.append("trade_exceeds_intended_capital")
    if (
        profile.max_loss_per_trade is not None
        and facts.worst_case_loss > profile.max_loss_per_trade
    ):
        blockers.append("trade_loss_cap_exceeded")
    if facts.temporary_assignment_exposure > profile.intended_capital:
        blockers.append("assignment_exposure_exceeds_budget")
    if facts.broker_margin_change is None:
        blockers.append("broker_margin_unknown")
    elif facts.broker_margin_change > profile.intended_capital:
        blockers.append("broker_margin_exceeds_budget")
    if facts.current_open_loss is None:
        blockers.append("open_exposure_unknown")
    elif (
        profile.max_open_loss is not None
        and facts.current_open_loss + facts.worst_case_loss > profile.max_open_loss
    ):
        blockers.append("open_loss_cap_exceeded")
    if facts.realized_daily_loss is None:
        blockers.append("daily_loss_unknown")
    elif (
        profile.max_daily_loss is not None
        and facts.realized_daily_loss + facts.worst_case_loss > profile.max_daily_loss
    ):
        blockers.append("daily_loss_cap_exceeded")
    return tuple(blockers)


def package_intent_sha256(
    profile: CapitalProfile, structure: LegStructure, account_id: str, owner_epoch: str
) -> str:
    """Hash package, policy revision and paper account/owner binding.

    A later effect permit must also bind live quote and broker snapshots.
    """
    if not account_id or not owner_epoch:
        raise ValueError("account and owner identity required")
    payload = {
        "schema": "operational-canary-intent/1",
        "environment": "ibkr-paper",
        "account_id": account_id,
        "owner_epoch": owner_epoch,
        "profile": {
            "id": profile.profile_id,
            "revision": profile.revision,
            "intended_capital": str(profile.intended_capital),
            "risk_style": profile.risk_style,
            "goals": profile.goals,
            "max_loss_per_trade": str(profile.max_loss_per_trade),
            "max_open_loss": str(profile.max_open_loss),
            "max_daily_loss": str(profile.max_daily_loss),
            "horizon_days": profile.horizon_days,
            "allowed_strategy_versions": profile.allowed_strategy_versions,
            "reward_tiers": [
                {"min_ratio": str(tier.min_ratio), "max_trade_loss": str(tier.max_trade_loss)}
                for tier in profile.reward_tiers
            ],
            "steady_win_floor": (
                str(profile.steady_win_floor) if profile.steady_win_floor is not None else None
            ),
        },
        "structure": structure.model_dump(mode="json"),
    }
    return sha256(canonical_bytes(payload)).hexdigest()


def review_canary_package(
    profile: CapitalProfile, structure: LegStructure, facts: CanaryFacts
) -> tuple[str, ...]:
    """Bind pure readiness screening to one immutable, defined-risk vertical."""
    blockers = list(review_canary(profile, facts))
    if structure.kind not in ("debit_vertical", "credit_vertical"):
        blockers.append("canary_structure_not_vertical")
    if structure.quantity != 1 or structure.quantity != facts.package_quantity:
        blockers.append("package_quantity_mismatch")
    if structure.max_loss() != facts.worst_case_loss:
        blockers.append("package_loss_mismatch")
    if (
        package_intent_sha256(profile, structure, facts.mandate_account_id, facts.owner_epoch)
        != facts.intent_sha256
    ):
        blockers.append("intent_hash_mismatch")
    return tuple(blockers)


# -- Wave 2: the equity arm and the shared session cap ----------------------


def entry_session_blockers(
    entries_this_session: int | None, max_entries: int = MAX_ENTRIES_PER_SESSION
) -> tuple[str, ...]:
    """The per-session entry cap, both asset kinds combined.

    ``entries_this_session`` counts the entries already sent this NYSE
    session; an unknown (None) or impossible (negative) count blocks."""
    if (
        entries_this_session is None
        or entries_this_session < 0
        or entries_this_session >= max_entries
    ):
        return (Blocker.SESSION_ENTRY_LIMIT_REACHED.value,)
    return ()


def _optional_cents(value: int | None, name: str) -> None:
    # Integer cents only: a float, a bool or a Decimal is a caller defect.
    if value is not None and type(value) is not int:
        raise ValueError(f"{name} must be integer cents")


def _cap_cents(value: Decimal) -> int:
    """A profile limit (Decimal USD) as whole cents, rounded toward zero so a
    sub-cent limit never grows."""
    return int((value * 100).to_integral_value(rounding=ROUND_FLOOR))


#: Equity entry window, in whole seconds (no timedelta outside time/): it
#: opens this long after a session's open (the opening auction settles) ...
EQUITY_ENTRY_OPEN_DELAY_S = 5 * 60
#: ... and closes this long before the session's close (13:00 on an early
#: close), leaving the desk's managed exits room before the forced exit.
EQUITY_ENTRY_CLOSE_CUTOFF_S = 30 * 60
#: Blocker for an equity entry outside the window (a wire string; lane K adds
#: it to ``contracts.vocabulary.Blocker``, which compares equal).
ENTRY_WINDOW_CLOSED = "entry_window_closed"


def equity_entry_session(now: datetime) -> date | None:
    """The NYSE session ``now`` falls on (its ET date), or None when that
    date is not a session in the repo's trex calendar. Naive instants refuse."""
    day = require_utc(now, what="entry check instant").astimezone(ET).date()
    return day if session_calendar().is_session(day) else None


def equity_entry_window(session: date) -> tuple[datetime, datetime]:
    """``[start, end]`` (UTC instants, both inclusive) of the equity entry
    window on ``session``: open + ``EQUITY_ENTRY_OPEN_DELAY_S`` to close -
    ``EQUITY_ENTRY_CLOSE_CUTOFF_S``, early closes included. A date that is
    not a session raises ``NotASessionError`` (fail closed)."""
    cal = session_calendar()
    start = shift_instant(cal.session_open(session), EQUITY_ENTRY_OPEN_DELAY_S)
    end = shift_instant(cal.session_close(session), -EQUITY_ENTRY_CLOSE_CUTOFF_S)
    return start, end


def equity_entry_window_blockers(now: datetime) -> tuple[str, ...]:
    """``(ENTRY_WINDOW_CLOSED,)`` unless ``now`` is inside its session's
    equity entry window; empty inside it."""
    session = equity_entry_session(now)
    if session is not None:
        start, end = equity_entry_window(session)
        if start <= now <= end:
            return ()
    return (ENTRY_WINDOW_CLOSED,)


@dataclass(frozen=True)
class EquityEntryFacts:
    """Observed facts for one equity entry; money in integer cents.

    ``market_data_type`` is what IBKR reported for the quote (``realtime``,
    ``delayed``, ``frozen`` or ``unknown``); ``settled_cash_cents`` is the
    account's settled cash (negative = borrowing); the loss figures come
    from a fresh risk snapshot. None means not observed, and blocks.
    """

    mandate_account_id: str
    checked_at: datetime
    market_data_type: str | None
    quote_observed_at: datetime | None
    settled_cash_cents: int | None
    open_loss_cents: int | None
    day_realized_loss_cents: int | None
    entries_this_session: int | None
    owner_healthy: bool
    protective_exit_ready: bool

    def __post_init__(self) -> None:
        if not self.mandate_account_id:
            raise ValueError("mandate account required")
        if not isinstance(self.checked_at, datetime):
            raise ValueError("checked_at required")
        for name in ("checked_at", "quote_observed_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        for name in (
            "settled_cash_cents",
            "open_loss_cents",
            "day_realized_loss_cents",
            "entries_this_session",
        ):
            _optional_cents(getattr(self, name), name)
        for name in ("open_loss_cents", "day_realized_loss_cents"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")


def review_equity_entry(
    profile: CapitalProfile,
    effect: SupervisedEquityEffect,
    facts: EquityEntryFacts,
    *,
    symbol_allowlist: frozenset[str] = EQUITY_SYMBOL_ALLOWLIST,
    gap_reserve_bps: int = EQUITY_GAP_RESERVE_BPS,
    max_entries_per_session: int = MAX_ENTRIES_PER_SESSION,
) -> tuple[str, ...]:
    """Blockers for one whole-share, long-only, cash-only stock entry.

    Pure: never an authorization. The loss held against the profile caps
    is the contract's ``equity_max_loss_cents`` (the stop loss, or the gap
    reserve when larger: the desk manages the stop, no broker stop rests).

    ``facts.checked_at`` must lie inside the regular-session entry window,
    ``[open + EQUITY_ENTRY_OPEN_DELAY_S, close - EQUITY_ENTRY_CLOSE_CUTOFF_S]``
    (09:35 to 15:30 ET, or 09:35 to 12:30 ET on a 13:00 early close, per the
    repo's trex calendar); otherwise ``entry_window_closed`` blocks.
    """
    blockers: list[str] = []
    if not profile.complete_for_review:
        blockers.append("profile_limits_missing")
    if effect.account_id != facts.mandate_account_id:
        blockers.append("effect_account_not_mandate_account")
    if effect.symbol not in symbol_allowlist:
        blockers.append(Blocker.EQUITY_SYMBOL_NOT_QUALIFIED.value)
    if effect.side != "BUY" or type(effect.quantity) is not int or effect.quantity < 1:
        blockers.append("equity_not_whole_share_long")
    blockers.extend(equity_entry_window_blockers(facts.checked_at))
    if facts.market_data_type != "realtime":
        blockers.append(Blocker.EQUITY_QUOTE_NOT_REALTIME.value)
    if facts.quote_observed_at is None:
        blockers.append("quote_stale_or_future")
    else:
        age = (facts.checked_at - facts.quote_observed_at).total_seconds()
        if age < 0 or age > QUOTE_MAX_AGE_S:
            blockers.append("quote_stale_or_future")
    if not facts.owner_healthy:
        blockers.append("broker_owner_unhealthy")
    if not facts.protective_exit_ready:
        blockers.append("protective_exit_unavailable")
    notional = equity_notional_cents(effect)
    cash = facts.settled_cash_cents
    # Cash only (no margin): settled cash must cover the whole notional.
    if cash is None or cash < 0 or notional > cash:
        blockers.append(Blocker.INSUFFICIENT_CASH.value)
    if notional > _cap_cents(profile.intended_capital):
        blockers.append("equity_notional_exceeds_intended_capital")
    loss = equity_max_loss_cents(effect, gap_reserve_bps)
    if loss > _cap_cents(profile.intended_capital):
        blockers.append("trade_exceeds_intended_capital")
    if profile.max_loss_per_trade is not None and loss > _cap_cents(profile.max_loss_per_trade):
        blockers.append("trade_loss_cap_exceeded")
    if facts.open_loss_cents is None:
        blockers.append("open_exposure_unknown")
    elif profile.max_open_loss is not None and facts.open_loss_cents + loss > _cap_cents(
        profile.max_open_loss
    ):
        blockers.append("open_loss_cap_exceeded")
    if facts.day_realized_loss_cents is None:
        blockers.append("daily_loss_unknown")
    elif profile.max_daily_loss is not None and facts.day_realized_loss_cents + loss > _cap_cents(
        profile.max_daily_loss
    ):
        blockers.append("daily_loss_cap_exceeded")
    blockers.extend(entry_session_blockers(facts.entries_this_session, max_entries_per_session))
    return tuple(blockers)
