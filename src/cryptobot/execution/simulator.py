"""Local dry-run execution adapter.

This is testing stage 2 from the specification: it produces orders and fills
without any broker involvement. It exists so the full pipeline (signal -> risk ->
order -> fill -> ledger -> journal) can be exercised in tests and by
``cryptobot dryrun`` with no credentials.

It is *not* a paper-trading account. It fills immediately at a reference price
plus modelled slippage and is therefore optimistic about queue position and
partial fills; that limitation is why Alpaca paper trading is a separate stage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Sequence

from cryptobot.core.contracts import (
    FillRecord,
    OrderIntent,
    OrderStatus,
    Side,
    to_decimal,
)
from cryptobot.execution.base import BrokerAccount, BrokerOrder, BrokerPosition

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LocalSimulatorAdapter:
    name = "local-simulator"

    def __init__(
        self,
        *,
        fee_bps: Decimal = Decimal("25"),
        slippage_bps: Decimal = Decimal("25"),
        starting_cash: Decimal = Decimal("100"),
        clock=_utcnow,
    ) -> None:
        self._fee_fraction = to_decimal(fee_bps, "fee_bps") / Decimal("10000")
        self._slippage_fraction = to_decimal(slippage_bps, "slippage_bps") / Decimal("10000")
        self._cash = to_decimal(starting_cash, "starting_cash")
        self._clock = clock
        self._prices: Dict[str, Decimal] = {}
        self._orders: Dict[str, BrokerOrder] = {}
        self._positions: Dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (qty, avg)

    # -- adapter contract ---------------------------------------------------

    @property
    def is_paper(self) -> bool:
        # Fills are simulated locally; this is never a real account.
        return False

    @property
    def is_simulated(self) -> bool:
        return True

    def set_price(self, symbol: str, price: Decimal) -> None:
        self._prices[symbol] = to_decimal(price, "price")

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            # Idempotent: same client_order_id returns the same order, no new fill.
            return existing

        reference = self._prices.get(intent.symbol)
        if reference is None:
            raise ValueError(f"no reference price set for {intent.symbol}")

        if intent.side is Side.BUY:
            fill_price = reference * (Decimal(1) + self._slippage_fraction)
        else:
            fill_price = reference * (Decimal(1) - self._slippage_fraction)

        fill = FillRecord(
            fill_id=f"sim-{intent.client_order_id[-16:]}",
            broker_order_id=f"sim-{intent.client_order_id}",
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=fill_price,
            fee=intent.quantity * fill_price * self._fee_fraction,
            fee_currency=intent.symbol.split("/")[1],
            ts=self._clock(),
        )

        self._apply_to_book(fill)

        order = BrokerOrder(
            client_order_id=intent.client_order_id,
            broker_order_id=fill.broker_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            status=OrderStatus.FILLED,
            filled_quantity=intent.quantity,
            avg_fill_price=fill_price,
            submitted_at=self._clock(),
            raw_status="filled",
            fills=(fill,),
        )
        self._orders[intent.client_order_id] = order
        return order

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        return self._orders.get(client_order_id)

    def list_open_orders(self) -> Sequence[BrokerOrder]:
        return [o for o in self._orders.values() if o.status.is_open]

    def cancel(self, client_order_id: str) -> bool:
        order = self._orders.get(client_order_id)
        if order is None or not order.status.is_open:
            return False
        self._orders[client_order_id] = replace(
            order, status=OrderStatus.CANCELED, raw_status="canceled"
        )
        return True

    def positions(self) -> Sequence[BrokerPosition]:
        out: list[BrokerPosition] = []
        for symbol, (quantity, avg) in self._positions.items():
            if quantity <= 0:
                continue
            price = self._prices.get(symbol, avg)
            out.append(
                BrokerPosition(
                    symbol=symbol,
                    quantity=quantity,
                    avg_entry_price=avg,
                    market_value=quantity * price,
                    unrealized_pl=(price - avg) * quantity,
                    current_price=price,
                )
            )
        return out

    def account(self) -> BrokerAccount:
        equity = self._cash
        for symbol, (quantity, avg) in self._positions.items():
            equity += quantity * self._prices.get(symbol, avg)
        return BrokerAccount(
            cash=self._cash,
            equity=equity,
            buying_power=self._cash,
            non_marginable_buying_power=self._cash,
        )

    # -- internals ----------------------------------------------------------

    def _apply_to_book(self, fill: FillRecord) -> None:
        quantity, avg = self._positions.get(fill.symbol, (Decimal("0"), Decimal("0")))
        fee_quote = fill.fee_in_quote()
        if fill.side is Side.BUY:
            self._cash -= fill.quantity * fill.price + fee_quote
            new_qty = quantity + fill.quantity
            new_avg = ((quantity * avg) + (fill.quantity * fill.price)) / new_qty if new_qty else Decimal("0")
            self._positions[fill.symbol] = (new_qty, new_avg)
        else:
            self._cash += fill.quantity * fill.price - fee_quote
            new_qty = quantity - fill.quantity
            self._positions[fill.symbol] = (new_qty if new_qty > 0 else Decimal("0"), avg)
