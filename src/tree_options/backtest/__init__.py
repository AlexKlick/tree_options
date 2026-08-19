"""Backtesting machinery (M2 proper)."""

from tree_options.backtest.equity import (
    BacktestSignal,
    EquityBacktestResult,
    EquityFillEngine,
    FiveBasisPointFeeModel,
    run_equity_backtest,
)

__all__ = [
    "BacktestSignal",
    "EquityBacktestResult",
    "EquityFillEngine",
    "FiveBasisPointFeeModel",
    "run_equity_backtest",
]
