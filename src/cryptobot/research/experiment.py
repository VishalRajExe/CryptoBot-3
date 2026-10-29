"""Experiment runner.

The hard part of research is not running a backtest, it is not lying about what
the result means. This module therefore records, for every run:

* the exact strategy version and parameter set,
* the dataset fingerprint and period,
* the cost assumptions,
* how many variants were attempted in the same suite,

and it always prints the multiple-comparisons caveat next to the numbers. A
"best" result is reported as *the best of N attempts on one sample*, never as a
validated edge.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Sequence

from cryptobot import __version__
from cryptobot.core.contracts import Bar
from cryptobot.core.errors import ContractError, CryptobotError, DataError
from cryptobot.journal.store import Journal
from cryptobot.research.backtest import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
    verify_no_lookahead,
)
from cryptobot.research.dataset import BarDataset
from cryptobot.strategy.base import Strategy

UTC = timezone.utc
CODE_VERSION = f"cryptobot-{__version__}"

StrategyFactory = Callable[[Mapping[str, object]], Strategy]


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    name: str
    params: Mapping[str, object]
    strategy_name: str
    strategy_version: str
    train: BacktestResult
    test: BacktestResult
    created_at: datetime
    lookahead_violations: tuple[str, ...] = ()
    notes: str = ""

    @property
    def train_return(self) -> Decimal:
        return self.train.total_return_pct

    @property
    def test_return(self) -> Decimal:
        return self.test.total_return_pct

    @property
    def degradation(self) -> Decimal:
        return self.train_return - self.test_return

    @property
    def min_trades_satisfied(self) -> bool:
        return min(len(self.train.trades), len(self.test.trades)) >= 1

    def to_payload(self) -> dict[str, object]:
        metrics = {
            "train_" + k: v for k, v in self.train.metrics().items()
        } | {"test_" + k: v for k, v in self.test.metrics().items()}
        return {
            "experiment_id": self.experiment_id,
            "created_at": self.created_at.isoformat(),
            "name": self.name,
            "strategy": self.strategy_name,
            "strategy_version": self.strategy_version,
            "params": dict(self.params),
            "costs": {
                "fee_bps": str(self.train.config.costs.fee_bps),
                "slippage_bps": str(self.train.config.costs.slippage_bps),
            },
            "period_start": self.train.dataset.start.isoformat(),
            "period_end": self.test.dataset.end.isoformat(),
            # Both windows are identified. A fingerprint covering only the
            # in-sample bars could not be used to verify an out-of-sample claim.
            "data_fingerprint": (
                f"{self.train.dataset.fingerprint}+{self.test.dataset.fingerprint}"
            ),
            "code_version": CODE_VERSION,
            "metrics": metrics,
            "notes": self.notes,
        }


@dataclass
class SuiteResult:
    name: str
    attempts: int
    records: list[ExperimentRecord] = field(default_factory=list)
    benchmark: BacktestResult | None = None
    caveats: tuple[str, ...] = ()
    min_trades: int = 1
    #: Combinations that could not even be constructed (e.g. fast >= slow).
    invalid: list[tuple[Mapping[str, object], str]] = field(default_factory=list)

    # -- selection ----------------------------------------------------------

    def best_on_train(self) -> ExperimentRecord | None:
        if not self.records:
            return None
        return max(self.records, key=lambda r: r.train_return)

    def best_on_test(self) -> ExperimentRecord | None:
        """Shown only to demonstrate that the test set is not a selection tool."""
        if not self.records:
            return None
        return max(self.records, key=lambda r: r.test_return)

    def beats_benchmark(self) -> bool:
        if self.benchmark is None:
            return False
        best = self.best_on_train()
        return best is not None and best.test_return > self.benchmark.total_return_pct

    # -- reporting ----------------------------------------------------------

    def summary(self, *, limit: int = 25) -> str:
        lines = [
            f"EXPERIMENT SUITE {self.name}",
            f"  code version     {CODE_VERSION}",
            f"  attempts         {self.attempts} (every attempted variant is recorded)",
        ]
        if self.invalid:
            lines.append(f"  invalid          {len(self.invalid)} combination(s) rejected:")
            for params, reason in self.invalid[:5]:
                lines.append(f"                   {dict(params)} -> {reason[:70]}")
        if self.records:
            dataset = self.records[0].train.dataset
            lines.append(f"  in-sample        {dataset.describe()}")
            lines.append(f"  out-of-sample    {self.records[0].test.dataset.describe()}")
        lines.append("")
        lines.append(
            f"  {'params':<40}{'train%':>9}{'test%':>9}{'dd%':>8}{'trades':>8}"
        )
        lines.append("  " + "-" * 76)
        for record in sorted(self.records, key=lambda r: r.train_return, reverse=True)[:limit]:
            label = ", ".join(f"{k}={v}" for k, v in record.params.items())[:39]
            lines.append(
                f"  {label:<40}{record.train_return:>9.2f}{record.test_return:>9.2f}"
                f"{record.test.max_drawdown_pct:>8.2f}"
                f"{len(record.test.trades):>8}"
            )
        if self.benchmark is not None:
            lines.append(
                f"  {'BUY & HOLD (same test window)':<40}"
                f"{'':>9}{self.benchmark.total_return_pct:>9.2f}"
                f"{self.benchmark.max_drawdown_pct:>8.2f}{0:>8}"
            )
        lines.append("")
        best = self.best_on_train()
        if best is not None:
            selected = f"  selected on train: {dict(best.params)} -> test {best.test_return:.2f}%"
            if self.benchmark is not None:
                selected += f" vs buy&hold {self.benchmark.total_return_pct:.2f}%"
            lines.append(selected)
            if len(best.test.trades) < self.min_trades:
                lines.append(
                    f"  WARNING: only {len(best.test.trades)} out-of-sample trades; "
                    f"the requirement is {self.min_trades}. Result is not informative."
                )
        lines.append("")
        lines.append("  CAVEATS")
        for caveat in self.caveats:
            lines.append(f"    - {caveat}")
        return "\n".join(lines)


def run_suite(
    name: str,
    strategy_cls: type[Strategy],
    dataset: BarDataset,
    grid: Mapping[str, Sequence[object]] | None = None,
    *,
    config: BacktestConfig | None = None,
    train_fraction: float = 0.7,
    journal: Journal | None = None,
    min_trades: int = 10,
    check_lookahead: bool = True,
) -> SuiteResult:
    """Run every parameter combination and record it, selected or not."""
    config = config or BacktestConfig()
    train_set, test_set = dataset.train_test(train_fraction=train_fraction)
    combinations = _expand(grid)
    if not combinations:
        combinations = [{}]

    records: list[ExperimentRecord] = []
    invalid: list[tuple[Mapping[str, object], str]] = []
    for params in combinations:
        try:
            strategy = strategy_cls(dict(params))
        except CryptobotError as exc:
            # An unconstructible parameter set is not a candidate, but it is
            # still reported so the attempted search space stays visible.
            invalid.append((dict(params), str(exc)))
            continue
        if len(train_set) <= strategy.min_bars or len(test_set) <= strategy.min_bars:
            # Still recorded as an attempt: silently dropping variants is how
            # selection bias creeps in.
            records.append(
                _degenerate(name, params, strategy, train_set, test_set, config, min_trades)
            )
            continue
        violations: tuple[str, ...] = ()
        if check_lookahead:
            violations = tuple(verify_no_lookahead(strategy, dataset.bars))
        train_result = run_backtest(strategy, train_set, config)
        test_result = run_backtest(strategy, test_set, config)
        record = ExperimentRecord(
            experiment_id=f"exp-{uuid.uuid4().hex[:16]}",
            name=name,
            params=dict(params),
            strategy_name=strategy.name,
            strategy_version=strategy.version,
            train=train_result,
            test=test_result,
            created_at=datetime.now(UTC),
            lookahead_violations=violations,
            notes="look-ahead violations detected" if violations else "",
        )
        records.append(record)
        if journal is not None:
            journal.save_experiment(record.to_payload())

    benchmark = None
    if len(test_set) > _BuyAndHold({}).min_bars:
        benchmark = run_backtest(_BuyAndHold({}), test_set, config)

    return SuiteResult(
        name=name,
        attempts=len(records) + len(invalid),
        records=records,
        benchmark=benchmark,
        caveats=(
            f"{len(records) + len(invalid)} variants were attempted, "
            f"{len(invalid)} of them invalid; the best of the rest is a "
            "selection, not a validated edge",
            "one symbol, one sample, one set of cost assumptions",
            f"out-of-sample window is {len(test_set)} bars, which is short",
            "fees use the taker rate on both sides; slippage is an assumption",
            "a profitable backtest does not imply a profitable live strategy",
        ),
        min_trades=min_trades,
        invalid=invalid,
    )


def walk_forward(
    strategy_cls: type[Strategy],
    params: Mapping[str, object],
    dataset: BarDataset,
    *,
    train_bars: int,
    test_bars: int,
    config: BacktestConfig | None = None,
) -> list[tuple[int, BacktestResult, BacktestResult]]:
    """Chronological folds. Each fold's test window is strictly after its train."""
    config = config or BacktestConfig()
    strategy = strategy_cls(dict(params))
    if train_bars < strategy.min_bars + 2:
        raise ContractError(
            f"train_bars must be at least {strategy.min_bars + 2} for {strategy.name}"
        )
    folds = dataset.walk_forward(train_bars=train_bars, test_bars=test_bars)
    out: list[tuple[int, BacktestResult, BacktestResult]] = []
    for index, (train_set, test_set) in enumerate(folds, start=1):
        out.append(
            (
                index,
                run_backtest(strategy, train_set, config),
                run_backtest(strategy, test_set, config),
            )
        )
    return out


def walk_forward_optimised(
    strategy_cls: type[Strategy],
    grid: Mapping[str, Sequence[object]],
    dataset: BarDataset,
    *,
    train_bars: int,
    test_bars: int,
    config: BacktestConfig | None = None,
) -> list[tuple[int, Mapping[str, object], BacktestResult, BacktestResult]]:
    """Walk-forward *with re-selection*: choose parameters on each train window.

    This is the form of walk-forward that actually tests a process rather than a
    single parameter set, and it is also where multiple-comparisons bias enters,
    because every fold selects a winner again. The caveats in the report say so.
    """
    config = config or BacktestConfig()
    combinations = _expand(grid)
    if not combinations:
        raise ContractError("walk-forward requires a parameter grid")
    out: list[tuple[int, Mapping[str, object], BacktestResult, BacktestResult]] = []
    for index, (train_set, test_set) in enumerate(
        dataset.walk_forward(train_bars=train_bars, test_bars=test_bars), start=1
    ):
        best_params: Mapping[str, object] | None = None
        best_train: BacktestResult | None = None
        for params in combinations:
            try:
                strategy = strategy_cls(dict(params))
            except CryptobotError:
                continue
            if len(train_set) <= strategy.min_bars:
                continue
            result = run_backtest(strategy, train_set, config)
            if best_train is None or result.total_return_pct > best_train.total_return_pct:
                best_params, best_train = dict(params), result
        if best_params is None or best_train is None:
            continue
        strategy = strategy_cls(dict(best_params))
        if len(test_set) <= strategy.min_bars:
            continue
        out.append((index, best_params, best_train, run_backtest(strategy, test_set, config)))
    return out


def render_walk_forward_optimised(
    folds: Iterable[tuple[int, Mapping[str, object], BacktestResult, BacktestResult]],
) -> str:
    rows = list(folds)
    if not rows:
        return "walk-forward: no folds produced (dataset too short for the grid)"
    lines = [
        f"{'fold':>5}  {'selected on train':<38}{'train%':>9}{'test%':>9}{'trades':>7}",
        "-" * 70,
    ]
    positives = 0
    selections = 0
    for index, params, train_result, test_result in rows:
        label = ", ".join(f"{k}={v}" for k, v in params.items())[:37]
        positives += test_result.total_return_pct > 0
        selections += 1
        lines.append(
            f"{index:>5}  {label:<38}{train_result.total_return_pct:>9.2f}"
            f"{test_result.total_return_pct:>9.2f}{len(test_result.trades):>7}"
        )
    lines.append("-" * 70)
    lines.append(
        f"{positives}/{selections} folds profitable out of sample after re-selecting "
        "parameters on each train window"
    )
    lines.append(
        "  parameters changed every fold by design; if they jump around, the strategy "
        "is fitting noise"
    )
    return "\n".join(lines)


def render_walk_forward(
    folds: Iterable[tuple[int, BacktestResult, BacktestResult]]
) -> str:
    rows = list(folds)
    if not rows:
        return "walk-forward: no folds produced (dataset too short)"
    lines = [
        f"{'fold':>5}{'train%':>10}{'test%':>10}{'test dd%':>10}{'test trades':>13}",
        "-" * 48,
    ]
    positives = 0
    total_trades = 0
    for index, train_result, test_result in rows:
        if test_result.total_return_pct > 0:
            positives += 1
        total_trades += len(test_result.trades)
        lines.append(
            f"{index:>5}{train_result.total_return_pct:>10.2f}"
            f"{test_result.total_return_pct:>10.2f}{test_result.max_drawdown_pct:>10.2f}"
            f"{len(test_result.trades):>13}"
        )
    lines.append("-" * 48)
    lines.append(
        f"{positives}/{len(rows)} folds were profitable out of sample; "
        "consistency matters more than the mean"
    )
    if total_trades == 0:
        lines.append(
            "  WARNING: no out-of-sample trades at all. These numbers measure idle "
            "cash, not the strategy; use a shorter timeframe window or a wider grid."
        )
    return "\n".join(lines)


class _BuyAndHold(Strategy):
    name = "buy_and_hold"
    description = "benchmark: buy at the first opportunity, never sell"

    @property
    def min_bars(self) -> int:
        return 2

    def generate(self, bars: Sequence[Bar]) -> list:
        from cryptobot.core.contracts import Action

        signals = []
        for index, bar in enumerate(bars):
            if index == 0:
                action, reason = Action.ENTER_LONG, "benchmark entry"
            else:
                action, reason = Action.HOLD, "benchmark holds"
            signals.append(self._signal(bar, action, reason, {}))
        return signals


def _expand(grid: Mapping[str, Sequence[object]] | None) -> list[dict[str, object]]:
    if not grid:
        return []
    keys = list(grid)
    values = [list(grid[k]) for k in keys]
    for key, items in zip(keys, values):
        if not items:
            raise ContractError(f"empty value list for parameter {key!r}")
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _degenerate(
    name: str,
    params: Mapping[str, object],
    strategy: Strategy,
    train_set: BarDataset,
    test_set: BarDataset,
    config: BacktestConfig,
    min_trades: int,
) -> ExperimentRecord:
    empty = BacktestResult(
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        params=dict(params),
        dataset=train_set,
        config=config,
    )
    empty_test = BacktestResult(
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        params=dict(params),
        dataset=test_set,
        config=config,
    )
    return ExperimentRecord(
        experiment_id=f"exp-{uuid.uuid4().hex[:16]}",
        name=name,
        params=dict(params),
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        train=empty,
        test=empty_test,
        created_at=datetime.now(UTC),
        notes=f"insufficient bars for {strategy.name} (min_bars={strategy.min_bars})",
    )
