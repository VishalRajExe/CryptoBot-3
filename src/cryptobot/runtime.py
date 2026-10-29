"""Object assembly and the trading loop.

Everything that "wires the app together" lives here so that the CLI stays a thin
argument parser and the tests can build an identical object graph. Two modes
exist and only two:

``simulated``
    A local adapter fills orders against cached bars. No network, no
    credentials. This is the mode used by the test suite and by
    ``cryptobot run-once``.

``paper``
    Alpaca's paper account over ``https://paper-api.alpaca.markets``. Requires
    credentials and is only reachable through an explicit operator command.

There is no third mode. That is the structural expression of "paper-only by
construction".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Sequence

from cryptobot.config.settings import Settings, load_settings
from cryptobot.core.contracts import Bar
from cryptobot.core.errors import CryptobotError, HaltedError
from cryptobot.core.logging import get_logger
from cryptobot.core.states import Capabilities, SystemState
from cryptobot.core.transport import HttpTransport
from cryptobot.data.alpaca_data import (
    AlpacaCryptoDataClient,
    MarketDataService,
    RefreshResult,
    build_data_client,
)
from cryptobot.engine import CycleResult, TradingEngine
from cryptobot.execution.base import ExecutionAdapter
from cryptobot.execution.service import OrderService
from cryptobot.journal.store import Journal
from cryptobot.monitoring.notify import TelegramNotifier
from cryptobot.preflight import PreflightReport, run_preflight
from cryptobot.reconciliation.reconciler import ReconciliationReport, Reconciler
from cryptobot.risk.manager import RiskManager
from cryptobot.sizing.ledger import AllocationLedger
from cryptobot.soak import SoakRecorder
from cryptobot.strategy.base import Strategy, build_strategy

log = get_logger(__name__)
UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BrokerMode(str, Enum):
    SIMULATED = "simulated"
    PAPER = "paper"

    @classmethod
    def parse(cls, value: str | "BrokerMode") -> "BrokerMode":
        if isinstance(value, BrokerMode):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise CryptobotError(
                f"unknown broker mode {value!r}; expected 'simulated' or 'paper'"
            ) from exc


def build_adapter(settings: Settings, mode: BrokerMode) -> ExecutionAdapter:
    if mode is BrokerMode.SIMULATED:
        from cryptobot.execution.simulator import LocalSimulatorAdapter

        return LocalSimulatorAdapter(
            fee_bps=settings.risk.fee_reserve_bps,
            slippage_bps=settings.risk.slippage_reserve_bps,
            starting_cash=settings.allocation.ceiling_usd,
        )
    from cryptobot.execution.alpaca_paper import build_paper_broker

    return build_paper_broker(settings.alpaca)


@dataclass
class Runtime:
    """One assembled application instance."""

    settings: Settings
    journal: Journal
    ledger: AllocationLedger
    data: MarketDataService
    adapter: ExecutionAdapter
    orders: OrderService
    risk: RiskManager
    strategy: Strategy
    engine: TradingEngine
    reconciler: Reconciler
    mode: BrokerMode
    notifier: TelegramNotifier | None = None

    # -- construction -------------------------------------------------------

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        mode: BrokerMode | str = BrokerMode.SIMULATED,
        adapter: ExecutionAdapter | None = None,
        data_client: AlpacaCryptoDataClient | None = None,
        transport: HttpTransport | None = None,
        notifier: TelegramNotifier | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> "Runtime":
        settings = settings or load_settings()
        mode = BrokerMode.parse(mode)
        if settings.app.environment != "paper":
            # Defence in depth: the config loader already refuses this.
            raise HaltedError(
                f"refusing to start: app.environment={settings.app.environment!r}; "
                "this application is paper-only"
            )

        journal = Journal(settings.database_path(), clock=clock).initialize()
        ledger = AllocationLedger(journal, settings.allocation.ceiling_usd)
        ledger.rebuild()

        client = data_client
        if client is None and mode is BrokerMode.PAPER:
            # Only paper mode is allowed to touch the network at all.
            client = build_data_client(settings, transport=transport)
        data = MarketDataService(settings, journal, client, clock=clock)

        broker = adapter or build_adapter(settings, mode)
        orders = OrderService(
            journal,
            ledger,
            broker,
            fee_reserve_fraction=settings.risk.fee_reserve_fraction,
            slippage_reserve_fraction=settings.risk.slippage_reserve_fraction,
            clock=clock,
        )
        risk = RiskManager(settings.risk)
        strategy = build_strategy(settings.strategy)
        engine = TradingEngine(
            settings, journal, ledger, strategy, risk, orders, data, clock=clock
        )
        reconciler = Reconciler(
            journal,
            ledger,
            broker,
            orders,
            grace_seconds=settings.execution.lost_order_grace_seconds,
            clock=clock,
        )
        return cls(
            settings=settings,
            journal=journal,
            ledger=ledger,
            data=data,
            adapter=broker,
            orders=orders,
            risk=risk,
            strategy=strategy,
            engine=engine,
            reconciler=reconciler,
            mode=mode,
            notifier=notifier,
        )

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self.journal.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- operations ---------------------------------------------------------

    def preflight(self, *, require_broker: bool | None = None) -> PreflightReport:
        if require_broker is None:
            require_broker = self.mode is BrokerMode.PAPER
        return run_preflight(
            self.settings,
            self.journal,
            self.ledger,
            adapter=self.adapter,
            data_service=self.data,
            require_broker=require_broker,
        )

    def refresh_data(self, symbols: Sequence[str] | None = None) -> list[RefreshResult]:
        results: list[RefreshResult] = []
        for symbol in symbols or self.settings.market.symbols:
            results.append(self.data.refresh(symbol))
        return results

    def sync_prices(self) -> dict[str, Decimal]:
        """Seed the simulator with the latest closed bar so fills are realistic."""
        prices: dict[str, Decimal] = {}
        setter = getattr(self.adapter, "set_price", None)
        for symbol in self.settings.market.symbols:
            bar = self.data.latest_closed(symbol)
            if bar is None:
                continue
            prices[symbol] = bar.close
            if callable(setter):
                setter(symbol, bar.close)
        return prices

    def capture_account(self) -> None:
        """Persist a balance and position snapshot; tolerate broker absence."""
        try:
            # The adapter's own name, not "broker": a journal outlives the mode
            # it was written in, and reconciliation refuses to compare one broker's
            # state with another's (cryptobot.reconciliation.reconciler).
            source = self.adapter.name
            account = self.adapter.account()
            self.journal.save_balance(
                cash=account.cash,
                equity=account.equity,
                buying_power=account.buying_power,
                non_marginable_buying_power=account.non_marginable_buying_power,
                source=source,
            )
            positions = self.adapter.positions()
            snapshots = [
                p.to_snapshot(self.journal.now(), source=source) for p in positions
            ]
            self.journal.save_position_snapshots(snapshots)
            unrealized = sum((p.unrealized_pl for p in positions), Decimal("0"))
            self.ledger.set_unrealized(unrealized)
        except Exception as exc:  # noqa: BLE001 - a snapshot is not worth a crash
            log.warning(
                "account snapshot failed",
                extra={"context": {"error": str(exc)[:200]}},
            )

    def reconcile(self, *, context: str = "periodic") -> ReconciliationReport:
        return self.reconciler.reconcile(context=context)

    def set_state(self, state: SystemState, reason: str) -> None:
        self.journal.set_system_state(state, reason)

    def capabilities(self) -> Capabilities:
        state, _, _ = self.journal.get_system_state()
        return Capabilities.for_state(state)

    def symbols(self) -> tuple[str, ...]:
        return tuple(self.settings.market.symbols)


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------


@dataclass
class LoopReport:
    cycles: int = 0
    results: list[CycleResult] = field(default_factory=list)
    reconciliations: list[ReconciliationReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Empty until the loop says why it stopped, so an operator stop is reported
    #: as such rather than as a normal completion.
    stopped_reason: str = ""

    @property
    def submitted(self) -> int:
        return sum(1 for r in self.results if r.submitted)

    @property
    def approved(self) -> int:
        return sum(1 for r in self.results if r.approved)

    @property
    def rejected(self) -> int:
        return sum(1 for r in self.results if r.action == "rejected")

    def summary(self) -> str:
        return (
            f"cycles={self.cycles} approved={self.approved} rejected={self.rejected} "
            f"submitted={self.submitted} reconciliations={len(self.reconciliations)} "
            f"errors={len(self.errors)} stopped={self.stopped_reason}"
        )


class TradingLoop:
    """A single-threaded cycle loop with explicit stop semantics.

    Deliberately not a scheduler: no daemonisation, no cron, no threading. The
    operator owns the process lifetime, which keeps "stopping the process" a
    lifecycle concern rather than a trading decision.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        submit: bool = False,
        interval_seconds: float = 60.0,
        reconcile_every: int = 5,
        refresh: bool = True,
        naplike: Callable[[float], None] = time.sleep,
        on_cycle: Callable[[CycleResult], None] | None = None,
        max_consecutive_errors: int = 3,
        instrument_soak: bool = True,
    ) -> None:
        self.runtime = runtime
        self.submit = submit
        self.interval_seconds = max(0.0, float(interval_seconds))
        self.reconcile_every = max(1, int(reconcile_every))
        self.refresh = refresh
        self._sleep = naplike
        self._on_cycle = on_cycle
        self._max_errors = max(1, int(max_consecutive_errors))
        self._instrument_soak = bool(instrument_soak)
        self._stop = False
        self._stop_reason = ""

    # -- control ------------------------------------------------------------

    def request_stop(self, reason: str = "operator request") -> None:
        self._stop = True
        self._stop_reason = reason

    @property
    def stop_requested(self) -> bool:
        return self._stop

    # -- run ----------------------------------------------------------------

    def run(
        self,
        *,
        iterations: int | None = None,
        stop_event: Callable[[], bool] | None = None,
    ) -> LoopReport:
        report = LoopReport()
        runtime = self.runtime
        consecutive_errors = 0

        try:
            preflight = runtime.preflight()
            if not preflight.ok:
                report.errors.append(preflight.summary())
                report.stopped_reason = "preflight failed"
                runtime.set_state(
                    SystemState.HALTED, f"preflight failed: {preflight.failures[0].detail}"
                )
                return report
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"preflight error: {exc}")
            report.stopped_reason = "preflight error"
            return report

        runtime.capture_account()

        # Soak evidence starts here, after preflight has proven the process is
        # fit to run. A failed preflight must not look like soak time.
        recorder: SoakRecorder | None = None
        if self._instrument_soak:
            recorder = SoakRecorder(
                runtime.journal, interval_seconds=max(self.interval_seconds, 1.0)
            )
            recorder.start()

        try:
            while not self._stop:
                if iterations is not None and report.cycles >= iterations:
                    report.stopped_reason = "iteration limit reached"
                    break
                if stop_event is not None and stop_event():
                    report.stopped_reason = "stop event set"
                    break

                error: str | None = None
                try:
                    if self.refresh:
                        runtime.refresh_data()
                    runtime.sync_prices()

                    state, reason, _ = runtime.journal.get_system_state()
                    if not Capabilities.for_state(state).allow_new_entries:
                        log.info(
                            "entries disabled; skipping evaluation",
                            extra={"context": {"state": state.value, "reason": reason}},
                        )
                    else:
                        for result in runtime.engine.evaluate_all(submit=self.submit):
                            report.results.append(result)
                            if self._on_cycle is not None:
                                self._on_cycle(result)

                    runtime.capture_account()
                    if report.cycles % self.reconcile_every == 0:
                        report.reconciliations.append(runtime.reconcile(context="periodic"))
                except KeyboardInterrupt:
                    report.stopped_reason = "interrupted"
                    self._stop = True
                    break
                except Exception as exc:  # noqa: BLE001 - the loop must not die silently
                    error = f"{type(exc).__name__}: {exc}"
                    report.errors.append(error)
                    log.error("cycle failed", extra={"context": {"error": error[:300]}})
                    if recorder is not None:
                        recorder.record_api_error(error)

                consecutive_errors = 0 if error is None else consecutive_errors + 1
                if consecutive_errors >= self._max_errors:
                    runtime.set_state(
                        SystemState.HALTED,
                        f"{consecutive_errors} consecutive cycle errors; last: {error}",
                    )
                    report.stopped_reason = "halted after repeated errors"
                    self._notify(f"CRYPTOBOT halted after {consecutive_errors} cycle errors")
                    break

                report.cycles += 1
                if recorder is not None:
                    recorder.checkpoint(cycles=report.cycles)
                if iterations is not None and report.cycles >= iterations:
                    continue
                if self.interval_seconds > 0 and not self._stop:
                    try:
                        self._sleep(self.interval_seconds)
                    except KeyboardInterrupt:
                        report.stopped_reason = "interrupted"
                        self._stop = True
                        break
        finally:
            if recorder is not None:
                recorder.close(report.stopped_reason or "loop ended")

        if not report.stopped_reason:
            report.stopped_reason = self._stop_reason or "completed"
        log.info("loop finished: %s", report.summary())
        return report

    def _notify(self, text: str) -> None:
        notifier = self.runtime.notifier
        if notifier is None:
            return
        try:
            notifier.send(text)
        except Exception:  # noqa: BLE001 - notifications never break trading
            pass


def bars_from_rows(rows: Sequence[dict], symbol: str, timeframe: str) -> list[Bar]:
    """Convenience for fixtures and the research runner."""
    return [Bar.from_row({**row, "symbol": symbol, "timeframe": timeframe}) for row in rows]
