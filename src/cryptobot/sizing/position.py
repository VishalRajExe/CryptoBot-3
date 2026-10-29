"""Position sizing.

Sizing is a pure function of explicit inputs: no hidden state, no clock, no
broker. That makes it trivially testable and keeps the $100 ceiling honest.

All arithmetic is ``Decimal`` and every result is floored to the asset's legal
quantity increment *before* it becomes an order, so we never send a size the
broker will reject for precision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cryptobot.core.contracts import InstrumentSpec, to_decimal
from cryptobot.core.errors import ContractError


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: Decimal
    notional: Decimal
    budget: Decimal
    ok: bool
    reason: str

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.quantity} (~{self.notional} quote) [{self.reason}]"


def entry_budget(
    *,
    available: Decimal,
    ceiling: Decimal,
    max_position_pct: Decimal,
    per_trade_risk_pct: Decimal,
) -> Decimal:
    """The tightest of: available allocation, position cap, per-trade cap."""
    available = to_decimal(available, "available")
    ceiling = to_decimal(ceiling, "ceiling")
    if ceiling <= 0:
        raise ContractError("ceiling must be positive")
    caps = [
        max(available, Decimal("0")),
        ceiling * to_decimal(max_position_pct, "max_position_pct") / Decimal(100),
        ceiling * to_decimal(per_trade_risk_pct, "per_trade_risk_pct") / Decimal(100),
    ]
    return min(caps)


def size_entry(
    *,
    available: Decimal,
    price: Decimal,
    ceiling: Decimal,
    max_position_pct: Decimal,
    per_trade_risk_pct: Decimal,
    instrument: InstrumentSpec,
    fee_reserve_fraction: Decimal = Decimal("0"),
    slippage_reserve_fraction: Decimal = Decimal("0"),
) -> SizingResult:
    """Convert a capital budget into an order quantity.

    The fee and slippage reserves are deducted from the budget *before* sizing so
    that the resulting order plus its expected costs still fits inside the
    allocation.
    """
    price = to_decimal(price, "price")
    if price <= 0:
        raise ContractError("price must be positive")

    budget = entry_budget(
        available=available,
        ceiling=ceiling,
        max_position_pct=max_position_pct,
        per_trade_risk_pct=per_trade_risk_pct,
    )
    total_reserve = to_decimal(fee_reserve_fraction, "fee_reserve") + to_decimal(
        slippage_reserve_fraction, "slippage_reserve"
    )
    spendable = budget * (Decimal(1) - total_reserve)
    if spendable <= 0:
        return SizingResult(Decimal("0"), Decimal("0"), budget, False, "no spendable budget")

    quantity = instrument.quantize_qty(spendable / price)
    notional = quantity * price

    if quantity <= 0:
        return SizingResult(
            quantity, notional, budget, False, f"budget {budget} is below one increment at {price}"
        )
    if quantity < instrument.min_order_size:
        return SizingResult(
            quantity,
            notional,
            budget,
            False,
            f"{quantity} is below min_order_size {instrument.min_order_size}",
        )
    problems = instrument.qty_issues(quantity)
    if problems:
        return SizingResult(quantity, notional, budget, False, "; ".join(problems))
    if notional > budget:
        return SizingResult(
            quantity, notional, budget, False, f"notional {notional} exceeds budget {budget}"
        )
    return SizingResult(quantity, notional, budget, True, "within budget")


def size_exit(*, position_quantity: Decimal, instrument: InstrumentSpec) -> SizingResult:
    """Close the whole position, respecting the quantity increment."""
    position_quantity = to_decimal(position_quantity, "position_quantity")
    if position_quantity <= 0:
        return SizingResult(Decimal("0"), Decimal("0"), Decimal("0"), False, "no position")
    quantity = instrument.quantize_qty(position_quantity)
    if quantity <= 0:
        return SizingResult(
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            False,
            f"position {position_quantity} is dust below increment {instrument.min_trade_increment}",
        )
    problems = instrument.qty_issues(quantity)
    return SizingResult(
        quantity,
        Decimal("0"),
        Decimal("0"),
        not problems,
        "close position" if not problems else "; ".join(problems),
    )


def size_by_risk(
    *,
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    instrument: InstrumentSpec,
) -> SizingResult:
    """Stop-distance sizing.

    Reserved for when stop orders are actually submitted; kept here because it is
    the canonical formula and unit-testing it now avoids re-deriving it later.
    """
    entry_price = to_decimal(entry_price, "entry_price")
    stop_price = to_decimal(stop_price, "stop_price")
    risk_amount = to_decimal(equity, "equity") * to_decimal(risk_pct, "risk_pct") / Decimal(100)
    per_unit_risk = entry_price - stop_price
    if per_unit_risk <= 0:
        return SizingResult(Decimal("0"), Decimal("0"), risk_amount, False, "stop must be below entry")
    quantity = instrument.quantize_qty(risk_amount / per_unit_risk)
    notional = quantity * entry_price
    if quantity < instrument.min_order_size:
        return SizingResult(
            quantity, notional, risk_amount, False, "risk-based size below min_order_size"
        )
    return SizingResult(quantity, notional, risk_amount, True, "risk based")
