"""Post-hoc evaluation of what actually happened, from the journal only.

Two separate questions, deliberately kept apart:

* **Did the execution match the intent?** :func:`evaluate_execution` compares
  each filled order with the reference price implied by the intent it came
  from, so slippage is measured rather than assumed.
* **What did each round trip earn?** :func:`evaluate_trades` reconstructs
  closed positions from fills with average-cost accounting, which is the same
  convention the allocation ledger uses.

This module reads the journal and returns numbers. It cannot place, cancel, or
size an order, and it imports nothing from ``execution``/``risk``/``sizing``
beyond the shared contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from cryptobot.core.contracts import (
    FillRecord,
    OrderType,
    Side,
    to_decimal,
)
from cryptobot.journal.store import Journal

ZERO = Decimal("0")
BPS = Decimal("10000")
EPSILON = Decimal("0.00000001")


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == ZERO:
        return ZERO
    return (numerator / denominator) * Decimal("100")


def _round(value: Decimal, places: str = "0.0001") -> Decimal:
    # An infinite profit factor cannot be quantised; leave it as the value says.
    if not value.is_finite():
        return value
    return value.quantize(Decimal(places))


@dataclass(frozen=True)
class ExecutedTrade:
    """One completed round trip, reconstructed from fills."""

    symbol: str
    quantity: Decimal
    entry_ts: datetime
    exit_ts: datetime
    entry_price: Decimal
    exit_price: Decimal
    fees: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    client_order_ids: tuple[str, ...] = ()

    @property
    def hold_seconds(self) -> int:
        return max(0, int((self.exit_ts - self.entry_ts).total_seconds()))

    @property
    def notional(self) -> Decimal:
        return self.entry_price * self.quantity

    @property
    def gross_return_pct(self) -> Decimal:
        return _pct(self.gross_pnl, self.notional)

    @property
    def net_return_pct(self) -> Decimal:
        return _pct(self.net_pnl, self.notional)

    @property
    def is_win(self) -> bool:
        return self.net_pnl > ZERO

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": str(self.quantity),
            "entry_ts": self.entry_ts.isoformat(),
            "exit_ts": self.exit_ts.isoformat(),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "fees": str(self.fees),
            "gross_pnl": str(_round(self.gross_pnl)),
            "net_pnl": str(_round(self.net_pnl)),
            "net_return_pct": str(_round(self.net_return_pct, "0.01")),
            "hold_seconds": self.hold_seconds,
            "client_order_ids": list(self.client_order_ids),
        }


@dataclass(frozen=True)
class OpenPosition:
    """A lot that has not been fully closed, with cost basis and mark."""

    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    fees: Decimal
    last_price: Decimal | None
    opened_at: datetime

    @property
    def average_price(self) -> Decimal:
        if self.quantity <= ZERO:
            return ZERO
        return self.cost_basis / self.quantity

    @property
    def unrealised_pnl(self) -> Decimal | None:
        if self.last_price is None:
            return None
        return (self.last_price * self.quantity) - self.cost_basis - self.fees

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": str(self.quantity),
            "cost_basis": str(_round(self.cost_basis)),
            "average_price": str(_round(self.average_price)),
            "fees": str(_round(self.fees)),
            "last_price": None if self.last_price is None else str(self.last_price),
            "unrealised_pnl": (
                None if self.unrealised_pnl is None else str(_round(self.unrealised_pnl))
            ),
            "opened_at": self.opened_at.isoformat(),
        }


@dataclass(frozen=True)
class TradeReport:
    """Aggregate trade statistics plus the open remainder."""

    trades: tuple[ExecutedTrade, ...]
    open_positions: tuple[OpenPosition, ...]
    ceiling: Decimal
    unbalanced: tuple[str, ...] = ()

    # -- counts ------------------------------------------------------------

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for trade in self.trades if trade.net_pnl > ZERO)

    @property
    def losses(self) -> int:
        return sum(1 for trade in self.trades if trade.net_pnl < ZERO)

    @property
    def scratches(self) -> int:
        """Trades that closed exactly flat; excluded from the win rate."""
        return self.count - self.wins - self.losses

    @property
    def win_rate_pct(self) -> Decimal:
        decided = self.wins + self.losses
        return _pct(Decimal(self.wins), Decimal(decided))

    # -- money -------------------------------------------------------------

    @property
    def gross_pnl(self) -> Decimal:
        return sum((trade.gross_pnl for trade in self.trades), ZERO)

    @property
    def net_pnl(self) -> Decimal:
        return sum((trade.net_pnl for trade in self.trades), ZERO)

    @property
    def fees_paid(self) -> Decimal:
        return sum((trade.fees for trade in self.trades), ZERO)

    @property
    def average_win(self) -> Decimal:
        wins = [trade.net_pnl for trade in self.trades if trade.net_pnl > ZERO]
        return (sum(wins, ZERO) / len(wins)) if wins else ZERO

    @property
    def average_loss(self) -> Decimal:
        losses = [trade.net_pnl for trade in self.trades if trade.net_pnl < ZERO]
        return (sum(losses, ZERO) / len(losses)) if losses else ZERO

    @property
    def best_trade(self) -> Decimal:
        return max((trade.net_pnl for trade in self.trades), default=ZERO)

    @property
    def worst_trade(self) -> Decimal:
        return min((trade.net_pnl for trade in self.trades), default=ZERO)

    @property
    def expectancy(self) -> Decimal:
        """Average net P&L per trade. The only per-trade edge worth quoting."""
        if not self.trades:
            return ZERO
        return self.net_pnl / Decimal(self.count)

    @property
    def profit_factor(self) -> Decimal | None:
        gross_win = sum((t.net_pnl for t in self.trades if t.net_pnl > ZERO), ZERO)
        gross_loss = -sum((t.net_pnl for t in self.trades if t.net_pnl < ZERO), ZERO)
        if gross_loss == ZERO:
            return None if gross_win == ZERO else Decimal("Infinity")
        return gross_win / gross_loss

    @property
    def net_return_pct(self) -> Decimal:
        return _pct(self.net_pnl, self.ceiling)

    @property
    def average_hold_seconds(self) -> int:
        if not self.trades:
            return 0
        total = sum(trade.hold_seconds for trade in self.trades)
        return total // self.count

    @property
    def open_unrealised_pnl(self) -> Decimal:
        return sum(
            (p.unrealised_pnl for p in self.open_positions if p.unrealised_pnl is not None),
            ZERO,
        )

    # -- reporting ---------------------------------------------------------

    def summary(self) -> str:
        factor = self.profit_factor
        breakdown = f"  ({self.wins}W / {self.losses}L"
        if self.scratches:
            breakdown += f" / {self.scratches} flat"
        breakdown += f", {self.win_rate_pct:.1f}% win)"
        lines = [
            "TRADE EVALUATION (from recorded fills)",
            f"  closed trades    {self.count}" + (breakdown if self.count else ""),
            f"  net P&L          {self.net_pnl:+.4f} USD"
            f"  ({self.net_return_pct:+.2f}% of {self.ceiling} ceiling)",
            f"  gross / fees     {self.gross_pnl:+.4f} / {self.fees_paid:.4f}",
            f"  expectancy       {self.expectancy:+.4f} per trade",
            "  profit factor    "
            + ("n/a (no losses yet)" if factor is None else ("inf" if factor.is_infinite() else f"{factor:.2f}")),
            f"  best / worst     {self.best_trade:+.4f} / {self.worst_trade:+.4f}",
            f"  average hold     {self.average_hold_seconds / 3600:.1f}h",
        ]
        if self.open_positions:
            lines.append("  open positions")
            for position in self.open_positions:
                mark = "unknown" if position.last_price is None else f"{position.last_price}"
                lines.append(
                    f"    {position.symbol:<10}{position.quantity:>14} @ "
                    f"{position.average_price:.4f}  mark {mark}"
                    f"  unrealised {position.unrealised_pnl if position.unrealised_pnl is not None else 'n/a'}"
                )
        if self.unbalanced:
            lines.append(f"  UNBALANCED       {len(self.unbalanced)} fill(s) could not be matched:")
            for detail in self.unbalanced[:5]:
                lines.append(f"    {detail}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        factor = self.profit_factor
        return {
            "count": self.count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": str(_round(self.win_rate_pct, "0.01")),
            "gross_pnl": str(_round(self.gross_pnl)),
            "net_pnl": str(_round(self.net_pnl)),
            "fees_paid": str(_round(self.fees_paid)),
            "expectancy": str(_round(self.expectancy)),
            "profit_factor": None if factor is None else str(_round(factor, "0.01")),
            "net_return_pct": str(_round(self.net_return_pct, "0.01")),
            "average_hold_seconds": self.average_hold_seconds,
            "open_unrealised_pnl": str(_round(self.open_unrealised_pnl)),
            "trades": [trade.as_dict() for trade in self.trades],
            "open_positions": [position.as_dict() for position in self.open_positions],
            "unbalanced": list(self.unbalanced),
        }


def reconstruct_symbol(
    symbol: str, fills: Sequence[FillRecord], *, last_price: Decimal | None = None
) -> tuple[list[ExecutedTrade], OpenPosition | None, list[str]]:
    """Rebuild round trips for one symbol using average-cost accounting."""
    open_quantity = ZERO
    open_cost = ZERO
    open_fees = ZERO
    opened_at: datetime | None = None
    open_ids: list[str] = []

    trades: list[ExecutedTrade] = []
    unbalanced: list[str] = []

    for fill in sorted(fills, key=lambda item: item.ts):
        quantity = fill.quantity
        if quantity <= ZERO:
            continue
        if fill.side is Side.BUY:
            # A crypto buy pays its fee out of the asset it is credited with, so
            # the lot is the net quantity; ``fee_in_quote`` values the fee in the
            # quote currency so that quantities, costs and fees never mix units.
            held = fill.net_quantity
            if held <= ZERO:
                continue
            open_quantity += held
            open_cost += held * fill.price
            open_fees += fill.fee_in_quote()
            opened_at = opened_at or fill.ts
            open_ids.append(fill.client_order_id)
            continue

        if open_quantity <= EPSILON:
            unbalanced.append(
                f"{symbol}: sell {quantity} at {fill.ts.isoformat()} with no open position"
            )
            continue

        matched = min(quantity, open_quantity)
        average = open_cost / open_quantity
        basis = average * matched
        # Fees accumulated on the open lot are released pro rata, so a partially
        # closed position cannot hide fees in the remainder.
        entry_fees = open_fees * (matched / open_quantity)
        exit_fees = fill.fee_in_quote() * (matched / quantity)
        proceeds = matched * fill.price

        trades.append(
            ExecutedTrade(
                symbol=symbol,
                quantity=matched,
                entry_ts=opened_at or fill.ts,
                exit_ts=fill.ts,
                entry_price=average,
                exit_price=fill.price,
                fees=entry_fees + exit_fees,
                gross_pnl=proceeds - basis,
                net_pnl=proceeds - basis - entry_fees - exit_fees,
                client_order_ids=tuple(open_ids),
            )
        )

        open_quantity -= matched
        open_cost -= basis
        open_fees -= entry_fees
        if open_quantity <= EPSILON:
            open_quantity = ZERO
            open_cost = ZERO
            open_fees = ZERO
            opened_at = None
            open_ids = []
        if quantity > matched:
            unbalanced.append(
                f"{symbol}: sell {quantity} at {fill.ts.isoformat()} matched only {matched}"
            )

    position: OpenPosition | None = None
    if open_quantity > EPSILON:
        position = OpenPosition(
            symbol=symbol,
            quantity=open_quantity,
            cost_basis=open_cost,
            fees=open_fees,
            last_price=last_price,
            opened_at=opened_at or fills[-1].ts,
        )
    return trades, position, unbalanced


def evaluate_trades(
    journal: Journal,
    *,
    symbols: Sequence[str] | None = None,
    ceiling: Decimal = Decimal("100"),
    since: datetime | None = None,
) -> TradeReport:
    """Evaluate every symbol that has fills (or the requested subset)."""
    wanted = list(symbols) if symbols else sorted({fill.symbol for fill in journal.fills(limit=100_000)})

    trades: list[ExecutedTrade] = []
    positions: list[OpenPosition] = []
    unbalanced: list[str] = []

    for symbol in wanted:
        fills = journal.fills_for_symbol(symbol, since=since)
        if not fills:
            continue
        last_price = journal.latest_market_price(symbol)
        if last_price is None:
            last_price = fills[-1].price
        symbol_trades, position, problems = reconstruct_symbol(
            symbol, fills, last_price=last_price
        )
        trades.extend(symbol_trades)
        if position is not None:
            positions.append(position)
        unbalanced.extend(problems)

    trades.sort(key=lambda trade: trade.exit_ts)
    return TradeReport(
        trades=tuple(trades),
        open_positions=tuple(positions),
        ceiling=to_decimal(ceiling, "ceiling"),
        unbalanced=tuple(unbalanced),
    )


# ---------------------------------------------------------------------------
# Execution quality
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionQuality:
    """Filled price measured against the price the intent was built on."""

    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    reference_price: Decimal
    fill_price: Decimal
    filled_quantity: Decimal
    fees: Decimal

    @property
    def slippage_bps(self) -> Decimal:
        """Signed adverse slippage: positive means worse than the reference."""
        if self.reference_price <= ZERO:
            return ZERO
        direction = Decimal("1") if self.side is Side.BUY else Decimal("-1")
        return ((self.fill_price - self.reference_price) / self.reference_price) * BPS * direction

    @property
    def notional(self) -> Decimal:
        return self.fill_price * self.filled_quantity

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "reference_price": str(self.reference_price),
            "fill_price": str(self.fill_price),
            "filled_quantity": str(self.filled_quantity),
            "fees": str(_round(self.fees)),
            "slippage_bps": str(_round(self.slippage_bps, "0.01")),
        }


@dataclass(frozen=True)
class ExecutionReport:
    orders: tuple[ExecutionQuality, ...]
    skipped: tuple[str, ...] = field(default_factory=tuple)

    @property
    def count(self) -> int:
        return len(self.orders)

    @property
    def average_slippage_bps(self) -> Decimal:
        if not self.orders:
            return ZERO
        return sum((order.slippage_bps for order in self.orders), ZERO) / Decimal(self.count)

    @property
    def worst_slippage_bps(self) -> Decimal:
        return max((order.slippage_bps for order in self.orders), default=ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((order.fees for order in self.orders), ZERO)

    def summary(self) -> str:
        lines = [
            "EXECUTION QUALITY (fill vs intent reference)",
            f"  filled orders    {self.count}",
            f"  average slippage {self.average_slippage_bps:+.2f} bps",
            f"  worst slippage   {self.worst_slippage_bps:+.2f} bps",
            f"  fees             {self.fees:.4f}",
        ]
        for order in self.orders[:10]:
            lines.append(
                f"    {order.symbol:<10}{order.side.value:<5}"
                f"ref {order.reference_price:>12.4f} fill {order.fill_price:>12.4f}"
                f" {order.slippage_bps:+7.2f} bps"
            )
        if self.skipped:
            lines.append(f"  skipped          {len(self.skipped)} order(s) without a reference")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "average_slippage_bps": str(_round(self.average_slippage_bps, "0.01")),
            "worst_slippage_bps": str(_round(self.worst_slippage_bps, "0.01")),
            "fees": str(_round(self.fees)),
            "orders": [order.as_dict() for order in self.orders],
            "skipped": list(self.skipped),
        }


def reference_price(intent_notional: Decimal, intent_quantity: Decimal, limit_price: Decimal | None) -> Decimal | None:
    """The price the intent was priced at.

    A limit order states it. A market order does not, but the intent's notional
    and quantity imply the price the sizer used, which is the only honest
    baseline available offline.
    """
    if limit_price is not None and limit_price > ZERO:
        return limit_price
    if intent_quantity > ZERO and intent_notional > ZERO:
        return intent_notional / intent_quantity
    return None


def evaluate_execution(journal: Journal, *, limit: int = 500) -> ExecutionReport:
    """Measure slippage and fees for every locally filled order."""
    quality: list[ExecutionQuality] = []
    skipped: list[str] = []

    for order in journal.list_orders(limit=limit):
        if order.filled_quantity <= ZERO or order.avg_fill_price is None:
            continue
        intent = journal.get_intent(order.client_order_id)
        if intent is None:
            skipped.append(f"{order.client_order_id}: no recorded intent")
            continue
        reference = reference_price(intent.notional, intent.quantity, intent.limit_price)
        if reference is None:
            skipped.append(f"{order.client_order_id}: intent carries no usable price")
            continue
        fees = sum((fill.fee for fill in journal.fills_for(order.client_order_id)), ZERO)
        quality.append(
            ExecutionQuality(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                order_type=intent.order_type,
                reference_price=reference,
                fill_price=order.avg_fill_price,
                filled_quantity=order.filled_quantity,
                fees=fees,
            )
        )

    quality.sort(key=lambda item: item.client_order_id)
    return ExecutionReport(orders=tuple(quality), skipped=tuple(skipped))


__all__ = [
    "ExecutedTrade",
    "ExecutionQuality",
    "ExecutionReport",
    "OpenPosition",
    "TradeReport",
    "evaluate_execution",
    "evaluate_trades",
    "reference_price",
    "reconstruct_symbol",
]
