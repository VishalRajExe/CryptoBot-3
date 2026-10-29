"""Evaluation: what the recorded evidence supports.

Three questions, in increasing scope:

* :mod:`cryptobot.evaluation.trades` - did execution match intent, and what did
  each round trip earn?
* :mod:`cryptobot.evaluation.challengers` - may a different parameter set
  replace the baseline on out-of-sample evidence?
* :mod:`cryptobot.evaluation.acceptance` - is the paper soak complete, and was
  it sound while it ran?

All three are read-only. None can place, cancel, or size an order.

:mod:`cryptobot.evaluation.promotion` is the one write in the package, and it
writes an approval rather than behavior: a named human, a reason, and the digest
of the evidence they were shown. Nothing in this package can change how the bot
trades, because promotion is deliberately a configuration edit by the operator.
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
from cryptobot.evaluation.promotion import (
    Approval,
    Evidence,
    Promotion,
    PromotionRefused,
    approve,
    evidence_from,
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
    "Approval",
    "ChallengerReport",
    "ChallengerVerdict",
    "Criterion",
    "Evidence",
    "ExecutedTrade",
    "ExecutionQuality",
    "ExecutionReport",
    "INSUFFICIENT",
    "OpenPosition",
    "PROMOTE",
    "Promotion",
    "PromotionRefused",
    "PromotionRule",
    "REJECT",
    "TradeReport",
    "approve",
    "evaluate_execution",
    "evaluate_trades",
    "evidence_from",
    "judge",
    "reference_price",
    "reconstruct_symbol",
    "run_acceptance",
    "run_challengers",
]
