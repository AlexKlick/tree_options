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

# The panel's ETFs (no earnings of their own); the rest are single stocks.
PANEL_ETFS: frozenset[str] = frozenset(
    {"SPY", "QQQ", "IWM", "SMH", "SOXX", "XLE", "XLV", "XLF", "GLD", "TQQQ", "SQQQ"}
)

# 3x leveraged ETFs: the desk gives them no options expression (plan D5);
# an XSMOM pick of one is logged, never substituted.
NO_OPTIONS_EXPRESSION: frozenset[str] = frozenset({"TQQQ", "SQQQ"})

# The chain recorder's universe: the panel minus the leveraged pair (35).
CHAIN_UNIVERSE: tuple[str, ...] = tuple(n for n in PANEL_NAMES if n not in NO_OPTIONS_EXPRESSION)

# XSMOM-TOP3 ranks the 36 tradables: every panel name except SPY.
XSMOM_TRADABLES: tuple[str, ...] = tuple(n for n in PANEL_NAMES if n != "SPY")

# Option classes whose regular session runs to 16:15 ET (13:15 on early-close
# days), 15 minutes past the equity close; every other chain name's options
# stop at the equity close. Source: Nasdaq Trader "Options Market Hours"
# (nasdaqtrader.com/Trader.aspx?id=optionshours), the exchange list of
# classes that "trade from 9:30 a.m. ET to 4:15 p.m. ET", fetched 2026-09-23,
# intersected with CHAIN_UNIVERSE. Cboe publishes no single list (its
# extended-hours FAQ gives only the rule). Corroborated by the recorded
# 2026-09-22 chains: SPY QQQ IWM XLE XLF options printed as late as 16:14:59
# (the single stocks never after 16:00). A class missing here is still held
# to 16:15 when its options printed after the close (store.validate).
LATE_CLOSE_OPTIONS: frozenset[str] = frozenset(
    {"SPY", "QQQ", "IWM", "SMH", "SOXX", "XLE", "XLF", "XLV", "GLD"}
)
