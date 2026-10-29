"""Fee accounting in the currency the broker actually charges it.

Alpaca charges a crypto fee on the asset a trade *credits* (verified 2026-10-29,
``docs/API_CAPABILITIES.md`` §4). A buy of BTC/USD therefore pays its fee in BTC,
not in USD, and the account is credited with the fill quantity *minus* that fee.
Three consequences are pinned here because each one was wrong in the first
implementation and each one fails silently:

* the fee currency on a buy is the base asset, so the recorded holding is the net
  quantity - booking the gross quantity overstates every position by one fee;
* partial fills must be booked as they happen, and the Orders API reports a
  *cumulative* ``filled_qty``, so booking without taking the delta double counts;
* reconciliation must tolerate a fee-sized difference, because ``committed`` and
  the broker's cost basis are both fee-free but are derived independently.

The last section covers the fees the broker posts after the fact: the Orders API
returns no fee at all, so what is recorded at fill time is an estimate that has to
be corrected exactly once from the ``CFEE``/``FEE`` account activities.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

import pytest

from cryptobot.core.contracts import (
    FillRecord,
    OrderIntent,
    OrderStatus,
    RiskVerdict,
    Side,
    base_currency,
    credited_currency,
    quote_currency,
)
from cryptobot.core.errors import ContractError
from cryptobot.execution.base import BrokerAccount, BrokerOrder, BrokerPosition
from cryptobot.execution.fees import (
    FeeActivity,
    attribute_fees,
    parse_fee_activities,
    parse_fee_activity,
)
from cryptobot.execution.service import OrderOutcome, OrderService
from cryptobot.journal.store import Journal
from cryptobot.reconciliation.reconciler import Reconciler
from cryptobot.sizing.ledger import AllocationLedger

from conftest import START, Clock, make_intent

UTC = START.tzinfo
BTC = "BTC/USD"
PRICE = Decimal("20000")

#: The configured reserve rate, i.e. the taker tier. Doubles as the fee estimate.
FEE_FRACTION = Decimal("0.0025")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _approved() -> RiskVerdict:
    return RiskVerdict(True, (), (), Decimal("100"), START)


def _broker_order(
    intent: OrderIntent,
    filled: Decimal,
    price: Decimal,
    status: OrderStatus,
) -> BrokerOrder:
    """An order as the broker reports it: a cumulative ``filled_quantity``."""
    return BrokerOrder(
        client_order_id=intent.client_order_id,
        broker_order_id=f"broker-{intent.client_order_id}",
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        status=status,
        filled_quantity=filled,
        avg_fill_price=price,
        submitted_at=START,
        raw_status=status.value,
    )


class FakeAdapter:
    """Replays a queue of broker orders and reports fixed positions."""

    name = "fake"
    is_paper = True
    is_simulated = False

    def __init__(
        self,
        *,
        orders: Sequence[BrokerOrder] = (),
        positions: Sequence[BrokerPosition] = (),
        cash: str = "100",
    ) -> None:
        self._orders = list(orders)
        self._positions = list(positions)
        self._cash = Decimal(cash)
        self._known: dict[str, BrokerOrder] = {}
        self.submitted: list[str] = []

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        self.submitted.append(intent.client_order_id)
        order = self._orders.pop(0)
        self._known[intent.client_order_id] = order
        return order

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        return self._known.get(client_order_id)

    def list_open_orders(self) -> Sequence[BrokerOrder]:
        return []

    def cancel(self, client_order_id: str) -> bool:
        return False

    def positions(self) -> Sequence[BrokerPosition]:
        return list(self._positions)

    def account(self) -> BrokerAccount:
        return BrokerAccount(
            cash=self._cash,
            equity=self._cash,
            buying_power=self._cash,
            non_marginable_buying_power=self._cash,
        )


def _fill(
    *,
    fill_id: str = "fill-1",
    client_order_id: str = "cb-buy-1",
    side: Side = Side.BUY,
    quantity: str = "0.001",
    price: str = "20000",
    fee: str = "0.0000025",
    fee_currency: str = "BTC",
    ts: datetime = START,
) -> FillRecord:
    return FillRecord(
        fill_id=fill_id,
        broker_order_id=f"broker-{fill_id}",
        client_order_id=client_order_id,
        symbol=BTC,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_currency=fee_currency,
        ts=ts,
    )


def _seed_buy(
    journal: Journal, ledger: AllocationLedger, fill: FillRecord | None = None
) -> FillRecord:
    """A recorded buy plus the ledger movement it implies."""
    fill = fill or _fill()
    journal.record_fill(fill)
    ledger.commit_buy(fill.client_order_id, fill.net_quantity, fill.price, fill.fee_in_quote())
    return fill


# ---------------------------------------------------------------------------
# the convention: fees are charged on the credited asset
# ---------------------------------------------------------------------------


def test_a_trade_is_charged_fees_on_what_it_credits():
    assert base_currency(BTC) == "BTC"
    assert quote_currency(BTC) == "USD"
    assert credited_currency(BTC, Side.BUY) == "BTC"
    assert credited_currency(BTC, Side.SELL) == "USD"
    # legacy symbols must not fork the answer
    assert credited_currency("ETHUSD", Side.BUY) == "ETH"
    assert credited_currency("eth/usd", Side.SELL) == "USD"


def test_a_base_currency_fee_reduces_the_quantity_actually_credited():
    fill = _fill()

    assert fill.fee_is_in_base
    assert not fill.fee_is_in_quote
    assert fill.net_quantity == Decimal("0.0009975")  # 0.001 - 0.0000025
    assert fill.fee_in_quote() == Decimal("0.05")  # 0.0000025 * 20000
    # capital conservation: what was credited plus what was charged is the trade
    assert fill.net_quantity * fill.price + fill.fee_in_quote() == fill.gross_notional


def test_a_quote_currency_fee_is_a_cost_not_a_quantity_reduction():
    fill = _fill(side=Side.SELL, quantity="0.0009975", price="21000", fee="0.05", fee_currency="USD")

    assert fill.fee_is_in_quote
    assert fill.net_quantity == fill.quantity
    assert fill.fee_in_quote() == Decimal("0.05")


def test_a_fee_that_consumes_the_whole_fill_is_refused():
    oversized = _fill(fee="0.001")

    with pytest.raises(ContractError):
        oversized.net_quantity


# ---------------------------------------------------------------------------
# the estimate booked at fill time
# ---------------------------------------------------------------------------


def test_a_filled_buy_estimates_its_fee_in_the_base_asset(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = make_intent(side=Side.BUY, quantity="0.001", notional="20")
    order = _broker_order(intent, Decimal("0.001"), PRICE, OrderStatus.FILLED)
    service = OrderService(journal, ledger, FakeAdapter(orders=[order]), clock=clock)

    result = service.submit(intent, _approved())

    assert result.outcome is OrderOutcome.FILLED
    fills = journal.fills_for(intent.client_order_id)
    assert len(fills) == 1
    fill = fills[0]
    # Regression: this used to be "USD" on a buy, which silently capitalised the
    # fee and overstated the position.
    assert fill.fee_currency == "BTC"
    assert fill.fee == Decimal("0.0000025")  # 0.001 * 0.0025, a fraction of quantity
    assert fill.net_quantity == Decimal("0.0009975")

    state = ledger.state()
    assert state.committed == Decimal("19.95")  # 0.0009975 * 20000
    assert state.reserved == 0
    assert state.realized_pnl == Decimal("-0.05")
    # The ledger and the independent fill-derived position must agree exactly.
    assert journal.realized_position(BTC) == (Decimal("0.0009975"), PRICE)


def test_a_filled_sell_estimates_its_fee_in_the_quote_asset(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    buy = make_intent(side=Side.BUY, quantity="0.001", notional="20", client_order_id="cb-buy-1")
    sell = make_intent(
        side=Side.SELL, quantity="0.0009975", notional="20.9475", client_order_id="cb-sell-1"
    )
    adapter = FakeAdapter(
        orders=[
            _broker_order(buy, Decimal("0.001"), PRICE, OrderStatus.FILLED),
            _broker_order(sell, Decimal("0.0009975"), Decimal("21000"), OrderStatus.FILLED),
        ]
    )
    service = OrderService(journal, ledger, adapter, clock=clock)

    service.submit(buy, _approved())
    service.submit(sell, _approved())

    sell_fill = journal.fills_for(sell.client_order_id)[0]
    assert sell_fill.fee_currency == "USD"
    assert sell_fill.fee == Decimal("0.05236875")  # 0.0009975 * 21000 * 0.0025
    assert sell_fill.net_quantity == sell_fill.quantity

    state = ledger.state()
    # A round trip of the whole position releases every cent of basis.
    assert state.committed == 0
    assert state.reserved == 0
    assert state.realized_pnl == Decimal("-0.05") + Decimal("0.94513125")


def test_a_round_trip_conserves_capital(journal: Journal, ledger: AllocationLedger):
    """Every dollar out is either basis, a fee, or realised P&L."""
    fee_estimate = Decimal("0.05")
    basis = Decimal("19.95")
    proceeds = Decimal("20.89513125")  # 20.9475 - 0.05236875

    _seed_buy(journal, ledger)
    committed = ledger.state().committed
    realised = ledger.apply_sell("cb-sell-1", Decimal("0.0009975"), Decimal("21000"), Decimal("0.05236875"), PRICE)

    assert committed == basis
    assert realised == proceeds - basis
    assert ledger.state().committed == 0
    assert ledger.state().realized_pnl == realised - fee_estimate


# ---------------------------------------------------------------------------
# partial fills
# ---------------------------------------------------------------------------


def test_a_partial_fill_is_booked_before_the_order_completes(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = make_intent(side=Side.BUY, quantity="0.001", notional="20")
    partial = _broker_order(intent, Decimal("0.0004"), PRICE, OrderStatus.PARTIALLY_FILLED)
    service = OrderService(journal, ledger, FakeAdapter(orders=[partial]), clock=clock)

    result = service.submit(intent, _approved())

    assert result.outcome is OrderOutcome.SUBMITTED
    fills = journal.fills_for(intent.client_order_id)
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("0.0004")
    assert fills[0].fee == Decimal("0.000001")  # 0.0004 * 0.0025

    state = ledger.state()
    assert state.committed == Decimal("7.98")  # 0.000399 * 20000
    assert state.realized_pnl == Decimal("-0.02")
    # The unfilled remainder stays reserved, so the cap still covers the order.
    assert state.reserved == Decimal("12.06")  # 0.0006 * 20000 * 1.005
    assert state.committed + state.reserved <= state.ceiling


def test_replaying_a_cumulative_partial_fill_does_not_double_book(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = make_intent(side=Side.BUY, quantity="0.001", notional="20")
    partial = _broker_order(intent, Decimal("0.0004"), PRICE, OrderStatus.PARTIALLY_FILLED)
    service = OrderService(journal, ledger, FakeAdapter(orders=[partial]), clock=clock)

    service.submit(intent, _approved())
    service.apply_broker_order(partial)
    service.apply_broker_order(partial)

    assert len(journal.fills_for(intent.client_order_id)) == 1
    assert ledger.state().committed == Decimal("7.98")


def test_completing_a_partial_fill_books_only_the_increment(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    intent = make_intent(side=Side.BUY, quantity="0.001", notional="20")
    partial = _broker_order(intent, Decimal("0.0004"), PRICE, OrderStatus.PARTIALLY_FILLED)
    complete = _broker_order(intent, Decimal("0.001"), PRICE, OrderStatus.FILLED)
    service = OrderService(journal, ledger, FakeAdapter(orders=[partial]), clock=clock)

    service.submit(intent, _approved())
    service.apply_broker_order(complete)

    assert [f.quantity for f in journal.fills_for(intent.client_order_id)] == [
        Decimal("0.0004"),
        Decimal("0.0006"),
    ]
    state = ledger.state()
    assert state.committed == Decimal("19.95")  # the whole position, once
    assert state.reserved == 0
    assert state.realized_pnl == Decimal("-0.05")


# ---------------------------------------------------------------------------
# reconciliation tolerance sized to one fee
# ---------------------------------------------------------------------------


def _reconcile(journal: Journal, ledger: AllocationLedger, positions, clock: Clock):
    adapter = FakeAdapter(positions=positions, cash="80")
    service = OrderService(journal, ledger, adapter, clock=clock)
    return Reconciler(journal, ledger, adapter, service, clock=clock).reconcile()


def test_a_fee_sized_quantity_difference_is_not_treated_as_drift(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    """The broker may report the fill gross while we hold it net, by one fee."""
    _seed_buy(journal, ledger)

    broker = BrokerPosition(
        symbol=BTC,
        quantity=Decimal("0.001"),  # gross: 0.0009975 net + 0.0000025 fee
        avg_entry_price=PRICE,
        market_value=Decimal("20"),
    )
    report = _reconcile(journal, ledger, [broker], clock)

    assert report.ok, report.summary()
    assert not report.halted


def test_a_missing_fill_is_still_critical_drift(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    _seed_buy(journal, ledger)

    broker = BrokerPosition(
        symbol=BTC,
        quantity=Decimal("0.002"),  # twice what our fills imply
        avg_entry_price=PRICE,
        market_value=Decimal("40"),
    )
    report = _reconcile(journal, ledger, [broker], clock)

    kinds = {d.kind for d in report.drifts}
    assert "position_mismatch" in kinds
    assert report.critical
    assert report.halted


# ---------------------------------------------------------------------------
# parsing the fee activities the broker posts
# ---------------------------------------------------------------------------


def test_a_cfee_activity_is_valued_in_the_base_asset():
    """The documented example from the Alpaca crypto-fees page."""
    payload = {
        "id": "20220812000000000::53be51ba-0000-0000-0000-000000000000",
        "activity_type": "CFEE",
        "date": "2022-08-12",
        "net_amount": "0",
        "description": "Coin Pair Transaction Fee (Non USD)",
        "symbol": "ETHUSD",
        "qty": "-0.000195",
        "price": "1884.5",
        "status": "executed",
    }

    activity = parse_fee_activity(payload)

    assert activity is not None
    assert activity.symbol == "ETH/USD"
    assert activity.currency == "ETH"
    assert activity.charged == Decimal("0.000195")  # the sign is the broker's
    assert activity.quote_value == Decimal("0.3674775")  # 0.000195 * 1884.5
    assert activity.side is Side.BUY
    assert activity.ts == datetime(2022, 8, 12, tzinfo=UTC)


def test_a_fee_activity_is_valued_from_net_amount():
    activity = parse_fee_activity(
        {"id": "a1", "activity_type": "FEE", "symbol": "BTCUSD", "qty": "0", "net_amount": "-0.06"}
    )

    assert activity is not None
    assert activity.currency == "USD"
    assert activity.quote_value == Decimal("0.06")
    assert activity.side is Side.SELL


def test_non_fee_activities_are_ignored():
    assert parse_fee_activity({"activity_type": "FILL", "symbol": "BTCUSD", "qty": "1"}) is None
    assert parse_fee_activity({"activity_type": "CFEE", "qty": "1"}) is None  # no symbol
    assert parse_fee_activities([{"activity_type": "CSD"}, {"activity_type": "INT"}]) == []


def test_a_base_asset_fee_without_a_price_is_not_assumed_to_be_free():
    payload = {"id": "a1", "activity_type": "CFEE", "symbol": "BTCUSD", "qty": "-0.0000025"}

    assert parse_fee_activity(payload) is None


def test_parse_sorts_by_time_and_requires_an_id():
    early = {
        "id": "a1",
        "activity_type": "CFEE",
        "symbol": "BTCUSD",
        "qty": "-1",
        "price": "2",
        "date": "2024-01-01",
    }
    late = {
        "id": "a2",
        "activity_type": "CFEE",
        "symbol": "BTCUSD",
        "qty": "-1",
        "price": "2",
        "date": "2030-01-01",
    }
    parsed = parse_fee_activities([late, early, {**early, "id": ""}])

    assert [a.activity_id for a in parsed] == ["a1", "a2"]


# ---------------------------------------------------------------------------
# attributing the actual charge to the fill that produced it
# ---------------------------------------------------------------------------


def test_the_actual_fee_replaces_the_estimate_and_refunds_the_difference(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    _seed_buy(journal, ledger)  # estimate 0.0000025 BTC == 0.05 quote
    activity = FeeActivity(
        activity_id="act-1",
        kind="CFEE",
        ts=START + timedelta(days=1),
        symbol=BTC,
        charged=Decimal("0.000002"),
        currency="BTC",
        quote_value=Decimal("0.04"),
    )

    report = attribute_fees(journal, ledger, [activity], clock=clock)

    assert report.corrected == 1
    assert report.ok
    assert report.delta_total == Decimal("-0.01")

    fill = journal.fills_for("cb-buy-1")[0]
    assert fill.fee == Decimal("0.000002")

    state = ledger.state()
    assert state.realized_pnl == Decimal("-0.04")
    # The fee was smaller, so slightly more of the asset was credited.
    assert state.committed == Decimal("19.96")
    assert state.committed + Decimal("0.04") == Decimal("20")
    # The ledger and the fill-derived position still agree.
    quantity, average = journal.realized_position(BTC)
    assert quantity * average == state.committed


def test_attribution_is_idempotent(journal: Journal, ledger: AllocationLedger, clock: Clock):
    _seed_buy(journal, ledger)
    activity = FeeActivity(
        activity_id="act-1",
        kind="CFEE",
        ts=START + timedelta(days=1),
        symbol=BTC,
        charged=Decimal("0.000002"),
        currency="BTC",
        quote_value=Decimal("0.04"),
    )

    attribute_fees(journal, ledger, [activity], clock=clock)
    before = ledger.state()
    second = attribute_fees(journal, ledger, [activity], clock=clock)

    assert second.corrected == 0
    assert second.already_recorded == 1
    assert second.delta_total == 0
    assert ledger.state() == before


def test_an_unmatched_fee_is_expensed_and_reported(journal: Journal, ledger: AllocationLedger, clock: Clock):
    """Money left the account, so the ledger must show it - but flag it too."""
    activity = FeeActivity(
        activity_id="act-orphan",
        kind="CFEE",
        ts=START + timedelta(days=1),
        symbol="ETH/USD",
        charged=Decimal("0.000195"),
        currency="ETH",
        quote_value=Decimal("0.3674775"),
    )

    report = attribute_fees(journal, ledger, [activity], clock=clock)

    assert report.unmatched == (activity,)
    assert not report.ok
    assert "UNATTRIBUTED" in report.summary()
    assert ledger.state().realized_pnl == Decimal("-0.3674775")
    assert ledger.state().committed == 0
    rows = journal.fee_activities()
    assert len(rows) == 1
    assert rows[0]["matched_fill_id"] is None


def test_a_quote_denominated_fee_is_written_from_its_value_not_its_quantity(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    """A ``FEE`` activity reports ``qty`` zero and carries the amount in net_amount."""
    journal.record_fill(
        _fill(
            side=Side.SELL,
            quantity="0.0009975",
            price="21000",
            fee="0.05",
            fee_currency="USD",
            ts=datetime(2024, 2, 1, tzinfo=UTC),
        )
    )
    activity = parse_fee_activity(
        {
            "id": "act-fee",
            "activity_type": "FEE",
            "symbol": "BTCUSD",
            "qty": "0",
            "net_amount": "-0.06",
            "date": "2024-02-02",
        }
    )
    assert activity is not None

    report = attribute_fees(journal, ledger, [activity], clock=clock)

    assert report.corrected == 1
    fill = journal.fills_for("cb-buy-1")[0]
    assert fill.fee == Decimal("0.06")  # not zero, and not the raw qty
    assert ledger.state().committed == 0  # a sell fee never touches basis


def test_an_already_attributed_fill_is_not_matched_twice(
    journal: Journal, ledger: AllocationLedger, clock: Clock
):
    _seed_buy(journal, ledger)
    first = FeeActivity(
        activity_id="act-1",
        kind="CFEE",
        ts=START + timedelta(days=1),
        symbol=BTC,
        charged=Decimal("0.0000025"),
        currency="BTC",
        quote_value=Decimal("0.05"),
    )
    second = FeeActivity(
        activity_id="act-2",
        kind="CFEE",
        ts=START + timedelta(days=2),
        symbol=BTC,
        charged=Decimal("0.0000025"),
        currency="BTC",
        quote_value=Decimal("0.05"),
    )

    report = attribute_fees(journal, ledger, [first, second], clock=clock)

    assert report.corrected == 1
    assert len(report.unmatched) == 1
    assert report.unmatched[0].activity_id == "act-2"
    assert len(journal.fills_for("cb-buy-1")) == 1
