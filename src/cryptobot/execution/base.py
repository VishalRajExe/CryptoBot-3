"""Broker-facing data types and the adapter contract.

Everything a broker reports is normalised into these types at the boundary, so
the rest of the application never imports ``alpaca``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, Sequence

from cryptobot.core.contracts import (
    FillRecord,
    OrderIntent,
    OrderStatus,
    PositionSnapshot,
    Side,
    normalize_symbol,
    to_decimal,
    to_utc,
)


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    non_marginable_buying_power: Decimal
    currency: str = "USD"
    status: str = "ACTIVE"
    trading_blocked: bool = False
    account_blocked: bool = False

    def __post_init__(self) -> None:
        for name in ("cash", "equity", "buying_power", "non_marginable_buying_power"):
            object.__setattr__(self, name, to_decimal(getattr(self, name), name))

    @property
    def usable(self) -> bool:
        return not (self.trading_blocked or self.account_blocked)


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal = Decimal("0")
    current_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        for name in ("quantity", "avg_entry_price", "market_value", "unrealized_pl"):
            object.__setattr__(self, name, to_decimal(getattr(self, name), name))
        if self.current_price is not None:
            object.__setattr__(self, "current_price", to_decimal(self.current_price, "current_price"))

    def to_snapshot(self, ts: datetime, source: str = "broker") -> PositionSnapshot:
        last = self.current_price
        if last is None:
            last = (
                self.market_value / self.quantity
                if self.quantity > 0
                else self.avg_entry_price
            )
        return PositionSnapshot(
            ts=ts,
            symbol=self.symbol,
            quantity=self.quantity,
            avg_entry_price=self.avg_entry_price,
            last_price=last,
            source=source,
        )


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    status: OrderStatus
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    submitted_at: datetime | None = None
    raw_status: str | None = None
    reason: str | None = None
    fills: tuple[FillRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "status", OrderStatus(self.status))
        object.__setattr__(self, "quantity", to_decimal(self.quantity, "quantity"))
        object.__setattr__(self, "filled_quantity", to_decimal(self.filled_quantity, "filled_quantity"))
        if self.avg_fill_price is not None:
            object.__setattr__(self, "avg_fill_price", to_decimal(self.avg_fill_price, "avg_fill_price"))
        if self.submitted_at is not None:
            object.__setattr__(self, "submitted_at", to_utc(self.submitted_at, "submitted_at"))

    @property
    def remaining_quantity(self) -> Decimal:
        remaining = self.quantity - self.filled_quantity
        return remaining if remaining > 0 else Decimal("0")


class ExecutionAdapter(Protocol):
    """The only interface through which orders may reach a broker."""

    name: str

    @property
    def is_paper(self) -> bool: ...

    @property
    def is_simulated(self) -> bool: ...

    def submit(self, intent: OrderIntent) -> BrokerOrder: ...

    def get_order(self, client_order_id: str) -> BrokerOrder | None: ...

    def list_open_orders(self) -> Sequence[BrokerOrder]: ...

    def cancel(self, client_order_id: str) -> bool: ...

    def positions(self) -> Sequence[BrokerPosition]: ...

    def account(self) -> BrokerAccount: ...
