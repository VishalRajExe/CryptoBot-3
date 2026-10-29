"""Challenger evaluation: may a different parameter set replace the baseline?

The rule enforced here is deliberately narrow and unflattering:

* Only **out-of-sample** results can promote a challenger. In-sample results are
  recorded because hiding them is how selection bias starts, never because they
  count as evidence.
* A challenger must beat the baseline by a stated margin, not merely tie. A
  strategy change that does not pay for its own risk is churn.
* It must not worsen the maximum drawdown by more than the stated slack, and its
  profit factor must clear a floor.
* Too few out-of-sample trades is reported as ``insufficient_evidence`` rather
  than as a rejection, because the honest answer is "we do not know yet".

Nothing here places orders, and nothing here writes to the live configuration.
A promoted challenger is a recommendation an operator must apply deliberately,
and :mod:`cryptobot.evaluation.promotion` is where that decision becomes an
auditable object rather than a convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from cryptobot.core.errors import CryptobotError
from cryptobot.research.backtest import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
    verify_no_lookahead,
)
from cryptobot.research.dataset import BarDataset
from cryptobot.strategy.base import Strategy

ZERO = Decimal("0")

PROMOTE = "promote"
REJECT = "reject"
INSUFFICIENT = "insufficient_evidence"


@dataclass(frozen=True)
class PromotionRule:
    """The bar a challenger must clear. Stated once, applied identically."""

    min_out_of_sample_trades: int = 10
    min_improvement_pct: Decimal = Decimal("0")
    max_drawdown_slack_pct: Decimal = Decimal("1")
    min_profit_factor: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.min_out_of_sample_trades < 0:
            raise ValueError("min_out_of_sample_trades cannot be negative")
        if self.max_drawdown_slack_pct < ZERO:
            raise ValueError("max_drawdown_slack_pct cannot be negative")


@dataclass(frozen=True)
class ChallengerVerdict:
    name: str
    params: Mapping[str, object]
    status: str
    reasons: tuple[str, ...]
    in_sample: BacktestResult
    out_of_sample: BacktestResult

    @property
    def promoted(self) -> bool:
        return self.status == PROMOTE

    @property
    def out_of_sample_return(self) -> Decimal:
        return self.out_of_sample.total_return_pct

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": dict(self.params),
            "status": self.status,
            "reasons": list(self.reasons),
            "in_sample_return_pct": str(_round(self.in_sample.total_return_pct)),
            "out_of_sample_return_pct": str(_round(self.out_of_sample.total_return_pct)),
            "out_of_sample_drawdown_pct": str(_round(self.out_of_sample.max_drawdown_pct)),
            "out_of_sample_trades": len(self.out_of_sample.trades),
        }


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _profit_factor(result: BacktestResult) -> Decimal | None:
    """None means "no losing trades": a perfect score, or no information."""
    return result.profit_factor


def judge(
    baseline: BacktestResult,
    challenger_in: BacktestResult,
    challenger_out: BacktestResult,
    *,
    name: str,
    params: Mapping[str, object],
    rule: PromotionRule,
) -> ChallengerVerdict:
    """Apply the rule to one challenger. Every failing reason is reported."""
    reasons: list[str] = []

    trades = len(challenger_out.trades)
    if trades < rule.min_out_of_sample_trades:
        reasons.append(
            f"only {trades} out-of-sample trade(s); {rule.min_out_of_sample_trades} required"
        )
        # Insufficient evidence short-circuits: return comparisons would be noise.
        return ChallengerVerdict(
            name=name,
            params=dict(params),
            status=INSUFFICIENT,
            reasons=tuple(reasons),
            in_sample=challenger_in,
            out_of_sample=challenger_out,
        )

    improvement = challenger_out.total_return_pct - baseline.total_return_pct
    if improvement <= rule.min_improvement_pct:
        reasons.append(
            f"out-of-sample return {challenger_out.total_return_pct:.2f}% does not beat "
            f"baseline {baseline.total_return_pct:.2f}% by {rule.min_improvement_pct:.2f} pp "
            f"(improvement {improvement:.2f} pp)"
        )

    drawdown_limit = baseline.max_drawdown_pct + rule.max_drawdown_slack_pct
    if challenger_out.max_drawdown_pct > drawdown_limit:
        reasons.append(
            f"drawdown {challenger_out.max_drawdown_pct:.2f}% exceeds baseline "
            f"{baseline.max_drawdown_pct:.2f}% plus {rule.max_drawdown_slack_pct:.2f} pp slack"
        )

    factor = _profit_factor(challenger_out)
    if factor is None:
        pass  # no losing trades; nothing to penalise
    elif factor < rule.min_profit_factor:
        reasons.append(
            f"profit factor {factor:.2f} is below the {rule.min_profit_factor:.2f} floor"
        )

    return ChallengerVerdict(
        name=name,
        params=dict(params),
        status=REJECT if reasons else PROMOTE,
        reasons=tuple(reasons),
        in_sample=challenger_in,
        out_of_sample=challenger_out,
    )


@dataclass(frozen=True)
class ChallengerReport:
    baseline_params: Mapping[str, object]
    baseline_in_sample: BacktestResult
    baseline_out_of_sample: BacktestResult
    verdicts: tuple[ChallengerVerdict, ...]
    rule: PromotionRule
    caveats: tuple[str, ...] = field(default_factory=tuple)

    @property
    def promotable(self) -> tuple[ChallengerVerdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.promoted)

    @property
    def insufficient(self) -> tuple[ChallengerVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == INSUFFICIENT)

    def best(self) -> ChallengerVerdict | None:
        """Best out-of-sample return among promoted challengers, else None."""
        promoted = self.promotable
        if not promoted:
            return None
        return max(promoted, key=lambda verdict: verdict.out_of_sample_return)

    def summary(self, *, limit: int = 25) -> str:
        lines = [
            "CHALLENGER EVALUATION",
            f"  baseline         {dict(self.baseline_params)}",
            f"  in-sample        {self.baseline_in_sample.total_return_pct:+.2f}%"
            f"  {len(self.baseline_in_sample.trades)} trade(s)",
            f"  out-of-sample    {self.baseline_out_of_sample.total_return_pct:+.2f}%"
            f"  drawdown {self.baseline_out_of_sample.max_drawdown_pct:.2f}%"
            f"  {len(self.baseline_out_of_sample.trades)} trade(s)",
            f"  rule             min {self.rule.min_out_of_sample_trades} OOS trades,"
            f" +{self.rule.min_improvement_pct:.2f} pp return,"
            f" drawdown slack {self.rule.max_drawdown_slack_pct:.2f} pp,"
            f" profit factor >= {self.rule.min_profit_factor:.2f}",
            "",
            f"  {'challenger':<40}{'OOS%':>9}{'dd%':>8}{'n':>6}  status",
            "  " + "-" * 78,
        ]
        ordered = sorted(
            self.verdicts,
            key=lambda verdict: verdict.out_of_sample_return,
            reverse=True,
        )
        for verdict in ordered[:limit]:
            label = ", ".join(f"{k}={v}" for k, v in verdict.params.items())[:39] or "(defaults)"
            lines.append(
                f"  {label:<40}{verdict.out_of_sample_return:>9.2f}"
                f"{verdict.out_of_sample.max_drawdown_pct:>8.2f}"
                f"{len(verdict.out_of_sample.trades):>6}  {verdict.status}"
            )
            for reason in verdict.reasons[:2]:
                lines.append(f"      - {reason[:100]}")
        lines.append("")
        best = self.best()
        if best is None:
            lines.append("  RESULT: no challenger may replace the baseline on this evidence")
        else:
            lines.append(
                f"  RESULT: {best.name} is a candidate for promotion; "
                "applying it remains a deliberate operator decision"
            )
        if self.insufficient:
            lines.append(
                f"  {len(self.insufficient)} challenger(s) had insufficient out-of-sample trades"
            )
        if self.caveats:
            lines.append("")
            lines.append("  CAVEATS")
            for caveat in self.caveats:
                lines.append(f"    - {caveat}")
        return "\n".join(lines)


CAVEATS: tuple[str, ...] = (
    "out-of-sample here is a single chronological split, not repeated walk-forward; "
    "confirm any candidate with `cryptobot walk-forward` before promoting it",
    "costs are the configured constants; a real fee change invalidates the comparison",
    "no challenger is promoted by this report; an operator must record an"
    " approval with `cryptobot promote` and then apply the change by hand",
)


def run_challengers(
    strategy_cls: type[Strategy],
    dataset: BarDataset,
    *,
    baseline: Mapping[str, object] | None = None,
    grid: Mapping[str, Sequence[object]] | None = None,
    config: BacktestConfig | None = None,
    train_fraction: float = 0.7,
    rule: PromotionRule | None = None,
    check_lookahead: bool = True,
) -> ChallengerReport:
    """Backtest the baseline and every grid variant, then judge them.

    A variant with a look-ahead violation is rejected outright: a strategy that
    reads the future cannot be compared with one that does not.
    """
    config = config or BacktestConfig()
    rule = rule or PromotionRule()
    baseline = dict(baseline or {})
    train_set, test_set = dataset.train_test(train_fraction=train_fraction)

    baseline_strategy = strategy_cls(baseline)
    baseline_in = run_backtest(baseline_strategy, train_set, config)
    baseline_out = run_backtest(baseline_strategy, test_set, config)

    verdicts: list[ChallengerVerdict] = []
    for params in _expand(grid):
        if params == baseline:
            continue
        try:
            strategy = strategy_cls(dict(params))
        except CryptobotError as exc:
            verdicts.append(
                ChallengerVerdict(
                    name=f"{strategy_cls.__name__}({params})",
                    params=dict(params),
                    status=REJECT,
                    reasons=(f"unconstructible parameter set: {exc}",),
                    in_sample=baseline_in,
                    out_of_sample=baseline_out,
                )
            )
            continue

        violations = tuple(verify_no_lookahead(strategy, dataset.bars)) if check_lookahead else ()
        if violations:
            verdicts.append(
                ChallengerVerdict(
                    name=f"{strategy_cls.__name__}({params})",
                    params=dict(params),
                    status=REJECT,
                    reasons=tuple(f"look-ahead violation: {v}" for v in violations),
                    in_sample=baseline_in,
                    out_of_sample=baseline_out,
                )
            )
            continue

        if len(train_set) <= strategy.min_bars or len(test_set) <= strategy.min_bars:
            verdicts.append(
                ChallengerVerdict(
                    name=f"{strategy_cls.__name__}({params})",
                    params=dict(params),
                    status=INSUFFICIENT,
                    reasons=(
                        f"dataset too short for this strategy: "
                        f"{len(train_set)} train / {len(test_set)} test bars, "
                        f"needs > {strategy.min_bars}",
                    ),
                    in_sample=baseline_in,
                    out_of_sample=baseline_out,
                )
            )
            continue

        train_result = run_backtest(strategy, train_set, config)
        test_result = run_backtest(strategy, test_set, config)
        verdicts.append(
            judge(
                baseline_out,
                train_result,
                test_result,
                name=f"{strategy_cls.__name__}({params})",
                params=params,
                rule=rule,
            )
        )

    return ChallengerReport(
        baseline_params=baseline,
        baseline_in_sample=baseline_in,
        baseline_out_of_sample=baseline_out,
        verdicts=tuple(verdicts),
        rule=rule,
        caveats=CAVEATS,
    )


def _expand(grid: Mapping[str, Sequence[object]] | None) -> Iterable[Mapping[str, object]]:
    if not grid:
        yield {}
        return
    keys = list(grid)
    combinations: list[dict[str, object]] = [{}]
    for key in keys:
        values = list(grid[key]) or [None]
        combinations = [{**combo, key: value} for combo in combinations for value in values]
    for combination in combinations:
        yield combination


__all__ = [
    "ChallengerReport",
    "ChallengerVerdict",
    "INSUFFICIENT",
    "PROMOTE",
    "PromotionRule",
    "REJECT",
    "judge",
    "run_challengers",
]
