"""Alpaca paper-trading adapter - the single place orders can reach a broker.

Paper-only by construction, in four independent layers:

1. The configuration loader refuses any ``trading_base_url`` outside a
   paper-only allowlist, so a live URL cannot even be loaded.
2. :func:`assert_paper_base_url` re-checks at adapter construction.
3. The client is always constructed with ``paper=True``; there is no parameter,
   flag, or fallback that can produce a live client.
4. :meth:`AlpacaPaperBroker.assert_paper_only_client` inspects the constructed
   client's base URL when available and raises if it is not a paper host.

Order payload construction (:func:`build_order_payload`) is a pure function and
is unit-tested; only the ``alpaca-py`` request marshalling is exercised solely
against the real SDK (marked ``@pytest.mark.broker`` and skipped by default).

Crypto specifics encoded here, all verified in docs/API_CAPABILITIES.md:
``qty`` is sent as a decimal string, ``time_in_force`` is ``gtc`` or ``ioc``
(never ``day``), and ``client_order_id`` is always deterministic.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from cryptobot.config.settings import PAPER_TRADING_HOSTS, AlpacaConfig
from cryptobot.core.contracts import (
    OrderIntent,
    OrderType,
    Side,
    map_broker_status,
    normalize_symbol,
    to_decimal,
)
from cryptobot.core.errors import (
    AmbiguousSubmission,
    DefiniteRejection,
    ExecutionError,
    MissingDependency,
    PaperOnlyViolation,
)
from cryptobot.execution.base import BrokerAccount, BrokerOrder, BrokerPosition

UTC = timezone.utc
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URLS = frozenset({"https://api.alpaca.markets", "https://api.alpaca.markets/v2"})

INSTALL_HINT = (
    "alpaca-py is required for broker order submission. "
    "Install it with: pip install -e \".[alpaca]\"  (or: pip install 'alpaca-py>=0.30')"
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _value(raw: Any) -> Any:
    """Unwrap enum-like attributes without importing alpaca types."""
    return getattr(raw, "value", raw)


def assert_paper_base_url(url: str | None) -> str:
    candidate = (url or "").strip().rstrip("/")
    if not candidate:
        raise PaperOnlyViolation("no trading base url supplied")
    if candidate in LIVE_BASE_URLS:
        raise PaperOnlyViolation(f"refusing live Alpaca endpoint: {candidate}")
    if candidate not in PAPER_TRADING_HOSTS:
        raise PaperOnlyViolation(
            f"refusing unlisted Alpaca endpoint {candidate!r}; allowed: {sorted(PAPER_TRADING_HOSTS)}"
        )
    return candidate


def build_order_payload(intent: OrderIntent) -> dict[str, str]:
    """Pure mapping from our intent to Alpaca's documented order fields."""
    if intent.order_type is OrderType.STOP_LIMIT:
        raise ExecutionError(
            "stop_limit orders require a stop price, which OrderIntent does not model yet; "
            "market and limit are the supported order types"
        )
    payload: dict[str, str] = {
        "symbol": intent.symbol,
        "qty": format(intent.quantity, "f"),
        "side": intent.side.value,
        "type": intent.order_type.value,
        "time_in_force": intent.time_in_force.value,
        "client_order_id": intent.client_order_id,
    }
    if intent.order_type is OrderType.LIMIT:
        if intent.limit_price is None:
            raise ExecutionError("limit orders require limit_price")
        payload["limit_price"] = format(intent.limit_price, "f")
    return payload


# ---------------------------------------------------------------------------
# alpaca-py marshalling (the only SDK-specific code in the project)
# ---------------------------------------------------------------------------


def _default_client_factory(api_key: str, secret_key: str) -> Any:
    try:
        from alpaca.trading.client import TradingClient
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise MissingDependency(INSTALL_HINT) from exc
    # paper=True is hardcoded: there is no code path that sets it False.
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=True)


def _default_request_builder(payload: Mapping[str, str]) -> Any:
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise MissingDependency(INSTALL_HINT) from exc

    common = {
        "symbol": payload["symbol"],
        "qty": payload["qty"],
        "side": OrderSide(payload["side"]),
        "time_in_force": TimeInForce(payload["time_in_force"]),
        "client_order_id": payload["client_order_id"],
    }
    if payload["type"] == OrderType.LIMIT.value:
        return LimitOrderRequest(limit_price=payload["limit_price"], **common)
    return MarketOrderRequest(**common)


def _default_orders_query_factory() -> Any:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise MissingDependency(INSTALL_HINT) from exc
    return GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------


class AlpacaPaperBroker:
    name = "alpaca-paper"

    def __init__(
        self,
        config: AlpacaConfig,
        *,
        client: Any | None = None,
        client_factory: Callable[[str, str], Any] | None = None,
        request_builder: Callable[[Mapping[str, str]], Any] | None = None,
        orders_query_factory: Callable[[], Any] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        assert_paper_base_url(config.trading_base_url)
        self._client = client
        self._client_factory = client_factory or _default_client_factory
        self._request_builder = request_builder or _default_request_builder
        self._orders_query_factory = orders_query_factory or _default_orders_query_factory
        self._env = env
        self._clock = clock

    # -- paper-only guards --------------------------------------------------

    @property
    def is_paper(self) -> bool:
        return True

    @property
    def is_simulated(self) -> bool:
        return False

    @property
    def base_url(self) -> str:
        return assert_paper_base_url(self._config.trading_base_url)

    @staticmethod
    def assert_paper_only_client(client: Any) -> None:
        for attribute in ("_base_url", "base_url"):
            url = getattr(client, attribute, None)
            if isinstance(url, str) and url.strip():
                assert_paper_base_url(url)
                return

    def _require_client(self) -> Any:
        if self._client is None:
            key, secret = self._config.credentials(self._env)
            self._client = self._client_factory(key, secret)
        # Checked on every use, not just on construction: an injected client would
        # otherwise skip layer 4 of the paper-only guarantee entirely.
        self.assert_paper_only_client(self._client)
        return self._client

    # -- adapter contract ---------------------------------------------------

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        client = self._require_client()
        request = self._request_builder(build_order_payload(intent))
        try:
            raw = client.submit_order(request)
        except Exception as exc:  # noqa: BLE001 - must classify, never re-raise blind
            raise self._classify(exc) from exc
        return self._from_alpaca_order(raw, fallback=intent)

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        client = self._require_client()
        try:
            raw = client.get_order_by_client_id(client_order_id)
        except Exception as exc:  # noqa: BLE001
            if self._is_not_found(exc):
                return None
            raise
        if raw is None:
            return None
        return self._from_alpaca_order(raw)

    def list_open_orders(self) -> Sequence[BrokerOrder]:
        client = self._require_client()
        raw_orders = client.get_orders(self._orders_query_factory()) or []
        out: list[BrokerOrder] = []
        for raw in raw_orders:
            try:
                out.append(self._from_alpaca_order(raw))
            except Exception:  # pragma: no cover - skip unparseable rows
                continue
        return out

    def cancel(self, client_order_id: str) -> bool:
        order = self.get_order(client_order_id)
        if order is None or not order.status.is_open or not order.broker_order_id:
            return False
        self._require_client().cancel_order_by_id(order.broker_order_id)
        return True

    def positions(self) -> Sequence[BrokerPosition]:
        client = self._require_client()
        out: list[BrokerPosition] = []
        for raw in client.get_all_positions() or []:
            symbol = getattr(raw, "symbol", None)
            if not symbol:
                continue
            out.append(
                BrokerPosition(
                    symbol=normalize_symbol(symbol),
                    quantity=getattr(raw, "qty", "0"),
                    avg_entry_price=getattr(raw, "avg_entry_price", "0"),
                    market_value=getattr(raw, "market_value", "0"),
                    unrealized_pl=getattr(raw, "unrealized_pl", "0") or "0",
                    current_price=getattr(raw, "current_price", None),
                )
            )
        return out

    def account(self) -> BrokerAccount:
        raw = self._require_client().get_account()
        return BrokerAccount(
            cash=getattr(raw, "cash", "0"),
            equity=getattr(raw, "equity", "0"),
            buying_power=getattr(raw, "buying_power", "0"),
            non_marginable_buying_power=getattr(raw, "non_marginable_buying_power", "0") or "0",
            currency=getattr(raw, "currency", "USD") or "USD",
            status=str(_value(getattr(raw, "status", "ACTIVE"))),
            trading_blocked=bool(getattr(raw, "trading_blocked", False)),
            account_blocked=bool(getattr(raw, "account_blocked", False)),
        )

    # -- normalisation ------------------------------------------------------

    def _from_alpaca_order(
        self, raw: Any, fallback: OrderIntent | None = None
    ) -> BrokerOrder:
        raw_status = str(_value(getattr(raw, "status", "")) or "")
        side_value = str(_value(getattr(raw, "side", None)) or (fallback.side.value if fallback else "buy"))
        quantity = getattr(raw, "qty", None) or (fallback.quantity if fallback else None)
        if quantity is None:
            raise ExecutionError("broker order has no quantity and no fallback intent")
        client_order_id = getattr(raw, "client_order_id", None) or (
            fallback.client_order_id if fallback else None
        )
        if not client_order_id:
            raise ExecutionError("broker order has no client_order_id")

        return BrokerOrder(
            client_order_id=str(client_order_id),
            broker_order_id=str(getattr(raw, "id", "") or "") or None,
            symbol=normalize_symbol(getattr(raw, "symbol", None) or (fallback.symbol if fallback else "")),
            side=Side(side_value),
            quantity=to_decimal(quantity, "quantity"),
            status=map_broker_status(raw_status),
            filled_quantity=to_decimal(getattr(raw, "filled_qty", None) or "0", "filled_qty"),
            avg_fill_price=getattr(raw, "filled_avg_price", None),
            submitted_at=getattr(raw, "submitted_at", None) or getattr(raw, "created_at", None),
            raw_status=raw_status or None,
            reason=str(_value(getattr(raw, "reject_reason", "") or "")) or None,
        )

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if status == 404:
            return True
        return "not found" in str(exc).lower()

    @staticmethod
    def _classify(exc: Exception) -> ExecutionError:
        """Map an SDK exception to ambiguous vs definite.

        The default is *ambiguous*, because treating an unknown failure as a clean
        rejection is precisely how bots submit duplicate orders.
        """
        text = str(exc)
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(status, int):
            if status == 429 or status >= 500:
                return AmbiguousSubmission(f"transient broker error {status}: {text}")
            if 400 <= status < 500:
                return DefiniteRejection(f"broker rejected the order ({status}): {text}")
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return AmbiguousSubmission(f"network failure during submit: {text}")
        return AmbiguousSubmission(f"unclassified submission failure: {text}")

    # -- preflight ----------------------------------------------------------

    def preflight(self) -> dict[str, Any]:
        """Verify the paper connection works. Safe: read-only calls only."""
        account = self.account()
        assert_paper_base_url(self._config.trading_base_url)
        if not account.usable:
            raise ExecutionError(
                f"paper account is not usable (status={account.status}, "
                f"trading_blocked={account.trading_blocked}, "
                f"account_blocked={account.account_blocked})"
            )
        return {
            "adapter": self.name,
            "base_url": self.base_url,
            "account_status": account.status,
            "currency": account.currency,
            "equity": format(account.equity, "f"),
            "cash": format(account.cash, "f"),
            "non_marginable_buying_power": format(account.non_marginable_buying_power, "f"),
        }


def build_paper_broker(config: AlpacaConfig, *, env: Mapping[str, str] | None = None) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(config, env=os.environ if env is None else env)
