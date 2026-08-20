"""Backtesting machinery (M2 proper, M3 options era)."""

from tree_options.backtest.equity import (
    BacktestSignal,
    EquityBacktestResult,
    EquityFillEngine,
    FiveBasisPointFeeModel,
    run_equity_backtest,
)
from tree_options.backtest.options import (
    OptionsBacktestResult,
    OptionsCounters,
    PositionRow,
    run_options_backtest,
)

__all__ = [
    "BacktestSignal",
    "EquityBacktestResult",
    "EquityFillEngine",
    "FiveBasisPointFeeModel",
    "OptionsBacktestResult",
    "OptionsCounters",
    "PositionRow",
    "run_equity_backtest",
    "run_options_backtest",
]
