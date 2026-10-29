"""Phase 3 (indicators + strategies) and Phase 7 (research harness).

Three properties are asserted here, and each one is a claim the project makes
about itself elsewhere:

* Indicators are causal — the value at index ``i`` never depends on ``i+1``.
* Signals are causal — proven by prefix invariance, including a deliberately
  cheating strategy that the harness must catch (a passing check is worthless
  if the check cannot fail).
* Backtests do not fill on the bar that produced the decision. The fill price is
  asserted exactly, against the *next* bar's open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cryptobot.core.contracts import Action, Bar
from cryptobot.core.errors import ConfigError, ContractError, DataError
from cryptobot.features import indicators as ind
from cryptobot.journal.store import Journal
from cryptobot.research.backtest import (
    BacktestConfig,
    CostModel,
    compare,
    parameter_sensitivity,
    rebase,
    run_backtest,
    summarise_sensitivity,
    verify_no_lookahead,
)
from cryptobot.research.dataset import (
    BarDataset,
    gaps,
    iter_windows,
    synthetic_dataset,
    synthetic_bars,
    write_csv,
    write_json,
)
from cryptobot.research.experiment import (
    render_walk_forward,
    render_walk_forward_optimised,
    run_suite,
    walk_forward,
    walk_forward_optimised,
)
from cryptobot.research.vectorbt_support import describe as vectorbt_describe
from cryptobot.research.vectorbt_support import run_cross_check, vectorbt_version
from cryptobot.strategy import available_strategies, build_strategy, strategy_class
from cryptobot.strategy.base import Strategy
from cryptobot.strategy.donchian import DonchianBreakout
from cryptobot.strategy.sma_cross import SmaCross

from conftest import BTC, START, make_bars, wave_bars

UTC = timezone.utc
HOUR = timedelta(hours=1)


def _bars(rows, *, symbol: str = "BTC/USD", timeframe: str = "1h") -> list[Bar]:
    """Bars from explicit ``(open, close)`` pairs; the wick is derived."""
    out: list[Bar] = []
    for index, (open_, close) in enumerate(rows):
        open_ = Decimal(str(open_))
        close = Decimal(str(close))
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                ts=START + HOUR * index,
                open=open_,
                high=max(open_, close) * Decimal("1.01"),
                low=min(open_, close) * Decimal("0.99"),
                close=close,
                volume=1,
            )
        )
    return out


def _dataset(bars, *, symbol: str = "BTC/USD", timeframe: str = "1h") -> BarDataset:
    return BarDataset(symbol, timeframe, tuple(bars))


# ---------------------------------------------------------------------------
# Phase 3: indicators are causal
# ---------------------------------------------------------------------------


def test_sma_warmup_and_values():
    values = [Decimal(n) for n in (1, 2, 3, 4, 5)]
    assert ind.sma(values, 3) == [
        None,
        None,
        Decimal(2),
        Decimal(3),
        Decimal(4),
    ]


@pytest.mark.parametrize("period", [1, 2, 3, 7, 20])
def test_sma_is_causal(period):
    values = [Decimal(n * n % 37) for n in range(60)]
    full = ind.sma(values, period)
    for cut in range(period, len(values)):
        assert ind.sma(values[:cut], period)[-1] == full[cut - 1]


def test_ema_seeds_with_the_sma_and_then_smooths():
    values = [Decimal(n) for n in (1, 2, 3, 4, 5, 6)]
    out = ind.ema(values, 3)
    assert out[:2] == [None, None]
    assert out[2] == Decimal(2)  # simple mean of the first three
    alpha = Decimal(2) / Decimal(4)
    assert out[3] == (Decimal(4) - Decimal(2)) * alpha + Decimal(2)


def test_ema_is_causal():
    values = [Decimal((n * 7) % 23) for n in range(50)]
    full = ind.ema(values, 5)
    for cut in range(5, len(values)):
        assert ind.ema(values[:cut], 5)[-1] == full[cut - 1]


def test_rsi_reaches_100_for_a_monotonic_rise():
    values = [Decimal(n) for n in range(1, 31)]
    out = ind.rsi(values, 14)
    assert out[13] is None  # warmup: no close pair has been averaged yet
    assert out[14] == Decimal(100)


def test_rsi_is_causal_and_bounded():
    values = [Decimal((n * 13) % 41) for n in range(80)]
    full = ind.rsi(values, 14)
    for cut in range(15, len(values)):
        assert ind.rsi(values[:cut], 14)[-1] == full[cut - 1]
    assert all(value is None or 0 <= value <= 100 for value in full)


def test_true_range_accounts_for_a_gap_from_the_previous_close():
    bars = _bars([(9.5, 9.5), (11.5, 11.5)])
    # bar 1: high 11.5*1.01, low 11.5*0.99, previous close 9.5 -> the gap dominates
    ranges = ind.true_ranges(bars)
    assert ranges[0] == bars[0].high - bars[0].low
    assert ranges[1] == abs(bars[1].high - bars[0].close)


def test_atr_is_causal():
    bars = wave_bars(60)
    full = ind.atr(bars, 5)
    for cut in range(5, len(bars)):
        assert ind.atr(bars[:cut], 5)[-1] == full[cut - 1]


def test_donchian_channel_excludes_the_current_bar():
    # Strictly rising highs: if bar 3 were included in its own channel it would
    # always be the breakout of itself, and the strategy would always fire.
    bars = _bars([(100 + n, 100 + n) for n in range(6)])
    upper, lower = ind.donchian(bars, 2)

    assert upper[:2] == [None, None]
    assert upper[2] == max(bars[0].high, bars[1].high)
    assert upper[3] == max(bars[1].high, bars[2].high)
    assert lower[3] == min(bars[1].low, bars[2].low)


# ---------------------------------------------------------------------------
# Phase 3: strategies
# ---------------------------------------------------------------------------


def test_sma_cross_emits_one_signal_per_bar():
    bars = wave_bars(120)
    signals = SmaCross({"fast_period": 5, "slow_period": 20}).generate(bars)

    assert len(signals) == len(bars)
    assert [s.bar_ts for s in signals] == [b.ts for b in bars]
    # A decision is stamped when its bar closed, never when it opened.
    assert [s.ts for s in signals] == [b.closes_at for b in bars]


def test_sma_cross_stays_flat_during_warmup():
    bars = wave_bars(120)
    strategy = SmaCross({"fast_period": 5, "slow_period": 20})
    signals = strategy.generate(bars)

    assert strategy.min_bars == 21
    assert all(s.action is Action.HOLD for s in signals[:20])
    assert any(s.action is Action.HOLD for s in signals[20:])


def test_sma_cross_alternates_enter_and_exit_on_a_wave():
    signals = SmaCross({"fast_period": 5, "slow_period": 20}).generate(wave_bars(200))
    actionable = [s.action for s in signals if s.action is not Action.HOLD]

    assert actionable[0] is Action.ENTER_LONG
    assert set(actionable) == {Action.ENTER_LONG, Action.EXIT_LONG}
    # Never two entries or two exits in a row.
    for left, right in zip(actionable, actionable[1:]):
        assert left is not right


@pytest.mark.parametrize(
    "params, message",
    [
        ({"fast_period": 20, "slow_period": 20}, "must be <"),
        ({"fast_period": 30, "slow_period": 10}, "must be <"),
        ({"fast_period": 1}, ">= 2"),
        ({"fast_period": "abc"}, "must be an integer"),
        ({"fast_period": True}, "must be an integer"),
    ],
)
def test_sma_cross_rejects_invalid_parameters(params, message):
    with pytest.raises(ConfigError, match=message):
        SmaCross(params)


def test_version_identifies_the_parameterisation():
    # Regression: the version was once computed before subclass attributes
    # existed, so every parameterisation shared one version.
    default = SmaCross({})
    explicit = SmaCross({"fast_period": 12, "slow_period": 48})
    other = SmaCross({"fast_period": 8, "slow_period": 48})

    assert default.version.startswith("sma_cross@")
    assert default.version == explicit.version  # same effective parameters
    assert other.version != default.version


def test_donchian_breakout_is_registered_and_signals():
    signals = DonchianBreakout({"period": 10, "atr_period": 5}).generate(wave_bars(120))
    assert len(signals) == 120
    assert all(s.action is Action.HOLD for s in signals[:10])
    assert any(s.action is not Action.HOLD for s in signals[10:])
    assert "donchian_upper" in signals[-1].indicators


def test_registry_exposes_both_strategies():
    names = available_strategies()
    assert "sma_cross" in names and "donchian_breakout" in names
    assert strategy_class("sma_cross") is SmaCross
    with pytest.raises(ConfigError, match="unknown strategy"):
        strategy_class("does_not_exist")


def test_build_strategy_uses_configured_parameters(settings):
    strategy = build_strategy(settings.strategy)
    assert isinstance(strategy, SmaCross)
    assert strategy.fast_period == settings.strategy.params["fast_period"]
    assert strategy.slow_period == settings.strategy.params["slow_period"]


# ---------------------------------------------------------------------------
# Phase 7: look-ahead is structurally impossible
# ---------------------------------------------------------------------------


class _Peeking(Strategy):
    """Deliberately cheats, so the detector can be shown to work."""

    name = "peeking"

    @property
    def min_bars(self) -> int:
        return 2

    def generate(self, bars):
        future_close = bars[-1].close  # the bug under test
        return [
            self._signal(
                bar,
                Action.ENTER_LONG if future_close > bar.close else Action.EXIT_LONG,
                "peeked at the end of the series",
                {},
            )
            for bar in bars
        ]


def test_verify_no_lookahead_passes_for_the_real_strategy():
    bars = wave_bars(240)
    assert verify_no_lookahead(SmaCross({"fast_period": 12, "slow_period": 48}), bars) == []


def test_verify_no_lookahead_catches_a_peeking_strategy():
    violations = verify_no_lookahead(_Peeking({}), wave_bars(60))
    assert violations
    assert "prefix says" in violations[0]


def test_verify_no_lookahead_needs_enough_data():
    with pytest.raises(DataError, match="not enough bars"):
        verify_no_lookahead(SmaCross({"fast_period": 5, "slow_period": 20}), wave_bars(10))


# ---------------------------------------------------------------------------
# Phase 7: backtest mechanics
# ---------------------------------------------------------------------------


class _Scheduled(Strategy):
    """One entry and one exit, at named bars. Removes all strategy randomness."""

    name = "scheduled"

    def __init__(self, params=None) -> None:
        super().__init__(params)
        self.entry_at = datetime.fromisoformat(str(self.params["entry_at"]))
        self.exit_at = datetime.fromisoformat(str(self.params["exit_at"]))

    @property
    def min_bars(self) -> int:
        return 2

    def generate(self, bars):
        out = []
        for bar in bars:
            if bar.ts == self.entry_at:
                action = Action.ENTER_LONG
            elif bar.ts == self.exit_at:
                action = Action.EXIT_LONG
            else:
                action = Action.HOLD
            out.append(self._signal(bar, action, "scheduled", {}))
        return out


def _scheduled(bars, entry_index: int, exit_index: int) -> _Scheduled:
    return _Scheduled(
        {"entry_at": bars[entry_index].ts.isoformat(), "exit_at": bars[exit_index].ts.isoformat()}
    )


def test_execution_delay_of_zero_is_refused():
    with pytest.raises(ContractError, match="execution_delay_bars"):
        BacktestConfig(execution_delay_bars=0)


def test_backtest_fills_at_the_next_bars_open_not_the_decision_close():
    bars = _bars([(100, 100.5), (101, 101.5), (102, 102.5), (103, 103.5), (104, 104.5), (105, 105.5)])
    config = BacktestConfig(ceiling=Decimal("100"), instrument=BTC)

    result = run_backtest(_scheduled(bars, 0, 3), _dataset(bars), config)

    assert len(result.trades) == 1
    trade = result.trades[0]
    # decision on bar 0 (close 100.5) -> fill on bar 1's open, plus slippage
    assert trade.entry_price == Decimal("101") * (Decimal(1) + Decimal("0.0025"))
    assert trade.entry_price != bars[0].close
    # decision on bar 3 -> fill on bar 4's open, minus slippage
    assert trade.exit_price == Decimal("104") * (Decimal(1) - Decimal("0.0025"))
    # timestamps describe the fills, matching the fill prices
    assert trade.entry_ts == bars[1].ts and trade.exit_ts == bars[4].ts
    assert trade.bars_held == 3


def test_fees_are_charged_on_both_sides():
    bars = _bars([(100, 100), (100, 100), (100, 100), (100, 100)])
    config = BacktestConfig(
        ceiling=Decimal("100"), instrument=BTC, costs=CostModel(fee_bps=Decimal("100"))
    )

    result = run_backtest(_scheduled(bars, 0, 1), _dataset(bars), config)

    trade = result.trades[0]
    assert trade.fees > 0
    assert trade.fees == result.fees_paid  # a single round trip
    assert trade.net_pnl < 0  # flat price, so the round trip is pure cost
    # 1% of each side: the exit side alone must be accounted for too.
    entry_notional = trade.entry_price * trade.quantity
    assert trade.fees > entry_notional * Decimal("0.0099")


def test_a_decision_on_the_final_bar_is_never_filled():
    bars = _bars([(100, 100), (101, 101), (102, 102), (103, 103)])
    config = BacktestConfig(instrument=BTC)

    result = run_backtest(_scheduled(bars, 3, 0), _dataset(bars), config)

    assert result.trades == []
    assert result.open_quantity == 0


def test_an_open_position_is_marked_not_force_closed():
    bars = _bars([(100, 100), (101, 101), (102, 102), (103, 103)])
    config = BacktestConfig(instrument=BTC)

    result = run_backtest(_scheduled(bars, 0, 0), _dataset(bars), config)

    assert result.trades == []
    assert result.open_quantity > 0
    assert result.final_equity == result.equity_curve[-1][1]


def test_backtest_refuses_a_dataset_shorter_than_the_strategy_needs():
    with pytest.raises(DataError, match="need at least"):
        run_backtest(SmaCross({"fast_period": 5, "slow_period": 20}), _dataset(wave_bars(15)))


def test_buy_and_hold_benchmark_is_derived_from_the_same_dataset():
    bars = _bars([(100, 100), (100, 110), (110, 121), (121, 120), (120, 90)])
    result = run_backtest(_scheduled(bars, 0, 0), _dataset(bars), BacktestConfig(instrument=BTC))
    expected = (bars[-1].close - bars[0].close) / bars[0].close * 100
    assert result.buy_and_hold_pct == pytest.approx(expected)


def test_compare_and_rebase_report_honest_deltas():
    bars = wave_bars(240)
    dataset = _dataset(bars)
    strategy = SmaCross({"fast_period": 5, "slow_period": 20})
    config = BacktestConfig(ceiling=Decimal("100"), instrument=BTC)

    small = run_backtest(strategy, dataset, config)
    large = rebase(strategy, dataset, config, Decimal("200"))
    table = compare(small, large)

    assert "total_return_pct" in table
    assert table["bars"][0] == table["bars"][1]  # same dataset either way
    assert large.ceiling == Decimal("200")
    assert small.ceiling == Decimal("100")
    # A bigger ceiling buys a bigger position, so the results must not be equal.
    assert table["final_equity"][2] != "0.00"


def test_parameter_sensitivity_sweeps_one_factor_at_a_time():
    dataset = _dataset(wave_bars(300))
    results = parameter_sensitivity(
        SmaCross, dataset, {"fast_period": [3, 5, 9]}, config=BacktestConfig(instrument=BTC)
    )
    assert len(results) == 3
    assert [params["fast_period"] for params, _ in results] == [3, 5, 9]
    # The slow period is never touched by a one-factor sweep.
    assert all("slow_period" not in params for params, _ in results)

    report = summarise_sensitivity(results)
    assert "spread across variants" in report
    assert "parameter luck" in report


# ---------------------------------------------------------------------------
# Phase 7: datasets
# ---------------------------------------------------------------------------


def test_dataset_rejects_mixed_symbols_and_timeframes():
    with pytest.raises(DataError, match="mixed symbols"):
        BarDataset("BTC/USD", "1h", (make_bars(["100"])[0], make_bars(["100"], symbol="ETH/USD")[0]))
    with pytest.raises(DataError, match="mixed timeframes"):
        BarDataset("BTC/USD", "1h", (make_bars(["100"])[0], make_bars(["100"], timeframe="4h")[0]))


def test_dataset_requires_strictly_increasing_timestamps():
    bars = make_bars(["100", "101"])
    with pytest.raises(DataError, match="strictly increasing"):
        BarDataset("BTC/USD", "1h", (bars[1], bars[0]))
    with pytest.raises(DataError, match="strictly increasing"):
        BarDataset("BTC/USD", "1h", (bars[0], bars[0]))


def test_empty_dataset_is_refused():
    with pytest.raises(DataError, match="empty"):
        BarDataset("BTC/USD", "1h", ())


def test_train_test_split_is_chronological_and_disjoint():
    dataset = _dataset(wave_bars(100))
    train, test = dataset.train_test(train_fraction=0.7)

    assert len(train) == 70 and len(test) == 30
    assert train.end <= test.start
    assert set(bar.ts for bar in train.bars).isdisjoint(bar.ts for bar in test.bars)


def test_walk_forward_folds_never_look_backwards():
    dataset = _dataset(wave_bars(120))
    folds = dataset.walk_forward(train_bars=50, test_bars=20)

    assert folds
    for train, test in folds:
        assert len(train) == 50 and len(test) == 20
        assert train.end <= test.start


def test_synthetic_bars_are_deterministic_and_labelled():
    first = synthetic_bars(count=50, seed=11)
    second = synthetic_bars(count=50, seed=11)
    third = synthetic_bars(count=50, seed=12)

    assert [bar.to_row() for bar in first] == [bar.to_row() for bar in second]
    assert [bar.to_row() for bar in first] != [bar.to_row() for bar in third]
    assert all(bar.volume > 0 for bar in first)
    # Synthetic data is never presented as market data.
    assert synthetic_dataset(count=50, seed=11).fingerprint == _dataset(
        synthetic_bars(count=50, seed=11)
    ).fingerprint


def test_json_and_csv_round_trips_preserve_the_fingerprint(tmp_path):
    dataset = _dataset(synthetic_bars(count=40, seed=3))

    json_path = write_json(dataset, tmp_path / "bars.json")
    csv_path = write_csv(dataset, tmp_path / "bars.csv")

    assert BarDataset.from_file(json_path).fingerprint == dataset.fingerprint
    assert BarDataset.from_file(csv_path).fingerprint == dataset.fingerprint


def test_dataset_from_journal_reads_the_cache(journal: Journal):
    journal.save_bars(make_bars([100, 101, 102]))
    dataset = BarDataset.from_journal(journal, "BTC/USD", "1h")
    assert len(dataset) == 3
    with pytest.raises(DataError, match="no cached bars"):
        BarDataset.from_journal(journal, "ETH/USD", "1h")


def test_gaps_and_windows_helpers():
    bars = make_bars([100, 101, 102])
    assert gaps(bars, "1h") == []
    # Non-overlapping by default: step == size.
    assert len(list(iter_windows(bars, 2))) == 1
    assert len(list(iter_windows(bars, 2, step=1))) == 2
    assert len(list(iter_windows(bars, 3))) == 1


# ---------------------------------------------------------------------------
# Phase 7: experiment runner
# ---------------------------------------------------------------------------


def test_suite_records_every_attempt_including_invalid_ones(settings):
    dataset = synthetic_dataset(count=600, seed=3)
    suite = run_suite(
        "grid",
        SmaCross,
        dataset,
        {"fast_period": [5, 12], "slow_period": [10, 20]},
        config=BacktestConfig(instrument=BTC),
        min_trades=1,
        journal=None,
    )

    # 4 combinations attempted; 12/10 is impossible and must be reported, not
    # silently dropped.
    assert suite.attempts == 4
    assert len(suite.records) == 3
    assert len(suite.invalid) == 1
    assert suite.invalid[0][0]["fast_period"] == 12

    assert all(record.train is not None and record.test is not None for record in suite.records)
    assert all(record.lookahead_violations == () for record in suite.records)


def test_suite_report_always_carries_its_caveats(settings):
    dataset = synthetic_dataset(count=400, seed=5)
    suite = run_suite(
        "caveats", SmaCross, dataset, {"fast_period": [5], "slow_period": [20]},
        config=BacktestConfig(instrument=BTC), min_trades=1000, journal=None,
    )
    report = suite.summary()

    assert "CAVEATS" in report
    assert "selection, not a validated edge" in report
    assert "profitable backtest does not imply" in report
    # With an unreachable trade requirement the report must say it is useless.
    assert "not informative" in report
    assert suite.benchmark is not None


def test_suite_can_persist_experiments_to_the_journal(journal: Journal):
    dataset = synthetic_dataset(count=300, seed=9)
    suite = run_suite(
        "persisted", SmaCross, dataset, {"fast_period": [5], "slow_period": [20]},
        config=BacktestConfig(instrument=BTC), min_trades=1, journal=journal,
    )
    payload = suite.records[0].to_payload()
    assert payload["strategy"] == "sma_cross"
    metrics = payload["metrics"]
    assert "train_total_return_pct" in metrics and "test_total_return_pct" in metrics
    # The persisted fingerprint must cover both windows, not just the in-sample one.
    record = suite.records[0]
    assert payload["data_fingerprint"] == (
        f"{record.train.dataset.fingerprint}+{record.test.dataset.fingerprint}"
    )
    assert payload["period_start"] < payload["period_end"]


def test_walk_forward_produces_ordered_folds():
    dataset = synthetic_dataset(count=600, seed=2)
    folds = walk_forward(
        SmaCross, {"fast_period": 5, "slow_period": 20}, dataset,
        train_bars=200, test_bars=100, config=BacktestConfig(instrument=BTC),
    )
    assert [index for index, _, _ in folds] == list(range(1, len(folds) + 1))
    rendered = render_walk_forward(folds)
    assert "fold" in rendered and "test%" in rendered


def test_walk_forward_optimised_reselects_parameters_per_fold():
    dataset = synthetic_dataset(count=800, seed=4)
    folds = walk_forward_optimised(
        SmaCross, {"fast_period": [3, 5, 9], "slow_period": [20, 40]}, dataset,
        train_bars=250, test_bars=120, config=BacktestConfig(instrument=BTC),
    )
    assert folds
    for index, params, train_result, test_result in folds:
        assert set(params) == {"fast_period", "slow_period"}
        assert train_result.params == dict(params)
        assert test_result.params == dict(params)
    rendered = render_walk_forward_optimised(folds)
    assert "selected on train" in rendered


def test_walk_forward_requires_a_grid():
    dataset = synthetic_dataset(count=300, seed=1)
    with pytest.raises(ContractError, match="parameter grid"):
        walk_forward_optimised(SmaCross, {}, dataset, train_bars=100, test_bars=50)


# ---------------------------------------------------------------------------
# Phase 7: optional VectorBT cross-check is never faked
# ---------------------------------------------------------------------------


def test_vectorbt_status_is_reported_honestly():
    text = vectorbt_describe()
    if vectorbt_version() is None:
        assert "NOT INSTALLED" in text
        with pytest.raises(Exception):
            run_cross_check(make_bars([100, 101]), [])
    else:
        assert "UNVERIFIED" in text


def test_cross_check_refuses_mismatched_inputs():
    bars = wave_bars(30)
    signals = SmaCross({"fast_period": 3, "slow_period": 10}).generate(bars)
    with pytest.raises(ValueError, match="same length"):
        run_cross_check(bars, signals[:-1])
