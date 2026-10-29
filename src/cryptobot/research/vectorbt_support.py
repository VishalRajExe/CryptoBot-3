"""Optional VectorBT cross-check.

The spec names VectorBT as the primary research framework, and it remains the
intended tool for large parameter sweeps. It is **not** a production dependency:
the authoritative backtester in :mod:`cryptobot.research.backtest` is pure
standard-library, uses the same sizing and cost code as live trading, and is the
one exercised by the test suite.

This module therefore does two things:

* reports honestly whether VectorBT is importable,
* when it is, runs an independent portfolio simulation from the *same* signal
  masks so the two engines can be compared.

Status at the time of writing: ``vectorbt`` is not installed in the development
environment, so :func:`run_cross_check` is **unverified**. See
``docs/API_CAPABILITIES.md``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from cryptobot.core.contracts import Action, Bar, StrategySignal
from cryptobot.core.errors import MissingDependency
from cryptobot.research.backtest import CostModel


def _import_vectorbt() -> Any:
    try:
        import vectorbt as vbt  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - any import failure means "unavailable"
        raise MissingDependency(
            "vectorbt is not installed; install the optional research extra with "
            "`pip install -e .[research]` (pandas and numpy are required by it)"
        ) from exc
    return vbt


def vectorbt_version() -> str | None:
    try:
        module = _import_vectorbt()
    except MissingDependency:
        return None
    return str(getattr(module, "__version__", "unknown"))


def available() -> bool:
    return vectorbt_version() is not None


def describe() -> str:
    version = vectorbt_version()
    if version is None:
        return (
            "vectorbt: NOT INSTALLED - cross-check unavailable. "
            "The stdlib backtester remains authoritative."
        )
    return f"vectorbt: {version} installed (cross-check UNVERIFIED in this build)"


def run_cross_check(
    bars: Sequence[Bar],
    signals: Sequence[StrategySignal],
    *,
    ceiling: Decimal = Decimal("100"),
    costs: CostModel | None = None,
) -> dict[str, Any]:
    """Run the same entries/exits through ``vbt.Portfolio.from_signals``.

    Returns a dict that always carries ``verified: False``: a second opinion from
    a different engine is useful, but agreement between two backtests still says
    nothing about live performance.
    """
    if len(bars) != len(signals):
        raise ValueError("bars and signals must be the same length")
    vbt = _import_vectorbt()
    import pandas as pd  # provided by the research extra

    costs = costs or CostModel()
    index = pd.DatetimeIndex([bar.ts for bar in bars])
    close = pd.Series([float(bar.close) for bar in bars], index=index)
    entries = [signal.action is Action.ENTER_LONG for signal in signals]
    exits = [signal.action is Action.EXIT_LONG for signal in signals]

    portfolio = vbt.Portfolio.from_signals(
        close,
        pd.Series(entries, index=index),
        pd.Series(exits, index=index),
        init_cash=float(ceiling),
        fees=float(costs.fee_fraction),
        slippage=float(costs.slippage_fraction),
    )
    return {
        "engine": "vectorbt",
        "version": vectorbt_version(),
        "verified": False,
        "total_return_pct": float(portfolio.total_return() * 100),
        "max_drawdown_pct": float(portfolio.max_drawdown() * 100),
        "trades": int(getattr(portfolio.trades, "count", lambda: 0)()),
    }
