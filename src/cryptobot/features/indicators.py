"""Causal indicator implementations.

Every function here is *causal*: the value at index ``i`` depends only on inputs
``0..i``. That single property is what makes look-ahead bias structurally
impossible in the strategy layer, and it is asserted directly in the tests
(`test_indicators.py::test_causality`).

Implemented with ``Decimal`` because a $100 allocation trading 0.0001 BTC is
exactly the regime where float error stops being cosmetic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from cryptobot.core.contracts import Bar


def sma(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[Decimal | None] = [None] * len(values)
    window = Decimal(0)
    for i, value in enumerate(values):
        window += value
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = Decimal(2) / (Decimal(period) + 1)
    previous = seed
    for i in range(period, len(values)):
        previous = (values[i] - previous) * alpha + previous
        out[i] = previous
    return out


def rsi(values: Sequence[Decimal], period: int = 14) -> list[Decimal | None]:
    """Wilder's RSI."""
    if period < 2:
        raise ValueError("period must be >= 2")
    n = len(values)
    out: list[Decimal | None] = [None] * n
    if n <= period:
        return out

    gains = Decimal(0)
    losses = Decimal(0)
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, Decimal(0))
        losses += max(-change, Decimal(0))
    avg_gain = gains / period
    avg_loss = losses / period

    def _value(gain: Decimal, loss: Decimal) -> Decimal:
        if loss == 0:
            return Decimal(100) if gain > 0 else Decimal(50)
        rs = gain / loss
        return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))

    out[period] = _value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = values[i] - values[i - 1]
        gain = max(change, Decimal(0))
        loss = max(-change, Decimal(0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _value(avg_gain, avg_loss)
    return out


def true_ranges(bars: Sequence[Bar]) -> list[Decimal]:
    ranges: list[Decimal] = []
    for i, bar in enumerate(bars):
        if i == 0:
            ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[i - 1].close
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return ranges


def atr(bars: Sequence[Bar], period: int = 14) -> list[Decimal | None]:
    """Wilder's Average True Range."""
    if period < 1:
        raise ValueError("period must be >= 1")
    ranges = true_ranges(bars)
    n = len(ranges)
    out: list[Decimal | None] = [None] * n
    if n < period:
        return out
    previous = sum(ranges[:period]) / period
    out[period - 1] = previous
    for i in range(period, n):
        previous = (previous * (period - 1) + ranges[i]) / period
        out[i] = previous
    return out


def donchian(
    bars: Sequence[Bar], period: int
) -> tuple[list[Decimal | None], list[Decimal | None]]:
    """Highest high / lowest low of the ``period`` bars *before* index ``i``.

    The current bar is excluded on purpose: including it would let the bar decide
    whether it is a breakout of itself.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    n = len(bars)
    upper: list[Decimal | None] = [None] * n
    lower: list[Decimal | None] = [None] * n
    for i in range(n):
        start = i - period
        if start < 0:
            continue
        window = bars[start:i]
        if not window:
            continue
        upper[i] = max(bar.high for bar in window)
        lower[i] = min(bar.low for bar in window)
    return upper, lower


def last(values: Sequence[Decimal | None]) -> Decimal | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def pct(part: Decimal, whole: Decimal) -> Decimal:
    if whole == 0:
        return Decimal(0)
    return (part / whole) * Decimal(100)
