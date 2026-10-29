"""Phase 11/12: trade evaluation, challengers, and the acceptance harness.

Every number here comes from the same journal readers the CLI uses, so these
tests prove the reported evidence rather than a parallel implementation of it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from cryptobot.config.settings import Settings
from cryptobot.core.contracts import (
    Action,
    FillRecord,
    OrderRecord,
    OrderStatus,
    Side,
    StrategySignal,
)
from cryptobot.core.states import SystemState
from cryptobot.evaluation.acceptance import AcceptanceCriteria, Criterion, run_acceptance
from cryptobot.evaluation.challengers import (
    INSUFFICIENT,
    PROMOTE,
    REJECT,
    PromotionRule,
    judge,
    run_challengers,
)
from cryptobot.evaluation.trades import (
    evaluate_execution,
    evaluate_trades,
    reconstruct_symbol,
    reference_price,
)
from cryptobot.journal.store import Journal
from cryptobot.research.backtest import BacktestConfig, BacktestResult, TradeRecord
from cryptobot.research.dataset import BarDataset
from cryptobot.strategy import strategy_class
from conftest import START, make_bars, make_intent

UTC_DAY = timedelta(days=1)
PRICE = Decimal("1000")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fill(
    *,
    fill_id: str,
    client_order_id: str,
    side: Side,
    quantity: str,
    price: str,
    when: datetime,
    fee: str = "0",
    symbol: str = "BTC/USD",
    fee_currency: str = "USD",
) -> FillRecord:
    return FillRecord(
        fill_id=fill_id,
        broker_order_id=f"broker-{fill_id}",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        ts=when,
        fee=Decimal(fee),
        fee_currency=fee_currency,
    )


def _round_trip(
    journal: Journal,
    *,
    entry: str = "1000",
    exit_price: str = "1100",
    quantity: str = "0.01",
    entry_fee: str = "0",
    exit_fee: str = "0",
    entry_at: datetime = START,
    exit_at: datetime | None = None,
    symbol: str = "BTC/USD",
) -> None:
    journal.record_fill(
        _fill(
            fill_id=f"buy-{entry_at.isoformat()}",
            client_order_id="cb-buy-1",
            side=Side.BUY,
            quantity=quantity,
            price=entry,
            when=entry_at,
            fee=entry_fee,
            symbol=symbol,
        )
    )
    journal.record_fill(
        _fill(
            fill_id=f"sell-{entry_at.isoformat()}",
            client_order_id="cb-sell-1",
            side=Side.SELL,
            quantity=quantity,
            price=exit_price,
            when=exit_at or (entry_at + timedelta(hours=10)),
            fee=exit_fee,
            symbol=symbol,
        )
    )


def _decision(journal: Journal, when: datetime, action: Action = Action.HOLD) -> str:
    signal = StrategySignal(
        symbol="BTC/USD",
        timeframe="1h",
        bar_ts=when,
        action=action,
        reason="test",
        strategy_name="sma_cross",
        strategy_version="sma_cross@test",
        ts=when,
    )
    return journal.record_decision(signal, data_fingerprint=f"fp-{when.isoformat()}")


def _backtest_result(
    *, pnls: list[str], ceiling: str = "100", drawdown: str | None = None
) -> BacktestResult:
    """A BacktestResult assembled from a chosen list of trade results."""
    trades = [
        TradeRecord(
            symbol="BTC/USD",
            entry_ts=START,
            exit_ts=START + timedelta(hours=index + 1),
            entry_price=Decimal("100"),
            exit_price=Decimal("100") + Decimal(pnl),
            quantity=Decimal("0.1"),
            fees=Decimal("0"),
            net_pnl=Decimal(pnl),
            bars_held=1,
            exit_reason="signal",
        )
        for index, pnl in enumerate(pnls)
    ]
    equity = Decimal(ceiling)
    curve = [(START, equity)]
    for pnl in pnls:
        equity += Decimal(pnl)
        curve.append((START + timedelta(hours=1), equity))
    if drawdown is not None:
        # An explicit dip so the drawdown is under the test's control, not the
        # accidental shape of the P&L sequence.
        curve.insert(1, (START, Decimal(ceiling) - Decimal(drawdown)))
    return BacktestResult(
        strategy_name="fake",
        strategy_version="fake@test",
        params={},
        dataset=BarDataset("BTC/USD", "1h", tuple(make_bars(["100", "101", "102"]))),
        trades=trades,
        equity_curve=curve,
        config=BacktestConfig(ceiling=Decimal(ceiling)),
    )


# ---------------------------------------------------------------------------
# trade reconstruction
# ---------------------------------------------------------------------------


def test_a_single_round_trip_is_reconstructed_with_its_fees(journal: Journal) -> None:
    _round_trip(journal, entry="1000", exit_price="1100", quantity="0.01",
                entry_fee="0.25", exit_fee="0.275")

    report = evaluate_trades(journal)

    assert report.count == 1
    trade = report.trades[0]
    assert trade.entry_price == Decimal("1000")
    assert trade.exit_price == Decimal("1100")
    assert trade.gross_pnl == Decimal("1.0000")
    assert trade.fees == Decimal("0.525")
    assert trade.net_pnl == Decimal("0.475")
    assert report.fees_paid == Decimal("0.525")
    assert report.net_pnl == trade.net_pnl


def test_the_average_cost_convention_matches_the_ledger(journal: Journal) -> None:
    journal.record_fill(
        _fill(fill_id="b1", client_order_id="cb-1", side=Side.BUY,
              quantity="0.01", price="1000", when=START)
    )
    journal.record_fill(
        _fill(fill_id="b2", client_order_id="cb-2", side=Side.BUY,
              quantity="0.01", price="1200", when=START + timedelta(hours=1))
    )
    journal.record_fill(
        _fill(fill_id="s1", client_order_id="cb-3", side=Side.SELL,
              quantity="0.02", price="1300", when=START + timedelta(hours=2))
    )

    report = evaluate_trades(journal)

    assert report.count == 1
    # (1000 + 1200) / 2, not FIFO 1000.
    assert report.trades[0].entry_price == Decimal("1100")
    assert report.trades[0].gross_pnl == Decimal("4.0000")


def test_a_partial_close_releases_only_its_share_of_the_fees(journal: Journal) -> None:
    journal.record_fill(
        _fill(fill_id="b1", client_order_id="cb-1", side=Side.BUY,
              quantity="0.02", price="1000", when=START, fee="1.00")
    )
    journal.record_fill(
        _fill(fill_id="s1", client_order_id="cb-2", side=Side.SELL,
              quantity="0.01", price="1100", when=START + timedelta(hours=1))
    )

    report = evaluate_trades(journal)

    # Half the position closed, so half the entry fee is released with it.
    assert report.count == 1
    assert report.trades[0].fees == Decimal("0.500")
    assert report.open_positions[0].fees == Decimal("0.500")


def test_an_open_lot_is_a_position_not_a_trade(journal: Journal) -> None:
    journal.record_fill(
        _fill(fill_id="b1", client_order_id="cb-1", side=Side.BUY,
              quantity="0.01", price="1000", when=START)
    )

    report = evaluate_trades(journal)

    assert report.count == 0
    assert report.expectancy == Decimal("0")
    assert report.profit_factor is None
    assert len(report.open_positions) == 1
    position = report.open_positions[0]
    assert position.quantity == Decimal("0.01")
    assert position.average_price == Decimal("1000")


def test_an_open_position_is_marked_from_the_latest_cached_bar(journal: Journal) -> None:
    journal.record_fill(
        _fill(fill_id="b1", client_order_id="cb-1", side=Side.BUY,
              quantity="0.01", price="1000", when=START)
    )
    journal.save_bars(make_bars(["1000", "1050", "1200"]))

    report = evaluate_trades(journal)

    position = report.open_positions[0]
    assert position.last_price == Decimal("1200")
    assert position.unrealised_pnl == Decimal("2.0000")
    assert report.open_unrealised_pnl == Decimal("2.0000")


def test_a_zero_loss_run_reports_an_infinite_profit_factor(journal: Journal) -> None:
    _round_trip(journal, entry="1000", exit_price="1100")

    report = evaluate_trades(journal)

    assert report.profit_factor is not None
    assert report.profit_factor.is_infinite()
    assert report.wins == 1


def test_a_mixed_run_reports_a_finite_profit_factor(journal: Journal) -> None:
    _round_trip(journal, entry="1000", exit_price="1100", quantity="0.01",
                entry_at=START)
    _round_trip(journal, entry="1000", exit_price="900", quantity="0.01",
                entry_at=START + UTC_DAY)

    report = evaluate_trades(journal)

    assert report.count == 2
    assert report.wins == 1
    assert report.losses == 1
    assert report.win_rate_pct == Decimal("50")
    assert report.profit_factor == Decimal("1")


def test_a_flat_trade_is_excluded_from_the_win_rate(journal: Journal) -> None:
    _round_trip(journal, entry="1000", exit_price="1100")
    _round_trip(journal, entry="1000", exit_price="1000", entry_at=START + UTC_DAY)

    report = evaluate_trades(journal)

    assert report.count == 2
    assert report.scratches == 1
    assert report.win_rate_pct == Decimal("100")


def test_a_sell_without_a_position_is_reported_not_silently_dropped(
    journal: Journal,
) -> None:
    journal.record_fill(
        _fill(fill_id="s1", client_order_id="cb-1", side=Side.SELL,
              quantity="0.01", price="1000", when=START)
    )

    report = evaluate_trades(journal)

    assert report.count == 0
    assert len(report.unbalanced) == 1
    assert "no open position" in report.unbalanced[0]
    assert "UNBALANCED" in report.summary()


def test_an_oversized_sell_is_reported_as_a_shortfall(journal: Journal) -> None:
    journal.record_fill(
        _fill(fill_id="b1", client_order_id="cb-1", side=Side.BUY,
              quantity="0.01", price="1000", when=START)
    )
    journal.record_fill(
        _fill(fill_id="s1", client_order_id="cb-2", side=Side.SELL,
              quantity="0.02", price="1100", when=START + timedelta(hours=1))
    )

    report = evaluate_trades(journal)

    assert report.count == 1
    assert report.trades[0].quantity == Decimal("0.01")
    assert any("matched only" in detail for detail in report.unbalanced)


def test_reconstruction_is_ordered_and_reports_hold_time(journal: Journal) -> None:
    _round_trip(journal, entry="1000", exit_price="1010", entry_at=START)
    _round_trip(journal, entry="1000", exit_price="1020", entry_at=START + UTC_DAY)

    report = evaluate_trades(journal)

    assert [trade.exit_ts for trade in report.trades] == sorted(
        trade.exit_ts for trade in report.trades
    )
    assert report.trades[0].hold_seconds == 10 * 3600
    assert report.average_hold_seconds == 10 * 3600


def test_an_empty_journal_yields_an_empty_report_without_dividing_by_zero(
    journal: Journal,
) -> None:
    report = evaluate_trades(journal)

    assert report.count == 0
    assert report.win_rate_pct == Decimal("0")
    assert report.net_return_pct == Decimal("0")
    assert report.average_hold_seconds == 0
    assert "closed trades    0" in report.summary()


def test_the_trade_report_is_json_serialisable(journal: Journal) -> None:
    _round_trip(journal, entry="1000", exit_price="1100")

    payload = evaluate_trades(journal).as_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["net_pnl"] == "1.0000"


def test_reconstruct_symbol_is_pure(journal: Journal) -> None:
    fills = [
        _fill(fill_id="b1", client_order_id="cb-1", side=Side.BUY,
              quantity="0.01", price="1000", when=START),
        _fill(fill_id="s1", client_order_id="cb-2", side=Side.SELL,
              quantity="0.01", price="1100", when=START + timedelta(hours=1)),
    ]

    first = reconstruct_symbol("BTC/USD", fills)
    second = reconstruct_symbol("BTC/USD", list(reversed(fills)))

    assert first[0][0].net_pnl == second[0][0].net_pnl
    assert first[2] == second[2] == []


# ---------------------------------------------------------------------------
# execution quality
# ---------------------------------------------------------------------------


def _recorded_order(
    journal: Journal,
    *,
    side: Side = Side.BUY,
    notional: str = "10",
    quantity: str = "0.01",
    limit_price: str | None = None,
    fill_price: str = "1000",
    fee: str = "0.05",
    fee_currency: str = "USD",
    client_order_id: str = "cb-order-1",
) -> None:
    intent = make_intent(
        side=side,
        quantity=quantity,
        notional=notional,
        limit_price=limit_price,
        client_order_id=client_order_id,
    )
    journal.record_intent(intent)
    journal.upsert_order(
        OrderRecord(
            client_order_id=client_order_id,
            symbol="BTC/USD",
            side=side,
            quantity=Decimal(quantity),
            status=OrderStatus.FILLED,
            updated_at=START,
            filled_quantity=Decimal(quantity),
            avg_fill_price=Decimal(fill_price),
        )
    )
    journal.record_fill(
        _fill(
            fill_id=f"fill-{client_order_id}",
            client_order_id=client_order_id,
            side=side,
            quantity=quantity,
            price=fill_price,
            when=START,
            fee=fee,
            fee_currency=fee_currency,
        )
    )


def test_a_base_asset_buy_fee_is_reported_in_the_quote_currency(journal: Journal) -> None:
    """The broker charges a buy in BTC; the report is in USD, so convert."""
    _recorded_order(
        journal, fill_price="70000", fee="0.0000007", fee_currency="BTC"
    )

    report = evaluate_execution(journal)

    assert report.orders[0].fees == Decimal("0.049")
    assert report.fees == Decimal("0.049")
    assert "0.0490" in report.summary(), "not 0.0000 next to a USD figure"


def test_a_quote_asset_sell_fee_is_reported_as_charged(journal: Journal) -> None:
    _recorded_order(
        journal, side=Side.SELL, fill_price="70000", fee="0.05", fee_currency="USD"
    )

    assert evaluate_execution(journal).fees == Decimal("0.05")


def test_a_buy_filled_above_the_reference_is_positive_slippage(journal: Journal) -> None:
    # Reference implied by the intent is 10 / 0.01 = 1000; the fill is 1010.
    _recorded_order(journal, notional="10", quantity="0.01", fill_price="1010")

    report = evaluate_execution(journal)

    assert report.count == 1
    assert report.orders[0].reference_price == Decimal("1000")
    assert report.orders[0].slippage_bps == Decimal("100.00")


def test_a_sell_filled_below_the_reference_is_positive_slippage(journal: Journal) -> None:
    _recorded_order(
        journal, side=Side.SELL, notional="10", quantity="0.01", fill_price="990"
    )

    report = evaluate_execution(journal)

    assert report.orders[0].slippage_bps == Decimal("100.00")


def test_a_buy_filled_below_the_reference_is_negative_slippage(journal: Journal) -> None:
    _recorded_order(journal, notional="10", quantity="0.01", fill_price="990")

    report = evaluate_execution(journal)

    assert report.orders[0].slippage_bps == Decimal("-100.00")
    assert report.worst_slippage_bps == Decimal("-100.00")


def test_a_limit_order_is_measured_against_its_limit_price(journal: Journal) -> None:
    _recorded_order(
        journal, notional="10", quantity="0.01", limit_price="1000", fill_price="1005"
    )

    report = evaluate_execution(journal)

    assert report.orders[0].reference_price == Decimal("1000")
    assert report.orders[0].slippage_bps == Decimal("50.00")


def test_an_unfilled_order_is_not_measured(journal: Journal) -> None:
    intent = make_intent(client_order_id="cb-unfilled")
    journal.record_intent(intent)
    journal.upsert_order(
        OrderRecord(
            client_order_id="cb-unfilled",
            symbol="BTC/USD",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            status=OrderStatus.ACCEPTED,
            updated_at=START,
        )
    )

    report = evaluate_execution(journal)

    assert report.count == 0
    assert report.average_slippage_bps == Decimal("0")


def test_a_filled_order_cannot_exist_without_an_intent(journal: Journal) -> None:
    """The schema forbids orphans, so the evaluator never has to guess one."""
    with pytest.raises(Exception) as caught:
        journal.upsert_order(
            OrderRecord(
                client_order_id="cb-orphan",
                symbol="BTC/USD",
                side=Side.BUY,
                quantity=Decimal("0.01"),
                status=OrderStatus.FILLED,
                updated_at=START,
                filled_quantity=Decimal("0.01"),
                avg_fill_price=Decimal("1000"),
            )
        )

    assert "intent" in str(caught.value).lower() or "FOREIGN KEY" in str(caught.value)
    assert evaluate_execution(journal).count == 0


def test_reference_price_prefers_the_limit_price() -> None:
    assert reference_price(Decimal("10"), Decimal("0.01"), Decimal("990")) == Decimal("990")
    assert reference_price(Decimal("10"), Decimal("0.01"), None) == Decimal("1000")
    assert reference_price(Decimal("0"), Decimal("0"), None) is None


def test_execution_report_is_json_serialisable(journal: Journal) -> None:
    _recorded_order(journal)

    payload = evaluate_execution(journal).as_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["orders"][0]["slippage_bps"] == "0.00"


# ---------------------------------------------------------------------------
# challengers
# ---------------------------------------------------------------------------


def test_a_challenger_with_too_few_trades_is_insufficient_not_rejected() -> None:
    baseline = _backtest_result(pnls=["1"] * 5)
    challenger = _backtest_result(pnls=["5"] * 3)

    verdict = judge(
        baseline,
        challenger,
        challenger,
        name="tiny",
        params={"fast_period": 3},
        rule=PromotionRule(min_out_of_sample_trades=10),
    )

    assert verdict.status == INSUFFICIENT
    assert not verdict.promoted
    assert "out-of-sample trade" in verdict.reasons[0]


def test_a_challenger_that_does_not_beat_the_baseline_is_rejected() -> None:
    baseline = _backtest_result(pnls=["1"] * 10)
    challenger = _backtest_result(pnls=["1"] * 10)

    verdict = judge(
        baseline,
        challenger,
        challenger,
        name="same",
        params={"fast_period": 12},
        rule=PromotionRule(min_out_of_sample_trades=10),
    )

    assert verdict.status == REJECT
    assert any("does not beat" in reason for reason in verdict.reasons)


def test_a_challenger_that_beats_the_baseline_is_promotable() -> None:
    baseline = _backtest_result(pnls=["1"] * 10)
    challenger = _backtest_result(pnls=["2"] * 10)

    verdict = judge(
        baseline,
        challenger,
        challenger,
        name="better",
        params={"fast_period": 8},
        rule=PromotionRule(min_out_of_sample_trades=10),
    )

    assert verdict.status == PROMOTE
    assert verdict.promoted
    assert verdict.reasons == ()


def test_a_deeper_drawdown_blocks_promotion_even_with_more_profit() -> None:
    baseline = _backtest_result(pnls=["1"] * 10, drawdown="1")
    challenger = _backtest_result(pnls=["3"] * 10, drawdown="9")

    verdict = judge(
        baseline,
        challenger,
        challenger,
        name="risky",
        params={"fast_period": 8},
        rule=PromotionRule(min_out_of_sample_trades=10, max_drawdown_slack_pct=Decimal("1")),
    )

    assert verdict.status == REJECT
    assert any("drawdown" in reason for reason in verdict.reasons)


def test_every_failing_reason_is_reported_not_just_the_first() -> None:
    baseline = _backtest_result(pnls=["1"] * 10, drawdown="1")
    challenger = _backtest_result(pnls=["-1", "-1", "-1", "-1", "-1", "-1", "-1", "-1", "-1", "-1"],
                                  drawdown="9")

    verdict = judge(
        baseline,
        challenger,
        challenger,
        name="bad",
        params={"fast_period": 2},
        rule=PromotionRule(min_out_of_sample_trades=10, max_drawdown_slack_pct=Decimal("1")),
    )

    assert verdict.status == REJECT
    assert len(verdict.reasons) >= 2


def test_the_promotion_rule_rejects_impossible_thresholds() -> None:
    with pytest.raises(ValueError):
        PromotionRule(max_drawdown_slack_pct=Decimal("-1"))
    with pytest.raises(ValueError):
        PromotionRule(min_out_of_sample_trades=-1)


def test_run_challengers_never_promotes_on_a_short_dataset() -> None:
    dataset = BarDataset("BTC/USD", "1h", tuple(make_bars([str(100 + i) for i in range(60)])))

    report = run_challengers(
        strategy_class("sma_cross"),
        dataset,
        baseline={"fast_period": 3, "slow_period": 8},
        grid={"fast_period": [3, 5], "slow_period": [8, 13]},
        rule=PromotionRule(min_out_of_sample_trades=999),
    )

    assert report.promotable == ()
    assert report.insufficient
    assert "no challenger may replace the baseline" in report.summary()


def test_run_challengers_reports_the_baseline_out_of_sample_result() -> None:
    dataset = BarDataset(
        "BTC/USD", "1h", tuple(make_bars([str(100 + (i % 12)) for i in range(240)]))
    )

    report = run_challengers(
        strategy_class("sma_cross"),
        dataset,
        baseline={"fast_period": 3, "slow_period": 8},
        grid={"fast_period": [3], "slow_period": [8, 13]},
        rule=PromotionRule(min_out_of_sample_trades=999),
    )

    assert report.baseline_out_of_sample.dataset is not None
    assert report.caveats
    assert "walk-forward" in " ".join(report.caveats)


# ---------------------------------------------------------------------------
# acceptance harness
# ---------------------------------------------------------------------------


def _lenient() -> AcceptanceCriteria:
    return AcceptanceCriteria(
        required_days=Decimal("30"),
        min_days_with_decisions=1,
        min_decisions=1,
        min_trades=0,
        min_reconciliations=0,
    )


def _by_name(report) -> dict[str, Criterion]:
    return {criterion.name: criterion for criterion in report.criteria}


def test_an_empty_journal_is_not_accepted(journal: Journal, settings: Settings) -> None:
    report = run_acceptance(journal, settings, criteria=_lenient())

    assert not report.ok
    checks = _by_name(report)
    assert not checks["journal_present"].ok
    assert checks["journal_present"].blocking


def test_duration_is_pending_not_failed_while_the_soak_runs(
    journal: Journal, settings: Settings, soak
) -> None:
    _decision(journal, START)
    # Three days of heartbeats. The window is the only thing that makes those
    # three days measurable; elapsed wall clock alone would not.
    soak(days=3, interval_hours=24.0)

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=3)
    )

    assert not report.ok
    assert [c.name for c in report.pending] == ["soak_duration"]
    assert report.failures == ()
    assert report.remaining_days == Decimal("27")
    assert "needs 27.0 more day(s)" in report.summary()


def test_a_completed_sound_soak_is_accepted(
    journal: Journal, settings: Settings, soak
) -> None:
    for day in range(0, 31):
        _decision(journal, START + timedelta(days=day))
    soak(days=30, interval_hours=24.0)

    report = run_acceptance(
        journal,
        settings,
        criteria=AcceptanceCriteria(
            required_days=Decimal("30"),
            min_days_with_decisions=30,
            min_decisions=30,
            min_trades=0,
            min_reconciliations=0,
        ),
        now=START + timedelta(days=31),
    )

    assert report.ok, report.summary()
    assert report.failures == ()
    assert report.pending == ()
    assert "ACCEPTED" in report.summary()


def test_a_halted_bot_cannot_be_accepted(journal: Journal, settings: Settings) -> None:
    _decision(journal, START)
    journal.set_system_state(SystemState.HALTED, "operator")

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    )

    checks = _by_name(report)
    assert not checks["operational_state"].ok
    assert "halted" in checks["operational_state"].detail
    assert not report.ok


def test_a_paused_bot_may_still_complete_a_soak(
    journal: Journal, settings: Settings
) -> None:
    _decision(journal, START)
    journal.set_system_state(SystemState.PAUSE_ENTRIES, "news window")

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    )

    assert _by_name(report)["operational_state"].ok


def test_a_critical_reconciliation_drift_blocks_acceptance(
    journal: Journal, settings: Settings
) -> None:
    _decision(journal, START)
    journal.record_reconciliation("position", "critical", {"detail": "mismatch"})

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    )

    checks = _by_name(report)
    assert not checks["reconciliation_critical"].ok
    assert "1 critical" in checks["reconciliation_critical"].detail


def test_unresolved_orders_block_acceptance(journal: Journal, settings: Settings) -> None:
    _decision(journal, START)
    intent = make_intent(client_order_id="cb-uncertain")
    journal.record_intent(intent)
    journal.upsert_order(
        OrderRecord(
            client_order_id="cb-uncertain",
            symbol="BTC/USD",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            status=OrderStatus.UNCERTAIN,
            updated_at=START,
        )
    )

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    )

    assert not _by_name(report)["unresolved_orders"].ok


def test_a_reservation_above_the_ceiling_fails_the_invariant(
    journal: Journal, settings: Settings
) -> None:
    _decision(journal, START)
    # Simulate a corrupted reservation: the ledger's job is to notice, not to hide it.
    journal.set_reservation("rogue", Decimal("150"))

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    )

    checks = _by_name(report)
    assert not checks["allocation_invariant"].ok
    assert "reserved=150" in checks["allocation_invariant"].detail
    assert "ceiling=100" in checks["allocation_invariant"].detail


def test_the_allocation_invariant_passes_on_an_honest_ledger(
    journal: Journal, settings: Settings, ledger
) -> None:
    _decision(journal, START)
    ledger.reserve("entry-1", Decimal("40"))

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    )

    assert _by_name(report)["allocation_invariant"].ok


def test_slippage_outside_budget_blocks_acceptance(
    journal: Journal, settings: Settings
) -> None:
    _decision(journal, START)
    _recorded_order(journal, notional="10", quantity="0.01", fill_price="1100")

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    )

    check = _by_name(report)["execution_slippage"]
    assert not check.ok
    assert "1000.00 bps" in check.detail


def test_no_trades_makes_slippage_vacuous_not_failing(
    journal: Journal, settings: Settings
) -> None:
    _decision(journal, START)

    report = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    )

    check = _by_name(report)["execution_slippage"]
    assert check.ok
    assert "no filled orders" in check.detail


def test_acceptance_reports_readiness_without_mutating_the_journal(
    journal: Journal, settings: Settings
) -> None:
    _decision(journal, START)
    before_health = journal.health()
    before_state = journal.get_system_state()[0]

    run_acceptance(journal, settings, criteria=_lenient(), now=START + timedelta(days=1))

    assert journal.health() == before_health
    assert journal.get_system_state()[0] is before_state


def test_the_acceptance_report_is_json_serialisable(
    journal: Journal, settings: Settings
) -> None:
    _decision(journal, START)

    payload = run_acceptance(
        journal, settings, criteria=_lenient(), now=START + timedelta(days=1)
    ).as_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["ok"] is False
    assert {entry["name"] for entry in payload["criteria"]} >= {
        "journal_present",
        "soak_duration",
        "allocation_invariant",
    }


def test_raise_if_not_ready_speaks_with_the_whole_report(
    journal: Journal, settings: Settings
) -> None:
    report = run_acceptance(journal, settings, criteria=_lenient(), now=START)

    with pytest.raises(Exception) as caught:
        report.raise_if_not_ready()

    assert "ACCEPTANCE REPORT" in str(caught.value)


def test_criteria_cannot_be_configured_impossibly() -> None:
    with pytest.raises(ValueError):
        AcceptanceCriteria(required_days=Decimal("0"))
    with pytest.raises(ValueError):
        AcceptanceCriteria(min_trades=-1)


# ---------------------------------------------------------------------------
# structural guarantees
# ---------------------------------------------------------------------------


def test_evaluation_cannot_reach_the_order_path() -> None:
    import inspect

    from cryptobot.evaluation import acceptance, challengers, trades

    for module in (acceptance, challengers, trades):
        source = inspect.getsource(module)
        assert "cryptobot.execution" not in source
        assert "cryptobot.risk" not in source
        assert "cryptobot.sizing.position" not in source


def test_the_evaluation_package_exposes_only_read_only_tools() -> None:
    import cryptobot.evaluation as evaluation

    forbidden = {"submit", "place_order", "cancel", "size", "allocate"}
    assert forbidden.isdisjoint(set(evaluation.__all__))
