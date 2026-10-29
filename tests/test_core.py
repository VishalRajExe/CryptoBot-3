"""Phase 1 + Phase 4: contracts, configuration, journal, and the allocation ledger."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import yaml

from cryptobot.config.settings import APP_ROOT, Settings, load_settings
from cryptobot.core.contracts import (
    AllocationState,
    Bar,
    FillRecord,
    InstrumentSpec,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    OrderType,
    RiskCheck,
    RiskVerdict,
    Side,
    TimeInForce,
    map_broker_status,
    normalize_symbol,
    quote_currency,
    to_decimal,
)
from cryptobot.core.errors import (
    ConfigError,
    ContractError,
    DuplicateOrderError,
    ReconciliationError,
    RiskRejected,
)
from cryptobot.journal.schema import SCHEMA_VERSION
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger

from conftest import START, make_bars, make_intent

UTC = timezone.utc
CONFIG_PATH = APP_ROOT / "config" / "trading.yaml"


def _raw_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _config_file(tmp_path, mutate) -> str:
    raw = _raw_config()
    mutate(raw)
    path = tmp_path / "trading.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("btc/usd", "BTC/USD"), ("BTCUSD", "BTC/USD"), ("  btc / usd ", "BTC/USD"),
     ("ETH/USDT", "ETH/USDT")],
)
def test_symbol_normalisation(raw: str, expected: str) -> None:
    assert normalize_symbol(raw) == expected


def test_quote_currency() -> None:
    assert quote_currency("btc/usd") == "USD"


def test_bar_rejects_naive_timestamp() -> None:
    with pytest.raises(ContractError):
        Bar(symbol="BTC/USD", timeframe="1h", ts=datetime(2024, 1, 1),
            open="1", high="2", low="1", close="2", volume="1")


def test_bar_rejects_inconsistent_ohlc() -> None:
    with pytest.raises(ContractError):
        Bar(symbol="BTC/USD", timeframe="1h", ts=START,
            open="10", high="9", low="8", close="9", volume="1")


def test_bar_rejects_unaligned_timestamp() -> None:
    with pytest.raises(ContractError):
        Bar(symbol="BTC/USD", timeframe="1h", ts=START + timedelta(minutes=17),
            open="10", high="11", low="9", close="10", volume="1")


def test_bar_allows_zero_volume() -> None:
    # Alpaca documents quote-derived bars with zero volume; it is not an error.
    bar = Bar(symbol="BTC/USD", timeframe="1h", ts=START,
              open="10", high="11", low="9", close="10", volume="0")
    assert bar.volume == 0
    assert bar.closes_at == START + timedelta(hours=1)


def test_to_decimal_rejects_nonsense() -> None:
    for bad in (True, float("nan"), float("inf"), None, "abc"):
        with pytest.raises((ContractError, ValueError, ArithmeticError)):
            to_decimal(bad, "x")


def test_to_decimal_avoids_float_noise() -> None:
    assert to_decimal(0.1) == Decimal("0.1")


def test_order_intent_rejects_overlong_client_order_id() -> None:
    with pytest.raises(ContractError):
        make_intent(client_order_id="x" * 49)


def test_order_intent_fingerprint_is_stable() -> None:
    first = make_intent()
    second = make_intent(side=Side.BUY)
    assert first.fingerprint == second.fingerprint


def test_risk_verdict_summarises_failed_checks() -> None:
    verdict = RiskVerdict.rejected(
        [
            RiskCheck("daily_loss_limit", False, "loss 6% > 5%"),
            RiskCheck("data_freshness", True, "ok"),
        ],
        Decimal("10"),
        START,
    )
    assert verdict.approved is False
    assert verdict.reasons == ("daily_loss_limit: loss 6% > 5%",)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("filled", OrderStatus.FILLED), ("canceled", OrderStatus.CANCELED),
     ("partially_filled", OrderStatus.PARTIALLY_FILLED), ("rejected", OrderStatus.REJECTED),
     ("something_new", OrderStatus.UNKNOWN), (None, OrderStatus.UNKNOWN)],
)
def test_broker_status_mapping(raw, expected: OrderStatus) -> None:
    assert map_broker_status(raw) is expected


def test_allocation_state_invariant() -> None:
    ok = AllocationState(ceiling=Decimal("100"), committed=Decimal("60"), reserved=Decimal("40"))
    bad = AllocationState(ceiling=Decimal("100"), committed=Decimal("60"), reserved=Decimal("41"))
    assert ok.invariant_holds and ok.available == 0
    assert not bad.invariant_holds and bad.available == 0


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_default_config_is_paper_and_capped() -> None:
    settings = load_settings()
    assert settings.app.environment == "paper"
    assert settings.allocation.ceiling_usd == Decimal("100")
    assert settings.execution.time_in_force is TimeInForce.GTC
    assert settings.alpaca.bars_path == "/v1beta3/crypto/us/bars"
    assert settings.market.timeframe == "1h"


def test_live_environment_is_refused(tmp_path) -> None:
    path = _config_file(tmp_path, lambda r: r["app"].__setitem__("environment", "live"))
    with pytest.raises(ConfigError, match="paper"):
        load_settings(path)


def test_live_endpoint_is_refused(tmp_path) -> None:
    def mutate(raw):
        raw["alpaca"]["trading_base_url"] = "https://api.alpaca.markets"

    with pytest.raises(ConfigError):
        load_settings(_config_file(tmp_path, mutate))


def test_day_time_in_force_is_refused_with_crypto_reason(tmp_path) -> None:
    def mutate(raw):
        raw["execution"]["time_in_force"] = "day"

    with pytest.raises(ConfigError, match="gtc|ioc|crypto"):
        load_settings(_config_file(tmp_path, mutate))


def test_unknown_config_key_is_refused(tmp_path) -> None:
    def mutate(raw):
        raw["risk"]["max_postion_pct"] = 25  # typo must not silently disable a limit

    with pytest.raises(ConfigError):
        load_settings(_config_file(tmp_path, mutate))


def test_public_config_never_contains_secret_values(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "PK-SECRET-VALUE")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "SECRET-VALUE")
    settings = load_settings()
    dumped = repr(settings.as_public_dict())
    assert "SECRET-VALUE" not in dumped
    assert settings.as_public_dict()["alpaca"]["credentials_present"] is True
    assert settings.alpaca.missing_credential_names() == []


def test_missing_credentials_reported_by_name_only(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    settings = load_settings()
    missing = settings.alpaca.missing_credential_names()
    assert missing == ["ALPACA_API_KEY", "ALPACA_API_SECRET_KEY"]


def test_database_path_can_be_overridden(tmp_path, monkeypatch) -> None:
    target = tmp_path / "custom.db"
    monkeypatch.setenv("CRYPTOBOT_DB", str(target))
    assert load_settings().database_path() == target


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------


def test_schema_version_and_health(journal: Journal) -> None:
    assert journal.schema_version == SCHEMA_VERSION
    health = journal.health()
    assert health["bars"] == 0 and health["orders"] == 0


def test_bars_are_upserted_not_duplicated(journal: Journal) -> None:
    bars = make_bars([100, 101, 102])
    assert journal.save_bars(bars) == 3
    assert journal.save_bars(bars) == 3
    assert journal.bar_count("BTC/USD", "1h") == 3
    assert journal.recent_bars("BTC/USD", "1h", 10)[-1].close == Decimal("102")


def test_duplicate_intent_is_rejected(journal: Journal) -> None:
    intent = make_intent()
    journal.record_intent(intent)
    with pytest.raises(DuplicateOrderError):
        journal.record_intent(intent)


def test_fill_insertion_is_idempotent(journal: Journal) -> None:
    journal.record_intent(make_intent())
    fill = FillRecord(
        fill_id="f-1", broker_order_id="b-1", client_order_id="cb-test-intent-0001",
        symbol="BTC/USD", side=Side.BUY, quantity="0.001", price="60000",
        ts=START, fee="0.15",
    )
    assert journal.record_fill(fill) is True
    assert journal.record_fill(fill) is False
    assert len(journal.fills()) == 1
    assert journal.sum_fees() == Decimal("0.15")


def test_realized_position_from_fills_only(journal: Journal) -> None:
    journal.record_intent(make_intent())
    for index, (side, qty, price) in enumerate(
        [(Side.BUY, "0.002", "100"), (Side.SELL, "0.001", "120")]
    ):
        journal.record_fill(
            FillRecord(
                fill_id=f"f-{index}", broker_order_id="b-1",
                client_order_id="cb-test-intent-0001", symbol="BTC/USD", side=side,
                quantity=qty, price=price, ts=START + timedelta(minutes=index),
            )
        )
    quantity, average = journal.realized_position("BTC/USD")
    assert quantity == Decimal("0.001")
    assert average == Decimal("100")


def test_order_row_requires_persisted_intent(journal: Journal) -> None:
    from cryptobot.core.errors import ExecutionError

    record = OrderRecord(
        client_order_id="cb-unknown", symbol="BTC/USD", side=Side.BUY,
        quantity="0.001", status=OrderStatus.PENDING_SUBMIT, updated_at=START,
    )
    with pytest.raises(ExecutionError, match="cannot persist order"):
        journal.upsert_order(record)


def test_system_state_roundtrip(journal: Journal) -> None:
    from cryptobot.core.states import SystemState

    journal.set_system_state(SystemState.HALTED, "drift")
    state, reason, updated = journal.get_system_state()
    assert state is SystemState.HALTED and reason == "drift" and updated is not None


# ---------------------------------------------------------------------------
# allocation ledger
# ---------------------------------------------------------------------------


def test_reserve_commit_release(ledger: AllocationLedger) -> None:
    ledger.reserve("r1", Decimal("60"))
    assert ledger.state().reserved == Decimal("60")
    ledger.commit_buy("r1", Decimal("1"), Decimal("50"), Decimal("0.1"))
    state = ledger.state()
    assert state.reserved == 0
    # committed mirrors the broker's cost basis exactly; the fee is expensed.
    assert state.committed == Decimal("50")
    assert state.realized_pnl == Decimal("-0.1")
    assert state.available == Decimal("50")


def test_a_closed_position_releases_every_cent_it_committed(ledger: AllocationLedger) -> None:
    # Regression: with fees capitalised into committed, every round trip left a
    # few cents of amortised fee behind, permanently shrinking deployable capital.
    for round_trip in range(20):
        ref = f"r{round_trip}"
        ledger.reserve(ref, Decimal("30"))
        ledger.commit_buy(ref, Decimal("0.001"), Decimal("20000"), Decimal("0.05"))
        ledger.apply_sell(ref, Decimal("0.001"), Decimal("20000"), Decimal("0.05"), Decimal("20000"))

    state = ledger.state()
    assert state.committed == 0
    assert state.available == Decimal("100")
    # 20 round trips x 2 fees of 0.05, all accounted for as realised losses.
    assert state.realized_pnl == Decimal("-2.00")


def test_committed_matches_the_broker_cost_basis_after_a_partial_sell(
    ledger: AllocationLedger,
) -> None:
    ledger.reserve("buy", Decimal("40"))
    ledger.commit_buy("buy", Decimal("0.002"), Decimal("20000"), Decimal("0.1"))
    ledger.apply_sell("sell", Decimal("0.0005"), Decimal("21000"), Decimal("0.05"), Decimal("20000"))

    state = ledger.state()
    # 0.0015 remaining at the 20000 cost basis, no fee residue.
    assert state.committed == Decimal("30")


def test_reservation_cannot_exceed_ceiling(ledger: AllocationLedger) -> None:
    ledger.reserve("r1", Decimal("90"))
    with pytest.raises(RiskRejected):
        ledger.reserve("r2", Decimal("11"))


def test_release_restores_availability(ledger: AllocationLedger) -> None:
    ledger.reserve("r1", Decimal("80"))
    assert ledger.available() == Decimal("20")
    assert ledger.release("r1") is True
    assert ledger.release("r1") is False
    assert ledger.available() == Decimal("100")


def test_realized_profit_does_not_raise_the_ceiling(ledger: AllocationLedger) -> None:
    ledger.reserve("buy", Decimal("100"))
    ledger.commit_buy("buy", Decimal("1"), Decimal("100"), Decimal("0"))
    ledger.apply_sell("sell", Decimal("1"), Decimal("120"), Decimal("0"), Decimal("100"))
    state = ledger.state()
    assert state.realized_pnl == Decimal("20")
    assert state.ceiling == Decimal("100")
    assert state.committed == 0
    assert state.available == Decimal("100")
    with pytest.raises(RiskRejected):
        ledger.reserve("too-big", Decimal("101"))


def test_adjustment_requires_a_note(ledger: AllocationLedger) -> None:
    with pytest.raises(ContractError):
        ledger.adjust(Decimal("5"), ref="x", note="")


def test_ledger_rebuilds_from_events(journal: Journal) -> None:
    first = AllocationLedger(journal, Decimal("100"))
    first.rebuild()
    first.reserve("r1", Decimal("40"))
    first.commit_buy("r1", Decimal("0.5"), Decimal("70"), Decimal("0.05"))
    snapshot = first.state()

    second = AllocationLedger(journal, Decimal("100"))
    second.rebuild()
    assert second.state() == snapshot


def test_randomized_operations_never_exceed_ceiling(journal: Journal) -> None:
    rng = random.Random(20240101)
    ledger = AllocationLedger(journal, Decimal("100"))
    ledger.rebuild()

    for step in range(400):
        action = rng.choice(["reserve", "release", "commit", "sell"])
        ref = f"r{step}"
        if action == "reserve":
            amount = Decimal(rng.randint(1, 30))
            try:
                ledger.reserve(ref, amount)
            except RiskRejected:
                pass
        elif action == "release":
            for existing in list(ledger.reservations):
                ledger.release(existing)
                break
        elif action == "commit":
            for existing, amount in list(ledger.reservations.items()):
                price = Decimal("1")
                ledger.commit_buy(existing, amount, price, Decimal("0"))
                break
        else:
            committed = ledger.state().committed
            if committed > 0:
                quantity = committed
                ledger.apply_sell(ref, quantity, Decimal("1"), Decimal("0"), Decimal("1"))

        state = ledger.state()
        assert state.committed + state.reserved <= state.ceiling + state.EPSILON
        assert state.committed >= 0 and state.reserved >= 0
