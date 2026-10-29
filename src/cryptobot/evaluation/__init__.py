"""Evaluation: what the recorded evidence supports.

Three questions, in increasing scope:

* :mod:`cryptobot.evaluation.trades` - did execution match intent, and what did
  each round trip earn?
* :mod:`cryptobot.evaluation.challengers` - may a different parameter set
  replace the baseline on out-of-sample evidence?
* :mod:`cryptobot.evaluation.acceptance` - is the paper soak complete, and was
  it sound while it ran?

All three are read-only. None can place, cancel, or size an order.
"""

from cryptobot.evaluation.acceptance import (
    AcceptanceCriteria,
    AcceptanceReport,
    Criterion,
    run_acceptance,
)
from cryptobot.evaluation.challengers import (
    INSUFFICIENT,
    PROMOTE,
    REJECT,
    ChallengerReport,
    ChallengerVerdict,
    PromotionRule,
    judge,
    run_challengers,
)
from cryptobot.evaluation.trades import (
    ExecutedTrade,
    ExecutionQuality,
    ExecutionReport,
    OpenPosition,
    TradeReport,
    evaluate_execution,
    evaluate_trades,
    reference_price,
    reconstruct_symbol,
)

__all__ = [
    "AcceptanceCriteria",
    "AcceptanceReport",
    "ChallengerReport",
    "ChallengerVerdict",
    "Criterion",
    "ExecutedTrade",
    "ExecutionQuality",
    "ExecutionReport",
    "INSUFFICIENT",
    "OpenPosition",
    "PROMOTE",
    "PromotionRule",
    "REJECT",
    "TradeReport",
    "evaluate_execution",
    "evaluate_trades",
    "judge",
    "reference_price",
    "reconstruct_symbol",
    "run_acceptance",
    "run_challengers",
]
