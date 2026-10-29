"""Alpaca cryptocurrency market data.

Endpoints used (verified against the official docs, see docs/API_CAPABILITIES.md):

* ``GET {data}/v1beta3/crypto/{loc}/bars`` with ``symbols``, ``timeframe``,
  ``start``, ``end``, ``limit``, ``page_token``.
* ``GET {paper}/v2/assets?asset_class=crypto`` for per-asset precision.

Assets are fetched from the *paper* trading host rather than the live host, so
no request in this project ever targets a live endpoint. Asset metadata is
read-only and identical on both hosts.

Why a hand-written REST client rather than ``alpaca-py``'s
``CryptoHistoricalDataClient``: bars are a trivial, fully documented GET, and
implementing it against the transport seam keeps the data layer testable with
zero third-party dependencies. Order submission (a far riskier surface) does use
``alpaca-py``. See docs/ARCHITECTURE.md, "Dependency boundaries".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from cryptobot.config.settings import AlpacaConfig, Settings
from cryptobot.core import timeframes
from cryptobot.core.contracts import Bar, InstrumentSpec, normalize_symbol
from cryptobot.core.errors import DataError
from cryptobot.core.transport import (
    HttpTransport,
    TransportError,
    UrllibTransport,
    request_with_retry,
)
from cryptobot.data.validation import (
    ValidationReport,
    drop_unclosed,
    validate_raw_bars,
)
from cryptobot.journal.store import Journal

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AlpacaCryptoDataClient:
    """Read-only crypto market data client."""

    def __init__(
        self,
        config: AlpacaConfig,
        *,
        transport: HttpTransport | None = None,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._transport = transport or UrllibTransport()
        self._env = env
        self._sleep = sleep
        self._clock = clock

    # -- auth ---------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        key, secret = self._config.credentials(self._env)
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _get(self, url: str, params: Mapping[str, Any]) -> Any:
        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        response = request_with_retry(
            self._transport,
            "GET",
            url,
            headers=self._headers(),
            params=params,
            timeout=self._config.request_timeout_seconds,
            max_retries=self._config.max_retries,
            **kwargs,
        )
        return response.json()

    # -- bars ---------------------------------------------------------------

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        page_limit: int = 1000,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch raw bar payloads, following ``next_page_token`` pagination."""
        symbol = normalize_symbol(symbol)
        timeframe = timeframes.canonical(timeframe)
        url = f"{self._config.data_base_url}{self._config.bars_path}"

        collected: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(max_pages):
            payload = self._get(
                url,
                {
                    "symbols": symbol,
                    "timeframe": timeframes.to_api(timeframe),
                    "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "limit": page_limit,
                    "page_token": token,
                },
            )
            self._raise_if_error(payload)
            chunk = payload.get("bars") or {}
            if isinstance(chunk, Mapping):
                collected.extend(chunk.get(symbol, []) or [])
            elif isinstance(chunk, Sequence):
                collected.extend(chunk)
            token = payload.get("next_page_token")
            if not token:
                break
        return collected

    @staticmethod
    def _raise_if_error(payload: Any) -> None:
        if isinstance(payload, Mapping) and payload.get("message") and not payload.get("bars"):
            raise DataError(f"Alpaca data API error: {payload.get('message')}")

    # -- assets -------------------------------------------------------------

    def list_crypto_assets(self) -> list[InstrumentSpec]:
        url = f"{self._config.trading_base_url}{self._config.assets_path}"
        payload = self._get(url, {"asset_class": "crypto", "status": "active"})
        if not isinstance(payload, Sequence):
            raise DataError(f"unexpected assets payload: {type(payload).__name__}")
        specs: list[InstrumentSpec] = []
        for asset in payload:
            if not isinstance(asset, Mapping):
                continue
            if asset.get("class") and asset["class"] != "crypto":
                continue
            if not asset.get("symbol"):
                continue
            try:
                specs.append(InstrumentSpec.from_asset(asset))
            except Exception:  # malformed single entry must not poison the list
                continue
        return specs

    def get_instrument(self, symbol: str) -> InstrumentSpec:
        symbol = normalize_symbol(symbol)
        for spec in self.list_crypto_assets():
            if spec.symbol == symbol:
                if not spec.tradable:
                    raise DataError(f"{symbol} is not currently tradable on Alpaca")
                return spec
        raise DataError(f"{symbol} is not listed as an active Alpaca crypto pair")


@dataclass(frozen=True, slots=True)
class RefreshResult:
    symbol: str
    timeframe: str
    fetched: int
    stored: int
    report: ValidationReport
    closed: tuple[Bar, ...] = ()

    @property
    def latest(self) -> Bar | None:
        """The newest *closed* bar.

        ``report.accepted`` deliberately keeps every bar the API returned; the
        last of those may still be forming, so it is never offered as "latest".
        """
        return self.closed[-1] if self.closed else None

    def summary(self) -> str:
        return f"{self.report.summary()} (fetched {self.fetched}, stored {self.stored})"


class MarketDataService:
    """Fetches, validates, caches, and serves bars.

    The journal is the cache: every bar that informed a decision is stored, which
    is what makes a decision reproducible after the fact.
    """

    def __init__(
        self,
        settings: Settings,
        journal: Journal,
        client: AlpacaCryptoDataClient | None = None,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._journal = journal
        self._client = client
        self._clock = clock

    @property
    def timeframe(self) -> str:
        return self._settings.market.timeframe

    def refresh(self, symbol: str, *, lookback_bars: int | None = None) -> RefreshResult:
        if self._client is None:
            raise DataError(
                "no market data client configured; construct MarketDataService with "
                "an AlpacaCryptoDataClient (requires credentials)"
            )
        symbol = normalize_symbol(symbol)
        tf = self.timeframe
        lookback = lookback_bars or self._settings.market.lookback_bars
        now = self._clock()
        start = now - timeframes.duration(tf) * (lookback + 2)

        raw = self._client.fetch_bars(symbol, tf, start, now)
        report = validate_raw_bars(raw, symbol, tf)
        closed = tuple(drop_unclosed(report.accepted, now))
        stored = self._journal.save_bars(closed)
        return RefreshResult(symbol, tf, len(raw), stored, report, closed)

    def ensure_history(self, symbol: str) -> list[Bar]:
        """Return cached history, refreshing once if the cache is empty."""
        bars = self.history(symbol, self._settings.market.lookback_bars)
        if not bars:
            self.refresh(symbol)
            bars = self.history(symbol, self._settings.market.lookback_bars)
        return bars

    def history(self, symbol: str, limit: int | None = None) -> list[Bar]:
        """Closed bars only.

        Any bar whose interval has not finished is dropped here, once, for every
        caller. A strategy must never be able to see a candle that is still
        forming.
        """
        wanted = limit or self._settings.market.lookback_bars
        cached = self._journal.recent_bars(
            normalize_symbol(symbol), self.timeframe, wanted + 2
        )
        closed = drop_unclosed(cached, self._clock())
        return closed[-wanted:]

    def latest_closed(self, symbol: str) -> Bar | None:
        """The newest candle whose interval has finished.

        Walks back over a few candidates instead of inspecting only the newest
        row: for most of every bucket the newest candle is still forming, and
        returning ``None`` then would leave position pricing unmarked.
        """
        recent = self._journal.recent_bars(normalize_symbol(symbol), self.timeframe, 4)
        closed = drop_unclosed(recent, self._clock())
        return closed[-1] if closed else None

    def instrument(self, symbol: str, *, refresh: bool = False) -> InstrumentSpec | None:
        symbol = normalize_symbol(symbol)
        if not refresh:
            cached = self._journal.get_instrument(symbol)
            if cached is not None:
                return cached
        if self._client is None:
            return None
        spec = self._client.get_instrument(symbol)
        self._journal.save_instrument(spec)
        return spec


def build_data_client(
    settings: Settings, *, transport: HttpTransport | None = None
) -> AlpacaCryptoDataClient:
    return AlpacaCryptoDataClient(settings.alpaca, transport=transport, env=os.environ)
