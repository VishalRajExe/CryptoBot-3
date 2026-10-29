"""The decision cycle.

One method, one order of operations, no shortcuts:

    closed bars -> strategy -> decision -> position gate -> sizing -> risk -> (optional) submit

Failure at any step produces a recorded, explainable outcome rather than an
exception that unwinds into ambiguity. ``submit=False`` is the default, so the
same engine drives dry-runs, tests, and paper trading; only the last step differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Sequence

from cryptobot.config.settings import Settings
from cryptobot.core.contracts import (
    Action,
    Bar,
    OrderIntent,
    RiskVerdict,
    Side,
    StrategyDecision,
    StrategySignal,
    to_decimal,
)
from cryptobot.core.errors import CryptobotError
from cryptobot.core.logging import get_logger
from cryptobot.core.states import Capabilities
from cryptobot.data.alpaca_data import MarketDataService
from cryptobot.data.instruments import fallback_instrument
from cryptobot.execution.orders import build_intent
from cryptobot.execution.service import ExecutionResult, OrderService
from cryptobot.journal.store import Journal
from cryptobot.risk.manager import RiskContext, RiskManager
from cryptobot.sizing.ledger import AllocationLedger
from cryptobot.sizing.position import size_entry, size_exit
from cryptobot.strategy.base import Strategy

log = get_logger(__name__)
UTC = timezone.utc

NO_SUBMIT_NOTE = "risk approved; submission not requested (analysis only)"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CycleResult:
    symbol: str
    action: str
    note: str
    bar_ts: datetime | None = None
    signal: StrategySignal | None = None
    decision_id: str | None = None
    verdict: RiskVerdict | None = None
    intent: OrderIntent | None = None
    execution: ExecutionResult | None = None
    blocked_reasons: tuple[str, ...] = ()
    position_quantity: Decimal = Decimal("0")

    @property
    def submitted(self) -> bool:
        return self.execution is not None and self.execution.submitted_to_broker

    @property
    def approved(self) -> bool:
        return self.verdict is not None and self.verdict.approved

    def summary(self) -> str:
        parts = [f"{self.symbol}: {self.action}"]
        if self.bar_ts:
            parts.append(f"bar={self.bar_ts.isoformat()}")
        if self.verdict is not None:
            parts.append(f"risk={'approved' if self.verdict.approved else 'rejected'}")
        parts.append(self.note)
        return " | ".join(parts)


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        journal: Journal,
        ledger: AllocationLedger,
        strategy: Strategy,
        risk: RiskManager,
        order_service: OrderService,
        data_service: MarketDataService,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._journal = journal
        self._ledger = ledger
        self._strategy = strategy
        self._risk = risk
        self._orders = order_service
        self._data = data_service
        self._clock = clock

    # -- context ------------------------------------------------------------

    def current_position(self, symbol: str) -> Decimal:
        quantity, _ = self._journal.realized_position(symbol)
        return quantity

    def instrument(self, symbol: str):
        """Instrument metadata, fetched once per symbol and remembered.

        The journal is the cache; a miss asks the data service, which consults
        ``/v2/assets`` and stores the result. Only when that is unavailable - no
        credentials, simulated mode, or a broker error - does the documented
        fallback apply, and it says so: sizing against constants the broker may
        have changed is worth a warning.
        """
        cached = self._journal.get_instrument(symbol)
        if cached is not None:
            return cached
        try:
            fetched = self._data.instrument(symbol)
        except CryptobotError as exc:
            log.warning(
                "instrument metadata unavailable; using the documented fallback",
                extra={"context": {"symbol": symbol, "error": str(exc)[:200]}},
            )
            fetched = None
        if fetched is not None:
            return fetched
        fallback = fallback_instrument(symbol)
        if fallback is not None:
            log.warning(
                "using documented fallback instrument metadata; refresh assets before paper trading",
                extra={"context": {"symbol": symbol}},
            )
        return fallback

    def build_context(self, symbol: str, now: datetime | None = None) -> RiskContext:
        now = now or self._clock()
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        state, _, _ = self._journal.get_system_state()
        allocation = self._ledger.state()
        balance = self._journal.latest_balance()

        if balance is not None:
            current_equity = to_decimal(balance["equity"], "equity")
        else:
            current_equity = allocation.ceiling + allocation.realized_pnl

        return RiskContext(
            now=now,
            system_state=state,
            allocation=allocation,
            latest_bar=self._journal.latest_bar(symbol, self._settings.market.timeframe),
            instrument=self.instrument(symbol),
            position_quantity=self.current_position(symbol),
            orders_today=self._journal.count_orders_since(day_start),
            realized_pnl_today=self._ledger.realized_since(day_start),
            peak_equity=self._journal.peak_equity(),
            current_equity=current_equity,
            recent_fingerprints=frozenset(self._journal.intent_fingerprints_since(day_start)),
            max_bar_age_seconds=self._settings.market.max_bar_age_seconds,
        )

    # -- cycle --------------------------------------------------------------

    def evaluate(self, symbol: str, *, submit: bool = False) -> CycleResult:
        now = self._clock()
        state, state_reason, _ = self._journal.get_system_state()
        capabilities = Capabilities.for_state(state)
        if not capabilities.allow_new_entries:
            return CycleResult(
                symbol=symbol,
                action="blocked",
                note=f"entries disabled in state {state.value}",
                blocked_reasons=(f"{state.value}: {state_reason or 'no reason recorded'}",),
            )

        bars = self._data.history(symbol, self._settings.market.lookback_bars)
        if not bars:
            return CycleResult(
                symbol=symbol,
                action="no_data",
                note="no cached bars; run `cryptobot data-refresh` first",
            )
        if len(bars) < self._strategy.min_bars:
            return CycleResult(
                symbol=symbol,
                action="warmup",
                note=(
                    f"{len(bars)} bars available, strategy {self._strategy.name} needs "
                    f"{self._strategy.min_bars}"
                ),
                bar_ts=bars[-1].ts,
            )

        signals = self._strategy.generate(bars)
        signal = signals[-1]
        fingerprint = StrategyDecision.fingerprint(bars)
        decision_id = self._journal.record_decision(signal, fingerprint)
        position_quantity = self.current_position(symbol)

        base = dict(
            symbol=symbol,
            bar_ts=signal.bar_ts,
            signal=signal,
            decision_id=decision_id,
            position_quantity=position_quantity,
        )

        if signal.action is Action.HOLD:
            return CycleResult(action="hold", note=signal.reason, **base)

        if signal.action is Action.ENTER_LONG and position_quantity > 0:
            return CycleResult(
                action="suppressed",
                note=f"already long {position_quantity}; duplicate entry suppressed",
                **base,
            )
        if signal.action is Action.EXIT_LONG and position_quantity <= 0:
            return CycleResult(action="ignored", note="no position to exit", **base)

        instrument = self.instrument(symbol)
        if instrument is None:
            return CycleResult(
                action="blocked",
                note="no instrument metadata for symbol",
                blocked_reasons=("instrument_unavailable",),
                **base,
            )

        last_close = bars[-1].close
        if signal.action is Action.ENTER_LONG:
            sizing = size_entry(
                available=self._ledger.available(),
                price=last_close,
                ceiling=self._ledger.ceiling,
                max_position_pct=self._settings.risk.max_position_pct,
                per_trade_risk_pct=self._settings.risk.per_trade_risk_pct,
                instrument=instrument,
                fee_reserve_fraction=self._settings.risk.fee_reserve_fraction,
                slippage_reserve_fraction=self._settings.risk.slippage_reserve_fraction,
            )
            side = Side.BUY
        else:
            sizing = size_exit(position_quantity=position_quantity, instrument=instrument)
            side = Side.SELL

        if not sizing.ok:
            return CycleResult(action="no_order", note=f"sizing refused: {sizing.reason}", **base)

        notional = sizing.quantity * last_close
        intent = build_intent(
            symbol=symbol,
            timeframe=self._settings.market.timeframe,
            bar_ts=signal.bar_ts,
            strategy_name=self._strategy.name,
            strategy_version=self._strategy.version,
            side=side,
            order_type=self._settings.execution.order_type,
            quantity=sizing.quantity,
            notional=notional,
            time_in_force=self._settings.execution.time_in_force,
            now=now,
            decision_id=decision_id,
        )

        verdict = self._risk.evaluate(intent, self.build_context(symbol, now))
        self._journal.record_verdict(decision_id, verdict)

        if not verdict.approved:
            if submit:
                self._orders.record_rejection(intent, verdict)
            return CycleResult(
                action="rejected",
                note=verdict.summary(),
                verdict=verdict,
                intent=intent,
                blocked_reasons=verdict.reasons,
                **base,
            )

        if not submit:
            return CycleResult(
                action="approved",
                note=NO_SUBMIT_NOTE,
                verdict=verdict,
                intent=intent,
                **base,
            )

        execution = self._orders.submit(intent, verdict)
        return CycleResult(
            action=execution.outcome.value,
            note=execution.message,
            verdict=verdict,
            intent=intent,
            execution=execution,
            **base,
        )

    def evaluate_all(self, *, submit: bool = False) -> list[CycleResult]:
        return [self.evaluate(symbol, submit=submit) for symbol in self._settings.market.symbols]
