"""Reproducible datasets for research.

A dataset is an immutable, chronologically ordered tuple of bars plus the
metadata needed to reproduce it later: symbol, timeframe, and a fingerprint that
is stable across runs. Three sources are supported:

* the journal (bars the live system actually saw),
* a JSON or CSV file (checked-in fixtures),
* a deterministic synthetic generator (offline experiments with no credentials).

The synthetic generator is deliberately seeded and documented as synthetic. It
must never be presented as market data.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from cryptobot.core import timeframes
from cryptobot.core.contracts import Bar, normalize_symbol
from cryptobot.core.errors import ContractError, DataError
from cryptobot.journal.store import Journal

UTC = timezone.utc
DEFAULT_START = datetime(2024, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class BarDataset:
    symbol: str
    timeframe: str
    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        if not self.bars:
            raise DataError("dataset is empty")
        previous: datetime | None = None
        for bar in self.bars:
            if bar.symbol != self.symbol:
                raise DataError(
                    f"mixed symbols in dataset: {bar.symbol} != {self.symbol}"
                )
            if bar.timeframe != self.timeframe:
                raise DataError(
                    f"mixed timeframes in dataset: {bar.timeframe} != {self.timeframe}"
                )
            if previous is not None and bar.ts <= previous:
                raise DataError(
                    f"bars must be strictly increasing: {bar.ts.isoformat()} after "
                    f"{previous.isoformat()}"
                )
            previous = bar.ts

    # -- metadata -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def start(self) -> datetime:
        return self.bars[0].ts

    @property
    def end(self) -> datetime:
        return self.bars[-1].closes_at

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(f"{self.symbol}|{self.timeframe}|".encode())
        for bar in self.bars:
            digest.update(
                f"{bar.ts.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}\n".encode()
            )
        return digest.hexdigest()[:32]

    def describe(self) -> str:
        return (
            f"{self.symbol} {self.timeframe} {len(self.bars)} bars "
            f"{self.start.isoformat()} -> {self.end.isoformat()} fp={self.fingerprint}"
        )

    # -- construction -------------------------------------------------------

    @classmethod
    def from_journal(
        cls,
        journal: Journal,
        symbol: str,
        timeframe: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> "BarDataset":
        symbol = normalize_symbol(symbol)
        tf = timeframes.canonical(timeframe)
        if start is not None or end is not None:
            lo = start or datetime(1970, 1, 1, tzinfo=UTC)
            hi = end or datetime(2100, 1, 1, tzinfo=UTC)
            bars = journal.bars_between(symbol, tf, lo, hi)
        else:
            bars = journal.recent_bars(symbol, tf, limit or 1000)
        if not bars:
            raise DataError(
                f"no cached bars for {symbol} {tf}; run `cryptobot data-refresh` first"
            )
        return cls(symbol, tf, tuple(bars))

    @classmethod
    def from_file(
        cls, path: str | Path, *, symbol: str | None = None, timeframe: str | None = None
    ) -> "BarDataset":
        path = Path(path)
        if not path.exists():
            raise DataError(f"dataset not found: {path}")
        if path.suffix.lower() == ".csv":
            return cls.from_csv(path, symbol=symbol, timeframe=timeframe)
        return cls.from_json(path)

    @classmethod
    def from_json(cls, path: str | Path) -> "BarDataset":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            symbol = payload.get("symbol")
            timeframe = payload.get("timeframe")
            rows = payload.get("bars", [])
        else:
            rows = payload
            symbol = None
            timeframe = None
        if not rows:
            raise DataError(f"{path}: no bars in file")
        first = rows[0]
        symbol = symbol or first.get("symbol")
        timeframe = timeframe or first.get("timeframe")
        bars = tuple(
            Bar.from_row({**row, "symbol": symbol, "timeframe": timeframe}) for row in rows
        )
        return cls(normalize_symbol(symbol), timeframes.canonical(timeframe), bars)

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> "BarDataset":
        rows: list[Bar] = []
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"ts", "open", "high", "low", "close"}
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise DataError(f"{path}: missing columns {sorted(missing)}")
            for row in reader:
                rows.append(
                    Bar(
                        symbol=row.get("symbol") or symbol or "BTC/USD",
                        timeframe=row.get("timeframe") or timeframe or "1h",
                        ts=datetime.fromisoformat(row["ts"]),
                        open=row["open"],
                        high=row["high"],
                        low=row["low"],
                        close=row["close"],
                        volume=row.get("volume") or "0",
                    )
                )
        if not rows:
            raise DataError(f"{path}: no rows")
        return cls(rows[0].symbol, rows[0].timeframe, tuple(rows))

    # -- splits -------------------------------------------------------------

    def slice(self, start: datetime | None, end: datetime | None) -> "BarDataset":
        selected = tuple(
            b
            for b in self.bars
            if (start is None or b.ts >= start) and (end is None or b.ts < end)
        )
        if not selected:
            raise DataError("slice produced an empty dataset")
        return BarDataset(self.symbol, self.timeframe, selected)

    def train_test(self, *, train_fraction: float = 0.7) -> tuple["BarDataset", "BarDataset"]:
        if not 0 < train_fraction < 1:
            raise ContractError("train_fraction must be between 0 and 1")
        cut = max(1, min(len(self.bars) - 1, int(len(self.bars) * train_fraction)))
        return (
            BarDataset(self.symbol, self.timeframe, self.bars[:cut]),
            BarDataset(self.symbol, self.timeframe, self.bars[cut:]),
        )

    def walk_forward(
        self, *, train_bars: int, test_bars: int, step: int | None = None
    ) -> list[tuple["BarDataset", "BarDataset"]]:
        """Chronological folds; test data never precedes its training window."""
        if train_bars < 1 or test_bars < 1:
            raise ContractError("train_bars and test_bars must be >= 1")
        step = step or test_bars
        if step < 1:
            raise ContractError("step must be >= 1")
        folds: list[tuple[BarDataset, BarDataset]] = []
        index = 0
        while index + train_bars + test_bars <= len(self.bars):
            train = BarDataset(
                self.symbol, self.timeframe, self.bars[index : index + train_bars]
            )
            test = BarDataset(
                self.symbol,
                self.timeframe,
                self.bars[index + train_bars : index + train_bars + test_bars],
            )
            folds.append((train, test))
            index += step
        return folds


# ---------------------------------------------------------------------------
# synthetic data (explicitly synthetic)
# ---------------------------------------------------------------------------


def synthetic_bars(
    *,
    symbol: str = "BTC/USD",
    timeframe: str = "1h",
    count: int = 600,
    seed: int = 7,
    start_price: Decimal = Decimal("60000"),
    drift_per_bar: float = 0.00002,
    vol_per_bar: float = 0.004,
    start: datetime = DEFAULT_START,
    volume_base: Decimal = Decimal("12"),
) -> list[Bar]:
    """Deterministic log-random-walk bars.

    These are **synthetic**. They exist so that the strategy, risk, execution,
    and reporting paths can be exercised end-to-end with zero credentials and
    without any claim about real market behaviour.
    """
    if count < 2:
        raise ContractError("count must be >= 2")
    step = timeframes.duration(timeframes.canonical(timeframe))
    rng = random.Random(seed)
    price = float(start_price)
    bars: list[Bar] = []
    for i in range(count):
        ts = start + step * i
        open_price = price
        shock = rng.gauss(0.0, 1.0)
        close_price = open_price * math.exp(drift_per_bar + vol_per_bar * shock)
        wick_up = abs(rng.gauss(0.0, 1.0)) * vol_per_bar * 0.6
        wick_down = abs(rng.gauss(0.0, 1.0)) * vol_per_bar * 0.6
        high = max(open_price, close_price) * (1.0 + wick_up)
        low = min(open_price, close_price) * (1.0 - wick_down)
        volume = float(volume_base) * (0.4 + abs(rng.gauss(0.0, 1.0)))
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                ts=ts,
                open=_q(open_price),
                high=_q(high),
                low=_q(low),
                close=_q(close_price),
                volume=_q(volume, places=8),
            )
        )
        price = close_price
    return bars


def _q(value: float, places: int = 2) -> Decimal:
    return Decimal(f"{value:.{places}f}")


def synthetic_dataset(**kwargs: object) -> BarDataset:
    symbol = str(kwargs.pop("symbol", "BTC/USD"))
    timeframe = str(kwargs.pop("timeframe", "1h"))
    bars = synthetic_bars(symbol=symbol, timeframe=timeframe, **kwargs)  # type: ignore[arg-type]
    return BarDataset(normalize_symbol(symbol), timeframes.canonical(timeframe), tuple(bars))


def write_json(dataset: BarDataset, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "symbol": dataset.symbol,
                "timeframe": dataset.timeframe,
                "fingerprint": dataset.fingerprint,
                "bars": [bar.to_row() for bar in dataset.bars],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def write_csv(dataset: BarDataset, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["symbol", "timeframe", "ts", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for bar in dataset.bars:
            writer.writerow(bar.to_row())
    return path


def iter_windows(
    bars: Sequence[Bar], size: int, step: int | None = None
) -> Iterator[Sequence[Bar]]:
    step = step or size
    for index in range(0, max(0, len(bars) - size) + 1, step):
        yield bars[index : index + size]


def gaps(bars: Iterable[Bar], timeframe: str) -> list[tuple[datetime, datetime]]:
    """Return ``(expected, found)`` pairs where an interval is missing."""
    expected = timeframes.duration(timeframes.canonical(timeframe))
    out: list[tuple[datetime, datetime]] = []
    previous: datetime | None = None
    for bar in bars:
        if previous is not None:
            delta = bar.ts - previous
            if delta != expected:
                out.append((previous + expected, bar.ts))
        previous = bar.ts
    return out
