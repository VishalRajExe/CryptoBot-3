"""Phase 8: operational safety.

Four claims are tested here, and each of them is a *behavioural* claim rather
than a structural one:

* preflight fails **closed** - a problem that makes trading unjustifiable stops
  the process, and a warning does not;
* the operational states stay distinct: pausing entries never cancels an order,
  the kill switch still permits cancellations, halting permits nothing;
* the decision cycle never reaches the broker unless ``submit=True``, and every
  refusal is explainable;
* the loop stops for an operator, for a stop event, for the iteration limit, and
  for repeated errors - and a notifier failure never breaks trading.

Nothing here contacts Alpaca: the simulated adapter is the only execution path.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cryptobot.config.settings import APP_ROOT, Settings
from cryptobot.core.contracts import (
    Action,
    Bar,
    OrderRecord,
    OrderStatus,
    StrategySignal,
)
from cryptobot.core.errors import HaltedError
from cryptobot.core.states import Capabilities, Severity, SystemState, capabilities_for
from cryptobot.data.alpaca_data import MarketDataService
from cryptobot.engine import TradingEngine
from cryptobot.execution.base import ExecutionAdapter
from cryptobot.execution.service import OrderService
from cryptobot.execution.simulator import LocalSimulatorAdapter
from cryptobot.journal.schema import SCHEMA_VERSION
from cryptobot.journal.store import Journal
from cryptobot.preflight import PreflightCheck, PreflightReport, run_preflight
from cryptobot.risk.manager import RiskManager
from cryptobot.runtime import BrokerMode, Runtime, TradingLoop
from cryptobot.sizing.ledger import AllocationLedger

from conftest import START, Clock, make_bars, make_intent

UTC = timezone.utc
CONFIG_PATH = APP_ROOT / "config" / "trading.yaml"
PRICE = "20000"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _closes(count: int = 60, *, seed: int = 3, end: str = PRICE) -> list[str]:
    """A price series whose last close is ``end``."""
    rng = random.Random(seed)
    values = [Decimal(end)]
    for _ in range(count - 1):
        values.append(values[-1] * (Decimal(1) + Decimal(rng.uniform(-0.01, 0.01))))
    values.reverse()
    return [format(value.quantize(Decimal("1")), "f") for value in values]


def _fresh_bars(clock: Clock, count: int = 60, **kwargs) -> list[Bar]:
    """Bars that end one interval before ``clock.now``, so they are all closed."""
    start = clock.now - timedelta(hours=count)
    return make_bars(_closes(count, **kwargs), start=start)


class _Scripted:
    """A strategy that always emits the same action. The engine gates it."""

    name = "scripted"
    version = "scripted@test"
    min_bars = 2

    def __init__(self, action: Action = Action.ENTER_LONG) -> None:
        self._action = action

    def generate(self, bars):
        last = bars[-1]
        return [
            StrategySignal(
                symbol=last.symbol,
                timeframe=last.timeframe,
                bar_ts=last.ts,
                action=self._action,
                reason=f"scripted {self._action.value}",
                strategy_name=self.name,
                strategy_version=self.version,
            )
        ]


def _data(settings: Settings, journal: Journal, clock: Clock) -> MarketDataService:
    return MarketDataService(settings, journal, None, clock=clock)


def _engine(
    settings: Settings,
    journal: Journal,
    ledger: AllocationLedger,
    clock: Clock,
    *,
    action: Action = Action.ENTER_LONG,
    price: str | None = None,
) -> TradingEngine:
    adapter = LocalSimulatorAdapter(starting_cash=ledger.ceiling, clock=clock)
    if price is not None:
        adapter.set_price(settings.market.symbols[0], Decimal(price))
    orders = OrderService(journal, ledger, adapter, clock=clock)
    return TradingEngine(
        settings,
        journal,
        ledger,
        _Scripted(action),
        RiskManager(settings.risk),
        orders,
        _data(settings, journal, clock),
        clock=clock,
    )


def _names(checks) -> set[str]:
    return {check.name for check in checks}


# ---------------------------------------------------------------------------
# operational states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "entries", "cancels", "restart", "manual"),
    [
        (SystemState.ACTIVE, True, True, True, False),
        (SystemState.PAUSE_ENTRIES, False, True, True, False),
        (SystemState.KILLED, False, True, True, True),
        (SystemState.HALTED, False, False, False, True),
    ],
)
def test_each_state_permits_exactly_what_the_spec_says(
    state: SystemState, entries: bool, cancels: bool, restart: bool, manual: bool
) -> None:
    caps = Capabilities.for_state(state)
    assert caps.allow_new_entries is entries
    assert caps.allow_cancel_orders is cancels
    assert caps.allow_process_restart is restart
    assert caps.requires_manual_intervention is manual


def test_pausing_entries_is_not_the_same_as_halting_or_killing() -> None:
    paused = capabilities_for("pause_entries")
    killed = capabilities_for(SystemState.KILLED)
    halted = capabilities_for(SystemState.HALTED)

    # A pause and a kill both still allow the operator to cancel orders; a halt
    # does not, because an untrustworthy state must not take automatic action.
    assert paused.allow_cancel_orders and killed.allow_cancel_orders
    assert not halted.allow_cancel_orders
    assert killed.requires_manual_intervention
    assert not paused.requires_manual_intervention


def test_an_unknown_persisted_state_is_rejected_rather_than_assumed() -> None:
    with pytest.raises(ValueError, match="unknown system state"):
        capabilities_for("mauled_by_a_bear")


def test_capabilities_have_no_way_to_close_a_position() -> None:
    # Closing a position is an explicit operator order, never a state side effect.
    assert "close_positions" not in Capabilities.__slots__


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def _offline_settings(settings: Settings) -> Settings:
    """Settings whose credential variables definitely do not exist."""
    return replace(
        settings,
        alpaca=replace(
            settings.alpaca,
            api_key_env="CRYPTOBOT_TEST_ABSENT_KEY",
            api_secret_env="CRYPTOBOT_TEST_ABSENT_SECRET",
        ),
    )


def test_preflight_passes_offline_with_warnings_only(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    report = run_preflight(
        _offline_settings(settings),
        journal,
        ledger,
        data_service=_data(settings, journal, clock),
        now=clock.now,
    )

    assert report.ok
    assert report.failures == ()
    # Missing credentials and missing bars are warnings when no broker is needed.
    assert {c.name for c in report.warnings} == {"credentials", "data_freshness"}
    assert "passed" in report.summary()
    report.raise_if_failed()  # must not raise


def test_preflight_fails_when_the_environment_is_not_paper(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    live_looking = replace(settings, app=replace(settings.app, environment="live"))

    report = run_preflight(live_looking, journal, ledger, now=clock.now)

    assert not report.ok
    assert _names(report.failures) == {"paper_environment"}
    assert "environment=live" in report.failures[0].detail


def test_preflight_fails_when_the_endpoint_is_not_paper(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    wrong = replace(
        settings,
        alpaca=replace(settings.alpaca, trading_base_url="https://api.alpaca.markets"),
    )

    report = run_preflight(wrong, journal, ledger, now=clock.now)

    assert not report.ok
    assert "paper_endpoint" in _names(report.failures)


def test_preflight_fails_when_the_system_is_halted(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.set_system_state(SystemState.HALTED, "reconciliation drift 0.42 USD")

    report = run_preflight(_offline_settings(settings), journal, ledger, now=clock.now)

    assert not report.ok
    assert _names(report.failures) == {"system_state"}
    assert "reconciliation drift" in report.failures[0].detail
    assert "unhalt" in report.failures[0].detail


def test_preflight_fails_when_the_kill_switch_is_engaged(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.set_system_state(SystemState.KILLED, "operator kill switch")

    report = run_preflight(_offline_settings(settings), journal, ledger, now=clock.now)

    assert not report.ok
    assert _names(report.failures) == {"system_state"}
    assert "kill switch" in report.failures[0].detail


def test_a_pause_is_deliberate_and_therefore_not_a_preflight_failure(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.set_system_state(SystemState.PAUSE_ENTRIES, "maintenance window")

    report = run_preflight(_offline_settings(settings), journal, ledger, now=clock.now)

    assert report.ok, "a pause is an operator choice; the process may still run"


def test_paper_mode_treats_missing_credentials_as_a_blocker(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    report = run_preflight(
        _offline_settings(settings),
        journal,
        ledger,
        require_broker=True,
        now=clock.now,
    )

    assert not report.ok
    assert _names(report.failures) == {"credentials"}
    assert "CRYPTOBOT_TEST_ABSENT_KEY" in report.failures[0].detail


def test_paper_mode_treats_missing_bars_as_a_blocker(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    report = run_preflight(
        _offline_settings(settings),
        journal,
        ledger,
        data_service=_data(settings, journal, clock),
        require_broker=True,
        now=clock.now,
    )

    assert not report.ok
    assert {"credentials", "data_freshness"} <= _names(report.failures)
    freshness = next(c for c in report.failures if c.name == "data_freshness")
    assert "no bars" in freshness.detail


def test_paper_mode_treats_a_stale_bar_as_a_blocker(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(make_bars(_closes(10)), source="test")  # START, long stale

    report = run_preflight(
        _offline_settings(settings),
        journal,
        ledger,
        data_service=_data(settings, journal, clock),
        require_broker=True,
        now=clock.now,
    )

    freshness = [c for c in report.checks if c.name == "data_freshness"]
    assert freshness and not freshness[0].ok
    assert "stale" in freshness[0].detail.lower() or "age" in freshness[0].detail.lower()


def test_unresolved_orders_only_block_a_broker_run(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    intent = make_intent()
    journal.record_intent(intent)
    journal.upsert_order(
        OrderRecord(
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            status=OrderStatus.UNCERTAIN,
            updated_at=clock.now,
            reason="transport reset during submission",
        )
    )

    offline = run_preflight(_offline_settings(settings), journal, ledger, now=clock.now)
    assert offline.ok, "offline analysis is still useful with an uncertain order"
    assert {"unresolved_orders"} <= _names(offline.warnings)

    broker = run_preflight(
        _offline_settings(settings), journal, ledger, require_broker=True, now=clock.now
    )
    assert not broker.ok
    assert "unresolved_orders" in _names(broker.failures)


def test_raise_if_failed_names_every_failure(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.set_system_state(SystemState.HALTED, "drift")

    report = run_preflight(
        replace(settings, app=replace(settings.app, environment="live")),
        journal,
        ledger,
        now=clock.now,
    )

    with pytest.raises(HaltedError) as excinfo:
        report.raise_if_failed()
    message = str(excinfo.value)
    assert "paper_environment" in message
    assert "system_state" in message


def test_a_report_with_only_warnings_is_still_ok() -> None:
    report = PreflightReport(
        (
            PreflightCheck("a", True, "fine"),
            PreflightCheck("b", False, "degraded but survivable", Severity.WARNING),
        )
    )
    assert report.ok
    assert report.failures == ()
    assert len(report.warnings) == 1
    report.raise_if_failed()


# ---------------------------------------------------------------------------
# runtime assembly
# ---------------------------------------------------------------------------


def test_runtime_refuses_an_environment_that_is_not_paper(settings: Settings) -> None:
    with pytest.raises(HaltedError, match="paper-only"):
        Runtime.build(replace(settings, app=replace(settings.app, environment="live")))


def test_there_are_exactly_two_broker_modes(tmp_path) -> None:
    assert {mode.value for mode in BrokerMode} == {"simulated", "paper"}
    with pytest.raises(Exception):
        BrokerMode.parse("live")


def test_runtime_wires_the_offline_graph(settings: Settings, clock: Clock) -> None:
    with Runtime.build(settings, mode="simulated", clock=clock) as runtime:
        assert runtime.mode is BrokerMode.SIMULATED
        assert runtime.adapter.is_simulated
        assert not runtime.adapter.is_paper
        assert runtime.data._client is None, "simulated mode must not build a network client"
        assert runtime.ledger.ceiling == settings.allocation.ceiling_usd
        report = runtime.preflight()
        assert report.ok


def test_sync_prices_seeds_the_simulator_from_closed_bars(
    settings: Settings, clock: Clock
) -> None:
    with Runtime.build(settings, mode="simulated", clock=clock) as runtime:
        runtime.journal.save_bars(_fresh_bars(clock, end="21000"), source="test")

        prices = runtime.sync_prices()

        assert prices["BTC/USD"] == Decimal("21000")
        # Proven through the adapter's public fill price: 21000 x (1 + 25bps).
        order = runtime.adapter.submit(make_intent(quantity="0.0001", notional="2"))
        assert order.avg_fill_price == Decimal("21052.5")


# ---------------------------------------------------------------------------
# the decision cycle
# ---------------------------------------------------------------------------


def test_a_cycle_without_bars_says_so_instead_of_inventing_one(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    result = _engine(settings, journal, ledger, clock).evaluate("BTC/USD")

    assert result.action == "no_data"
    assert "data-refresh" in result.note


def test_a_cycle_with_too_few_bars_waits_for_warmup(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock, count=1), source="test")

    result = _engine(settings, journal, ledger, clock).evaluate("BTC/USD")

    assert result.action == "warmup"
    assert "needs 2" in result.note


def test_hold_produces_no_intent(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")

    result = _engine(settings, journal, ledger, clock, action=Action.HOLD).evaluate("BTC/USD")

    assert result.action == "hold"
    assert result.intent is None
    assert journal.list_orders() == []


def test_an_entry_is_evaluated_but_not_submitted_by_default(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")

    result = _engine(settings, journal, ledger, clock).evaluate("BTC/USD")

    assert result.action == "approved"
    assert result.verdict is not None and result.verdict.approved
    assert result.intent is not None
    assert result.intent.side.value == "buy"
    assert result.execution is None
    assert not result.submitted
    # A decision and its verdict are recorded even though nothing was sent.
    assert journal.health()["decisions"] == 1
    assert journal.health()["risk_verdicts"] == 1
    assert journal.list_orders() == []


def test_entries_are_disabled_by_the_kill_switch(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")
    journal.set_system_state(SystemState.KILLED, "operator")

    result = _engine(settings, journal, ledger, clock).evaluate("BTC/USD")

    assert result.action == "blocked"
    assert "killed" in result.note
    assert result.blocked_reasons == ("killed: operator",)
    assert journal.health()["decisions"] == 0


def test_stale_data_is_rejected_by_the_risk_manager(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(make_bars(_closes(10)), source="test")  # 400 days old

    result = _engine(settings, journal, ledger, clock).evaluate("BTC/USD")

    assert result.action == "rejected"
    assert result.verdict is not None and not result.verdict.approved
    assert "data_freshness" in result.verdict.failed_checks
    assert result.execution is None
    assert journal.list_orders() == []


def test_a_rejection_is_recorded_when_submission_was_requested(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(make_bars(_closes(10)), source="test")

    result = _engine(settings, journal, ledger, clock).evaluate("BTC/USD", submit=True)

    assert result.action == "rejected"
    orders = journal.list_orders()
    assert len(orders) == 1
    assert orders[0].status.value == "rejected"
    assert not result.submitted


def test_sizing_that_cannot_buy_one_increment_refuses_the_order(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")
    tiny = AllocationLedger(journal, Decimal("0.01"))

    result = _engine(settings, journal, tiny, clock).evaluate("BTC/USD")

    assert result.action == "no_order"
    assert "sizing refused" in result.note
    assert result.intent is None


def test_a_second_entry_while_long_is_suppressed(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")
    engine = _engine(settings, journal, ledger, clock)

    # The position comes from fills in production; the seam keeps this a gate test.
    engine.current_position = lambda symbol: Decimal("0.0005")  # type: ignore[method-assign]

    result = engine.evaluate("BTC/USD")

    assert result.action == "suppressed"
    assert "already long" in result.note
    assert result.position_quantity == Decimal("0.0005")


def test_an_exit_without_a_position_is_ignored(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")

    result = _engine(settings, journal, ledger, clock, action=Action.EXIT_LONG).evaluate(
        "BTC/USD"
    )

    assert result.action == "ignored"
    assert "no position to exit" in result.note


def test_an_exit_sells_the_whole_position(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")
    engine = _engine(settings, journal, ledger, clock, action=Action.EXIT_LONG)
    engine.current_position = lambda symbol: Decimal("0.0012")  # type: ignore[method-assign]

    result = engine.evaluate("BTC/USD")

    assert result.intent is not None
    assert result.intent.side.value == "sell"
    # Floored to the 0.0001 increment, never rounded up into an over-sell.
    assert result.intent.quantity == Decimal("0.0012")


def test_evaluate_all_covers_every_configured_symbol(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")

    results = _engine(settings, journal, ledger, clock).evaluate_all()

    assert [r.symbol for r in results] == list(settings.market.symbols)


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


@dataclass
class _StubEngine:
    results: list
    calls: int = 0

    def evaluate_all(self, *, submit: bool = False):
        self.calls += 1
        return list(self.results)


class _FakeRuntime:
    """The loop's collaborators, with failures the test can schedule."""

    def __init__(
        self,
        journal: Journal,
        *,
        preflight_ok: bool = True,
        fail_cycles: int = 0,
        notifier=None,
    ) -> None:
        self.journal = journal
        self.notifier = notifier
        self.engine = _StubEngine(results=[])
        self.fail_cycles = fail_cycles
        self.sync_calls = 0
        self.reconciliations: list[str] = []
        self.snapshots = 0
        self._preflight_ok = preflight_ok

    def preflight(self) -> PreflightReport:
        return PreflightReport(
            (PreflightCheck("stub", self._preflight_ok, "stub preflight detail"),)
        )

    def capture_account(self) -> None:
        self.snapshots += 1

    def refresh_data(self) -> None:
        raise AssertionError("refresh must be disabled in these tests")

    def sync_prices(self) -> dict[str, Decimal]:
        self.sync_calls += 1
        if self.sync_calls <= self.fail_cycles:
            raise RuntimeError("feed unavailable")
        return {}

    def reconcile(self, *, context: str = "periodic"):
        self.reconciliations.append(context)
        return type("Report", (), {"ok": True, "summary": lambda self: "clean"})()

    def set_state(self, state: SystemState, reason: str) -> None:
        self.journal.set_system_state(state, reason)


def _loop(runtime, **kwargs) -> TradingLoop:
    kwargs.setdefault("interval_seconds", 0.0)
    kwargs.setdefault("refresh", False)
    kwargs.setdefault("naplike", lambda seconds: None)
    return TradingLoop(runtime, **kwargs)


def test_the_loop_stops_at_the_iteration_limit(journal: Journal) -> None:
    runtime = _FakeRuntime(journal)

    report = _loop(runtime).run(iterations=3)

    assert report.cycles == 3
    assert report.stopped_reason == "iteration limit reached"
    assert runtime.engine.calls == 3
    assert runtime.snapshots == 4, "a snapshot is taken at startup and after each cycle"


def test_a_failing_preflight_stops_the_loop_and_halts_the_system(journal: Journal) -> None:
    runtime = _FakeRuntime(journal, preflight_ok=False)

    report = _loop(runtime).run(iterations=5)

    assert report.cycles == 0
    assert report.stopped_reason == "preflight failed"
    assert report.errors and "preflight" in report.errors[0]
    state, reason, _ = journal.get_system_state()
    assert state is SystemState.HALTED
    assert "stub preflight detail" in (reason or "")
    assert runtime.engine.calls == 0


def test_a_kill_switch_skips_evaluation_but_keeps_the_loop_alive(journal: Journal) -> None:
    journal.set_system_state(SystemState.KILLED, "operator")
    runtime = _FakeRuntime(journal)

    report = _loop(runtime).run(iterations=2)

    assert report.cycles == 2
    assert report.results == []
    assert runtime.engine.calls == 0
    assert runtime.reconciliations, "reconciliation still runs: it reads nothing"


def test_repeated_cycle_errors_halt_the_loop(journal: Journal) -> None:
    runtime = _FakeRuntime(journal, fail_cycles=10)

    report = _loop(runtime, max_consecutive_errors=3).run(iterations=10)

    assert report.stopped_reason == "halted after repeated errors"
    assert len(report.errors) == 3
    state, reason, _ = journal.get_system_state()
    assert state is SystemState.HALTED
    assert "3 consecutive cycle errors" in (reason or "")


def test_a_transient_error_does_not_halt_the_loop(journal: Journal) -> None:
    runtime = _FakeRuntime(journal, fail_cycles=2)

    report = _loop(runtime, max_consecutive_errors=3).run(iterations=5)

    assert len(report.errors) == 2
    assert report.cycles == 5
    assert journal.get_system_state()[0] is SystemState.ACTIVE


def test_an_operator_stop_is_honoured(journal: Journal) -> None:
    runtime = _FakeRuntime(journal)
    loop = _loop(runtime)
    loop.request_stop("market closure")

    report = loop.run(iterations=5)

    assert report.cycles == 0
    assert report.stopped_reason == "market closure"
    assert loop.stop_requested


def test_a_stop_event_is_honoured(journal: Journal) -> None:
    runtime = _FakeRuntime(journal)

    report = _loop(runtime).run(iterations=5, stop_event=lambda: True)

    assert report.cycles == 0
    assert report.stopped_reason == "stop event set"


def test_a_misbehaving_notifier_cannot_break_a_halt(journal: Journal) -> None:
    class _Exploding:
        def send(self, text: str) -> bool:
            raise RuntimeError("telegram is down")

    runtime = _FakeRuntime(journal, fail_cycles=10, notifier=_Exploding())

    report = _loop(runtime, max_consecutive_errors=2).run(iterations=10)

    assert report.stopped_reason == "halted after repeated errors"
    assert journal.get_system_state()[0] is SystemState.HALTED


def test_reconciliation_runs_on_the_configured_cadence(journal: Journal) -> None:
    runtime = _FakeRuntime(journal)

    _loop(runtime, reconcile_every=2).run(iterations=4)

    # Cycles 0 and 2 are reconciled.
    assert len(runtime.reconciliations) == 2
    assert set(runtime.reconciliations) == {"periodic"}


def test_the_loop_never_submits_unless_asked(settings: Settings, clock: Clock) -> None:
    with Runtime.build(settings, mode="simulated", clock=clock) as runtime:
        runtime.journal.save_bars(_fresh_bars(clock), source="test")

        report = TradingLoop(runtime, interval_seconds=0.0, refresh=False, naplike=lambda s: None).run(
            iterations=1
        )

        assert report.cycles == 1
        assert report.submitted == 0
        assert runtime.journal.list_orders() == []
        assert runtime.journal.health()["decisions"] == 1


def test_a_dry_run_is_reproducible_from_the_journal(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")
    engine = _engine(settings, journal, ledger, clock)

    first = engine.evaluate("BTC/USD")
    second = engine.evaluate("BTC/USD")

    # Same bars, same state, same numbers - and since nothing was sent, nothing
    # is recorded as an order. Re-evaluating a dry run is not a duplicate trade.
    assert first.intent is not None and second.intent is not None
    assert first.intent.fingerprint == second.intent.fingerprint
    assert first.intent.client_order_id == second.intent.client_order_id
    assert first.action == second.action == "approved"
    assert journal.list_orders() == []


def test_a_submitted_entry_is_not_repeated(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_bars(_fresh_bars(clock), source="test")
    engine = _engine(settings, journal, ledger, clock, price=PRICE)

    first = engine.evaluate("BTC/USD", submit=True)
    second = engine.evaluate("BTC/USD", submit=True)

    assert first.submitted, first.note
    assert second.action == "suppressed"
    assert len(journal.list_orders()) == 1, "one bar, one order"
    assert ledger.state().committed > 0


def test_the_journal_records_the_schema_version_the_preflight_expects(
    journal: Journal,
) -> None:
    assert journal.schema_version == SCHEMA_VERSION


def test_the_simulator_declares_itself_simulated_not_paper() -> None:
    adapter = LocalSimulatorAdapter()
    assert adapter.name == "local-simulator"
    assert adapter.is_simulated and not adapter.is_paper
    for method in ("submit", "get_order", "list_open_orders", "cancel", "positions", "account"):
        assert callable(getattr(adapter, method)), method


def test_bar_freshness_is_measured_against_the_decision_clock() -> None:
    now = datetime(2024, 6, 1, 12, tzinfo=UTC)
    bar = make_bars(["100"], start=now - timedelta(hours=1))[0]

    assert bar.is_closed(now)
    assert bar.age_seconds(now) == pytest.approx(0, abs=1)
    assert not make_bars(["100"], start=now)[0].is_closed(now)
