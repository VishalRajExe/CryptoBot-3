"""Shared fixtures.

Every fixture is offline: no test in this suite contacts Alpaca, Telegram, or a
model provider. The suite must pass with no credentials set.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

import pytest

from cryptobot.config.settings import Settings, load_settings
from cryptobot.core.contracts import (
    AllocationState,
    Bar,
    InstrumentSpec,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
)
from cryptobot.core.transport import HttpResponse
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)

BTC = InstrumentSpec(
    symbol="BTC/USD",
    min_order_size="0.0001",
    min_trade_increment="0.0001",
    price_increment="1",
)


class Clock:
    """A clock the test owns."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or (START + timedelta(days=400))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path) -> Settings:
    base = load_settings()
    return replace(base, journal=replace(base.journal, path=str(tmp_path / "cryptobot.db")))


@pytest.fixture
def journal(settings: Settings, clock: Clock):
    store = Journal(settings.database_path(), clock=clock)
    store.initialize()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def ledger(journal: Journal) -> AllocationLedger:
    book = AllocationLedger(journal, Decimal("100"))
    book.rebuild()
    return book


@pytest.fixture
def soak(journal: Journal, clock: "Clock"):
    """Write soak evidence: one window, one process, regularly spaced heartbeats.

    The clock is advanced between heartbeats rather than passed in, because that
    is what a real run looks like: the only thing that moves is time.
    """
    from cryptobot.soak import SoakRecorder

    def _instrument(
        *,
        days: int = 30,
        interval_hours: float = 24.0,
        start: datetime = START,
        api_errors: int = 0,
        close: bool = True,
    ) -> str:
        clock.now = start
        recorder = SoakRecorder(
            journal, interval_seconds=Decimal(str(interval_hours * 3600))
        )
        window_id = recorder.start()
        for index in range(api_errors):
            recorder.record_api_error(f"synthetic api error {index}")
        step = timedelta(hours=interval_hours)
        recorder.checkpoint(cycles=0)
        for index in range(1, days + 1):
            clock.now = start + step * index
            recorder.checkpoint(cycles=index)
        if close:
            recorder.close("instrumented")
        return window_id

    return _instrument


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def make_bars(
    prices: Sequence[float | str],
    *,
    symbol: str = "BTC/USD",
    timeframe: str = "1h",
    start: datetime = START,
    volume: str = "10",
) -> list[Bar]:
    """Bars from closes; OHLC is derived so the series is always valid."""
    step = timedelta(hours=1)
    bars: list[Bar] = []
    previous = Decimal(str(prices[0]))
    for index, raw in enumerate(prices):
        close = Decimal(str(raw))
        high = max(previous, close) * Decimal("1.001")
        low = min(previous, close) * Decimal("0.999")
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                ts=start + step * index,
                open=previous,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
        previous = close
    return bars


def wave_bars(count: int = 240, *, base: float = 100.0, amplitude: float = 8.0,
              period: float = 24.0) -> list[Bar]:
    """A deterministic oscillation: guarantees SMA crossovers in both directions."""
    import math

    prices = [base + amplitude * math.sin(index / period * 2 * math.pi) for index in range(count)]
    return make_bars(prices)


def make_intent(
    *,
    side: Side = Side.BUY,
    quantity: str = "0.001",
    notional: str = "60",
    client_order_id: str = "cb-test-intent-0001",
    symbol: str = "BTC/USD",
    bar_ts: datetime | None = None,
    ts: datetime | None = None,
    strategy_name: str = "sma_cross",
    strategy_version: str = "sma_cross@test",
    order_type: OrderType = OrderType.MARKET,
    limit_price: str | None = None,
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        notional=notional,
        time_in_force=time_in_force,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        bar_ts=bar_ts or START,
        ts=ts or START,
        limit_price=Decimal(limit_price) if limit_price is not None else None,
    )


def allocation(
    *,
    ceiling: str = "100",
    committed: str = "0",
    reserved: str = "0",
    realized: str = "0",
) -> AllocationState:
    return AllocationState(
        ceiling=Decimal(ceiling),
        committed=Decimal(committed),
        reserved=Decimal(reserved),
        realized_pnl=Decimal(realized),
    )


# ---------------------------------------------------------------------------
# fake transport
# ---------------------------------------------------------------------------


class FakeTransport:
    """Replays queued responses and records every call."""

    def __init__(self, responses: Sequence[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def push(self, *items: Any) -> None:
        self.responses.extend(items)

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:  # noqa: D401
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request to {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(method, url, **kwargs)
        return item


def json_response(payload: Any, status: int = 200, headers: dict[str, str] | None = None):
    import json

    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers=headers or {"Content-Type": "application/json"},
    )
