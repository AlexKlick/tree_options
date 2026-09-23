"""The desk universe: the research panel's 37 names.

PANEL_NAMES is copied from ``artifacts/paper-trades/fetch_ohlc.py`` NAMES
(the 2026-09-11 paid-plan universe: the original 29 plus SMH SOXX XLE XLV
XLF GLD TQQQ SQQQ). It is a copy, not an import: that script is an
untracked research artifact and ``desk/`` never imports research code.
Keep the two in step by hand if the research universe ever changes.
"""

from __future__ import annotations

PANEL_NAMES: tuple[str, ...] = tuple(
    (
        "AAPL MSFT NVDA GOOGL AMZN META TSLA AVGO LLY JPM V UNH XOM PG MA COST HD "
        "ADBE NFLX CRM AMD PEP KO DIS INTC QCOM SPY QQQ IWM "
        "SMH SOXX XLE XLV XLF GLD TQQQ SQQQ"
    ).split()
)

# 3x leveraged ETFs: the desk gives them no options expression (plan D5);
# an XSMOM pick of one is logged, never substituted.
NO_OPTIONS_EXPRESSION: frozenset[str] = frozenset({"TQQQ", "SQQQ"})

# The chain recorder's universe: the panel minus the leveraged pair (35).
CHAIN_UNIVERSE: tuple[str, ...] = tuple(n for n in PANEL_NAMES if n not in NO_OPTIONS_EXPRESSION)

# XSMOM-TOP3 ranks the 36 tradables: every panel name except SPY.
XSMOM_TRADABLES: tuple[str, ...] = tuple(n for n in PANEL_NAMES if n != "SPY")
