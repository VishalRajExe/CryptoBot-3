"""Soak evidence: proof that the bot was running, not just that it once started.

A thirty-day paper soak is only meaningful if the thirty days are evidenced.
"First decision was thirty days ago" is not evidence: the process may have been
dead for twenty-nine of those days, and a journal that only records activity
cannot tell the difference. This module therefore measures duration from
heartbeats.

Three kinds of row are written while the loop runs:

``soak_windows``
    The interval under test. It survives restarts, so a resumed process
    continues the same interval instead of starting a fresh clock.

``soak_processes``
    One run of the bot inside the window. Two rows mean the bot was restarted;
    anything the restart lost stays lost, and stays visible.

``soak_checkpoints``
    A heartbeat per loop cycle: uptime, cycles, decisions, orders, fills and
    API errors at that moment.

Coverage is the sum of the intervals between consecutive checkpoints that are
close enough together to be continuous. A gap longer than
``interval_seconds * max_gap_factor`` contributes **zero** to coverage, so an
outage is never billed as operation. Nothing here backfills a missed heartbeat:
inventing history is the one thing a soak must never do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from cryptobot.core.contracts import to_decimal
from cryptobot.core.logging import get_logger
from cryptobot.journal.store import Journal

log = get_logger(__name__)
UTC = timezone.utc
ZERO = Decimal("0")
SECONDS_PER_DAY = Decimal("86400")
DEFAULT_INTERVAL_SECONDS = Decimal("60")
#: A checkpoint three heartbeat intervals late is a gap, not jitter.
DEFAULT_MAX_GAP_FACTOR = Decimal("3")


def _seconds(delta: timedelta) -> Decimal:
    return Decimal(str(delta.total_seconds()))


def _days(seconds: Decimal) -> Decimal:
    return seconds / SECONDS_PER_DAY


@dataclass(frozen=True)
class Gap:
    """An interval with no heartbeat evidence, or too sparse to trust."""

    start: datetime
    end: datetime

    @property
    def seconds(self) -> Decimal:
        return _seconds(self.end - self.start)

    def line(self) -> str:
        hours = self.seconds / Decimal("3600")
        return (
            f"  gap {self.start.isoformat()} -> {self.end.isoformat()} "
            f"({hours:.1f}h without a heartbeat)"
        )


@dataclass(frozen=True)
class SoakCoverage:
    """What the journals can prove about how long the bot actually ran."""

    window_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    interval_seconds: Decimal = DEFAULT_INTERVAL_SECONDS
    gap_threshold_seconds: Decimal = DEFAULT_INTERVAL_SECONDS * DEFAULT_MAX_GAP_FACTOR
    checkpoints: int = 0
    processes: int = 0
    covered_seconds: Decimal = ZERO
    observed_seconds: Decimal = ZERO
    api_errors: int = 0
    last_checkpoint: datetime | None = None
    gaps: tuple[Gap, ...] = ()

    @property
    def open(self) -> bool:
        return self.window_id is not None and self.ended_at is None

    @property
    def instrumented(self) -> bool:
        """Two heartbeats are the minimum that can show one interval of life."""
        return self.checkpoints >= 2

    @property
    def restarts(self) -> int:
        return max(0, self.processes - 1)

    @property
    def covered_days(self) -> Decimal:
        return _days(self.covered_seconds)

    @property
    def observed_days(self) -> Decimal:
        return _days(self.observed_seconds)

    @property
    def unobserved_seconds(self) -> Decimal:
        return max(ZERO, self.observed_seconds - self.covered_seconds)

    def summary(self) -> str:
        if self.window_id is None:
            return "soak: no window recorded; elapsed time cannot be evidenced"
        return (
            f"soak {self.window_id} started {self.started_at.isoformat()} "
            f"({'open' if self.open else 'closed'}) "
            f"covered={self.covered_days:.2f}d of observed={self.observed_days:.2f}d "
            f"heartbeats={self.checkpoints} (threshold {self.gap_threshold_seconds}s) "
            f"processes={self.processes} restarts={self.restarts} "
            f"gaps={len(self.gaps)} api_errors={self.api_errors}"
        )

    def describe(self) -> str:
        lines = [self.summary()]
        if not self.instrumented:
            lines.append(
                "  not instrumented: fewer than two heartbeats, so no interval "
                "of operation is evidenced"
            )
        for gap in self.gaps:
            lines.append(gap.line())
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "open": self.open,
            "instrumented": self.instrumented,
            "interval_seconds": str(self.interval_seconds),
            "gap_threshold_seconds": str(self.gap_threshold_seconds),
            "checkpoints": self.checkpoints,
            "processes": self.processes,
            "restarts": self.restarts,
            "covered_days": str(self.covered_days.quantize(Decimal("0.01"))),
            "observed_days": str(self.observed_days.quantize(Decimal("0.01"))),
            "unobserved_seconds": str(self.unobserved_seconds.quantize(Decimal("1"))),
            "api_errors": self.api_errors,
            "last_checkpoint": (
                self.last_checkpoint.isoformat() if self.last_checkpoint else None
            ),
            "gaps": [
                {
                    "start": gap.start.isoformat(),
                    "end": gap.end.isoformat(),
                    "seconds": str(gap.seconds.quantize(Decimal("1"))),
                }
                for gap in self.gaps
            ],
        }


def compute_coverage(
    journal: Journal,
    *,
    now: datetime | None = None,
    max_gap_factor: Decimal | float = DEFAULT_MAX_GAP_FACTOR,
) -> SoakCoverage:
    """Measure coverage from the latest soak window's heartbeats."""
    window = journal.soak_window()
    if window is None:
        return SoakCoverage()

    window_id = row_text(window["window_id"])
    checkpoints = journal.soak_checkpoints(window_id)
    processes = journal.soak_processes(window_id)
    started = datetime.fromisoformat(row_text(window["started_at"]))
    ended_raw = window["ended_at"]
    ended = datetime.fromisoformat(row_text(ended_raw)) if ended_raw else None
    horizon = ended or now or datetime.now(UTC)
    interval = to_decimal(window["interval_seconds"], "interval_seconds")
    if interval <= ZERO:
        interval = DEFAULT_INTERVAL_SECONDS
    threshold = interval * to_decimal(max_gap_factor, "max_gap_factor")

    times = [datetime.fromisoformat(row_text(row["ts"])) for row in checkpoints]
    covered = ZERO
    gaps: list[Gap] = []
    for previous, current in zip(times, times[1:]):
        delta = _seconds(current - previous)
        if delta <= threshold:
            covered += delta
        else:
            gaps.append(Gap(previous, current))

    # ``api_errors`` is a per-process counter, so the total is the last value of
    # each process. Summing every heartbeat would multiply one outage by however
    # many times it was observed.
    errors_by_process: dict[str, int] = {}
    for row in checkpoints:
        process = row_text(row["process_id"])
        errors_by_process[process] = max(
            errors_by_process.get(process, 0), int(row["api_errors"])
        )

    observed = _seconds(horizon - started) if horizon > started else ZERO
    return SoakCoverage(
        window_id=window_id,
        started_at=started,
        ended_at=ended,
        interval_seconds=interval,
        gap_threshold_seconds=threshold,
        checkpoints=len(times),
        processes=len(processes),
        # Coverage is a sum of evidenced intervals; it can never exceed the
        # window's own length, however many heartbeats were written.
        covered_seconds=min(covered, observed),
        observed_seconds=observed,
        api_errors=sum(errors_by_process.values()),
        last_checkpoint=times[-1] if times else None,
        gaps=tuple(gaps),
    )


def row_text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


class SoakRecorder:
    """Writes soak evidence as the loop runs.

    Deliberately dumb: it records what the loop did and never invents what it
    missed. If the process dies, the missing heartbeats stay missing and
    ``compute_coverage`` reports them as a gap.
    """

    def __init__(
        self,
        journal: Journal,
        *,
        interval_seconds: Decimal | float = DEFAULT_INTERVAL_SECONDS,
        note: str = "",
    ) -> None:
        self.journal = journal
        self.interval_seconds = to_decimal(interval_seconds, "interval_seconds")
        if self.interval_seconds <= ZERO:
            self.interval_seconds = DEFAULT_INTERVAL_SECONDS
        self.note = note
        self.window_id: str | None = None
        self.process_id: str | None = None
        self._started: datetime | None = None
        self._api_errors = 0

    @property
    def active(self) -> bool:
        return self.window_id is not None and self.process_id is not None

    def start(self) -> str:
        """Join the open window, or open one. Never resets existing evidence."""
        window = self.journal.open_soak_window(
            self.note, interval_seconds=self.interval_seconds
        )
        self.window_id = row_text(window["window_id"])
        self.process_id = self.journal.start_soak_process(self.window_id, self.note)
        self._started = self.journal.now()
        self._api_errors = 0
        self.journal.record_soak_event(
            self.window_id,
            "process_start",
            f"process {self.process_id} started "
            f"(covered_soak_day={compute_coverage(self.journal).covered_days:.2f})",
        )
        log.info(
            "soak process started",
            extra={"context": {"window": self.window_id, "process": self.process_id}},
        )
        return self.window_id

    def checkpoint(self, *, cycles: int = 0, note: str = "") -> None:
        """Record one heartbeat. Silently does nothing before ``start()``.

        Decision, order and fill counts are read back from the journal rather
        than accumulated in memory: the journal is what survives a restart, so
        it is the only count worth reporting.
        """
        if not self.active:
            return
        window_id, process_id = self.window_id, self.process_id
        assert window_id is not None and process_id is not None
        now = self.journal.now()
        decisions, orders, fills = self._window_counts(now)
        started = self._started or now
        self.journal.record_soak_checkpoint(
            window_id,
            process_id,
            uptime_seconds=max(ZERO, _seconds(now - started)),
            cycles=cycles,
            decisions=decisions,
            orders=orders,
            fills=fills,
            api_errors=self._api_errors,
            note=note,
        )

    def _window_counts(self, now: datetime) -> tuple[int, int, int]:
        window = self.journal.soak_window()
        if window is None:
            return 0, 0, 0
        started = datetime.fromisoformat(row_text(window["started_at"]))
        buckets = self.journal.counts_between(started, now + timedelta(seconds=1))
        totals = {"decisions": 0, "orders": 0, "fills": 0}
        for bucket in buckets.values():
            for key in totals:
                totals[key] += int(bucket.get(key, 0))
        return totals["decisions"], totals["orders"], totals["fills"]

    def record_event(self, kind: str, detail: str = "") -> None:
        if not self.active:
            return
        assert self.window_id is not None
        self.journal.record_soak_event(self.window_id, kind, detail)

    def record_api_error(self, detail: str) -> None:
        """Count a broker/API failure. The count is cumulative per process."""
        self._api_errors += 1
        self.record_event("api_error", detail)

    def close(self, reason: str = "") -> None:
        if not self.active:
            return
        assert self.process_id is not None
        self.record_event("process_stop", reason or "stopped")
        self.journal.end_soak_process(self.process_id, reason)
        self.process_id = None


# ---------------------------------------------------------------------------
# daily summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailySummary:
    day: str
    decisions: int = 0
    orders: int = 0
    fills: int = 0
    heartbeats: int = 0
    api_errors: int = 0
    process_starts: int = 0

    @property
    def alive(self) -> bool:
        return self.heartbeats > 0

    def line(self) -> str:
        return (
            f"  {self.day}  decisions={self.decisions:<5} orders={self.orders:<4} "
            f"fills={self.fills:<4} heartbeats={self.heartbeats:<5} "
            f"api_errors={self.api_errors:<4} starts={self.process_starts}"
            + ("" if self.alive else "   NO HEARTBEAT")
        )


def daily_summaries(
    journal: Journal,
    *,
    now: datetime | None = None,
    window: Mapping[str, Any] | None = None,
) -> list[DailySummary]:
    """One row per UTC day, including days with no activity at all.

    Empty days are the interesting ones: they are where a soak stops being a
    soak. They are emitted with zeros rather than omitted.
    """
    window = window if window is not None else journal.soak_window()
    if window is None:
        first, _last = journal.activity_span()
        if first is None:
            return []
        started = first
        horizon = now or datetime.now(UTC)
    else:
        started = datetime.fromisoformat(row_text(window["started_at"]))
        ended_raw = window["ended_at"]
        horizon = (
            datetime.fromisoformat(row_text(ended_raw))
            if ended_raw
            else (now or datetime.now(UTC))
        )
    if horizon < started:
        horizon = started

    window_id = row_text(window["window_id"]) if window is not None else None
    counts = journal.counts_between(started, horizon + timedelta(days=1))
    checkpoints = journal.soak_checkpoints(window_id)
    processes = journal.soak_processes(window_id)

    heartbeat_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    previous_errors: dict[str, int] = {}
    for row in checkpoints:
        day = row_text(row["ts"])[:10]
        process = row_text(row["process_id"])
        heartbeat_counts[day] = heartbeat_counts.get(day, 0) + 1
        # The checkpoint carries a cumulative per-process counter, so a day's
        # errors are the growth since that process was last seen.
        cumulative = int(row["api_errors"])
        growth = cumulative - previous_errors.get(process, 0)
        if growth > 0:
            error_counts[day] = error_counts.get(day, 0) + growth
        previous_errors[process] = cumulative
    starts: dict[str, int] = {}
    for row in processes:
        day = row_text(row["started_at"])[:10]
        starts[day] = starts.get(day, 0) + 1

    summaries: list[DailySummary] = []
    day = started.date()
    last = horizon.date()
    while day <= last:
        key = day.isoformat()
        bucket = counts.get(key, {})
        summaries.append(
            DailySummary(
                day=key,
                decisions=int(bucket.get("decisions", 0)),
                orders=int(bucket.get("orders", 0)),
                fills=int(bucket.get("fills", 0)),
                heartbeats=heartbeat_counts.get(key, 0),
                api_errors=error_counts.get(key, 0),
                process_starts=starts.get(key, 0),
            )
        )
        day += timedelta(days=1)
    return summaries


def daily_report(summaries: Sequence[DailySummary], *, limit: int | None = None) -> str:
    lines = ["SOAK DAILY SUMMARY", ""]
    if not summaries:
        lines.append("  no soak window and no activity to summarise")
        return "\n".join(lines)
    shown = summaries[-limit:] if limit and limit > 0 else summaries
    omitted = len(summaries) - len(shown)
    if omitted:
        lines.append(f"  {omitted} earlier day(s) omitted; widen the window to see them")
        lines.append("")
    lines.extend(summary.line() for summary in shown)
    silent = [s.day for s in summaries if not s.alive]
    if silent:
        lines.append("")
        lines.append(f"  {len(silent)} day(s) without a heartbeat: {', '.join(silent[:5])}"
                     + (" ..." if len(silent) > 5 else ""))
    return "\n".join(lines)


__all__ = [
    "DailySummary",
    "Gap",
    "SoakCoverage",
    "SoakRecorder",
    "compute_coverage",
    "daily_report",
    "daily_summaries",
]
