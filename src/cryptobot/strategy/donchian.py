"""Alternative registered strategy: Donchian breakout.

Registered to prove the strategy registry accepts a second implementation without
touching the engine. The run-state default config selects ``sma_cross``; use this
one only through an explicit experiment.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Mapping, Sequence

from cryptobot.core.contracts import Action, Bar, StrategySignal
from cryptobot.features.indicators import atr, donchian
from cryptobot.strategy.base import Strategy, register


@register
class DonchianBreakout(Strategy):
    name: ClassVar[str] = "donchian_breakout"

    def __init__(self, params: Mapping[str, object] | None = None) -> None:
        super().__init__(params)
        self.period = self._int_param("period", 20, minimum=2)
        self.atr_period = self._int_param("atr_period", 14, minimum=2)

    @property
    def min_bars(self) -> int:
        return max(self.period, self.atr_period) + 1

    def generate(self, bars: Sequence[Bar]) -> list[StrategySignal]:
        upper, lower = donchian(bars, self.period)
        atrs = atr(bars, self.atr_period)

        signals: list[StrategySignal] = []
        for i, bar in enumerate(bars):
            indicators: dict[str, Decimal | None] = {
                "donchian_upper": upper[i],
                "donchian_lower": lower[i],
                "atr": atrs[i],
            }
            if upper[i] is None or lower[i] is None:
                signals.append(self._signal(bar, Action.HOLD, "warmup", indicators))
                continue

            if bar.close > upper[i]:
                signals.append(
                    self._signal(
                        bar,
                        Action.ENTER_LONG,
                        f"close {bar.close:.8f} broke above {upper[i]:.8f}",
                        indicators,
                    )
                )
            elif bar.close < lower[i]:
                signals.append(
                    self._signal(
                        bar,
                        Action.EXIT_LONG,
                        f"close {bar.close:.8f} broke below {lower[i]:.8f}",
                        indicators,
                    )
                )
            else:
                signals.append(self._signal(bar, Action.HOLD, "inside channel", indicators))
        return signals
