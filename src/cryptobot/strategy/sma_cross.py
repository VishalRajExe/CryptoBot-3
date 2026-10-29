"""Baseline strategy: simple moving average crossover.

Chosen as the baseline because the rule is fully traceable and has no tunable
obscurity: enter when the fast SMA crosses above the slow SMA, exit when it
crosses back below. Parameter count is two, so the search space stays honest
(see Phase 7's multiple-comparisons warning).

Interface shape adapted from Freqtrade's ``IStrategy`` (indicators computed once,
entry/exit separated from position management); the position bookkeeping lives in
the engine here, not in the strategy, which is what keeps live and backtest
identical.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Mapping, Sequence

from cryptobot.core.contracts import Action, Bar, StrategySignal
from cryptobot.core.errors import ConfigError
from cryptobot.features.indicators import sma
from cryptobot.strategy.base import Strategy, register


@register
class SmaCross(Strategy):
    name: ClassVar[str] = "sma_cross"

    def __init__(self, params: Mapping[str, object] | None = None) -> None:
        super().__init__(params)
        self.fast_period = self._int_param("fast_period", 12, minimum=2)
        self.slow_period = self._int_param("slow_period", 48, minimum=3)
        if self.fast_period >= self.slow_period:
            raise ConfigError(
                f"{self.name}: fast_period ({self.fast_period}) must be < "
                f"slow_period ({self.slow_period})"
            )

    @property
    def min_bars(self) -> int:
        # slow SMA needs slow_period bars; a crossover needs one more to compare.
        return self.slow_period + 1

    def generate(self, bars: Sequence[Bar]) -> list[StrategySignal]:
        closes = [bar.close for bar in bars]
        fast = sma(closes, self.fast_period)
        slow = sma(closes, self.slow_period)

        signals: list[StrategySignal] = []
        for i, bar in enumerate(bars):
            indicators: dict[str, Decimal | None] = {
                "fast_sma": fast[i],
                "slow_sma": slow[i],
            }
            if i == 0 or fast[i] is None or slow[i] is None or fast[i - 1] is None or slow[i - 1] is None:
                signals.append(self._signal(bar, Action.HOLD, "warmup", indicators))
                continue

            crossed_up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
            crossed_down = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
            if crossed_up:
                signals.append(
                    self._signal(
                        bar,
                        Action.ENTER_LONG,
                        f"fast SMA {fast[i]:.8f} crossed above slow SMA {slow[i]:.8f}",
                        indicators,
                    )
                )
            elif crossed_down:
                signals.append(
                    self._signal(
                        bar,
                        Action.EXIT_LONG,
                        f"fast SMA {fast[i]:.8f} crossed below slow SMA {slow[i]:.8f}",
                        indicators,
                    )
                )
            else:
                signals.append(self._signal(bar, Action.HOLD, "no crossover", indicators))
        return signals
