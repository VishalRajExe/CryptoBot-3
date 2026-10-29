"""Market data validation.

Rule: data that cannot be trusted is *rejected*, not repaired. A candle with an
impossible high/low, a non-positive price, a misaligned timestamp, or a duplicate
open time never reaches the strategy.

Note on volume: Alpaca documents that crypto bars are built from quote
mid-prices, so a bar with zero volume is legitimate (no trade occurred in the
window). Zero volume is counted and reported, not rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from cryptobot.core import timeframes
from cryptobot.core.contracts import Bar
from cryptobot.core.errors import DataError, StaleDataError

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class ValidationReport:
    symbol: str
    timeframe: str
    accepted: tuple[Bar, ...] = ()
    rejected: tuple[Mapping[str, Any], ...] = ()
    duplicates: int = 0
    zero_volume: int = 0
    gaps: tuple[tuple[datetime, datetime], ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.accepted) and not self.rejected

    @property
    def quality(self) -> str:
        if not self.accepted:
            return "empty"
        if self.rejected or self.gaps:
            return "degraded"
        return "clean"

    def summary(self) -> str:
        return (
            f"{self.symbol} {self.timeframe}: {len(self.accepted)} accepted, "
            f"{len(self.rejected)} rejected, {self.duplicates} duplicate(s), "
            f"{len(self.gaps)} gap(s), {self.zero_volume} zero-volume"
        )


def _get(raw: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return None


def _parse_ts(value: Any) -> datetime:
    if value is None:
        raise ValueError("bar has no timestamp field (t/timestamp/time)")
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def validate_raw_bars(
    raw_bars: Iterable[Mapping[str, Any]],
    symbol: str,
    timeframe: str,
) -> ValidationReport:
    """Turn raw API payloads into :class:`Bar` objects, rejecting bad ones."""
    timeframe = timeframes.canonical(timeframe)
    accepted: dict[datetime, Bar] = {}
    rejected: list[Mapping[str, Any]] = []
    duplicates = 0
    zero_volume = 0

    for index, raw in enumerate(raw_bars):
        if not isinstance(raw, Mapping):
            rejected.append({"index": index, "reason": "not a mapping"})
            continue
        try:
            ts = _parse_ts(_get(raw, "t", "timestamp", "time"))
            bar = Bar(
                symbol=symbol,
                timeframe=timeframe,
                ts=ts,
                open=_get(raw, "o", "open"),
                high=_get(raw, "h", "high"),
                low=_get(raw, "l", "low"),
                close=_get(raw, "c", "close"),
                volume=_get(raw, "v", "volume") if _get(raw, "v", "volume") is not None else 0,
            )
        except Exception as exc:  # ContractError, ValueError, TypeError
            rejected.append({"index": index, "reason": str(exc)})
            continue

        if bar.ts in accepted:
            duplicates += 1
            # Keep the later observation: it is the most complete revision.
            accepted[bar.ts] = bar
            continue
        accepted[bar.ts] = bar
        if bar.volume == 0:
            zero_volume += 1

    ordered = tuple(accepted[key] for key in sorted(accepted))
    return ValidationReport(
        symbol=symbol,
        timeframe=timeframe,
        accepted=ordered,
        rejected=tuple(rejected),
        duplicates=duplicates,
        zero_volume=zero_volume,
        gaps=find_gaps(ordered, timeframe),
    )


def find_gaps(bars: Sequence[Bar], timeframe: str) -> tuple[tuple[datetime, datetime], ...]:
    """Return ``(expected_start, missing_until)`` pairs for missing candles."""
    if len(bars) < 2:
        return ()
    step = timeframes.duration(timeframe)
    gaps: list[tuple[datetime, datetime]] = []
    for previous, current in zip(bars, bars[1:]):
        expected = previous.ts + step
        if current.ts > expected:
            gaps.append((expected, current.ts))
    return tuple(gaps)


def detect_lookahead(bars: Sequence[Bar], now: datetime) -> tuple[Bar, ...]:
    """Bars whose close time is in the future cannot be used for decisions."""
    return tuple(bar for bar in bars if bar.closes_at > now)


def drop_unclosed(bars: Sequence[Bar], now: datetime) -> list[Bar]:
    """Keep only candles that have fully closed at ``now``."""
    return [bar for bar in bars if bar.is_closed(now)]


def assert_fresh(latest: Bar, now: datetime, max_age_seconds: int) -> None:
    """Raise if the newest closed bar is older than the configured tolerance."""
    age = latest.age_seconds(now)
    if age > max_age_seconds:
        raise StaleDataError(
            f"{latest.symbol} {latest.timeframe}: newest closed bar {latest.ts.isoformat()} "
            f"is {age:.0f}s old, exceeding max_bar_age_seconds={max_age_seconds}"
        )


def assert_sufficient(bars: Sequence[Bar], required: int) -> None:
    if len(bars) < required:
        raise DataError(f"need {required} bars, have {len(bars)}")
