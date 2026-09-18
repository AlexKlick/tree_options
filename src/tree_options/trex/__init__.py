"""trex — defined-risk trade execution lane (IBKR, paper-first).

Deliberately decoupled from the research protocol packages: trex encodes a
pre-approved, operator-authored trade plan and mechanically enforces its
exit discipline. It never derives signals, and it has no code path that
widens, rolls, or naked-exposes a position — the plan's caps are the only
degrees of freedom.

The decision core (:mod:`tree_options.trex.engine`) is pure and broker-free;
:mod:`tree_options.trex.ibkr` is thin I/O around it.
"""

from tree_options.trex.clock import ET, EntryWindow, now_et
from tree_options.trex.engine import (
    AbortEntry,
    Action,
    ComboQuote,
    EngineConfig,
    ExitOrder,
    ExitReason,
    NoAction,
    PlaceEntry,
    Snapshot,
    decide,
)
from tree_options.trex.plan import PutSpread, TradePlan, load_plan
from tree_options.trex.state import BookState, Status, StructureState

__all__ = [
    "ET",
    "AbortEntry",
    "Action",
    "BookState",
    "ComboQuote",
    "EngineConfig",
    "EntryWindow",
    "ExitOrder",
    "ExitReason",
    "NoAction",
    "PlaceEntry",
    "PutSpread",
    "Snapshot",
    "Status",
    "StructureState",
    "TradePlan",
    "decide",
    "load_plan",
    "now_et",
]
