"""C2.7: the frozen blocker and event vocabulary of Wave 2.

Values are the wire strings: they appear in command results, admission
blocker lists, ``events.jsonl`` and the web UI. Adding a member is an
additive contract change; renaming or removing one is a breaking change.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Blocker", "DeskEvent"]


class Blocker(StrEnum):
    """Why an entry, approval or command was refused (machine-readable)."""

    # --- frozen by the Wave 2 plan -------------------------------------
    ENTRIES_PAUSED = "entries_paused"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RISK_SNAPSHOT_STALE = "risk_snapshot_stale"
    EQUITY_QUOTE_NOT_REALTIME = "equity_quote_not_realtime"
    EQUITY_SYMBOL_NOT_QUALIFIED = "equity_symbol_not_qualified"
    INSUFFICIENT_CASH = "insufficient_cash"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_EPOCH_MISMATCH = "approval_epoch_mismatch"
    APPROVAL_PROFILE_MISMATCH = "approval_profile_mismatch"
    APPROVAL_EFFECT_MISMATCH = "approval_effect_mismatch"
    MANDATE_ALREADY_CONSUMED = "mandate_already_consumed"
    DUPLICATE_COMMAND = "duplicate_command"
    COMMAND_BODY_MISMATCH = "command_body_mismatch"
    # --- added by the contracts lane (see WAVE2-CONTRACTS.md) -----------
    APPROVAL_PROPOSAL_MISMATCH = "approval_proposal_mismatch"
    COMMAND_EXPIRED = "command_expired"
    COMMAND_UNAUTHORIZED = "command_unauthorized"
    PROPOSAL_UNKNOWN = "proposal_unknown"
    SESSION_ENTRY_LIMIT_REACHED = "session_entry_limit_reached"


class DeskEvent(StrEnum):
    """Event names Wave 2 lanes append to the desk's ``events.jsonl``."""

    PROPOSAL_COMPOSED = "proposal_composed"
    ENTRY_APPROVED = "entry_approved"
    ENTRY_REJECTED = "entry_rejected"
    ENTRIES_PAUSED = "entries_paused"
    ENTRIES_RESUMED = "entries_resumed"
    APPROVALS_INVALIDATED = "approvals_invalidated"
    COMMAND_APPLIED = "command_applied"
    COMMAND_REFUSED = "command_refused"
    EQUITY_EXIT_STOP = "equity_exit_stop"
    EQUITY_EXIT_TARGET = "equity_exit_target"
    EQUITY_EXIT_DEADLINE = "equity_exit_deadline"
    EQUITY_EXIT_FORCED = "equity_exit_forced"
    RECONCILIATION_CLEAN = "reconciliation_clean"
    RECONCILIATION_REQUIRED = "reconciliation_required"
