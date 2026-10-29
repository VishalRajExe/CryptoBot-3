"""Strategy implementations and registry.

Importing this package registers every built-in strategy.
"""

from __future__ import annotations

from cryptobot.strategy.base import (
    Strategy,
    available_strategies,
    build_strategy,
    register,
    strategy_class,
)
from cryptobot.strategy.donchian import DonchianBreakout
from cryptobot.strategy.sma_cross import SmaCross

__all__ = [
    "DonchianBreakout",
    "SmaCross",
    "Strategy",
    "available_strategies",
    "build_strategy",
    "register",
    "strategy_class",
]
