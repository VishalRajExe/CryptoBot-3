"""Deterministic client order identifiers.

The ``client_order_id`` is the spine of duplicate protection:

* It is derived purely from ``(symbol, timeframe, bar, strategy, side)``, so the
  same decision can only ever produce the same identifier. Retrying a submit
  reuses the identifier, which makes the retry idempotent *at the broker*.
* Alpaca rejects a reused ``client_order_id``, giving us a second, independent
  guarantee beyond our own database constraint.
* It fits in the documented 48-character limit with room to spare.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from cryptobot.core.contracts import (
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    normalize_symbol,
    to_decimal,
    to_utc,
)

PREFIX = "cb-"
_MAX_LEN = 48


def build_client_order_id(
    *,
    symbol: str,
    timeframe: str,
    bar_ts: datetime,
    strategy_name: str,
    side: Side,
) -> str:
    payload = "|".join(
        (
            normalize_symbol(symbol),
            str(timeframe),
            to_utc(bar_ts, "bar_ts").isoformat(),
            strategy_name,
            Side(side).value,
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    identifier = f"{PREFIX}{digest[:32]}"
    assert len(identifier) <= _MAX_LEN
    return identifier


def build_intent(
    *,
    symbol: str,
    timeframe: str,
    bar_ts: datetime,
    strategy_name: str,
    strategy_version: str,
    side: Side,
    order_type: OrderType,
    quantity: Decimal,
    notional: Decimal,
    time_in_force: TimeInForce,
    now: datetime,
    limit_price: Decimal | None = None,
    decision_id: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=build_client_order_id(
            symbol=symbol,
            timeframe=timeframe,
            bar_ts=bar_ts,
            strategy_name=strategy_name,
            side=side,
        ),
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=to_decimal(quantity, "quantity"),
        notional=to_decimal(notional, "notional"),
        time_in_force=time_in_force,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        bar_ts=to_utc(bar_ts, "bar_ts"),
        ts=to_utc(now, "now"),
        limit_price=limit_price,
        decision_id=decision_id,
    )


def expected_cost(notional: Decimal, fee_fraction: Decimal, slippage_fraction: Decimal) -> Decimal:
    return to_decimal(notional, "notional") * (
        to_decimal(fee_fraction, "fee") + to_decimal(slippage_fraction, "slippage")
    )
