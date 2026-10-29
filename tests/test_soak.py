"""Soak evidence: coverage, gaps, restarts, daily summaries, acceptance wiring.

One rule is under test throughout: elapsed time is not evidence. Only heartbeats
are, and a stretch without heartbeats is a gap rather than a quiet period.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import yaml

from cryptobot.cli import OK, main
from cryptobot.config.settings import APP_ROOT
from cryptobot.core.contracts import Action, StrategySignal
from cryptobot.evaluation.acceptance import AcceptanceCriteria, run_acceptance
from cryptobot.journal import schema
from cryptobot.journal.store import Journal
from cryptobot.runtime import Runtime, TradingLoop
from cryptobot.soak import (
    SoakRecorder,
    compute_coverage,
    daily_report,
    daily_summaries,
)

from conftest import START, Clock, make_bars, make_intent

UTC = timezone.utc
CONFIG_PATH = APP_ROOT / "config" / "trading.yaml"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _start(journal: Journal, clock: Clock, at: datetime, *, interval_hours=1.0):
    clock.now = at
    recorder = SoakRecorder(
        journal, interval_seconds=Decimal(str(interval_hours * 3600))
    )
    recorder.start()
    return recorder


def _beat(recorder: SoakRecorder, clock: Clock, at: datetime, *, cycles: int = 1) -> None:
    clock.now = at
    recorder.checkpoint(cycles=cycles)


def _decision(journal: Journal, at: datetime) -> None:
    journal.record_decision(
        StrategySignal(
            symbol="BTC/USD",
            timeframe="1h",
            bar_ts=at,
            action=Action.HOLD,
            reason="soak test",
            strategy_name="sma_cross",
            strategy_version="sma_cross@test",
        ),
        data_fingerprint="fingerprint",
    )


def _criteria(**overrides) -> AcceptanceCriteria:
    base = dict(
        required_days=Decimal("30"),
        min_days_with_decisions=0,
        min_decisions=0,
        min_trades=0,
        min_reconciliations=0,
    )
    base.update(overrides)
    return AcceptanceCriteria(**base)


def _checks(report) -> dict:
    return {criterion.name: criterion for criterion in report.criteria}


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


def test_no_window_means_no_evidence(journal: Journal) -> None:
    coverage = compute_coverage(journal, now=START + timedelta(days=40))

    assert coverage.window_id is None
    assert not coverage.instrumented
    assert coverage.covered_days == Decimal("0")
    assert "no window recorded" in coverage.summary()
    assert coverage.as_dict()["instrumented"] is False


def test_a_single_heartbeat_is_not_evidence(journal: Journal, clock: Clock) -> None:
    recorder = _start(journal, clock, START)
    _beat(recorder, clock, START)

    coverage = compute_coverage(journal, now=START + timedelta(hours=1))

    assert coverage.checkpoints == 1
    assert not coverage.instrumented
    assert coverage.covered_seconds == Decimal("0")
    assert "not instrumented" in coverage.describe()


def test_regular_heartbeats_cover_the_interval(journal: Journal, clock: Clock) -> None:
    recorder = _start(journal, clock, START)
    for hour in range(0, 25):
        _beat(recorder, clock, START + timedelta(hours=hour))

    coverage = compute_coverage(journal, now=START + timedelta(hours=24))

    assert coverage.checkpoints == 25
    assert coverage.gaps == ()
    assert coverage.covered_seconds == Decimal("86400")
    assert coverage.covered_days == Decimal("1")
    assert coverage.observed_days == Decimal("1")
    assert coverage.restarts == 0


def test_a_missed_stretch_is_a_gap_and_costs_coverage(
    journal: Journal, clock: Clock
) -> None:
    recorder = _start(journal, clock, START)
    for hour in range(0, 11):
        _beat(recorder, clock, START + timedelta(hours=hour))
    # Ten hours with no heartbeat: four times the 3x-interval threshold.
    for hour in range(20, 31):
        _beat(recorder, clock, START + timedelta(hours=hour))

    coverage = compute_coverage(journal, now=START + timedelta(hours=30))

    assert len(coverage.gaps) == 1
    assert coverage.gaps[0].seconds == Decimal("36000")
    assert "without a heartbeat" in coverage.gaps[0].line()
    assert coverage.covered_seconds == Decimal("72000")
    assert coverage.observed_seconds == Decimal("108000")
    assert coverage.covered_days < coverage.observed_days
    assert coverage.covered_days.quantize(Decimal("0.01")) == Decimal("0.83")


def test_a_restart_resumes_the_same_window(journal: Journal, clock: Clock) -> None:
    first = _start(journal, clock, START)
    _beat(first, clock, START)
    _beat(first, clock, START + timedelta(hours=1))
    first.close("simulated crash")
    started_at = journal.soak_window()["started_at"]

    second = _start(journal, clock, START + timedelta(hours=2))
    _beat(second, clock, START + timedelta(hours=2))
    _beat(second, clock, START + timedelta(hours=3))

    window = journal.soak_window()
    assert window["window_id"] == first.window_id
    assert window["started_at"] == started_at, "a restart must not reset the window"
    assert journal.soak_processes(first.window_id)[0]["exit_reason"] == "simulated crash"

    coverage = compute_coverage(journal, now=START + timedelta(hours=3))
    assert coverage.processes == 2
    assert coverage.restarts == 1
    assert coverage.checkpoints == 4
    assert coverage.covered_seconds == Decimal("10800"), "three continuous hours"
    assert coverage.gaps == ()


def test_closing_the_window_freezes_the_clock(journal: Journal, clock: Clock) -> None:
    recorder = _start(journal, clock, START)
    _beat(recorder, clock, START)
    _beat(recorder, clock, START + timedelta(hours=1))
    recorder.close("process finished")
    # A stopped process is not a finished soak, so the window survives it ...
    assert journal.soak_window()["ended_at"] is None

    clock.now = START + timedelta(hours=1)
    assert journal.close_soak_window("shipped") == 1

    coverage = compute_coverage(journal, now=START + timedelta(days=100))

    assert not coverage.open
    assert coverage.observed_seconds == Decimal("3600")
    assert coverage.covered_seconds == Decimal("3600")


def test_api_errors_are_counted_once_per_process(
    journal: Journal, clock: Clock
) -> None:
    first = _start(journal, clock, START)
    first.record_api_error("503 from the broker")
    first.record_api_error("429 from the broker")
    _beat(first, clock, START)
    _beat(first, clock, START + timedelta(hours=1))
    first.close("restart")

    second = _start(journal, clock, START + timedelta(hours=2))
    second.record_api_error("timeout")
    _beat(second, clock, START + timedelta(hours=2))
    _beat(second, clock, START + timedelta(hours=3))

    coverage = compute_coverage(journal, now=START + timedelta(hours=3))

    assert coverage.api_errors == 3, "a cumulative counter must not be summed per beat"
    events = journal.soak_events(first.window_id)
    assert [event["kind"] for event in events].count("api_error") == 3


def test_a_recorder_before_start_writes_nothing(journal: Journal, clock: Clock) -> None:
    recorder = SoakRecorder(journal, interval_seconds=60)

    recorder.checkpoint(cycles=1)
    recorder.record_api_error("ignored")
    recorder.close("ignored")

    assert journal.soak_window() is None
    assert journal.soak_checkpoints() == []


def test_widening_the_interval_is_recorded(journal: Journal, clock: Clock) -> None:
    recorder = _start(journal, clock, START, interval_hours=1.0)
    before = journal.soak_window()["interval_seconds"]

    SoakRecorder(journal, interval_seconds=Decimal("7200")).start()

    after = journal.soak_window()["interval_seconds"]
    assert Decimal(before) == Decimal("3600")
    assert Decimal(after) == Decimal("7200"), "a slower heartbeat must not look like a gap"
    kinds = [event["kind"] for event in journal.soak_events(recorder.window_id)]
    assert "interval_changed" in kinds


# ---------------------------------------------------------------------------
# daily summaries
# ---------------------------------------------------------------------------


def test_daily_summaries_include_silent_days(journal: Journal, clock: Clock) -> None:
    recorder = _start(journal, clock, START, interval_hours=24.0)
    _decision(journal, START)
    journal.record_intent(make_intent(client_order_id="cb-day-two", ts=START + timedelta(days=1)))
    _beat(recorder, clock, START)
    _beat(recorder, clock, START + timedelta(days=1))
    _beat(recorder, clock, START + timedelta(days=3))
    recorder.close("done")

    summaries = daily_summaries(journal, now=START + timedelta(days=3))

    assert [summary.day for summary in summaries] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    first, second, third, fourth = summaries
    assert (first.decisions, first.heartbeats, first.process_starts) == (1, 1, 1)
    assert second.orders == 1
    assert third.heartbeats == 0 and not third.alive
    assert fourth.heartbeats == 1
    assert "NO HEARTBEAT" in daily_report(summaries)
    assert "2024-01-03" in daily_report(summaries)


def test_daily_summaries_are_empty_without_a_window_or_activity(
    journal: Journal,
) -> None:
    assert daily_summaries(journal) == []
    assert "no soak window" in daily_report([])


def test_daily_summaries_attribute_api_errors_to_the_day(
    journal: Journal, clock: Clock
) -> None:
    recorder = _start(journal, clock, START, interval_hours=24.0)
    _beat(recorder, clock, START)
    recorder.record_api_error("503")
    _beat(recorder, clock, START + timedelta(days=1))
    recorder.record_api_error("503 again")
    _beat(recorder, clock, START + timedelta(days=2))

    summaries = daily_summaries(journal, now=START + timedelta(days=2))

    assert [summary.api_errors for summary in summaries] == [0, 1, 1]


# ---------------------------------------------------------------------------
# acceptance integration
# ---------------------------------------------------------------------------


def test_acceptance_refuses_an_uninstrumented_soak(
    journal: Journal, settings
) -> None:
    _decision(journal, START)

    report = run_acceptance(
        journal, settings, criteria=_criteria(), now=START + timedelta(days=31)
    )

    checks = _checks(report)
    assert not checks["soak_instrumented"].ok
    assert checks["soak_instrumented"].blocking
    assert "no soak window" in checks["soak_instrumented"].detail
    assert [c.name for c in report.pending] == ["soak_duration"]
    assert report.remaining_days == Decimal("30")
    assert not report.ok
    assert report.coverage.covered_days == Decimal("0")


def test_acceptance_accepts_a_fully_covered_soak(
    journal: Journal, settings, soak
) -> None:
    _decision(journal, START)
    soak(days=30, interval_hours=24.0)

    report = run_acceptance(
        journal, settings, criteria=_criteria(), now=START + timedelta(days=30)
    )

    assert report.ok, report.summary()
    assert report.coverage.covered_days == Decimal("30")
    assert report.coverage.gaps == ()
    assert report.remaining_days == Decimal("0")


def test_acceptance_rejects_a_gap_even_when_the_dates_span_enough_days(
    journal: Journal, settings, clock: Clock
) -> None:
    _decision(journal, START)
    recorder = _start(journal, clock, START, interval_hours=1.0)

    # Eleven hours, a ten-hour hole, then eleven more: 20 evidenced hours over a
    # 30-hour walk. The journal's first decision is 30 hours old either way.
    for hour in range(0, 11):
        _beat(recorder, clock, START + timedelta(hours=hour))
    for hour in range(20, 31):
        _beat(recorder, clock, START + timedelta(hours=hour))

    report = run_acceptance(
        journal,
        settings,
        criteria=_criteria(required_days=Decimal("1")),
        now=START + timedelta(hours=30),
    )

    checks = _checks(report)
    assert not checks["soak_continuity"].ok
    assert "1 gap(s)" in checks["soak_continuity"].detail
    assert not checks["soak_duration"].ok
    assert not report.ok
    assert [c.name for c in report.failures] == ["soak_continuity"]


def test_acceptance_counts_restarts_against_a_budget(
    journal: Journal, settings, clock: Clock
) -> None:
    _decision(journal, START)
    clock.now = START
    window = journal.open_soak_window(interval_seconds=Decimal("3600"))
    for _ in range(12):
        journal.start_soak_process(window["window_id"])

    report = run_acceptance(
        journal,
        settings,
        criteria=_criteria(max_restarts=10),
        now=START + timedelta(days=1),
    )

    checks = _checks(report)
    assert not checks["restart_budget"].ok
    assert "11 restart(s)" in checks["restart_budget"].detail


def test_the_journal_reports_the_schema_it_actually_has(journal: Journal) -> None:
    # Pinned to the constant rather than a literal: the migration is additive, so
    # what matters is that an opened journal agrees with the code that opened it.
    assert schema.SCHEMA_VERSION >= 3, "the soak tables arrived in version 3"
    assert journal.schema_version == schema.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# the loop writes the evidence
# ---------------------------------------------------------------------------


def test_the_trading_loop_records_heartbeats(
    settings, clock: Clock
) -> None:
    with Runtime.build(settings, mode="simulated", clock=clock) as runtime:
        start = clock.now - timedelta(hours=60)
        runtime.journal.save_bars(
            make_bars([100 + (index % 5) for index in range(60)], start=start),
            source="test",
        )

        report = TradingLoop(
            runtime, interval_seconds=0.0, refresh=False, naplike=lambda seconds: None
        ).run(iterations=2)

        assert report.cycles == 2
        coverage = compute_coverage(runtime.journal)
        assert coverage.checkpoints == 2
        assert coverage.processes == 1
        assert coverage.covered_seconds == Decimal("0"), "no time passed in the test"
        assert coverage.instrumented, "two heartbeats are enough to be measurable"
        assert coverage.open, "the window is closed by an operator, not by a stop"
        assert runtime.journal.soak_processes()[0]["ended_at"] is not None


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def _config(tmp_path) -> str:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["journal"]["path"] = str(tmp_path / "soak.db")
    path = tmp_path / "trading.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return str(path)


def test_the_cli_reports_an_absent_window(tmp_path, capsys) -> None:
    code = main(["soak", "--config", _config(tmp_path)])

    output = capsys.readouterr().out
    assert code == OK
    assert "no window recorded" in output


def test_the_cli_declares_and_closes_a_window(tmp_path, capsys) -> None:
    config = _config(tmp_path)

    assert main(["soak", "start", "--note", "pre-launch", "--config", config]) == OK
    started = capsys.readouterr().out
    assert "open since" in started

    assert main(["soak", "status", "--json", "--config", config]) == OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"]["window_id"].startswith("soak-")
    assert payload["coverage"]["open"] is True

    assert main(["soak", "close", "--reason", "shipped", "--config", config]) == OK
    assert "closed" in capsys.readouterr().out

    assert main(["soak", "status", "--json", "--config", config]) == OK
    assert json.loads(capsys.readouterr().out)["coverage"]["open"] is False


def test_the_cli_daily_view_lists_days(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    main(["soak", "start", "--config", config])
    capsys.readouterr()

    assert main(["soak", "daily", "--config", config]) == OK

    output = capsys.readouterr().out
    assert "SOAK DAILY SUMMARY" in output
