"""Policy defaults chosen where the 2026-10-14 product plan is silent.

Each value is a DEFAULT the lanes enforce until the owner rules otherwise;
WAVE2-CONTRACTS.md lists the open question behind each one. Durations are
whole seconds (the repo bans ``timedelta`` outside ``tree_options.time``).
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "APPROVAL_TTL_S",
    "CASH_ONLY",
    "COMMAND_MAX_TTL_S",
    "DEFAULT_STOP_CONFIRM_TICKS",
    "EQUITY_GAP_RESERVE_BPS",
    "EQUITY_SYMBOL_ALLOWLIST",
    "FORCED_EXIT_ET",
    "MAX_ENTRIES_PER_SESSION",
    "PROPOSAL_MAX_TTL_S",
    "RECONCILIATION_MAX_AGE_S",
    "RISK_SNAPSHOT_STALE_AFTER_S",
]

#: Equities are bought with settled cash only: no margin, no short sales.
CASH_ONLY: Final = True

#: Symbols the equity lane may trade until more real-time data is licensed.
EQUITY_SYMBOL_ALLOWLIST: Final = frozenset({"SPY"})

#: An owner approval lives at most this long: mandate expiry is
#: min(proposal send_deadline, approved_at + APPROVAL_TTL_S).
APPROVAL_TTL_S: Final = 300

#: A command may be applied only inside [issued_at, issued_at + this].
COMMAND_MAX_TTL_S: Final = 300

#: A proposal's send deadline is at most this long after it was composed.
PROPOSAL_MAX_TTL_S: Final = 900

#: New entries (both asset kinds) the desk sends per NYSE session.
MAX_ENTRIES_PER_SESSION: Final = 3

#: Gap reserve on an equity entry, basis points of notional (20%): the stop
#: is desk-managed (no resting broker stop), so an overnight or halt gap can
#: blow through it.
EQUITY_GAP_RESERVE_BPS: Final = 2000

#: Risk snapshot and reconciliation freshness bounds.
RISK_SNAPSHOT_STALE_AFTER_S: Final = 120
RECONCILIATION_MAX_AGE_S: Final = 120

#: Equity exit policy defaults (the desk closes positions itself).
FORCED_EXIT_ET: Final = "15:45"
DEFAULT_STOP_CONFIRM_TICKS: Final = 3
