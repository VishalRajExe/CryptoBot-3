"""Conformance between this project and the real ``alpaca-py`` SDK.

These tests are the reason ``alpaca-py`` is a declared dependency rather than an
optional extra bolted on at the end. They import the genuine SDK and build genuine
request models, so a renamed field or a changed enum breaks the suite here instead
of at 3am against a live endpoint. They are the second of four verification
levels; see ``docs/API_CAPABILITIES.md``:

1. unit tests against fakes - no SDK involved (the rest of ``tests/``);
2. **this file** - real SDK objects, mocked HTTP, no network;
3. authenticated paper calls - *not performed in this environment*;
4. a 30-day soak - *not performed in this environment*.

Nothing here contacts Alpaca. ``TradingClient`` is constructed (which builds no
connection) and every HTTP call is intercepted, so the file must pass with no
credentials set and no network available.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cryptobot.config.settings import Settings
from cryptobot.core.contracts import OrderStatus, OrderType, Side, map_broker_status
from cryptobot.core.errors import (
    AmbiguousSubmission,
    ConfigError,
    DefiniteRejection,
    ExecutionError,
    PaperOnlyViolation,
)
from cryptobot.execution.alpaca_paper import (
    AlpacaPaperBroker,
    _default_client_factory,
    _default_orders_query_factory,
    _default_request_builder,
    _readable_url,
    build_order_payload,
)

from conftest import START, make_intent

UTC = timezone.utc
CREDS = {"ALPACA_API_KEY": "test-key", "ALPACA_API_SECRET_KEY": "test-secret"}

pytest.importorskip("alpaca", reason="alpaca-py is an optional dependency")


# ---------------------------------------------------------------------------
# level 2: the SDK is present and its surface is what we coded against
# ---------------------------------------------------------------------------


def test_the_sdk_surface_we_depend_on_exists():
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

    for attribute in ("submit_order", "get_order_by_client_id", "get_orders", "get_account"):
        assert hasattr(TradingClient, attribute)
    assert OrderSide.BUY.value == "buy"
    assert {TimeInForce.GTC.value, TimeInForce.IOC.value} <= {m.value for m in TimeInForce}
    assert QueryOrderStatus.OPEN.value == "open"
    for model in (MarketOrderRequest, LimitOrderRequest, GetOrdersRequest):
        assert hasattr(model, "model_fields")


def test_the_sdk_offers_no_activity_api_so_fees_must_be_read_over_http():
    """Documents why :meth:`AlpacaPaperBroker.list_fee_activities` exists."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.models import Order

    assert not hasattr(TradingClient, "get_account_activities")
    assert not [name for name in Order.model_fields if "fee" in name.lower()]


def test_a_market_intent_becomes_a_real_sdk_request():
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    intent = make_intent(side=Side.BUY, quantity="0.001", notional="20")

    request = _default_request_builder(build_order_payload(intent))

    assert isinstance(request, MarketOrderRequest)
    assert request.symbol == "BTC/USD"
    # The SDK types ``qty`` as a number and coerces our decimal string. Checked
    # here so a version that starts rejecting strings is noticed; the string we
    # send is asserted in test_a_quantity_smaller_than_four_decimals...
    assert float(request.qty) == 0.001
    assert request.side is OrderSide.BUY
    assert request.time_in_force is TimeInForce.GTC
    assert request.client_order_id == intent.client_order_id


def test_a_limit_intent_becomes_a_real_sdk_request():
    from alpaca.trading.enums import OrderSide
    from alpaca.trading.requests import LimitOrderRequest

    intent = make_intent(
        side=Side.SELL,
        quantity="0.0005",
        notional="10",
        order_type=OrderType.LIMIT,
        limit_price="19500",
    )
    request = _default_request_builder(build_order_payload(intent))

    assert isinstance(request, LimitOrderRequest)
    assert float(request.limit_price) == 19500.0
    assert request.side is OrderSide.SELL


def test_a_quantity_smaller_than_four_decimals_is_not_sent_in_exponent_form():
    """``0.00001`` must serialise as ``0.00001``; ``1E-5`` is rejected by the API."""
    intent = make_intent(quantity="0.00001", notional="0.5")

    payload = build_order_payload(intent)

    assert payload["qty"] == "0.00001"
    assert "E" not in payload["qty"].upper()


def test_the_open_orders_query_is_a_real_sdk_request():
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    query = _default_orders_query_factory()

    assert isinstance(query, GetOrdersRequest)
    assert query.status is QueryOrderStatus.OPEN
    assert query.limit == 100


def test_the_crypto_bars_request_carries_the_documented_fields():
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    request = CryptoBarsRequest(
        symbol_or_symbols="BTC/USD", timeframe=TimeFrame.Hour, limit=200
    )

    assert request.symbol_or_symbols == "BTC/USD"
    # ``TimeFrame.Hour`` builds a new object per access, so compare its parts.
    assert request.timeframe.amount == 1
    assert request.timeframe.unit.value == "Hour"
    # Alpaca documents exactly one crypto data location; anything else is a guess.
    assert request.currency is None  # defaults to the documented location


# ---------------------------------------------------------------------------
# the paper-only guard, against real clients
# ---------------------------------------------------------------------------


def test_a_real_paper_client_passes_the_guard():
    from alpaca.trading.client import TradingClient

    client = TradingClient(api_key="k", secret_key="s", paper=True)

    AlpacaPaperBroker.assert_paper_only_client(client)
    assert _readable_url(client) == "https://paper-api.alpaca.markets"


def test_a_real_live_client_is_refused():
    from alpaca.trading.client import TradingClient

    client = TradingClient(api_key="k", secret_key="s", paper=False)

    with pytest.raises(PaperOnlyViolation):
        AlpacaPaperBroker.assert_paper_only_client(client)


def test_a_client_overridden_to_the_live_host_is_refused():
    from alpaca.trading.client import TradingClient

    client = TradingClient(api_key="k", secret_key="s", url_override="https://api.alpaca.markets")

    with pytest.raises(PaperOnlyViolation):
        AlpacaPaperBroker.assert_paper_only_client(client)


def test_the_endpoint_is_readable_without_unwrapping_the_enum():
    """``BaseURL`` subclasses ``str``, so the guard always had a usable URL.

    Recorded because it is easy to assume the opposite. The guard's actual gaps
    were ``url_override`` and the silent pass when nothing was readable at all;
    both are covered by the tests around this one.
    """
    from alpaca.trading.client import TradingClient

    client = TradingClient(api_key="k", secret_key="s", paper=True)

    assert isinstance(client._base_url, str)
    assert client._base_url == "https://paper-api.alpaca.markets"


def test_the_guard_fails_closed_when_a_trading_client_hides_its_endpoint():
    from alpaca.trading.client import TradingClient

    class Opaque(TradingClient):
        """A client whose endpoint cannot be read at all."""

        def __init__(self) -> None:  # noqa: D401 - deliberately skips the base init
            pass

    with pytest.raises(PaperOnlyViolation):
        AlpacaPaperBroker.assert_paper_only_client(Opaque())


def test_a_client_that_is_not_the_sdk_is_left_to_the_url_check():
    class NotAClient:
        base_url = "https://paper-api.alpaca.markets"

    AlpacaPaperBroker.assert_paper_only_client(NotAClient())


def test_the_client_factory_can_never_produce_a_live_client():
    client = _default_client_factory("k", "s")

    assert _readable_url(client) == "https://paper-api.alpaca.markets"


# ---------------------------------------------------------------------------
# exception handling: the SDK's properties can raise
# ---------------------------------------------------------------------------


class _HttpError(Exception):
    """The minimum shape ``APIError.status_code`` dereferences."""

    def __init__(self, status: int) -> None:
        self.response = type("Response", (), {"status_code": status})()


def _api_error(body: str, status: int | None):
    from alpaca.common.exceptions import APIError

    return APIError(body, _HttpError(status) if status is not None else None)


def test_the_sdk_error_code_property_raises_when_the_body_lacks_it():
    """Why the adapter never reads ``exc.code`` while classifying."""
    exc = _api_error('{"message": "insufficient balance"}', 403)

    with pytest.raises(KeyError):
        _ = exc.code
    assert exc.status_code == 403  # this one is safe


def test_a_client_error_is_a_definite_rejection():
    exc = _api_error('{"message": "insufficient balance"}', 403)

    classified = AlpacaPaperBroker._classify(exc)

    assert isinstance(classified, DefiniteRejection)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_transient_error_is_ambiguous(status: int):
    exc = _api_error('{"message": "try later"}', status)

    assert isinstance(AlpacaPaperBroker._classify(exc), AmbiguousSubmission)


def test_an_error_with_no_status_is_ambiguous_not_a_rejection():
    """Treating an unknown failure as a clean rejection is how duplicates happen."""
    exc = _api_error('{"message": "?"}', None)

    assert AlpacaPaperBroker._status_of(exc) is None
    assert isinstance(AlpacaPaperBroker._classify(exc), AmbiguousSubmission)


def test_a_not_found_order_is_absent_rather_than_a_crash():
    exc = _api_error('{"message": "order not found"}', 404)

    assert AlpacaPaperBroker._is_not_found(exc) is True
    # and the property that would raise is never consulted
    with pytest.raises(KeyError):
        _ = exc.code


def test_an_error_without_a_code_key_is_not_mistaken_for_a_missing_order():
    exc = _api_error('{"message": "unknown"}', 500)

    assert AlpacaPaperBroker._is_not_found(exc) is False


# ---------------------------------------------------------------------------
# state mapping: every status the SDK can report has a defined meaning
# ---------------------------------------------------------------------------


def test_every_sdk_order_status_maps_to_a_local_state():
    from alpaca.trading.enums import OrderStatus as SdkStatus

    for member in SdkStatus:
        mapped = map_broker_status(member.value)
        assert isinstance(mapped, OrderStatus)
        if member.value == "pending_review":
            # Not mapped on purpose: an unrecognised state must never look settled.
            assert mapped is OrderStatus.UNKNOWN
            assert mapped.needs_broker_confirmation
    # The states the bot acts on must be exact, not approximate.
    assert map_broker_status("filled") is OrderStatus.FILLED
    assert map_broker_status("partially_filled") is OrderStatus.PARTIALLY_FILLED
    assert map_broker_status("canceled") is OrderStatus.CANCELED
    assert map_broker_status("rejected") is OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------


def _sdk_order(**overrides):
    from alpaca.trading.models import Order

    body = {
        "id": "6b1cf1c8-0000-0000-0000-000000000001",
        "client_order_id": "cb-btcusd-1h-20240101-buy",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:05Z",
        "submitted_at": "2024-01-01T00:00:00Z",
        "order_class": "simple",
        "time_in_force": "gtc",
        "status": "partially_filled",
        "extended_hours": False,
        "symbol": "BTC/USD",
        "qty": "0.001",
        "filled_qty": "0.0004",
        "filled_avg_price": "20000",
        "side": "buy",
        "type": "market",
    }
    return Order.model_validate({**body, **overrides})


def test_a_partial_fill_response_is_normalised():
    broker = AlpacaPaperBroker(_settings_stub(), env=CREDS)

    order = broker._from_alpaca_order(_sdk_order())

    assert order.client_order_id == "cb-btcusd-1h-20240101-buy"
    assert order.symbol == "BTC/USD"
    assert order.side is Side.BUY
    assert order.quantity == Decimal("0.001")
    assert order.filled_quantity == Decimal("0.0004")
    assert order.avg_fill_price == Decimal("20000")
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.remaining_quantity == Decimal("0.0006")


def test_a_filled_response_is_terminal():
    broker = AlpacaPaperBroker(_settings_stub(), env=CREDS)

    order = broker._from_alpaca_order(
        _sdk_order(status="filled", filled_qty="0.001", filled_avg_price="20010.5")
    )

    assert order.status is OrderStatus.FILLED
    assert order.status.is_terminal
    assert order.remaining_quantity == 0
    assert order.avg_fill_price == Decimal("20010.5")


def test_a_rejected_response_keeps_the_reason():
    broker = AlpacaPaperBroker(_settings_stub(), env=CREDS)

    order = broker._from_alpaca_order(
        _sdk_order(status="rejected", filled_qty="0", filled_avg_price=None)
    )

    assert order.status is OrderStatus.REJECTED
    assert order.filled_quantity == 0


# ---------------------------------------------------------------------------
# fee activities over mocked HTTP
# ---------------------------------------------------------------------------


def _settings_stub():
    from cryptobot.config.settings import AlpacaConfig

    return AlpacaConfig(
        trading_base_url="https://paper-api.alpaca.markets",
        data_base_url="https://data.alpaca.markets",
        crypto_location="us",
        api_key_env="ALPACA_API_KEY",
        api_secret_env="ALPACA_API_SECRET_KEY",
        request_timeout_seconds=20.0,
        max_retries=3,
    )


class _Response:
    def __init__(self, payload) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_fee_activities_are_fetched_per_type_and_parsed(monkeypatch):
    pages = {
        "CFEE": [
            {
                "id": "20240102000000000::cfee-1",
                "activity_type": "CFEE",
                "symbol": "BTCUSD",
                "qty": "-0.000002",
                "price": "20000",
                "date": "2024-01-02",
            }
        ],
        "FEE": [
            {
                "id": "20240102000000000::fee-1",
                "activity_type": "FEE",
                "symbol": "BTCUSD",
                "qty": "0",
                "net_amount": "-0.05",
                "date": "2024-01-02",
            }
        ],
    }
    seen: list[str] = []

    def fake_urlopen(request, timeout=None):
        seen.append(request.full_url)
        kind = "CFEE" if "activity_types=CFEE" in request.full_url else "FEE"
        return _Response(pages[kind])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    broker = AlpacaPaperBroker(_settings_stub(), env=CREDS)

    activities = broker.list_fee_activities()

    assert [a.kind for a in activities] == ["CFEE", "FEE"]
    assert activities[0].currency == "BTC"
    assert activities[0].quote_value == Decimal("0.04")
    assert activities[1].currency == "USD"
    assert activities[1].quote_value == Decimal("0.05")
    assert len(seen) == 2  # one request per activity type
    assert all("paper-api.alpaca.markets" in url for url in seen)
    assert all("%3A%3A" in url for url in seen) is False  # no id leaked into the query


def test_fee_activity_requests_send_the_credentials_but_never_echo_them(monkeypatch):
    captured: dict[str, str] = {}

    def fake_urlopen(request, timeout=None):
        captured.update({k.lower(): v for k, v in request.headers.items()})
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    broker = AlpacaPaperBroker(_settings_stub(), env=CREDS)

    with pytest.raises(ExecutionError) as caught:
        broker.list_fee_activities()

    assert captured["apca-api-key-id"] == "test-key"
    assert captured["apca-api-secret-key"] == "test-secret"
    message = str(caught.value)
    assert "401" in message
    assert "test-secret" not in message
    assert "test-key" not in message


def test_pagination_follows_the_documented_page_token(monkeypatch):
    page_one = [
        {
            "id": f"20240102000000000::cfee-{index}",
            "activity_type": "CFEE",
            "symbol": "BTCUSD",
            "qty": "-0.000001",
            "price": "20000",
            "date": "2024-01-02",
        }
        for index in range(2)
    ]
    page_two = [
        {
            "id": "20240102000000000::cfee-2",
            "activity_type": "CFEE",
            "symbol": "BTCUSD",
            "qty": "-0.000001",
            "price": "20000",
            "date": "2024-01-02",
        }
    ]
    urls: list[str] = []
    responses: list[list[dict]] = [page_one, page_two, []]

    def fake_urlopen(request, timeout=None):
        urls.append(request.full_url)
        return _Response(responses.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    broker = AlpacaPaperBroker(_settings_stub(), env=CREDS)

    rows = broker._fetch_activities("CFEE", after=None, key="k", secret="s", page_size=2)

    assert len(rows) == 3
    assert "page_token" not in urls[0]
    # the token is the id of the last row returned, per the documented contract
    assert "page_token=20240102000000000" in urls[1]


def test_an_error_object_instead_of_a_list_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: _Response({"message": "nope"})
    )
    broker = AlpacaPaperBroker(_settings_stub(), env=CREDS)

    with pytest.raises(ExecutionError):
        broker.list_fee_activities()


# ---------------------------------------------------------------------------
# configuration validation
# ---------------------------------------------------------------------------


def test_configuration_refuses_a_live_trading_endpoint():
    from cryptobot.config.settings import AlpacaConfig

    with pytest.raises(ConfigError):
        AlpacaConfig.from_dict(
            {
                "trading_base_url": "https://api.alpaca.markets",
                "data_base_url": "https://data.alpaca.markets",
            }
        )


def test_configuration_refuses_an_unknown_crypto_location():
    from cryptobot.config.settings import AlpacaConfig

    with pytest.raises(ConfigError):
        AlpacaConfig.from_dict(
            {
                "trading_base_url": "https://paper-api.alpaca.markets",
                "data_base_url": "https://data.alpaca.markets",
                "crypto_location": "eu",
            }
        )


def test_missing_credentials_are_named_but_never_printed(settings: Settings):
    missing = settings.alpaca.missing_credential_names({})

    assert missing == ["ALPACA_API_KEY", "ALPACA_API_SECRET_KEY"]
    with pytest.raises(ConfigError) as caught:
        settings.alpaca.credentials({})
    assert "ALPACA_API_KEY" in str(caught.value)
    assert "ALPACA_API_SECRET_KEY" in str(caught.value)
