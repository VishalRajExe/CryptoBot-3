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
is unit-tested without the SDK. The marshalling below is exercised against the
real ``alpaca-py`` in ``tests/test_sdk_integration.py``: genuine request models,
response models and exceptions, with every HTTP call intercepted. That file is
the second of four verification levels (unit / SDK-mocked / authenticated paper /
soak); the last two have **not** been performed against a real account in this
environment.

Crypto specifics encoded here, all verified in docs/API_CAPABILITIES.md:
``qty`` is built as a decimal string (which the SDK then types as a number),
``time_in_force`` is ``gtc`` or ``ioc`` (never ``day``), and ``client_order_id``
is always deterministic.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
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
from cryptobot.execution.fees import FEE_ACTIVITY_TYPES, FeeActivity, parse_fee_activities

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
    """Unwrap an enum-like attribute to the broker's own text.

    Used for anything that may end up in a comparison or a log, so a status or
    endpoint compares as ``partially_filled`` rather than as ``Member.X`` and never
    depends on whether a particular enum happens to subclass ``str``.
    """
    return getattr(raw, "value", raw)


def _readable_url(client: Any) -> str | None:
    """The endpoint a client is actually pointed at, whatever form it stores.

    Returns ``None`` when nothing can be read; callers decide whether that is
    fatal. Enum members, plain strings and ``url_override`` are all covered.
    """
    for attribute in ("_base_url", "base_url", "url_override"):
        try:
            candidate = _value(getattr(client, attribute, None))
        except Exception:  # noqa: BLE001 - a property may raise; absence is not evidence
            continue
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _is_alpaca_trading_client(client: Any) -> bool:
    """True when this really is an alpaca-py trading client (SDK permitting)."""
    try:
        from alpaca.trading.client import TradingClient
    except Exception:  # noqa: BLE001 - SDK absent means it cannot be one
        return False
    return isinstance(client, TradingClient)


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
        """Refuse a client that is not pointed at the paper host.

        ``alpaca-py`` stores the endpoint in ``_base_url`` as a ``BaseURL`` member,
        which subclasses ``str``, so a stock client is caught by the loop below.
        The parts worth having are the ones a bare ``_base_url`` check misses:
        ``url_override`` redirects a paper client to another host without touching
        ``_base_url``, and a genuine ``TradingClient`` whose endpoint cannot be read
        at all is refused rather than waved through, because paper-only cannot be
        proven for it.
        """
        url = _readable_url(client)
        if url is not None:
            assert_paper_base_url(url)
            return
        if _is_alpaca_trading_client(client):
            raise PaperOnlyViolation(
                "refusing an alpaca trading client whose endpoint cannot be read; "
                "paper-only cannot be proven, so it is not assumed"
            )

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
                    cost_basis=getattr(raw, "cost_basis", None),
                    qty_available=getattr(raw, "qty_available", None),
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
            accrued_fees=getattr(raw, "accrued_fees", None),
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
        """True when the broker has no such order.

        Deliberately does not touch ``exc.code``: that property parses the error
        body and raises for any payload without a ``code`` key, which would turn
        "no such order" into a crash inside the handler that exists to absorb it.
        """
        if AlpacaPaperBroker._status_of(exc) == 404:
            return True
        return "not found" in str(exc).lower()

    @staticmethod
    def _status_of(exc: Exception) -> int | None:
        """HTTP status of an SDK exception, read without trusting the object.

        ``APIError.status_code`` is a property that dereferences a wrapped
        ``requests`` response, and ``APIError.code`` does
        ``json.loads(self._error)["code"]`` - which raises ``KeyError`` or
        ``JSONDecodeError`` for any payload that is not shaped as expected. A
        classifier that raises inside an ``except`` block converts a definite
        rejection into an unclassified crash, so every read is defensive here.
        """
        try:
            status = getattr(exc, "status_code", None)
        except Exception:  # noqa: BLE001
            return None
        return status if isinstance(status, int) else None

    @staticmethod
    def _classify(exc: Exception) -> ExecutionError:
        """Map an SDK exception to ambiguous vs definite.

        The default is *ambiguous*, because treating an unknown failure as a clean
        rejection is precisely how bots submit duplicate orders.
        """
        text = str(exc)
        status = AlpacaPaperBroker._status_of(exc)
        if status is not None:
            if status == 429 or status >= 500:
                return AmbiguousSubmission(f"transient broker error {status}: {text}")
            if 400 <= status < 500:
                return DefiniteRejection(f"broker rejected the order ({status}): {text}")
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return AmbiguousSubmission(f"network failure during submit: {text}")
        return AmbiguousSubmission(f"unclassified submission failure: {text}")

    # -- fees ---------------------------------------------------------------

    def list_fee_activities(self, *, after: date | None = None) -> list[FeeActivity]:
        """Posted crypto fees. Read-only: this never places or changes an order.

        ``alpaca-py`` exposes no activities API, so this calls the documented
        endpoint directly with the same credentials. One activity type per request
        avoids relying on how a repeated query parameter is encoded.
        """
        key, secret = self._config.credentials(self._env)
        out: list[FeeActivity] = []
        for activity_type in FEE_ACTIVITY_TYPES:
            out.extend(
                parse_fee_activities(self._fetch_activities(activity_type, after=after, key=key, secret=secret))
            )
        return sorted(out, key=lambda item: item.ts)

    def _fetch_activities(
        self,
        activity_type: str,
        *,
        after: date | None,
        key: str,
        secret: str,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        base = self.base_url
        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(50):  # bounded: 50 pages is far more than a day produces
            query: dict[str, str] = {
                "activity_types": activity_type,
                "direction": "asc",
                "page_size": str(page_size),
            }
            if after is not None:
                query["after"] = after.isoformat()
            if page_token:
                query["page_token"] = page_token
            url = f"{base}/v2/account/activities?{urllib.parse.urlencode(query)}"
            request = urllib.request.Request(
                url,
                headers={
                    "APCA-API-KEY-ID": key,
                    "APCA-API-SECRET-KEY": secret,
                    "accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                # The message must not echo headers, which carry the secret.
                raise ExecutionError(
                    f"activity request failed for {activity_type}: HTTP {exc.code}"
                ) from exc
            except urllib.error.URLError as exc:
                raise ExecutionError(
                    f"activity request failed for {activity_type}: {exc.reason}"
                ) from exc

            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ExecutionError("activity response was not JSON") from exc
            if isinstance(payload, dict):
                raise ExecutionError(
                    f"activity request for {activity_type} returned an error object: "
                    f"{str(payload.get('message', payload))[:200]}"
                )
            if not isinstance(payload, list) or not payload:
                break
            rows.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < page_size:
                break
            # Documented pagination: the token is the id of the last row returned.
            page_token = str(payload[-1].get("id") or "")
            if not page_token:
                break
        return rows

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
