"""A small, honest backtester.

Design constraints, all of which come from the spec:

* **Same code path as live.** The strategy, the sizing function, and the
  instrument precision rules are the production ones. Only the fill source
  differs.
* **No look-ahead, structurally.** A decision taken on the close of bar *i* is
  filled at the **open of bar i+1**. :func:`verify_no_lookahead` additionally
  proves prefix-invariance for the strategy itself.
* **Costs are explicit.** Fees and slippage are charged on both sides, using the
  same reserve assumptions the risk manager uses.
* **The $100 ceiling is enforced here too**, not just in live trading.

This is not a vectorised engine and makes no claim to be fast. It is small
enough to audit, which matters more for a $100 paper account.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from cryptobot.core.contracts import (
    Action,
    Bar,
    InstrumentSpec,
    to_decimal,
)
from cryptobot.core.errors import ContractError, DataError
from cryptobot.data.instruments import fallback_instrument
from cryptobot.research.dataset import BarDataset
from cryptobot.sizing.position import size_entry
from cryptobot.strategy.base import Strategy

HUNDRED = Decimal("100")


@dataclass(frozen=True)
class CostModel:
    """Explicit, conservative cost assumptions.

    Defaults match Alpaca's published tier-1 crypto fees (maker 15 bps / taker
    25 bps) with the taker rate applied on both sides, because a market order
    always takes liquidity. Slippage defaults to the same 25 bps the risk manager
    reserves.
    """

    fee_bps: Decimal = Decimal("25")
    slippage_bps: Decimal = Decimal("25")

    def __post_init__(self) -> None:
        for name in ("fee_bps", "slippage_bps"):
            value = to_decimal(getattr(self, name), name)
            if value < 0:
                raise ContractError(f"{name} must be >= 0")
            object.__setattr__(self, name, value)

    @property
    def fee_fraction(self) -> Decimal:
        return self.fee_bps / Decimal("10000")

    @property
    def slippage_fraction(self) -> Decimal:
        return self.slippage_bps / Decimal("10000")


@dataclass(frozen=True)
class BacktestConfig:
    ceiling: Decimal = Decimal("100")
    max_position_pct: Decimal = Decimal("25")
    per_trade_risk_pct: Decimal = Decimal("20")
    costs: CostModel = field(default_factory=CostModel)
    #: Decision on bar *i*'s close, fill on bar *i + delay*'s open. Never 0.
    execution_delay_bars: int = 1
    instrument: InstrumentSpec | None = None

    def __post_init__(self) -> None:
        if self.execution_delay_bars < 1:
            raise ContractError(
                "execution_delay_bars must be >= 1; filling on the decision bar's own "
                "close would leak the price the decision was based on"
            )
        if to_decimal(self.ceiling) <= 0:
            raise ContractError("ceiling must be positive")

    def resolve_instrument(self, symbol: str) -> InstrumentSpec:
        if self.instrument is not None:
            return self.instrument
        found = fallback_instrument(symbol)
        if found is None:
            raise DataError(
                f"no instrument metadata for {symbol}; pass BacktestConfig.instrument"
            )
        return found


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    fees: Decimal
    net_pnl: Decimal
    bars_held: int
    exit_reason: str

    @property
    def return_pct(self) -> Decimal:
        cost = self.entry_price * self.quantity
        return (self.net_pnl / cost * HUNDRED) if cost else Decimal("0")


@dataclass
class BacktestResult:
    strategy_name: str
    strategy_version: str
    params: Mapping[str, object]
    dataset: BarDataset
    config: BacktestConfig
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    fees_paid: Decimal = Decimal("0")
    bars_holding: int = 0
    signals_seen: int = 0
    signals_rejected: int = 0
    open_quantity: Decimal = Decimal("0")
    open_entry_price: Decimal = Decimal("0")

    # -- derived ------------------------------------------------------------

    @property
    def ceiling(self) -> Decimal:
        return to_decimal(self.config.ceiling)

    @property
    def final_equity(self) -> Decimal:
        if not self.equity_curve:
            return self.ceiling
        return self.equity_curve[-1][1]

    @property
    def total_return_pct(self) -> Decimal:
        return (self.final_equity - self.ceiling) / self.ceiling * HUNDRED

    @property
    def max_drawdown_pct(self) -> Decimal:
        peak = self.ceiling
        worst = Decimal("0")
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak * HUNDRED)
        return worst

    @property
    def buy_and_hold_pct(self) -> Decimal:
        first = self.dataset.bars[0].close
        last = self.dataset.bars[-1].close
        return (last - first) / first * HUNDRED if first else Decimal("0")

    @property
    def exposure_pct(self) -> Decimal:
        if not self.dataset.bars:
            return Decimal("0")
        return Decimal(self.bars_holding) / Decimal(len(self.dataset.bars)) * HUNDRED

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl < 0)

    @property
    def win_rate_pct(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return Decimal(self.wins) / Decimal(len(self.trades)) * HUNDRED

    @property
    def profit_factor(self) -> Decimal | None:
        gross_win = sum((t.net_pnl for t in self.trades if t.net_pnl > 0), Decimal("0"))
        gross_loss = -sum((t.net_pnl for t in self.trades if t.net_pnl < 0), Decimal("0"))
        if gross_loss == 0:
            return None if gross_win == 0 else Decimal("Infinity")
        return gross_win / gross_loss

    @property
    def beats_buy_and_hold(self) -> bool:
        return self.total_return_pct > self.buy_and_hold_pct

    def metrics(self) -> dict[str, str]:
        pf = self.profit_factor
        return {
            "bars": str(len(self.dataset.bars)),
            "final_equity": format(self.final_equity, ".2f"),
            "total_return_pct": format(self.total_return_pct, ".2f"),
            "buy_and_hold_pct": format(self.buy_and_hold_pct, ".2f"),
            "excess_vs_hold_pct": format(self.total_return_pct - self.buy_and_hold_pct, ".2f"),
            "max_drawdown_pct": format(self.max_drawdown_pct, ".2f"),
            "trades": str(len(self.trades)),
            "win_rate_pct": format(self.win_rate_pct, ".2f"),
            "profit_factor": "n/a" if pf is None else format(pf, ".2f"),
            "fees_paid": format(self.fees_paid, ".2f"),
            "exposure_pct": format(self.exposure_pct, ".2f"),
            "signals_seen": str(self.signals_seen),
            "signals_rejected": str(self.signals_rejected),
            "open_quantity": format(self.open_quantity, "f"),
        }

    def summary(self) -> str:
        if not self.trades:
            verdict = "no closed trades; metrics are not informative"
        elif self.beats_buy_and_hold:
            verdict = "outperformed buy-and-hold on this sample (one sample, not evidence)"
        else:
            verdict = "did not beat buy-and-hold on this sample"
        lines = [
            f"{self.strategy_name} {self.strategy_version} on {self.dataset.describe()}",
            f"  costs            fee={self.config.costs.fee_bps}bps "
            f"slippage={self.config.costs.slippage_bps}bps",
            f"  ceiling          {format(self.ceiling, '.2f')}",
        ]
        for key, value in self.metrics().items():
            lines.append(f"  {key:<20}{value}")
        lines.append(f"  verdict          {verdict}")
        if self.open_quantity > 0:
            lines.append(
                f"  open position    {format(self.open_quantity, 'f')} @ "
                f"{format(self.open_entry_price, '.2f')} (marked, not closed)"
            )
        return "\n".join(lines)


def verify_no_lookahead(
    strategy: Strategy, bars: Sequence[Bar], *, samples: int = 12
) -> list[str]:
    """Prove that a strategy's signal for bar *i* ignores bars after *i*.

    Compares ``generate(bars[:k])[-1]`` against ``generate(bars)[k - 1]`` at a
    spread of cut points. Returns a list of human-readable violations; an empty
    list means every sample agreed.
    """
    if len(bars) < strategy.min_bars + 1:
        raise DataError("not enough bars to verify causality")
    full = strategy.generate(bars)
    step = max(1, (len(bars) - strategy.min_bars) // max(1, samples))
    violations: list[str] = []
    for k in range(strategy.min_bars, len(bars), step):
        prefix_signal = strategy.generate(bars[:k])[-1]
        full_signal = full[k - 1]
        if prefix_signal.action is not full_signal.action:
            violations.append(
                f"at index {k}: prefix says {prefix_signal.action.value}, "
                f"full series says {full_signal.action.value}"
            )
        if prefix_signal.indicators != full_signal.indicators:
            violations.append(f"at index {k}: indicator values differ")
    return violations


def run_backtest(
    strategy: Strategy,
    dataset: BarDataset,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    config = config or BacktestConfig()
    bars = dataset.bars
    if len(bars) < strategy.min_bars + config.execution_delay_bars + 1:
        raise DataError(
            f"need at least {strategy.min_bars + config.execution_delay_bars + 1} bars, "
            f"got {len(bars)}"
        )

    instrument = config.resolve_instrument(dataset.symbol)
    costs = config.costs
    signals = strategy.generate(bars)

    cash = config.ceiling
    quantity = Decimal("0")
    entry_price = Decimal("0")
    entry_fee = Decimal("0")
    entry_index = 0
    pending: str | None = None

    result = BacktestResult(
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        params=dict(strategy.params),
        dataset=dataset,
        config=config,
    )

    for index, bar in enumerate(bars):
        # 1. fill the decision taken `execution_delay_bars` bars ago
        if pending == "buy" and quantity == 0:
            fill_price = bar.open * (Decimal(1) + costs.slippage_fraction)
            sizing = size_entry(
                available=cash,
                price=fill_price,
                ceiling=config.ceiling,
                max_position_pct=config.max_position_pct,
                per_trade_risk_pct=config.per_trade_risk_pct,
                instrument=instrument,
                fee_reserve_fraction=costs.fee_fraction,
                slippage_reserve_fraction=costs.slippage_fraction,
            )
            if sizing.ok and sizing.quantity > 0:
                notional = sizing.quantity * fill_price
                fee = notional * costs.fee_fraction
                cash -= notional + fee
                quantity = sizing.quantity
                entry_price = fill_price
                entry_fee = fee
                entry_index = index
                result.fees_paid += fee
            else:
                result.signals_rejected += 1
        elif pending == "sell" and quantity > 0:
            fill_price = bar.open * (Decimal(1) - costs.slippage_fraction)
            notional = quantity * fill_price
            fee = notional * costs.fee_fraction
            cash += notional - fee
            result.fees_paid += fee
            gross = (fill_price - entry_price) * quantity
            result.trades.append(
                TradeRecord(
                    symbol=dataset.symbol,
                    entry_ts=bars[entry_index].ts,
                    exit_ts=bar.ts,
                    entry_price=entry_price,
                    exit_price=fill_price,
                    quantity=quantity,
                    fees=entry_fee + fee,
                    net_pnl=gross - entry_fee - fee,
                    bars_held=index - entry_index,
                    exit_reason="strategy exit",
                )
            )
            quantity = Decimal("0")
            entry_price = Decimal("0")
            entry_fee = Decimal("0")
        pending = None

        # 2. mark the book at the close
        if quantity > 0:
            result.bars_holding += 1
        result.equity_curve.append((bar.ts, cash + quantity * bar.close))

        # 3. take this bar's decision, to be filled on a later bar
        signal = signals[index]
        if signal.action is not Action.HOLD:
            result.signals_seen += 1
        if index >= len(bars) - config.execution_delay_bars:
            continue
        if signal.action is Action.ENTER_LONG and quantity == 0:
            pending = "buy"
        elif signal.action is Action.EXIT_LONG and quantity > 0:
            pending = "sell"

    # an open position is marked, never force-closed: closing at the last bar
    # would invent a fill that the strategy did not ask for
    if quantity > 0:
        result.open_quantity = quantity
        result.open_entry_price = entry_price

    return result


def compare(
    left: BacktestResult, right: BacktestResult
) -> dict[str, tuple[str, str, str]]:
    """Side-by-side metric comparison: ``{metric: (left, right, delta)}``."""
    a, b = left.metrics(), right.metrics()
    out: dict[str, tuple[str, str, str]] = {}
    for key in a:
        if key not in b:
            continue
        try:
            delta = Decimal(a[key]) - Decimal(b[key])
            out[key] = (a[key], b[key], format(delta, ".2f"))
        except Exception:  # noqa: BLE001 - non-numeric metrics
            out[key] = (a[key], b[key], "n/a")
    return out


def parameter_sensitivity(
    strategy_factory,
    dataset: BarDataset,
    grid: Mapping[str, Sequence[object]],
    *,
    config: BacktestConfig | None = None,
) -> list[tuple[Mapping[str, object], BacktestResult]]:
    """One-factor-at-a-time sweep.

    Deliberately *not* a full cartesian product: the point is to see whether a
    result survives small perturbations, not to maximise it.
    """
    baseline = dict(strategy_factory().params)
    out: list[tuple[Mapping[str, object], BacktestResult]] = []
    for key, values in grid.items():
        for value in values:
            params = dict(baseline)
            params[key] = value
            strategy = strategy_factory(params)
            out.append((params, run_backtest(strategy, dataset, config)))
    return out


def summarise_sensitivity(
    results: Iterable[tuple[Mapping[str, object], BacktestResult]]
) -> str:
    rows = [(dict(params), result) for params, result in results]
    if not rows:
        return "no sensitivity results"
    best = max(rows, key=lambda item: item[1].total_return_pct)
    worst = min(rows, key=lambda item: item[1].total_return_pct)
    lines = [
        f"{'params':<44}{'return%':>10}{'dd%':>8}{'trades':>8}{'vs hold':>10}",
        "-" * 80,
    ]
    for params, result in rows:
        label = ", ".join(f"{k}={v}" for k, v in params.items())[:43]
        lines.append(
            f"{label:<44}{result.total_return_pct:>10.2f}{result.max_drawdown_pct:>8.2f}"
            f"{len(result.trades):>8}{result.total_return_pct - result.buy_and_hold_pct:>10.2f}"
        )
    spread = best[1].total_return_pct - worst[1].total_return_pct
    lines.append("-" * 80)
    lines.append(
        f"spread across variants: {spread:.2f}pp; best={best[1].total_return_pct:.2f}% "
        f"worst={worst[1].total_return_pct:.2f}% "
        "(a wide spread means the result is parameter luck, not edge)"
    )
    return "\n".join(lines)


def rebase(
    strategy: Strategy,
    dataset: BarDataset,
    config: BacktestConfig,
    ceiling: Decimal,
) -> BacktestResult:
    """Re-run the strategy with a different ceiling.

    A true re-run, not a relabelling of an existing result: a different ceiling
    changes the order size, and therefore the trade sequence itself.
    """
    return run_backtest(strategy, dataset, replace(config, ceiling=ceiling))
