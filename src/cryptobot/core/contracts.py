"""Typed contracts exchanged between modules.

Design rules:

* Every monetary or quantity value is a :class:`~decimal.Decimal`. Floats are
  accepted on input but immediately converted via ``Decimal(str(x))`` so that no
  binary floating point error ever reaches persisted state or a hash.
* Every timestamp is timezone-aware UTC. Naive datetimes are rejected outright
  rather than silently assumed to be local time.
* Construction validates. If an object exists, it is well formed.
* Serialisation is explicit and lossless (Decimals become strings).

These types are the *only* things that cross module boundaries; see
docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import Enum
from typing import Any, ClassVar, Mapping

from cryptobot.core import jsonutil, timeframes
from cryptobot.core.errors import ContractError

UTC = timezone.utc

_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH")


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def to_decimal(value: Any, name: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise ContractError(f"{name}: booleans are not numeric")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ContractError(f"{name}: non-finite float {value!r}")
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ContractError(f"{name}: not a decimal: {value!r}") from exc
    else:
        raise ContractError(f"{name}: unsupported numeric type {type(value).__name__}")
    if not result.is_finite():
        raise ContractError(f"{name}: non-finite decimal")
    return result


def to_utc(value: Any, name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime):
        raise ContractError(f"{name}: expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ContractError(f"{name}: naive datetime rejected; supply UTC")
    return value.astimezone(UTC)


def normalize_symbol(value: Any) -> str:
    """``btc/usd`` -> ``BTC/USD``; legacy ``BTCUSD`` -> ``BTC/USD``."""
    if not isinstance(value, str) or not value.strip():
        raise ContractError("symbol must be a non-empty string")
    # Whitespace anywhere is stripped rather than tolerated: "BTC / USD" must not
    # become a symbol that silently fails an allowlist comparison.
    text = "".join(value.split()).upper().replace("-", "/").replace("_", "/")
    if "/" in text:
        base, _, quote = text.partition("/")
        if not base or not quote or "/" in quote:
            raise ContractError(f"symbol: malformed pair {value!r}")
        return f"{base}/{quote}"
    for quote in _QUOTES:
        if text.endswith(quote) and len(text) > len(quote):
            return f"{text[: -len(quote)]}/{quote}"
    raise ContractError(f"symbol: cannot infer quote currency from {value!r}")


def row_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise ``sqlite3.Row`` and dict rows into one plain mapping.

    ``sqlite3.Row`` supports indexing and ``keys()`` but has no ``.get``; every
    ``from_row`` therefore goes through this helper instead of assuming a dict.
    """
    return {key: row[key] for key in row.keys()}


def base_currency(symbol: str) -> str:
    return normalize_symbol(symbol).split("/")[0]


def quote_currency(symbol: str) -> str:
    return normalize_symbol(symbol).split("/")[1]


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Action(str, Enum):
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"
    HOLD = "hold"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    """Crypto-valid time-in-force values only.

    Verified in docs/API_CAPABILITIES.md: Alpaca accepts ``gtc`` and ``ioc`` for
    crypto. ``day`` is deliberately absent - it is a stock-only value and using
    it would produce broker rejections.
    """

    GTC = "gtc"
    IOC = "ioc"


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "pending_submit"
    UNCERTAIN = "uncertain"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REPLACED = "replaced"
    REJECTED = "rejected"
    EXPIRED = "expired"
    LOST = "lost"
    UNKNOWN = "unknown"

    @property
    def is_open(self) -> bool:
        return self in _OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def needs_broker_confirmation(self) -> bool:
        """States where the local record may be wrong and the broker is the truth."""
        return self in _OPEN_STATUSES or self is OrderStatus.UNKNOWN


_OPEN_STATUSES = frozenset(
    {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.UNCERTAIN,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.UNKNOWN,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
        OrderStatus.LOST,
    }
)

#: Broker status string -> internal status. Anything absent maps to UNKNOWN,
#: which the reconciler treats as "needs a human", never as success.
BROKER_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.ACCEPTED,
    "accepted": OrderStatus.ACCEPTED,
    "pending_new": OrderStatus.ACCEPTED,
    "accepted_for_bidding": OrderStatus.ACCEPTED,
    "pending_cancel": OrderStatus.ACCEPTED,
    "pending_replace": OrderStatus.ACCEPTED,
    "stopped": OrderStatus.ACCEPTED,
    "suspended": OrderStatus.ACCEPTED,
    "held": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "done_for_day": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "replaced": OrderStatus.REPLACED,
    "rejected": OrderStatus.REJECTED,
    "calculated": OrderStatus.FILLED,
}


def map_broker_status(raw: str | None) -> OrderStatus:
    if raw is None:
        return OrderStatus.UNKNOWN
    return BROKER_STATUS_MAP.get(str(raw).strip().lower(), OrderStatus.UNKNOWN)


# ---------------------------------------------------------------------------
# market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed OHLCV candle. ``ts`` is the bar's *open* time."""

    symbol: str
    timeframe: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timeframe", timeframes.canonical(self.timeframe))
        object.__setattr__(self, "ts", to_utc(self.ts, "bar.ts"))
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, to_decimal(getattr(self, name), f"bar.{name}"))

        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ContractError(f"{self.symbol}@{self.ts.isoformat()}: non-positive price")
        if self.volume < 0:
            raise ContractError(f"{self.symbol}@{self.ts.isoformat()}: negative volume")
        if self.high < self.low:
            raise ContractError(f"{self.symbol}@{self.ts.isoformat()}: high < low")
        if self.high < max(self.open, self.close):
            raise ContractError(f"{self.symbol}@{self.ts.isoformat()}: high below body")
        if self.low > min(self.open, self.close):
            raise ContractError(f"{self.symbol}@{self.ts.isoformat()}: low above body")
        if not timeframes.is_aligned(self.ts, self.timeframe):
            raise ContractError(
                f"{self.symbol}@{self.ts.isoformat()}: not aligned to {self.timeframe}"
            )

    @property
    def closes_at(self) -> datetime:
        return timeframes.bucket_end(self.ts, self.timeframe)

    def is_closed(self, now: datetime) -> bool:
        return to_utc(now, "now") >= self.closes_at

    def age_seconds(self, now: datetime) -> float:
        return (to_utc(now, "now") - self.closes_at).total_seconds()

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "ts": self.ts.isoformat(),
            "open": format(self.open, "f"),
            "high": format(self.high, "f"),
            "low": format(self.low, "f"),
            "close": format(self.close, "f"),
            "volume": format(self.volume, "f"),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Bar":
        return cls(
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            ts=datetime.fromisoformat(row["ts"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
        )


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Per-asset tradability rules, taken from ``GET /v2/assets``.

    Sizes are never hardcoded: Alpaca documents that precision varies per coin
    and "could change in the future".
    """

    symbol: str
    min_order_size: Decimal
    min_trade_increment: Decimal
    price_increment: Decimal
    tradable: bool = True
    fractionable: bool = True
    shortable: bool = False
    marginable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        for name in ("min_order_size", "min_trade_increment", "price_increment"):
            value = to_decimal(getattr(self, name), f"instrument.{name}")
            if value <= 0:
                raise ContractError(f"instrument.{name} must be > 0")
            object.__setattr__(self, name, value)

    @classmethod
    def from_asset(cls, asset: Mapping[str, Any]) -> "InstrumentSpec":
        def _get(*names: str, default: str = "0") -> Any:
            for name in names:
                if name in asset and asset[name] not in (None, ""):
                    return asset[name]
            return default

        symbol = asset.get("symbol")
        if not symbol:
            raise ContractError("asset payload has no symbol")
        return cls(
            symbol=symbol,
            min_order_size=_get("min_order_size", default="0.00000001"),
            min_trade_increment=_get("min_trade_increment", default="0.00000001"),
            price_increment=_get("price_increment", default="0.00000001"),
            tradable=bool(asset.get("tradable", True)),
            fractionable=bool(asset.get("fractionable", True)),
            shortable=bool(asset.get("shortable", False)),
            marginable=bool(asset.get("marginable", False)),
        )

    def quantize_qty(self, qty: Decimal) -> Decimal:
        """Floor ``qty`` to a legal increment. Never rounds up."""
        steps = (qty / self.min_trade_increment).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.min_trade_increment).normalize()

    def qty_issues(self, qty: Decimal) -> list[str]:
        problems: list[str] = []
        if qty <= 0:
            problems.append("quantity must be positive")
            return problems
        if qty < self.min_order_size:
            problems.append(f"quantity {qty} below min_order_size {self.min_order_size}")
        if self.quantize_qty(qty) != qty.normalize():
            problems.append(
                f"quantity {qty} is not a multiple of min_trade_increment "
                f"{self.min_trade_increment}"
            )
        return problems

    def min_notional(self, price: Decimal) -> Decimal:
        return self.min_order_size * price


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategySignal:
    symbol: str
    timeframe: str
    bar_ts: datetime
    action: Action
    reason: str
    strategy_name: str
    strategy_version: str
    indicators: Mapping[str, Decimal] = field(default_factory=dict)
    ts: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timeframe", timeframes.canonical(self.timeframe))
        object.__setattr__(self, "bar_ts", to_utc(self.bar_ts, "signal.bar_ts"))
        object.__setattr__(self, "action", Action(self.action))
        object.__setattr__(
            self, "ts", to_utc(self.ts, "signal.ts") if self.ts else self.bar_ts
        )
        object.__setattr__(
            self,
            "indicators",
            {str(k): to_decimal(v, f"indicator.{k}") for k, v in dict(self.indicators).items()},
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_ts": self.bar_ts.isoformat(),
            "action": self.action.value,
            "reason": self.reason,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "indicators_json": jsonutil.dumps(dict(self.indicators)),
            "ts": self.ts.isoformat() if self.ts else None,
        }


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """A signal plus the identity of the exact inputs that produced it."""

    decision_id: str
    signal: StrategySignal
    data_fingerprint: str
    created_at: datetime

    @property
    def is_actionable(self) -> bool:
        return self.signal.action is not Action.HOLD

    @staticmethod
    def fingerprint(bars: list[Bar]) -> str:
        """SHA-256 over the exact bar data used, for later reconstruction."""
        hasher = hashlib.sha256()
        for bar in bars:
            hasher.update(jsonutil.dumps(bar.to_row()).encode("utf-8"))
        return hasher.hexdigest()


# ---------------------------------------------------------------------------
# orders and fills
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A request to trade, before any risk verdict and before submission."""

    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    notional: Decimal
    time_in_force: TimeInForce
    strategy_name: str
    strategy_version: str
    bar_ts: datetime
    ts: datetime
    limit_price: Decimal | None = None
    decision_id: str | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id or len(self.client_order_id) > 48:
            raise ContractError("client_order_id must be 1..48 characters")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "time_in_force", TimeInForce(self.time_in_force))
        object.__setattr__(self, "quantity", to_decimal(self.quantity, "quantity"))
        object.__setattr__(self, "notional", to_decimal(self.notional, "notional"))
        object.__setattr__(self, "bar_ts", to_utc(self.bar_ts, "bar_ts"))
        object.__setattr__(self, "ts", to_utc(self.ts, "ts"))
        if self.limit_price is not None:
            object.__setattr__(
                self, "limit_price", to_decimal(self.limit_price, "limit_price")
            )
        if self.quantity <= 0:
            raise ContractError("quantity must be positive")
        if self.notional <= 0:
            raise ContractError("notional must be positive")
        if self.order_type is not OrderType.MARKET and self.limit_price is None:
            raise ContractError(f"{self.order_type.value} orders require limit_price")

    @property
    def fingerprint(self) -> str:
        """Identity for duplicate-intent detection (ignores timestamp/precision)."""
        payload = {
            "symbol": self.symbol,
            "side": self.side.value,
            "strategy": self.strategy_name,
            "bar_ts": self.bar_ts.isoformat(),
        }
        return hashlib.sha256(jsonutil.dumps(payload).encode("utf-8")).hexdigest()[:32]

    def to_row(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "ts": self.ts.isoformat(),
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "qty": format(self.quantity, "f"),
            "limit_price": format(self.limit_price, "f") if self.limit_price is not None else None,
            "notional": format(self.notional, "f"),
            "time_in_force": self.time_in_force.value,
            "strategy": self.strategy_name,
            "strategy_version": self.strategy_version,
            "bar_ts": self.bar_ts.isoformat(),
            "decision_id": self.decision_id,
            "intent_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "OrderIntent":
        row = row_mapping(row)
        return cls(
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=row["side"],
            order_type=row["order_type"],
            quantity=row["qty"],
            notional=row["notional"],
            time_in_force=row["time_in_force"],
            strategy_name=row["strategy"],
            strategy_version=row["strategy_version"],
            bar_ts=datetime.fromisoformat(row["bar_ts"]),
            ts=datetime.fromisoformat(row["ts"]),
            limit_price=row.get("limit_price"),
            decision_id=row.get("decision_id"),
        )


@dataclass(frozen=True, slots=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    approved: bool
    checks: tuple[RiskCheck, ...]
    reasons: tuple[str, ...]
    available_before: Decimal
    ts: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_before", to_decimal(self.available_before, "available"))
        object.__setattr__(self, "ts", to_utc(self.ts, "verdict.ts"))
        if self.approved and self.reasons:
            raise ContractError("approved verdict cannot carry rejection reasons")

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if not c.passed)

    def summary(self) -> str:
        if self.approved:
            return "approved"
        return "rejected: " + "; ".join(self.reasons)

    def to_row(self) -> dict[str, Any]:
        return {
            "approved": int(self.approved),
            "reasons_json": jsonutil.dumps(list(self.reasons)),
            "checks_json": jsonutil.dumps(
                [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks]
            ),
            "available_before": format(self.available_before, "f"),
            "ts": self.ts.isoformat(),
        }

    @classmethod
    def rejected(cls, checks: list[RiskCheck], available: Decimal, ts: datetime) -> "RiskVerdict":
        reasons = tuple(f"{c.name}: {c.detail}" for c in checks if not c.passed)
        return cls(False, tuple(checks), reasons, available, ts)


@dataclass(frozen=True, slots=True)
class OrderRecord:
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    status: OrderStatus
    updated_at: datetime
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    submitted_at: datetime | None = None
    reason: str | None = None
    raw_status: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "status", OrderStatus(self.status))
        object.__setattr__(self, "quantity", to_decimal(self.quantity, "quantity"))
        object.__setattr__(
            self, "filled_quantity", to_decimal(self.filled_quantity, "filled_quantity")
        )
        object.__setattr__(self, "updated_at", to_utc(self.updated_at, "updated_at"))
        if self.submitted_at is not None:
            object.__setattr__(self, "submitted_at", to_utc(self.submitted_at, "submitted_at"))
        if self.avg_fill_price is not None:
            object.__setattr__(
                self, "avg_fill_price", to_decimal(self.avg_fill_price, "avg_fill_price")
            )
        if self.filled_quantity > self.quantity:
            raise ContractError("filled quantity exceeds order quantity")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    def to_row(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "status": self.status.value,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": format(self.quantity, "f"),
            "filled_qty": format(self.filled_quantity, "f"),
            "avg_fill_price": (
                format(self.avg_fill_price, "f") if self.avg_fill_price is not None else None
            ),
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "updated_at": self.updated_at.isoformat(),
            "reason": self.reason,
            "raw_status": self.raw_status,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "OrderRecord":
        row = row_mapping(row)
        return cls(
            client_order_id=row["client_order_id"],
            broker_order_id=row.get("broker_order_id"),
            status=row["status"],
            symbol=row["symbol"],
            side=row["side"],
            quantity=row["qty"],
            filled_quantity=row.get("filled_qty") or "0",
            avg_fill_price=row.get("avg_fill_price"),
            submitted_at=(
                datetime.fromisoformat(row["submitted_at"]) if row.get("submitted_at") else None
            ),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            reason=row.get("reason"),
            raw_status=row.get("raw_status"),
        )


@dataclass(frozen=True, slots=True)
class FillRecord:
    fill_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    ts: datetime
    fee: Decimal = Decimal("0")
    fee_currency: str = "USD"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "quantity", to_decimal(self.quantity, "quantity"))
        object.__setattr__(self, "price", to_decimal(self.price, "price"))
        object.__setattr__(self, "fee", to_decimal(self.fee, "fee"))
        object.__setattr__(self, "ts", to_utc(self.ts, "fill.ts"))
        if self.quantity <= 0 or self.price <= 0:
            raise ContractError("fill quantity and price must be positive")

    @property
    def gross_notional(self) -> Decimal:
        return self.quantity * self.price

    def fee_in_quote(self) -> Decimal:
        """Fees are charged in the received asset; convert to quote for accounting."""
        return self.fee if self.fee_currency == quote_currency(self.symbol) else self.fee * self.price

    @property
    def natural_key(self) -> str:
        return f"{self.broker_order_id}:{self.fill_id}"

    def to_row(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "broker_order_id": self.broker_order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": format(self.quantity, "f"),
            "price": format(self.price, "f"),
            "fee": format(self.fee, "f"),
            "fee_currency": self.fee_currency,
            "ts": self.ts.isoformat(),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "FillRecord":
        row = row_mapping(row)
        return cls(
            fill_id=row["fill_id"],
            broker_order_id=row["broker_order_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=row["side"],
            quantity=row["qty"],
            price=row["price"],
            fee=row.get("fee") or "0",
            fee_currency=row.get("fee_currency") or "USD",
            ts=datetime.fromisoformat(row["ts"]),
        )


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    ts: datetime
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    last_price: Decimal
    source: str = "broker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", to_utc(self.ts, "snapshot.ts"))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        for name in ("quantity", "avg_entry_price", "last_price"):
            object.__setattr__(self, name, to_decimal(getattr(self, name), name))

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.last_price

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.avg_entry_price

    @property
    def unrealized_pnl(self) -> Decimal:
        return self.market_value - self.cost_basis

    def to_row(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "symbol": self.symbol,
            "qty": format(self.quantity, "f"),
            "avg_entry_price": format(self.avg_entry_price, "f"),
            "last_price": format(self.last_price, "f"),
            "market_value": format(self.market_value, "f"),
            "unrealized_pnl": format(self.unrealized_pnl, "f"),
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# allocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllocationState:
    """The application's own capital accounting.

    ``ceiling`` is fixed for the life of the configuration. Realised profit is
    reported in ``realized_pnl`` but never added back to ``ceiling``, so a
    profitable run cannot silently scale the bot's exposure.
    """

    ceiling: Decimal
    committed: Decimal
    reserved: Decimal
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")

    EPSILON: ClassVar[Decimal] = Decimal("0.00000001")

    def __post_init__(self) -> None:
        for name in ("ceiling", "committed", "reserved", "realized_pnl", "unrealized_pnl"):
            object.__setattr__(self, name, to_decimal(getattr(self, name), f"allocation.{name}"))
        if self.ceiling <= 0:
            raise ContractError("allocation ceiling must be positive")
        if self.committed < 0:
            raise ContractError("committed capital cannot be negative")
        if self.reserved < 0:
            raise ContractError("reserved capital cannot be negative")

    @property
    def allocated(self) -> Decimal:
        return self.committed + self.reserved

    @property
    def available(self) -> Decimal:
        remaining = self.ceiling - self.allocated
        return remaining if remaining > 0 else Decimal("0")

    @property
    def deployment_pct(self) -> Decimal:
        return (self.allocated / self.ceiling * 100).normalize()

    @property
    def invariant_holds(self) -> bool:
        return self.allocated <= self.ceiling + self.EPSILON

    def with_unrealized(self, unrealized: Decimal) -> "AllocationState":
        return AllocationState(
            ceiling=self.ceiling,
            committed=self.committed,
            reserved=self.reserved,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=to_decimal(unrealized, "unrealized_pnl"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "ceiling": format(self.ceiling, "f"),
            "committed": format(self.committed, "f"),
            "reserved": format(self.reserved, "f"),
            "available": format(self.available, "f"),
            "realized_pnl": format(self.realized_pnl, "f"),
            "unrealized_pnl": format(self.unrealized_pnl, "f"),
            "deployment_pct": format(self.deployment_pct, "f"),
        }
