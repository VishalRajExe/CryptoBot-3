"""Strategy interface and registry.

The interface is deliberately *batch oriented*: a strategy turns a sequence of
closed bars into one signal per bar. Two consequences, both required by the spec:

1. Live and backtest cannot diverge. The backtester and the live engine call the
   same ``generate`` on the same input; there is no second "live only" path whose
   logic could drift.
2. Look-ahead is testable. Because ``generate`` only ever appends signals in
   order, a test can feed a truncated series and assert the earlier signals are
   unchanged.

Signals describe a *desired state change* (enter / exit). The engine decides
whether the change is already satisfied by the current position, which is where
duplicate-signal suppression lives.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, ClassVar, Mapping, Sequence

from cryptobot.config.settings import StrategyConfig
from cryptobot.core import jsonutil
from cryptobot.core.contracts import Action, Bar, StrategySignal
from cryptobot.core.errors import ConfigError

#: Bump when indicator maths changes, so recorded strategy versions stay honest.
ENGINE_VERSION = "strategies-v1"


class Strategy(ABC):
    name: ClassVar[str] = "base"

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})

    # -- identity -----------------------------------------------------------

    @property
    def version(self) -> str:
        """Stable identity of *this parameterisation*.

        Computed lazily so that subclass attributes (the values actually in
        force) are visible. Two instances built with different parameters must
        produce different versions, otherwise recorded decisions could not be
        attributed to the configuration that produced them.
        """
        return self._compute_version()

    def _effective_params(self) -> dict[str, Any]:
        scalar = (bool, int, float, str, Decimal)
        declared = {
            key: value
            for key, value in self.__dict__.items()
            if not key.startswith("_") and isinstance(value, scalar)
        }
        fallback = {
            key: value for key, value in self.params.items() if isinstance(value, scalar)
        }
        return fallback | declared

    def _compute_version(self) -> str:
        payload = {
            "strategy": self.name,
            "engine": ENGINE_VERSION,
            "params": {k: str(v) for k, v in sorted(self._effective_params().items())},
        }
        digest = hashlib.sha256(jsonutil.dumps(payload).encode("utf-8")).hexdigest()
        return f"{self.name}@{digest[:12]}"

    # -- parameters ---------------------------------------------------------

    def _int_param(self, key: str, default: int, *, minimum: int = 1) -> int:
        raw = self.params.get(key, default)
        if isinstance(raw, bool):
            raise ConfigError(f"{self.name}.{key} must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{self.name}.{key} must be an integer") from exc
        if value < minimum:
            raise ConfigError(f"{self.name}.{key} must be >= {minimum}")
        return value

    def _decimal_param(self, key: str, default: str) -> Decimal:
        raw = self.params.get(key, default)
        try:
            return Decimal(str(raw))
        except Exception as exc:
            raise ConfigError(f"{self.name}.{key} must be numeric") from exc

    # -- contract -----------------------------------------------------------

    @property
    @abstractmethod
    def min_bars(self) -> int:
        """Bars required before any non-HOLD signal can be produced."""

    @abstractmethod
    def generate(self, bars: Sequence[Bar]) -> list[StrategySignal]:
        """One signal per input bar, in the same order."""

    # -- convenience --------------------------------------------------------

    def warmup_ok(self, bars: Sequence[Bar]) -> bool:
        return len(bars) >= self.min_bars

    def evaluate(self, bars: Sequence[Bar]) -> StrategySignal:
        """The signal for the most recent bar."""
        if not bars:
            raise ConfigError("cannot evaluate a strategy on an empty series")
        return self.generate(bars)[-1]

    # -- helpers for subclasses --------------------------------------------

    def _signal(
        self,
        bar: Bar,
        action: Action,
        reason: str,
        indicators: Mapping[str, Decimal | None],
    ) -> StrategySignal:
        return StrategySignal(
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            bar_ts=bar.ts,
            action=action,
            reason=reason,
            strategy_name=self.name,
            strategy_version=self.version,
            indicators={k: v for k, v in indicators.items() if v is not None},
            ts=bar.closes_at,
        )


_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if not getattr(cls, "name", None) or cls.name == "base":
        raise ValueError(f"{cls.__name__} must define a unique name")
    if cls.name in _REGISTRY:
        raise ValueError(f"strategy {cls.name!r} is already registered")
    _REGISTRY[cls.name] = cls
    return cls


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


def strategy_class(name: str) -> type[Strategy]:
    """Look up a registered strategy class (research needs the class, not an instance)."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ConfigError(
            f"unknown strategy {name!r}; available: {available_strategies()}"
        ) from None


def build_strategy(config: StrategyConfig) -> Strategy:
    try:
        cls = _REGISTRY[config.name]
    except KeyError:
        raise ConfigError(
            f"unknown strategy {config.name!r}; available: {available_strategies()}"
        ) from None
    return cls(config.params)
