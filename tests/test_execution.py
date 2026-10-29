"""Phase 5 + Phase 6: the order path, recovery, and reconciliation.

The central claim of the project is structural: *one* path reaches the broker,
it is paper-only in four independent layers, and an ambiguous submission is
never retried. Each claim is tested here against a fake adapter, so the suite
never contacts Alpaca and never places an order.

Two regressions are pinned by name because both would have been silent in
production:

* reconciliation compared fee-inclusive committed capital against the broker's
  fee-free cost basis, so the first fee-bearing fill would have halted the system;
* a closed position left amortised fees in ``committed``, shrinking deployable
  capital on every round trip.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

import pytest

from cryptobot.config.settings import Settings
from cryptobot.core.contracts import (
    FillRecord,
    OrderIntent,
    OrderStatus,
    OrderType,
    RiskVerdict,
    Side,
    TimeInForce,
)
from cryptobot.core.errors import (
    AmbiguousSubmission,
    ContractError,
    DefiniteRejection,
    DuplicateOrderError,
    ExecutionError,
    PaperOnlyViolation,
    RiskRejected,
)
from cryptobot.core.states import SystemState
from cryptobot.execution.alpaca_paper import (
    AlpacaPaperBroker,
    assert_paper_base_url,
    build_order_payload,
)
from cryptobot.execution.base import BrokerAccount, BrokerOrder, BrokerPosition
from cryptobot.execution.orders import build_client_order_id, build_intent
from cryptobot.execution.service import OrderOutcome, OrderService
from cryptobot.execution.simulator import LocalSimulatorAdapter
from cryptobot.journal.store import Journal
from cryptobot.reconciliation.reconciler import Reconciler
from cryptobot.sizing.ledger import AllocationLedger

from conftest import START, Clock, make_intent

UTC = timezone.utc
ENV = {"ALPACA_API_KEY": "test-key", "ALPACA_API_SECRET_KEY": "test-secret"}
PRICE = Decimal("20000")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _approved() -> RiskVerdict:
    return RiskVerdict(True, (), (), Decimal("100"), START)


def _buy_intent(**overrides) -> OrderIntent:
    base = {"side": Side.BUY, "quantity": "0.001", "notional": "20"}
    return make_intent(**{**base, **overrides})


def _fill(intent: OrderIntent, price: Decimal = PRICE, fee: str = "0.05") -> FillRecord:
    return FillRecord(
        fill_id="fill-1",
        broker_order_id="broker-1",
        client_order_id=intent.client_order_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        price=price,
        fee=Decimal(fee),
        fee_currency="USD",
        ts=START,
    )


def _filled(intent: OrderIntent, price: Decimal = PRICE) -> BrokerOrder:
    return BrokerOrder(
        client_order_id=intent.client_order_id,
        broker_order_id="broker-1",
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        status=OrderStatus.FILLED,
        filled_quantity=intent.quantity,
        avg_fill_price=price,
        submitted_at=START,
        raw_status="filled",
        fills=(_fill(intent, price),),
    )


def _working(intent: OrderIntent) -> BrokerOrder:
    return BrokerOrder(
        client_order_id=intent.client_order_id,
        broker_order_id="broker-1",
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        status=OrderStatus.ACCEPTED,
        submitted_at=START,
        raw_status="new",
    )


class FakeAdapter:
    """An ``ExecutionAdapter`` whose every answer is chosen by the test."""

    name = "fake"
    is_paper = True
    is_simulated = False

    def __init__(
        self,
        *,
        order: BrokerOrder | None = None,
        submit_error: Exception | None = None,
        open_orders: Sequence[BrokerOrder] = (),
        positions: Sequence[BrokerPosition] = (),
        account_error: Exception | None = None,
        on_submit=None,
    ) -> None:
        self._order = order
        self._submit_error = submit_error
        self._open_orders = list(open_orders)
        self._positions = list(positions)
        self._account_error = account_error
        self._on_submit = on_submit
        self.submitted: list[str] = []
        self.known: dict[str, BrokerOrder] = {}

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        self.submitted.append(intent.client_order_id)
        if self._on_submit is not None:
            self._on_submit(intent)
        if self._submit_error is not None:
            raise self._submit_error
        assert self._order is not None, "fake adapter has no order configured"
        self.known[intent.client_order_id] = self._order
        return self._order

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        return self.known.get(client_order_id)

    def list_open_orders(self) -> Sequence[BrokerOrder]:
        return list(self._open_orders)

    def cancel(self, client_order_id: str) -> bool:
        return False

    def positions(self) -> Sequence[BrokerPosition]:
        return list(self._positions)

    def account(self) -> BrokerAccount:
        if self._account_error is not None:
            raise self._account_error
        return BrokerAccount(
            cash=Decimal("100"),
            equity=Decimal("100"),
            buying_power=Decimal("100"),
            non_marginable_buying_power=Decimal("100"),
        )


def _service(journal: Journal, ledger: AllocationLedger, adapter, clock) -> OrderService:
    return OrderService(journal, ledger, adapter, clock=clock)


def _reconciler(journal, ledger, adapter, service, clock) -> Reconciler:
    return Reconciler(journal, ledger, adapter, service, grace_seconds=120, clock=clock)


# ---------------------------------------------------------------------------
# Phase 5: deterministic client order ids
# ---------------------------------------------------------------------------


def test_client_order_id_is_deterministic_and_within_the_broker_limit():
    kwargs = {
        "symbol": "btc/usd",
        "timeframe": "1h",
        "bar_ts": START,
        "strategy_name": "sma_cross",
        "side": Side.BUY,
    }
    first = build_client_order_id(**kwargs)
    again = build_client_order_id(**{**kwargs, "symbol": "BTC/USD"})

    assert first == again  # symbol normalisation must not fork the identity
    assert first.startswith("cb-")
    assert len(first) <= 48

    assert build_client_order_id(**{**kwargs, "side": Side.SELL}) != first
    assert build_client_order_id(**{**kwargs, "bar_ts": START + timedelta(hours=1)}) != first
    assert build_client_order_id(**{**kwargs, "timeframe": "4h"}) != first


def test_build_intent_reuses_the_identifier_and_stamps_utc():
    intent = build_intent(
        symbol="BTC/USD",
        timeframe="1h",
        bar_ts=START,
        strategy_name="sma_cross",
        strategy_version="sma_cross@abc",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        notional=Decimal("20"),
        time_in_force=TimeInForce.GTC,
        now=START + timedelta(minutes=5),
    )
    assert intent.client_order_id == build_client_order_id(
        symbol="BTC/USD",
        timeframe="1h",
        bar_ts=START,
        strategy_name="sma_cross",
        side=Side.BUY,
    )
    assert intent.ts == START + timedelta(minutes=5)
    assert intent.fingerprint == build_intent(
        symbol="BTC/USD",
        timeframe="1h",
        bar_ts=START,
        strategy_name="sma_cross",
        strategy_version="sma_cross@other",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        notional=Decimal("20"),
        time_in_force=TimeInForce.GTC,
        now=START,
    ).fingerprint


# ---------------------------------------------------------------------------
# Phase 5: paper-only enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://api.alpaca.markets", "https://api.alpaca.markets/v2", "http://localhost:9999"],
)
def test_non_paper_endpoints_are_refused(url):
    with pytest.raises(PaperOnlyViolation):
        assert_paper_base_url(url)


def test_paper_endpoint_is_accepted_and_normalised():
    assert assert_paper_base_url("https://paper-api.alpaca.markets/") == (
        "https://paper-api.alpaca.markets"
    )


def test_empty_endpoint_is_refused():
    with pytest.raises(PaperOnlyViolation, match="no trading base url"):
        assert_paper_base_url("")


def test_adapter_refuses_a_live_configuration_at_construction(settings: Settings):
    live = replace(settings.alpaca, trading_base_url="https://api.alpaca.markets")
    with pytest.raises(PaperOnlyViolation, match="refusing live"):
        AlpacaPaperBroker(live, env=ENV)


def test_an_injected_client_on_a_live_host_is_rejected(settings: Settings):
    class LiveClient:
        base_url = "https://api.alpaca.markets"

    broker = AlpacaPaperBroker(settings.alpaca, client=LiveClient(), env=ENV)
    with pytest.raises(PaperOnlyViolation):
        broker.account()


def test_paper_adapter_reports_itself_as_paper(settings: Settings):
    broker = AlpacaPaperBroker(settings.alpaca, client=object(), env=ENV)
    assert broker.is_paper is True
    assert broker.is_simulated is False
    assert broker.base_url == "https://paper-api.alpaca.markets"


def test_crypto_time_in_force_is_restricted_to_gtc_and_ioc():
    # `day` is invalid for Alpaca crypto orders; the enum makes it unrepresentable.
    assert {member.value for member in TimeInForce} == {"gtc", "ioc"}


# ---------------------------------------------------------------------------
# Phase 5: order payload
# ---------------------------------------------------------------------------


def test_market_order_payload_uses_decimal_strings():
    intent = _buy_intent()
    payload = build_order_payload(intent)

    assert payload["symbol"] == "BTC/USD"
    assert payload["qty"] == "0.001"
    assert payload["side"] == "buy"
    assert payload["type"] == "market"
    assert payload["time_in_force"] == "gtc"
    assert payload["client_order_id"] == intent.client_order_id
    assert "limit_price" not in payload


def test_limit_order_payload_carries_the_limit_price():
    intent = make_intent(order_type=OrderType.LIMIT, limit_price="19000")
    payload = build_order_payload(intent)
    assert payload["type"] == "limit"
    assert payload["limit_price"] == "19000"


def test_stop_limit_orders_are_refused_rather_than_partially_supported():
    intent = make_intent(order_type=OrderType.STOP_LIMIT, limit_price="19000")
    with pytest.raises(ExecutionError, match="stop_limit"):
        build_order_payload(intent)


class _ApiError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _BrokerClient:
    """Stand-in for ``alpaca-py``'s ``TradingClient``."""

    def __init__(self, *, submit_error: Exception | None = None, lookup_error=None) -> None:
        self._submit_error = submit_error
        self._lookup_error = lookup_error
        self.requests: list[Mapping[str, Any]] = []

    def submit_order(self, request: Any) -> Any:
        self.requests.append(request)
        if self._submit_error is not None:
            raise self._submit_error
        raise AssertionError("not used")

    def get_order_by_client_id(self, client_order_id: str) -> Any:
        if self._lookup_error is not None:
            raise self._lookup_error
        return None


def _broker(settings: Settings, client: Any) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        settings.alpaca,
        client=client,
        request_builder=lambda payload: dict(payload),
        env=ENV,
    )


@pytest.mark.parametrize(
    "status, expected",
    [(503, AmbiguousSubmission), (429, AmbiguousSubmission), (422, DefiniteRejection),
     (403, DefiniteRejection)],
)
def test_submit_errors_are_classified(settings: Settings, status: int, expected: type):
    broker = _broker(settings, _BrokerClient(submit_error=_ApiError("boom", status)))
    with pytest.raises(expected):
        broker.submit(_buy_intent())


def test_an_unclassified_submit_failure_is_treated_as_ambiguous(settings: Settings):
    # Guessing "rejected" is how duplicate orders get created.
    broker = _broker(settings, _BrokerClient(submit_error=ValueError("weird")))
    with pytest.raises(AmbiguousSubmission):
        broker.submit(_buy_intent())


def test_get_order_returns_none_when_the_broker_has_never_heard_of_it(settings: Settings):
    broker = _broker(settings, _BrokerClient(lookup_error=_ApiError("not found", 404)))
    assert broker.get_order("cb-missing") is None


# ---------------------------------------------------------------------------
# Phase 5: OrderService sequencing and failure handling
# ---------------------------------------------------------------------------


def test_intent_and_pending_row_are_written_before_the_network(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = _buy_intent()
    observed: dict[str, Any] = {}

    def inspect(current: OrderIntent) -> None:
        observed["intent"] = journal.get_intent(current.client_order_id)
        record = journal.get_order(current.client_order_id)
        observed["status"] = record.status if record else None
        observed["reserved"] = ledger.state().reserved

    service = _service(
        journal, ledger, FakeAdapter(order=_filled(intent), on_submit=inspect), clock
    )
    service.submit(intent, _approved())

    assert observed["intent"] is not None
    assert observed["status"] is OrderStatus.PENDING_SUBMIT
    assert observed["reserved"] == Decimal("20.1")  # notional + fee/slippage buffer


def test_an_unapproved_verdict_never_reaches_the_adapter(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    adapter = FakeAdapter(order=_filled(_buy_intent()))
    service = _service(journal, ledger, adapter, clock)
    rejected = RiskVerdict.rejected([], Decimal("100"), START)

    with pytest.raises(RiskRejected):
        service.submit(_buy_intent(), rejected)
    assert adapter.submitted == []
    assert ledger.state().reserved == 0


def test_a_filled_buy_commits_the_cost_basis_and_expenses_the_fee(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = _buy_intent()
    service = _service(journal, ledger, FakeAdapter(order=_filled(intent)), clock)

    result = service.submit(intent, _approved())

    assert result.outcome is OrderOutcome.FILLED
    state = ledger.state()
    assert state.committed == Decimal("20")  # 0.001 * 20000, fee excluded
    assert state.reserved == 0
    assert state.realized_pnl == Decimal("-0.05")
    assert journal.get_order(intent.client_order_id).status is OrderStatus.FILLED


def test_replaying_the_same_broker_fill_does_not_double_book(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = _buy_intent()
    order = _filled(intent)
    service = _service(journal, ledger, FakeAdapter(order=order), clock)

    service.submit(intent, _approved())
    service.apply_broker_order(order)
    service.apply_broker_order(order)

    state = ledger.state()
    assert state.committed == Decimal("20")
    assert state.realized_pnl == Decimal("-0.05")
    assert len(journal.fills_for(intent.client_order_id)) == 1


def test_duplicate_intent_is_refused_by_the_journal(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = _buy_intent()
    service = _service(journal, ledger, FakeAdapter(order=_filled(intent)), clock)
    service.submit(intent, _approved())

    with pytest.raises(DuplicateOrderError):
        service.submit(intent, _approved())


def test_ambiguous_submission_marks_uncertain_and_is_never_retried(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    adapter = FakeAdapter(
        order=_filled(_buy_intent()), submit_error=AmbiguousSubmission("timeout after send")
    )
    intent = _buy_intent()
    service = _service(journal, ledger, adapter, clock)

    result = service.submit(intent, _approved())

    assert result.outcome is OrderOutcome.UNCERTAIN
    assert journal.get_order(intent.client_order_id).status is OrderStatus.UNCERTAIN
    assert len(adapter.submitted) == 1  # no automatic retry
    # The capital stays reserved: the order may still fill at the broker.
    assert ledger.state().reserved == Decimal("20.1")


def test_definite_rejection_releases_the_reservation(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    adapter = FakeAdapter(
        order=_filled(_buy_intent()), submit_error=DefiniteRejection("422 insufficient funds")
    )
    intent = _buy_intent()
    service = _service(journal, ledger, adapter, clock)

    result = service.submit(intent, _approved())

    assert result.outcome is OrderOutcome.REJECTED
    assert journal.get_order(intent.client_order_id).status is OrderStatus.REJECTED
    assert ledger.state().reserved == 0


def test_a_reservation_that_cannot_be_funded_never_touches_the_broker(
    journal: Journal, clock: Clock
):
    # A $10 ceiling cannot cover a $20 notional plus the fee buffer. The refusal
    # happens before the adapter is called, so nothing reaches the broker.
    ledger = AllocationLedger(journal, Decimal("10"))
    adapter = FakeAdapter(order=_filled(_buy_intent()))
    service = _service(journal, ledger, adapter, clock)

    result = service.submit(_buy_intent(), _approved())

    assert result.outcome is OrderOutcome.REJECTED
    assert "reservation refused" in result.message
    assert adapter.submitted == []
    assert journal.get_order(_buy_intent().client_order_id).status is OrderStatus.REJECTED
    assert ledger.state().reserved == 0


# ---------------------------------------------------------------------------
# Phase 5: recovery
# ---------------------------------------------------------------------------


def test_recovery_waits_out_the_grace_period_then_declares_the_order_lost(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = _buy_intent()
    adapter = FakeAdapter(
        order=_filled(intent), submit_error=AmbiguousSubmission("connection reset")
    )
    service = _service(journal, ledger, adapter, clock)
    service.submit(intent, _approved())

    record = journal.get_order(intent.client_order_id)
    assert service.recover(record, grace_seconds=120) == "pending"

    clock.advance(seconds=200)
    assert service.recover(record, grace_seconds=120) == "lost"

    stored = journal.get_order(intent.client_order_id)
    assert stored.status is OrderStatus.LOST
    assert ledger.state().reserved == 0  # released, not lost track of
    events = [row["kind"] for row in journal.recent_reconciliations(limit=5)]
    assert "order_lost" in events


def test_recovery_adopts_the_broker_state_when_the_order_exists(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = _buy_intent()
    order = _filled(intent)
    adapter = FakeAdapter(order=order, submit_error=AmbiguousSubmission("timeout"))
    service = _service(journal, ledger, adapter, clock)
    service.submit(intent, _approved())

    adapter.known[intent.client_order_id] = order  # the broker did receive it
    assert service.recover(journal.get_order(intent.client_order_id), grace_seconds=120) == (
        "recovered"
    )
    assert journal.get_order(intent.client_order_id).status is OrderStatus.FILLED
    assert ledger.state().committed == Decimal("20")


def test_recovery_never_resubmits(journal: Journal, ledger: AllocationLedger, clock: Clock):
    intent = _buy_intent()
    adapter = FakeAdapter(order=_filled(intent), submit_error=AmbiguousSubmission("timeout"))
    service = _service(journal, ledger, adapter, clock)
    service.submit(intent, _approved())

    for _ in range(3):
        service.recover(journal.get_order(intent.client_order_id), grace_seconds=0)

    assert len(adapter.submitted) == 1


# ---------------------------------------------------------------------------
# Phase 6: reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_is_clean_with_no_positions(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    adapter = FakeAdapter()
    service = _service(journal, ledger, adapter, clock)
    report = _reconciler(journal, ledger, adapter, service, clock).reconcile()

    assert report.ok, [str(drift) for drift in report.drifts]
    assert report.halted is False
    assert journal.get_system_state()[0] is SystemState.ACTIVE


def test_reconciliation_does_not_false_alarm_after_a_fee_bearing_fill(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    # Regression: committed used to include the buy fee while the broker's cost
    # basis does not, so this exact scenario halted the system on the first fill.
    intent = _buy_intent()
    adapter = FakeAdapter(order=_filled(intent))
    service = _service(journal, ledger, adapter, clock)
    service.submit(intent, _approved())

    adapter._positions = [
        BrokerPosition(
            symbol="BTC/USD",
            quantity="0.001",
            avg_entry_price="20000",
            market_value="20",
            current_price="20000",
        )
    ]
    report = _reconciler(journal, ledger, adapter, service, clock).reconcile()

    assert report.ok, [str(drift) for drift in report.drifts]
    assert journal.get_system_state()[0] is SystemState.ACTIVE


def test_a_position_mismatch_halts_the_system(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    adapter = FakeAdapter()
    service = _service(journal, ledger, adapter, clock)
    adapter._positions = [
        BrokerPosition(
            symbol="BTC/USD",
            quantity="0.001",
            avg_entry_price="20000",
            market_value="20",
            current_price="20000",
        )
    ]

    report = _reconciler(journal, ledger, adapter, service, clock).reconcile()

    kinds = {drift.kind for drift in report.drifts}
    assert "position_mismatch" in kinds
    assert report.halted is True
    assert journal.get_system_state()[0] is SystemState.HALTED


def test_an_unknown_broker_order_is_critical(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = _buy_intent()
    adapter = FakeAdapter(open_orders=[_working(intent)])
    service = _service(journal, ledger, adapter, clock)

    report = _reconciler(journal, ledger, adapter, service, clock).reconcile()

    assert "unknown_broker_order" in {drift.kind for drift in report.drifts}
    assert report.halted is True


def test_a_broker_outage_is_critical_and_fails_closed(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    adapter = FakeAdapter(account_error=RuntimeError("proxy unreachable"))
    service = _service(journal, ledger, adapter, clock)

    report = _reconciler(journal, ledger, adapter, service, clock).reconcile()

    assert "broker_unavailable" in {drift.kind for drift in report.drifts}
    assert report.halted is True
    assert journal.get_system_state()[0] is SystemState.HALTED


def test_reconciliation_can_run_without_halting_for_observation(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = _buy_intent()
    adapter = FakeAdapter(open_orders=[_working(intent)])
    service = _service(journal, ledger, adapter, clock)

    report = _reconciler(journal, ledger, adapter, service, clock).reconcile(
        halt_on_critical=False
    )

    assert report.drifts
    assert report.halted is False
    assert journal.get_system_state()[0] is SystemState.ACTIVE


# ---------------------------------------------------------------------------
# Phase 6: restart recovery
# ---------------------------------------------------------------------------


def test_state_survives_a_restart(tmp_path, clock: Clock, settings: Settings):
    path = str(tmp_path / "restart.db")
    intent = _buy_intent()

    first = Journal(path, clock=clock)
    first.initialize()
    try:
        ledger = AllocationLedger(first, Decimal("100"))
        service = _service(first, ledger, FakeAdapter(order=_filled(intent)), clock)
        service.submit(intent, _approved())
        before = ledger.state()
    finally:
        first.close()

    reopened = Journal(path, clock=clock)
    reopened.initialize()
    try:
        rebuilt = AllocationLedger(reopened, Decimal("100"))
        after = rebuilt.state()

        assert after.committed == before.committed == Decimal("20")
        assert after.realized_pnl == before.realized_pnl == Decimal("-0.05")
        assert after.reserved == before.reserved == 0
        assert after.ceiling == Decimal("100")

        # The order, its intent, and its fill all survived.
        assert reopened.get_intent(intent.client_order_id) is not None
        assert reopened.get_order(intent.client_order_id).status is OrderStatus.FILLED
        assert len(reopened.fills_for(intent.client_order_id)) == 1

        # And the rebuilt state reconciles against the broker.
        adapter = FakeAdapter(
            positions=[
                BrokerPosition(
                    symbol="BTC/USD",
                    quantity="0.001",
                    avg_entry_price="20000",
                    market_value="20",
                    current_price="20000",
                )
            ]
        )
        service = _service(reopened, rebuilt, adapter, clock)
        report = _reconciler(reopened, rebuilt, adapter, service, clock).reconcile()
        assert report.ok, [str(drift) for drift in report.drifts]
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# Stage 2: the local simulator
# ---------------------------------------------------------------------------


def test_simulator_fills_at_the_reference_price_plus_slippage():
    simulator = LocalSimulatorAdapter(starting_cash=Decimal("100"), clock=lambda: START)
    simulator.set_price("BTC/USD", Decimal("20000"))
    intent = _buy_intent()

    order = simulator.submit(intent)

    assert order.status is OrderStatus.FILLED
    assert order.avg_fill_price == Decimal("20000") * (Decimal(1) + Decimal("0.0025"))
    assert order.fills[0].fee > 0


def test_simulator_is_idempotent_for_the_same_client_order_id():
    simulator = LocalSimulatorAdapter(clock=lambda: START)
    simulator.set_price("BTC/USD", Decimal("20000"))
    intent = _buy_intent()

    first = simulator.submit(intent)
    second = simulator.submit(intent)

    assert first == second
    assert simulator.account().cash < Decimal("100")  # charged exactly once


def test_simulator_requires_a_reference_price():
    simulator = LocalSimulatorAdapter(clock=lambda: START)
    with pytest.raises(ValueError, match="no reference price"):
        simulator.submit(_buy_intent())


def test_simulator_books_sells_against_the_position():
    simulator = LocalSimulatorAdapter(clock=lambda: START)
    simulator.set_price("BTC/USD", Decimal("20000"))
    simulator.submit(_buy_intent())
    sell = make_intent(side=Side.SELL, quantity="0.001", notional="20", client_order_id="cb-sell")

    simulator.submit(sell)

    assert simulator.positions() == []
    assert simulator.account().equity < Decimal("100")  # slippage and fees


def test_simulator_cannot_cancel_a_filled_order():
    simulator = LocalSimulatorAdapter(clock=lambda: START)
    simulator.set_price("BTC/USD", Decimal("20000"))
    intent = _buy_intent()
    simulator.submit(intent)

    assert simulator.cancel(intent.client_order_id) is False


def test_simulator_is_honest_about_being_a_simulation():
    simulator = LocalSimulatorAdapter()
    assert simulator.is_simulated is True
    assert simulator.is_paper is False  # never claim this is a real account
