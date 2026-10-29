"""Phase 2 (market data) and Phase 4 (deterministic risk).

Two promises are under test here:

* Bad or incomplete market data never reaches a strategy. Candles are validated
  on ingest, and any candle whose interval has not finished is dropped once, in
  the data layer, for every caller.
* Every risk limit is able to reject an order *on its own*. A single aggregate
  "risk ok" flag would let one broken check hide behind fourteen passing ones,
  so each limit is exercised independently and the exact set of failing checks
  is asserted.

No test here contacts a network endpoint; the transport seam is faked.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cryptobot.config.settings import Settings
from cryptobot.core.contracts import Bar, InstrumentSpec, Side
from cryptobot.core.errors import ConfigError, DataError, StaleDataError
from cryptobot.core.states import SystemState
from cryptobot.core.transport import HttpResponse, TransportError
from cryptobot.data.alpaca_data import AlpacaCryptoDataClient, MarketDataService
from cryptobot.data.validation import (
    assert_fresh,
    detect_lookahead,
    drop_unclosed,
    find_gaps,
    validate_raw_bars,
)
from cryptobot.journal.store import Journal
from cryptobot.risk.manager import RiskContext, RiskManager

from conftest import (
    BTC,
    START,
    Clock,
    FakeTransport,
    allocation,
    json_response,
    make_bars,
    make_intent,
)

UTC = timezone.utc
ENV = {"ALPACA_API_KEY": "test-key", "ALPACA_API_SECRET_KEY": "test-secret"}


def _raw(ts: str, o=100, h=110, low=90, c=105, v=1):
    """One bar in Alpaca's compact wire format."""
    return {"t": ts, "o": o, "h": h, "l": low, "c": c, "v": v}


def _payload(symbol: str = "BTC/USD", *bars, token=None):
    return {"bars": {symbol: list(bars)}, "next_page_token": token}


# ---------------------------------------------------------------------------
# Phase 2: bar validation
# ---------------------------------------------------------------------------


def test_validate_accepts_well_formed_payload():
    report = validate_raw_bars(
        [_raw("2024-01-01T00:00:00Z"), _raw("2024-01-01T01:00:00Z")],
        "btc/usd",
        "1Hour",
    )
    assert report.ok
    assert report.quality == "clean"
    assert [bar.ts.isoformat() for bar in report.accepted] == [
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T01:00:00+00:00",
    ]
    # Symbol and timeframe are normalised on the way in.
    assert report.accepted[0].symbol == "BTC/USD"
    assert report.accepted[0].timeframe == "1h"
    assert report.duplicates == 0
    assert report.gaps == ()


@pytest.mark.parametrize(
    "bad, reason",
    [
        (_raw("2024-01-01T00:00:00Z", o=100, h=80, low=90, c=105), "high < low"),
        (_raw("2024-01-01T00:00:00Z", c=0), "non-positive price"),
        (_raw("2024-01-01T00:00:00Z", v=-1), "negative volume"),
        (_raw("2024-01-01T00:30:00Z"), "not aligned"),
        ({"o": 1, "h": 2, "l": 1, "c": 2, "v": 1}, "timestamp"),  # no t field
        ("not-a-bar", "not a mapping"),
    ],
    ids=["high-below-low", "zero-price", "negative-volume", "misaligned", "no-ts", "not-mapping"],
)
def test_validate_rejects_rather_than_repairs(bad, reason):
    report = validate_raw_bars([bad], "BTC/USD", "1h")
    assert not report.accepted
    assert len(report.rejected) == 1
    assert reason in report.rejected[0]["reason"].lower()
    assert report.quality == "empty"


def test_validate_counts_duplicates_and_keeps_the_later_revision():
    report = validate_raw_bars(
        [
            _raw("2024-01-01T00:00:00Z", c=105),
            _raw("2024-01-01T00:00:00Z", c=107),
            _raw("2024-01-01T01:00:00Z", c=108),
        ],
        "BTC/USD",
        "1h",
    )
    assert report.duplicates == 1
    assert len(report.accepted) == 2
    assert report.accepted[0].close == Decimal("107")


def test_zero_volume_is_legitimate_for_alpaca_crypto_bars():
    # Alpaca builds crypto bars from quote mid-prices, so a window with no trade
    # is a real, usable candle. It must be reported, never rejected.
    report = validate_raw_bars(
        [_raw("2024-01-01T00:00:00Z", v=0), _raw("2024-01-01T01:00:00Z", v=5)],
        "BTC/USD",
        "1h",
    )
    assert len(report.accepted) == 2
    assert report.zero_volume == 1
    assert report.quality == "clean"


def test_missing_volume_defaults_to_zero_instead_of_crashing():
    bar = {"t": "2024-01-01T00:00:00Z", "o": 100, "h": 110, "l": 90, "c": 105}
    report = validate_raw_bars([bar], "BTC/USD", "1h")
    assert report.accepted[0].volume == Decimal("0")


def test_validate_accepts_epoch_timestamps():
    report = validate_raw_bars([_raw(1704067200)], "BTC/USD", "1h")
    assert report.accepted[0].ts == datetime(2024, 1, 1, tzinfo=UTC)


def test_gaps_are_reported_not_papered_over():
    report = validate_raw_bars(
        [
            _raw("2024-01-01T00:00:00Z"),
            _raw("2024-01-01T01:00:00Z"),
            _raw("2024-01-01T04:00:00Z"),  # 02:00 and 03:00 missing
        ],
        "BTC/USD",
        "1h",
    )
    assert report.gaps == ((datetime(2024, 1, 1, 2, tzinfo=UTC), datetime(2024, 1, 1, 4, tzinfo=UTC)),)
    assert report.quality == "degraded"


def test_find_gaps_ignores_empty_and_single_series():
    assert find_gaps([], "1h") == ()
    assert find_gaps(make_bars(["100"]), "1h") == ()
    assert find_gaps(make_bars(["100", "101"]), "1h") == ()


def test_minutely_gaps_use_the_minute_step():
    bars = [
        Bar(symbol="BTC/USD", timeframe="5m", ts=START, open=100, high=101, low=99,
            close=100, volume=1),
        Bar(symbol="BTC/USD", timeframe="5m", ts=START + timedelta(minutes=15), open=100,
            high=101, low=99, close=100, volume=1),
    ]
    gaps = find_gaps(bars, "5m")
    assert gaps == ((START + timedelta(minutes=5), START + timedelta(minutes=15)),)


def test_drop_unclosed_removes_the_forming_candle():
    bars = make_bars(["100", "101", "102"])  # 00:00, 01:00, 02:00
    now = START + timedelta(hours=2, minutes=27)
    kept = drop_unclosed(bars, now)
    assert [bar.ts.hour for bar in kept] == [0, 1]
    assert detect_lookahead(bars, now) == (bars[2],)


def test_candle_closes_exactly_at_its_bucket_end():
    bar = make_bars(["100"])[0]
    assert bar.closes_at == START + timedelta(hours=1)
    assert not bar.is_closed(START + timedelta(minutes=59, seconds=59))
    assert bar.is_closed(START + timedelta(hours=1))


def test_assert_fresh_rejects_a_stale_candle():
    bar = make_bars(["100"])[0]  # closes at 01:00
    assert_fresh(bar, START + timedelta(hours=2), 7200)  # age exactly 1h: fine
    with pytest.raises(StaleDataError):
        assert_fresh(bar, START + timedelta(hours=4), 7200)


# ---------------------------------------------------------------------------
# Phase 2: Alpaca REST client
# ---------------------------------------------------------------------------


def _client(settings: Settings, transport: FakeTransport) -> AlpacaCryptoDataClient:
    return AlpacaCryptoDataClient(
        settings.alpaca, transport=transport, env=ENV, sleep=lambda _seconds: None
    )


def test_fetch_bars_uses_documented_parameters_and_auth_headers(settings):
    transport = FakeTransport([json_response(_payload("BTC/USD", _raw("2024-01-01T00:00:00Z")))])
    client = _client(settings, transport)

    bars = client.fetch_bars("btc/usd", "1h", START, START + timedelta(hours=2))

    assert len(bars) == 1
    method, url, kwargs = transport.calls[0]
    assert method == "GET"
    assert url == "https://data.alpaca.markets/v1beta3/crypto/us/bars"
    assert kwargs["headers"] == {
        "APCA-API-KEY-ID": "test-key",
        "APCA-API-SECRET-KEY": "test-secret",
    }
    params = kwargs["params"]
    assert params["symbols"] == "BTC/USD"
    assert params["timeframe"] == "1Hour"
    assert params["start"] == "2024-01-01T00:00:00Z"
    assert params["end"] == "2024-01-01T02:00:00Z"
    assert params["limit"] == 1000
    assert params["page_token"] is None


def test_fetch_bars_follows_next_page_token(settings):
    transport = FakeTransport(
        [
            json_response(_payload("BTC/USD", _raw("2024-01-01T00:00:00Z"), token="page-2")),
            json_response(_payload("BTC/USD", _raw("2024-01-01T01:00:00Z"))),
        ]
    )
    bars = _client(settings, transport).fetch_bars("BTC/USD", "1h", START, START + timedelta(hours=3))

    assert len(bars) == 2
    assert transport.calls[1][2]["params"]["page_token"] == "page-2"


def test_fetch_bars_raises_on_an_api_error_message(settings):
    transport = FakeTransport([json_response({"message": "invalid symbol: XRP/USD"})])
    with pytest.raises(DataError, match="invalid symbol"):
        _client(settings, transport).fetch_bars("BTC/USD", "1h", START, START + timedelta(hours=2))


def test_fetch_bars_retries_a_transient_status_then_succeeds(settings):
    transport = FakeTransport(
        [
            HttpResponse(status=429, body=b"", headers={"Retry-After": "0"}),
            json_response(_payload("BTC/USD", _raw("2024-01-01T00:00:00Z"))),
        ]
    )
    bars = _client(settings, transport).fetch_bars("BTC/USD", "1h", START, START + timedelta(hours=2))
    assert len(bars) == 1
    assert len(transport.calls) == 2


def test_authentication_failure_is_reported_without_leaking_the_secret(settings):
    transport = FakeTransport([HttpResponse(status=401, body=b'{"message":"unauthorized"}')])
    with pytest.raises(TransportError) as caught:
        _client(settings, transport).fetch_bars("BTC/USD", "1h", START, START + timedelta(hours=2))
    message = str(caught.value)
    assert "authentication failed" in message
    assert "test-secret" not in message


def test_missing_credentials_fail_before_any_request(settings):
    transport = FakeTransport()
    client = AlpacaCryptoDataClient(settings.alpaca, transport=transport, env={})
    with pytest.raises(ConfigError) as caught:
        client.fetch_bars("BTC/USD", "1h", START, START + timedelta(hours=2))
    message = str(caught.value)
    assert "ALPACA_API_KEY" in message and "ALPACA_API_SECRET_KEY" in message
    assert transport.calls == []


def test_list_crypto_assets_reads_from_the_paper_host_and_filters(settings):
    payload = [
        {
            "symbol": "BTC/USD",
            "class": "crypto",
            "status": "active",
            "tradable": True,
            "min_order_size": "0.0001",
            "min_trade_increment": "0.0001",
            "price_increment": "1",
        },
        {"symbol": "AAPL", "class": "us_equity", "tradable": True},
        {"class": "crypto"},  # no symbol
        "garbage",
    ]
    transport = FakeTransport([json_response(payload)])
    specs = _client(settings, transport).list_crypto_assets()

    assert [spec.symbol for spec in specs] == ["BTC/USD"]
    assert specs[0].min_trade_increment == Decimal("0.0001")
    url = transport.calls[0][1]
    assert url.startswith("https://paper-api.alpaca.markets/v2/assets")
    assert transport.calls[0][2]["params"]["asset_class"] == "crypto"


def test_get_instrument_rejects_unknown_and_untradable_pairs(settings):
    unknown = FakeTransport([json_response([])])
    with pytest.raises(DataError, match="not listed"):
        _client(settings, unknown).get_instrument("DOGE/USD")

    untradable = FakeTransport(
        [json_response([{"symbol": "BTC/USD", "class": "crypto", "tradable": False}])]
    )
    with pytest.raises(DataError, match="not currently tradable"):
        _client(settings, untradable).get_instrument("BTC/USD")


# ---------------------------------------------------------------------------
# Phase 2: caching service
# ---------------------------------------------------------------------------


class _StubClient:
    """Stands in for the REST client; no network, no credentials."""

    def __init__(self, payloads, instrument: InstrumentSpec | None = BTC) -> None:
        self.payloads = list(payloads)
        self.instrument = instrument
        self.asset_calls = 0

    def fetch_bars(self, symbol, timeframe, start, end):
        return list(self.payloads)

    def get_instrument(self, symbol):
        self.asset_calls += 1
        if self.instrument is None:
            raise DataError("not listed")
        return self.instrument


CLOCK_NOW = datetime(2024, 1, 1, 6, 27, tzinfo=UTC)


def _service(settings: Settings, journal: Journal, clock, client) -> MarketDataService:
    return MarketDataService(settings, journal, client, clock=clock)


def test_refresh_stores_closed_bars_only(settings, journal):
    clock = Clock(CLOCK_NOW)
    payloads = [_raw(f"2024-01-01T0{hour}:00:00Z") for hour in (4, 5, 6)]
    result = _service(settings, journal, clock, _StubClient(payloads)).refresh("BTC/USD")

    assert result.fetched == 3
    assert len(result.report.accepted) == 3  # the wire payload is fully reported
    assert result.stored == 2  # the 06:00 candle has not closed at 06:27
    assert result.latest is not None and result.latest.ts.hour == 5


def test_history_never_returns_a_forming_candle(settings, journal):
    # Regression: the strategy once decided on the 06:00 candle at 06:27, i.e. on
    # an incomplete bar. The filter lives in the data layer so no caller can
    # reintroduce the bug.
    journal.save_bars(make_bars([100, 101, 102, 103, 104], start=datetime(2024, 1, 1, 2, tzinfo=UTC)))
    service = _service(settings, journal, Clock(CLOCK_NOW), None)

    bars = service.history("BTC/USD", limit=10)

    assert [bar.ts.hour for bar in bars] == [2, 3, 4, 5]
    assert service.latest_closed("BTC/USD").ts.hour == 5


def test_latest_closed_returns_none_when_the_only_bar_is_forming(settings, journal):
    journal.save_bars(make_bars([100], start=datetime(2024, 1, 1, 6, tzinfo=UTC)))
    service = _service(settings, journal, Clock(CLOCK_NOW), None)
    assert service.latest_closed("BTC/USD") is None


def test_refresh_without_a_client_fails_loudly(settings, journal):
    service = _service(settings, journal, Clock(CLOCK_NOW), None)
    with pytest.raises(DataError, match="no market data client"):
        service.refresh("BTC/USD")


def test_ensure_history_refreshes_an_empty_cache(settings, journal):
    clock = Clock(CLOCK_NOW)
    client = _StubClient([_raw(f"2024-01-01T0{hour}:00:00Z") for hour in (4, 5)])
    bars = _service(settings, journal, clock, client).ensure_history("BTC/USD")
    assert [bar.ts.hour for bar in bars] == [4, 5]


def test_instrument_is_cached_after_the_first_fetch(settings, journal):
    clock = Clock(CLOCK_NOW)
    client = _StubClient([], instrument=BTC)
    service = _service(settings, journal, clock, client)

    assert service.instrument("BTC/USD").symbol == "BTC/USD"
    assert service.instrument("BTC/USD").symbol == "BTC/USD"
    assert client.asset_calls == 1
    assert service.instrument("BTC/USD", refresh=True) is not None
    assert client.asset_calls == 2


# ---------------------------------------------------------------------------
# Phase 4: deterministic risk manager
# ---------------------------------------------------------------------------

CHECK_ORDER = (
    "system_state",
    "symbol_allowlist",
    "instrument_available",
    "data_freshness",
    "quantity_precision",
    "positive_notional",
    "max_position_value",
    "per_trade_risk",
    "aggregate_allocation",
    "daily_loss_limit",
    "max_drawdown",
    "duplicate_intent",
    "order_rate_limit",
    "sell_within_position",
    "spread",
)

#: Everything below is legal for the shipped configuration: notional 20 is
#: exactly the per-trade cap, the quantity obeys the BTC/USD increment, and the
#: candle has been closed for an hour.
BASE_INTENT = {"side": Side.BUY, "quantity": "0.001", "notional": "20"}
RISK_NOW = START + timedelta(hours=2)


def _risk_ctx(**overrides) -> RiskContext:
    values = dict(
        now=RISK_NOW,
        system_state=SystemState.ACTIVE,
        allocation=allocation(),
        latest_bar=make_bars(["20000"])[0],
        instrument=BTC,
        position_quantity=Decimal("0"),
    )
    values.update(overrides)
    return RiskContext(**values)


def _verdict(settings, *, intent=None, ctx=None, config=None):
    manager = RiskManager(replace(settings.risk, **config) if config else settings.risk)
    order = make_intent(**{**BASE_INTENT, **(intent or {})})
    context_overrides = dict(ctx or {})
    if context_overrides.pop("self_fingerprint", False):
        context_overrides["recent_fingerprints"] = frozenset({order.fingerprint})
    return manager.evaluate(order, _risk_ctx(**context_overrides))


def test_baseline_intent_passes_every_check(settings):
    verdict = _verdict(settings)

    assert [check.name for check in verdict.checks] == list(CHECK_ORDER)
    assert verdict.approved, verdict.reasons
    assert verdict.failed_checks == ()
    assert verdict.available_before == Decimal("100")
    assert verdict.reasons == ()


def test_positive_notional_fires_when_contract_validation_is_bypassed(settings):
    # OrderIntent.__post_init__ rejects qty<=0 / notional<=0, so in normal
    # operation this check cannot fire. It exists as a second line of defence
    # and is proven to be wired by bypassing that validation on purpose.
    intent = make_intent(**BASE_INTENT)
    object.__setattr__(intent, "notional", Decimal("0"))

    verdict = RiskManager(settings.risk).evaluate(intent, _risk_ctx())

    assert verdict.failed_checks == ("positive_notional",)


LIMIT_CASES = [
    pytest.param(
        "system_state",
        {"ctx": {"system_state": SystemState.PAUSE_ENTRIES}},
        id="entries-paused",
    ),
    pytest.param(
        "symbol_allowlist",
        {"intent": {"symbol": "ETH/USD"}},
        id="symbol-not-allowlisted",
    ),
    pytest.param(
        "instrument_available",
        {"ctx": {"instrument": replace(BTC, tradable=False)}},
        id="instrument-not-tradable",
    ),
    pytest.param(
        "data_freshness",
        {"ctx": {"now": RISK_NOW + timedelta(hours=3)}},
        id="stale-candle",
    ),
    pytest.param(
        "quantity_precision",
        {"intent": {"quantity": "0.00105"}},
        id="qty-wrong-increment",
    ),
    pytest.param(
        "max_position_value",
        {
            "intent": {"notional": "30"},
            "config": {"per_trade_risk_pct": Decimal("100")},
        },
        id="above-position-cap",
    ),
    pytest.param(
        "per_trade_risk",
        {"intent": {"notional": "22"}},
        id="above-per-trade-cap",
    ),
    pytest.param(
        "aggregate_allocation",
        {"ctx": {"allocation": allocation(committed="85")}},
        id="ceiling-would-be-breached",
    ),
    pytest.param(
        "daily_loss_limit",
        {"ctx": {"realized_pnl_today": Decimal("-6")}},
        id="daily-loss-breached",
    ),
    pytest.param(
        "max_drawdown",
        {"ctx": {"peak_equity": Decimal("100"), "current_equity": Decimal("80")}},
        id="drawdown-breached",
    ),
    pytest.param(
        "duplicate_intent",
        {"ctx": {"self_fingerprint": True}},
        id="duplicate-fingerprint",
    ),
    pytest.param(
        "order_rate_limit",
        {"ctx": {"orders_today": 6}},
        id="daily-order-cap",
    ),
    pytest.param(
        "sell_within_position",
        {"intent": {"side": Side.SELL}, "ctx": {"position_quantity": Decimal("0")}},
        id="sell-without-position",
    ),
    pytest.param(
        "spread",
        {"ctx": {"spread_bps": Decimal("80")}},
        id="spread-too-wide",
    ),
]


@pytest.mark.parametrize("expected,overrides", LIMIT_CASES)
def test_each_limit_blocks_on_its_own(settings, expected, overrides):
    verdict = _verdict(settings, **overrides)

    assert not verdict.approved
    # Exactly one check may fail: a second failure means the case is not
    # isolating the limit it claims to test.
    assert verdict.failed_checks == (expected,)
    assert any(reason.startswith(f"{expected}:") for reason in verdict.reasons)


def test_a_sell_needs_no_allocation(settings):
    verdict = _verdict(
        settings,
        intent={"side": Side.SELL},
        ctx={"position_quantity": Decimal("0.002"), "allocation": allocation(committed="95")},
    )
    assert verdict.approved, verdict.reasons


def test_rejection_reports_the_available_allocation_before_the_order(settings):
    verdict = _verdict(settings, ctx={"allocation": allocation(committed="85")})
    assert verdict.available_before == Decimal("15")
    assert "ceiling=100" in " ".join(verdict.reasons)


def test_limits_summary_matches_the_shipped_configuration(settings):
    summary = RiskManager(settings.risk).limits_summary(Decimal("100"))
    assert summary["max_position_value"] == "25"
    assert summary["per_trade_cap"] == "20"
    assert summary["daily_loss_cap"] == "5"
    assert summary["max_orders_per_day"] == "6"
    assert summary["allowlist"] == "BTC/USD"


def test_entry_gate_is_clear_for_a_healthy_context(settings):
    manager = RiskManager(settings.risk)
    assert manager.entry_gate_reasons(_risk_ctx()) == ()


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"system_state": SystemState.HALTED}, "system state is halted"),
        ({"allocation": allocation(committed="100")}, "no allocation available"),
        ({"realized_pnl_today": Decimal("-6")}, "daily loss limit reached"),
        (
            {"peak_equity": Decimal("100"), "current_equity": Decimal("80")},
            "drawdown limit reached",
        ),
        ({"orders_today": 6}, "daily order cap reached"),
    ],
    ids=["halted", "no-allocation", "daily-loss", "drawdown", "order-cap"],
)
def test_entry_gate_names_its_reason(settings, overrides, fragment):
    reasons = RiskManager(settings.risk).entry_gate_reasons(_risk_ctx(**overrides))
    assert any(fragment in reason for reason in reasons)
