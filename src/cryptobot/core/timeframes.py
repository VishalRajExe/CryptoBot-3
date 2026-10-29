"""Timeframe parsing, normalisation, and bucketing.

Alpaca crypto bars accept a restricted set of timeframe strings
(``[1-59]Min``, ``[1-23]Hour``, ``1Day``, ``1Week``, plus ``M``/``H``/``T``/``D``/``W``
short forms - verified in docs/API_CAPABILITIES.md). Internally we use a single
canonical lowercase form (``1m``, ``1h``, ``1d``, ``1w``) so that configuration,
SQLite keys, and strategy parameters all agree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptobot.core.errors import ContractError

UTC = timezone.utc

# canonical -> (amount, unit)
_SPECS: dict[str, tuple[int, str]] = {
    "1m": (1, "m"),
    "5m": (5, "m"),
    "15m": (15, "m"),
    "30m": (30, "m"),
    "1h": (1, "h"),
    "2h": (2, "h"),
    "4h": (4, "h"),
    "6h": (6, "h"),
    "12h": (12, "h"),
    "1d": (1, "d"),
    "1w": (1, "w"),
}

_API_SUFFIX = {"m": "Min", "h": "Hour", "d": "Day", "w": "Week"}

_ALIASES: dict[str, str] = {}
for _canon, (_amount, _unit) in _SPECS.items():
    _ALIASES[_canon] = _canon
    _ALIASES[f"{_amount}{_API_SUFFIX[_unit]}"] = _canon
    _ALIASES[f"{_amount}{_API_SUFFIX[_unit].lower()}"] = _canon
for _canon, _suffix in (("1m", "T"), ("1h", "H"), ("1d", "D"), ("1w", "W")):
    _ALIASES[f"1{_suffix.lower()}"] = _canon


def canonical(timeframe: str) -> str:
    """Return the canonical form of ``timeframe`` or raise :class:`ContractError`."""
    key = str(timeframe).strip().lower()
    try:
        return _ALIASES[key]
    except KeyError:
        raise ContractError(
            f"unsupported timeframe {timeframe!r}; supported: {sorted(_SPECS)}"
        ) from None


def spec(timeframe: str) -> tuple[int, str]:
    return _SPECS[canonical(timeframe)]


def duration(timeframe: str) -> timedelta:
    amount, unit = spec(timeframe)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    return timedelta(weeks=amount)


def to_api(timeframe: str) -> str:
    """Render the timeframe the way Alpaca's REST API expects it."""
    amount, unit = spec(timeframe)
    return f"{amount}{_API_SUFFIX[unit]}"


def bucket_start(ts: datetime, timeframe: str) -> datetime:
    """Floor ``ts`` to the start of its candle bucket."""
    if ts.tzinfo is None:
        raise ContractError("bucket_start requires a timezone-aware datetime")
    ts = ts.astimezone(UTC)
    amount, unit = spec(timeframe)
    if unit == "m":
        return ts.replace(minute=ts.minute - ts.minute % amount, second=0, microsecond=0)
    if unit == "h":
        return ts.replace(hour=ts.hour - ts.hour % amount, minute=0, second=0, microsecond=0)
    if unit == "d":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


def bucket_end(ts: datetime, timeframe: str) -> datetime:
    """The exclusive end of the bucket that starts at ``bucket_start(ts)``."""
    return bucket_start(ts, timeframe) + duration(timeframe)


def is_aligned(ts: datetime, timeframe: str) -> bool:
    return bucket_start(ts, timeframe) == ts.astimezone(UTC)


def expected_buckets(start: datetime, end: datetime, timeframe: str) -> list[datetime]:
    """All bucket starts in ``[start, end)``. Used for gap detection."""
    step = duration(timeframe)
    cursor = bucket_start(start, timeframe)
    end = end.astimezone(UTC)
    out: list[datetime] = []
    while cursor < end:
        out.append(cursor)
        cursor += step
    return out
