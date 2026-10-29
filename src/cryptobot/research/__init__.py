"""Research tooling: datasets, the stdlib backtester, and experiment records."""

from cryptobot.research.backtest import (
    BacktestConfig,
    BacktestResult,
    CostModel,
    TradeRecord,
    compare,
    parameter_sensitivity,
    run_backtest,
    summarise_sensitivity,
    verify_no_lookahead,
)
from cryptobot.research.dataset import (
    BarDataset,
    gaps,
    synthetic_bars,
    synthetic_dataset,
    write_csv,
    write_json,
)
from cryptobot.research.experiment import (
    CODE_VERSION,
    ExperimentRecord,
    SuiteResult,
    render_walk_forward,
    render_walk_forward_optimised,
    run_suite,
    walk_forward,
    walk_forward_optimised,
)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "BarDataset",
    "CODE_VERSION",
    "CostModel",
    "ExperimentRecord",
    "SuiteResult",
    "TradeRecord",
    "compare",
    "gaps",
    "parameter_sensitivity",
    "render_walk_forward",
    "render_walk_forward_optimised",
    "run_backtest",
    "run_suite",
    "summarise_sensitivity",
    "synthetic_bars",
    "synthetic_dataset",
    "verify_no_lookahead",
    "walk_forward",
    "walk_forward_optimised",
    "write_csv",
    "write_json",
]
