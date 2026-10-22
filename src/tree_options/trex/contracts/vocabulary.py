"""C2.7: the frozen blocker and event vocabulary of Wave 2.

Values are the wire strings: they appear in command results, admission
blocker lists, ``events.jsonl`` and the web UI. Adding a member is an
additive contract change; renaming or removing one is a breaking change.
Member names are the values spelled as identifiers (``:`` becomes ``_``)
and no two members share a value (``unique``; a test pins both).

Amendment 2026-10-21 (lane K, coordinator-approved): the wire strings the
merged Wave 2 lanes emit were added, each value exactly as its producer
spells it. Producers may keep passing the plain strings: a ``StrEnum``
member compares and serializes equal to its value.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = ["Blocker", "DeskEvent"]


@unique
class Blocker(StrEnum):
    """Why an entry, approval, command or sell was refused, or why a fact a
    view shows is unavailable (machine-readable)."""

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
    # --- amendment 2026-10-21: emitted by the merged Wave 2 lanes -------
    # kill files and owner facts (trex.pause, trex.admission, desk.enter)
    HALT = "halt"
    AUTO_OFF = "auto_off"
    EXIT_OWNER_NOT_READY = "exit_owner_not_ready"
    SHADOW_ONLY_BUILD = "shadow_only_build"
    SELECTION_RULE_NOT_ACTIVATED = "selection_rule_not_activated"
    # entry screening (trex.supervised_canary, action_graph.canary)
    CREDIT_OPEN_NOT_SUPPORTED_V1 = "credit_open_not_supported_v1"
    RISK_SNAPSHOT_ACCOUNT_MISMATCH = "risk_snapshot_account_mismatch"
    PROFILE_LIMITS_MISSING = "profile_limits_missing"
    EFFECT_ACCOUNT_NOT_MANDATE_ACCOUNT = "effect_account_not_mandate_account"
    EQUITY_NOT_WHOLE_SHARE_LONG = "equity_not_whole_share_long"
    QUOTE_STALE_OR_FUTURE = "quote_stale_or_future"
    BROKER_OWNER_UNHEALTHY = "broker_owner_unhealthy"
    PROTECTIVE_EXIT_UNAVAILABLE = "protective_exit_unavailable"
    EQUITY_NOTIONAL_EXCEEDS_INTENDED_CAPITAL = "equity_notional_exceeds_intended_capital"
    TRADE_EXCEEDS_INTENDED_CAPITAL = "trade_exceeds_intended_capital"
    TRADE_LOSS_CAP_EXCEEDED = "trade_loss_cap_exceeded"
    OPEN_EXPOSURE_UNKNOWN = "open_exposure_unknown"
    OPEN_LOSS_CAP_EXCEEDED = "open_loss_cap_exceeded"
    DAILY_LOSS_UNKNOWN = "daily_loss_unknown"
    DAILY_LOSS_CAP_EXCEEDED = "daily_loss_cap_exceeded"
    # equity preflight (trex.supervised_ibkr; the session guards are shared
    # with the option preflight)
    GATEWAY_DISCONNECTED = "gateway_disconnected"
    NOT_PAPER_GATEWAY_PORT = "not_paper_gateway_port"
    WRONG_CLIENT_ID = "wrong_client_id"
    ACCOUNT_NOT_MANAGED_BY_SESSION = "account_not_managed_by_session"
    ACCOUNT_NOT_PAPER = "account_not_paper"
    EQUITY_CON_ID_MISMATCH = "equity_con_id_mismatch"
    EQUITY_QUOTE_INCOMPLETE = "equity_quote_incomplete"
    BROKER_VIEW_UNREADABLE_ACCOUNT_VALUES = "broker_view_unreadable:account_values"
    BROKER_VIEW_UNREADABLE_OPEN_ORDERS = "broker_view_unreadable:open_orders"
    CASH_COMMITMENT_UNBOUNDED = "cash_commitment_unbounded"
    # per-trade authority refusals (trex.supervised, trex.grant_policy)
    EFFECT_INVALID = "effect_invalid"
    EFFECTS_HALTED = "effects_halted"
    ENTRY_MANDATE_ALREADY_GRANTED = "entry_mandate_already_granted"
    INTENT_ID_NOT_PATH_SAFE = "intent_id_not_path_safe"
    MANDATE_ABSENT = "mandate_absent"
    MANDATE_ACCOUNT_MISMATCH = "mandate_account_mismatch"
    MANDATE_BINDING_MISMATCH = "mandate_binding_mismatch"
    MANDATE_SCOPE_MISMATCH = "mandate_scope_mismatch"
    PERMIT_ALREADY_ISSUED = "permit_already_issued"
    PRODUCT_STRATEGY_REQUIRES_APPROVAL = "product_strategy_requires_approval"
    # holdings checks before a desk equity sell (trex.desk_runtime ``why``
    # of a ``reconciliation_required`` event)
    HOLDINGS_MISMATCH = "holdings_mismatch"
    HOLDINGS_EXCESS = "holdings_excess"
    NOT_A_STOCK_ROW = "not_a_stock_row"
    FRACTIONAL_POSITION = "fractional_position"
    ORDER_FILL_NOT_WHOLE_SHARES = "order_fill_not_whole_shares"
    # web command ingress and store (trex_web.commands_view, command_store)
    BODY_TOO_LARGE = "body_too_large"
    INVALID_JSON = "invalid_json"
    ISSUED_BY_IS_STAMPED_BY_THE_BACKEND = "issued_by_is_stamped_by_the_backend"
    INVALID_COMMAND = "invalid_command"
    COMMAND_NOT_YET_VALID = "command_not_yet_valid"
    UNKNOWN_COMMAND = "unknown_command"
    COMMAND_STORE_UNAVAILABLE = "command_store_unavailable"
    COMMANDS_DIR_UNWRITABLE = "commands_dir_unwritable"
    COMMAND_STORE_UNREADABLE = "command_store_unreadable"
    COMMAND_STORE_FOREIGN_FILE = "command_store_foreign_file"
    COMMAND_RESULT_INVALID = "command_result_invalid"
    STAGED_COMMAND_INVALID = "staged_command_invalid"
    STAGED_COMMAND_MISMATCH = "staged_command_mismatch"
    # web views (trex_web.commands_view proposals, trex_web.portfolio_view)
    PROPOSAL_UNREADABLE = "proposal_unreadable"
    PROPOSAL_INVALID = "proposal_invalid"
    PROPOSAL_NOT_CANONICAL = "proposal_not_canonical"
    PROPOSAL_ID_MISMATCH = "proposal_id_mismatch"
    MANDATE_UNREADABLE = "mandate_unreadable"
    HOLDINGS_MISSING = "holdings_missing"
    HOLDINGS_UNREADABLE = "holdings_unreadable"
    HOLDINGS_INVALID = "holdings_invalid"
    RISK_SNAPSHOT_MISSING = "risk_snapshot_missing"
    RISK_SNAPSHOT_UNREADABLE = "risk_snapshot_unreadable"
    RISK_SNAPSHOT_INVALID = "risk_snapshot_invalid"
    RISK_SNAPSHOT_UNAVAILABLE = "risk_snapshot_unavailable"
    RECONCILIATION_MISSING = "reconciliation_missing"
    RECONCILIATION_UNREADABLE = "reconciliation_unreadable"
    RECONCILIATION_INVALID = "reconciliation_invalid"
    RECONCILIATION_STALE = "reconciliation_stale"
    RECONCILIATION_UNAVAILABLE = "reconciliation_unavailable"
    ACCOUNT_MISMATCH = "account_mismatch"
    NON_PAPER_ACCOUNT = "non_paper_account"
    # reserved for the Wave 2b lanes (coordinator 2026-10-21)
    ENTRY_WINDOW_CLOSED = "entry_window_closed"
    OUTSIDE_UNDEFINED_RISK = "outside_undefined_risk"
    EXIT_QUOTE_BLIND = "exit_quote_blind"
    # --- amendment 2026-10-21 (W2c integration): additive ---------------
    # a standing FLATTEN stops every entry sender (trex.admission)
    FLATTEN = "flatten"


@unique
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
    # --- amendment 2026-10-21: the desk runtime's equity path -----------
    EQUITY_CONTRACT_MISMATCH = "equity_contract_mismatch"
    EQUITY_UNQUALIFIED = "equity_unqualified"
    EXIT_EVIDENCE_INCONCLUSIVE = "exit_evidence_inconclusive"
    HOLDINGS_UNVERIFIABLE = "holdings_unverifiable"
    STRUCTURE_TICK_FAILED = "structure_tick_failed"
    # --- amendment 2026-10-21 (W2c integration): additive ---------------
    # a contained command-stage failure (trex.desk_commands, supervised_desk)
    COMMANDS_FAILED = "commands_failed"
