"""Phase 9 + 10: shadow AI, notifications, reporting, and the local dashboard.

The interesting claim in this file is a negative one: the AI analyst cannot
influence trading. That is enforced structurally (no import path to
``execution``, ``sizing`` or ``risk``) and behaviourally (every failure - a
timeout, an overspend, malformed JSON, a dead provider - is swallowed and
recorded, never raised into a cycle).

The monitoring half is tested the way an operator uses it: the status view has to
answer "what is my exposure, what is pending, why did the bot not trade, and is
anything inconsistent?" without opening SQLite, and neither the CLI text nor the
dashboard JSON may ever contain a credential.
"""

from __future__ import annotations

import inspect
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest

from cryptobot.ai import shadow
from cryptobot.ai.shadow import (
    MAX_OUTPUT_CHARS,
    AssessmentError,
    MarketContext,
    NullProvider,
    ProviderResponse,
    ShadowAnalyst,
    StaticProvider,
    build_context_from_bars,
    build_prompt,
    extract_json,
    outcome_window_end,
    parse_assessment,
)
from cryptobot.config.settings import Settings
from cryptobot.core.contracts import OrderRecord, OrderStatus, Side
from cryptobot.core.errors import MissingDependency
from cryptobot.core.transport import HttpResponse, TransportError
from cryptobot.execution.base import BrokerPosition
from cryptobot.journal.store import Journal
from cryptobot.monitoring.dashboard import DashboardServer, make_server, status_payload
from cryptobot.monitoring.notify import CHAT_ENV, TOKEN_ENV, TelegramNotifier
from cryptobot.monitoring.report import render_ledger, render_orders, render_status
from cryptobot.sizing.ledger import AllocationLedger

from conftest import START, Clock, make_bars, make_intent

TOKEN = "123456789:AAFakeTokenValueForTestsOnly_000"
CHAT = "42"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _context(symbol: str = "BTC/USD", bars: int = 30) -> MarketContext:
    return build_context_from_bars(make_bars([str(100 + i) for i in range(bars)], symbol=symbol))


def _json_payload(**overrides) -> str:
    base = {
        "regime": "trending_up",
        "bias": "bullish",
        "confidence": 0.72,
        "horizon_bars": 12,
        "rationale": "higher highs on the hourly",
        "key_risks": ["funding reset", "thin liquidity"],
    }
    base.update(overrides)
    return json.dumps(base)


@dataclass
class _FakeProvider:
    name: str = "fake"
    response: ProviderResponse | None = None
    error: Exception | None = None
    calls: int = 0

    def assess(self, prompt: str, context: MarketContext) -> ProviderResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class _Transport:
    """Records requests and returns a scripted response or raises."""

    def __init__(self, *, error: Exception | None = None, status: int = 200) -> None:
        self.error = error
        self.status = status
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, *, headers=None, params=None, json_body=None, timeout=20.0):
        self.calls.append((method, url, dict(json_body or {})))
        if self.error is not None:
            raise self.error
        return HttpResponse(status=self.status, body=b'{"ok":true}')


def _notifications(journal: Journal) -> list[tuple[str, str, str]]:
    """The notifications table is the operator's audit trail; there is no reader yet."""
    rows = journal.conn.execute(
        "SELECT channel, status, detail FROM notifications ORDER BY id"
    ).fetchall()
    return [(row["channel"], row["status"], row["detail"]) for row in rows]


def _seed_book(settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock):
    """A little history so the reporting views have something to render."""
    intent = make_intent(quantity="0.001", notional="20")
    journal.record_intent(intent)
    ledger.reserve(intent.client_order_id, Decimal("20.10"), note="pending buy order")
    ledger.commit_buy(intent.client_order_id, Decimal("0.001"), Decimal("20000"), Decimal("0.05"))
    journal.upsert_order(
        OrderRecord(
            client_order_id=intent.client_order_id,
            broker_order_id="broker-1",
            symbol=intent.symbol,
            side=Side.BUY,
            quantity=Decimal("0.001"),
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("0.001"),
            avg_fill_price=Decimal("20000"),
            updated_at=clock.now,
            submitted_at=clock.now,
            raw_status="filled",
        )
    )
    journal.save_balance(
        cash=Decimal("79.95"),
        equity=Decimal("100.10"),
        buying_power=Decimal("79.95"),
        non_marginable_buying_power=Decimal("79.95"),
        source="broker",
    )
    journal.save_position_snapshots(
        [
            BrokerPosition(
                symbol="BTC/USD",
                quantity=Decimal("0.001"),
                avg_entry_price=Decimal("20000"),
                market_value=Decimal("20.10"),
                unrealized_pl=Decimal("0.10"),
                current_price=Decimal("20100"),
            ).to_snapshot(clock.now, source="broker")
        ]
    )
    return intent


# ---------------------------------------------------------------------------
# shadow AI: off by default, and unable to trade
# ---------------------------------------------------------------------------


def test_the_default_analyst_is_disabled_and_sends_nothing(journal: Journal) -> None:
    analyst = ShadowAnalyst(journal)

    assert not analyst.enabled
    assert analyst.provider_name == "null"
    assert analyst.assess(_context()) is None
    assert journal.unevaluated_assessments() == []


def test_enabling_a_null_provider_stays_disabled(journal: Journal) -> None:
    analyst = ShadowAnalyst(journal, NullProvider(), enabled=True)

    assert not analyst.enabled, "the default provider refuses to run by design"
    assert analyst.assess(_context()) is None


def test_the_shadow_module_cannot_reach_the_trading_path() -> None:
    source = inspect.getsource(shadow)
    for forbidden in (
        "cryptobot.execution",
        "cryptobot.sizing",
        "cryptobot.risk",
        "cryptobot.reconciliation",
        "cryptobot.engine",
        "cryptobot.runtime",
    ):
        assert forbidden not in source, f"shadow mode must not import {forbidden}"


def test_the_prompt_states_that_the_output_cannot_trade() -> None:
    prompt = build_prompt(_context())

    assert "SHADOW" in prompt
    assert "cannot place orders" in prompt
    assert "Return ONE JSON object" in prompt
    assert "BTC/USD" in prompt and "1h" in prompt


def test_a_context_is_built_from_closed_bars_only() -> None:
    bars = make_bars([str(100 + i) for i in range(30)])

    context = build_context_from_bars(bars, indicators={"sma_12": Decimal("114")})

    assert context.as_of == bars[-1].closes_at, "the decision point is when the bar closed"
    assert context.last_price == bars[-1].close
    assert context.indicators["sma_12"] == Decimal("114")
    assert set(context.returns) == {"ret_1b_pct", "ret_6b_pct", "ret_24b_pct"}
    assert context.returns["ret_1b_pct"] == pytest.approx(
        Decimal(bars[-1].close - bars[-2].close) / bars[-2].close * 100
    )


def test_a_context_cannot_be_built_without_bars() -> None:
    with pytest.raises(AssessmentError, match="without bars"):
        build_context_from_bars([])


def test_the_outcome_window_ends_at_the_horizon() -> None:
    context = _context()

    assert outcome_window_end(context, 12) == context.as_of + timedelta(hours=12)


# ---------------------------------------------------------------------------
# shadow AI: the record
# ---------------------------------------------------------------------------


def test_a_valid_assessment_is_recorded_with_full_provenance(journal: Journal) -> None:
    analyst = ShadowAnalyst(journal, StaticProvider(), enabled=True)
    context = _context()

    assessment = analyst.assess(context)

    assert assessment is not None
    assert assessment.regime == "ranging"
    assert assessment.bias == "neutral"
    assert assessment.confidence == Decimal("0.50")
    assert assessment.horizon_bars == 12
    assert assessment.provider == "static"
    assert len(assessment.prompt_hash) == 32
    assert assessment.key_risks == ("static fixture, not a model output",)

    rows = journal.unevaluated_assessments()
    assert len(rows) == 1
    stored = rows[0]
    assert stored["assessment_id"] == assessment.assessment_id
    assert stored["mode"] == "shadow"
    assert stored["symbol"] == "BTC/USD"
    assert json.loads(stored["payload_json"])["prompt_version"] == shadow.PROMPT_VERSION
    assert json.loads(stored["payload_json"])["raw"]


def test_a_provider_error_is_swallowed_and_recorded(journal: Journal) -> None:
    provider = _FakeProvider(error=RuntimeError("model endpoint returned 500"))
    analyst = ShadowAnalyst(journal, provider, enabled=True)

    assert analyst.assess(_context()) is None
    assert analyst.last_error is not None
    assert analyst.last_error.startswith("provider_error")
    assert journal.unevaluated_assessments() == []
    assert ("ai", "skipped:provider_error") in [(c, s) for c, s, _ in _notifications(journal)]


def test_a_missing_provider_dependency_is_not_an_error(journal: Journal) -> None:
    provider = _FakeProvider(error=MissingDependency("no api key"))
    analyst = ShadowAnalyst(journal, provider, enabled=True)

    assert analyst.assess(_context()) is None
    assert analyst.last_error == "provider unavailable"


def test_the_latency_budget_is_enforced(journal: Journal) -> None:
    slow = StaticProvider(latency_ms=30_000)
    analyst = ShadowAnalyst(journal, slow, enabled=True, max_latency_ms=1_000)

    assert analyst.assess(_context()) is None
    assert "latency_limit" in (analyst.last_error or "")
    assert journal.unevaluated_assessments() == []


def test_the_cost_budget_is_enforced(journal: Journal) -> None:
    provider = _FakeProvider(
        response=ProviderResponse(text=_json_payload(), model="expensive", cost_usd=Decimal("0.50"))
    )
    analyst = ShadowAnalyst(journal, provider, enabled=True, max_cost_usd=Decimal("0.02"))

    assert analyst.assess(_context()) is None
    assert "cost_limit" in (analyst.last_error or "")


def test_an_oversized_response_is_rejected(journal: Journal) -> None:
    provider = _FakeProvider(
        response=ProviderResponse(text="x" * (MAX_OUTPUT_CHARS + 1), model="chatty")
    )
    analyst = ShadowAnalyst(journal, provider, enabled=True)

    assert analyst.assess(_context()) is None
    assert "oversized_response" in (analyst.last_error or "")


def test_malformed_output_is_rejected_rather_than_coerced(journal: Journal) -> None:
    provider = _FakeProvider(response=ProviderResponse(text="buy now, thank me later", model="m"))
    analyst = ShadowAnalyst(journal, provider, enabled=True)

    assert analyst.assess(_context()) is None
    assert "malformed_response" in (analyst.last_error or "")


def test_a_persistence_failure_is_reported_not_raised(journal: Journal) -> None:
    analyst = ShadowAnalyst(journal, StaticProvider(), enabled=True)
    journal.close()  # the database goes away underneath it

    assert analyst.assess(_context()) is None
    assert "persistence_error" in (analyst.last_error or "")


# ---------------------------------------------------------------------------
# shadow AI: parsing is strict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        _json_payload(regime="mooning"),
        _json_payload(bias="maybe"),
        _json_payload(confidence=1.4),
        _json_payload(confidence=-0.1),
        _json_payload(horizon_bars=0),
        _json_payload(horizon_bars=10_000),
        _json_payload(rationale=""),
        "not json at all",
        "[]",
        "",
    ],
)
def test_parse_assessment_rejects_everything_unexpected(payload: str) -> None:
    assert parse_assessment(payload) is None


def test_parse_assessment_accepts_a_bare_object() -> None:
    parsed = parse_assessment(_json_payload(confidence=0.5))

    assert parsed is not None
    assert parsed["regime"] == "trending_up"
    assert parsed["confidence"] == Decimal("0.50")
    assert parsed["horizon_bars"] == 12
    assert len(parsed["key_risks"]) == 2


def test_parse_assessment_tolerates_prose_and_a_code_fence() -> None:
    fenced = "```json\n" + _json_payload() + "\n```"
    prose = "Here is my view: " + _json_payload() + " Hope that helps."

    assert extract_json(fenced) is not None
    assert extract_json(prose) is not None
    assert parse_assessment(fenced) is not None
    assert parse_assessment(prose) is not None


def test_key_risks_are_capped_and_truncated() -> None:
    parsed = parse_assessment(_json_payload(key_risks=["a" * 500, "b", "c", "d", "e"]))

    assert parsed is not None
    assert len(parsed["key_risks"]) == 3
    assert len(parsed["key_risks"][0]) == 120


# ---------------------------------------------------------------------------
# shadow AI: scoring against what happened
# ---------------------------------------------------------------------------


def test_a_bullish_call_is_scored_against_the_realised_move(journal: Journal) -> None:
    provider = _FakeProvider(response=ProviderResponse(text=_json_payload(), model="m"))
    analyst = ShadowAnalyst(journal, provider, enabled=True)
    context = _context()
    assert analyst.assess(context) is not None

    prices = {context.as_of: Decimal("100"), outcome_window_end(context, 12): Decimal("104")}
    counters = analyst.evaluate_outcomes(lambda symbol, ts: prices.get(ts))

    assert counters == {"evaluated": 1, "correct": 1, "incorrect": 0, "skipped": 0}
    assert journal.unevaluated_assessments() == []
    outcome = json.loads(
        journal.conn.execute("SELECT outcome_json FROM ai_assessments").fetchone()[0]
    )
    assert outcome["realised_bias"] == "bullish"
    assert outcome["correct"] is True
    assert outcome["change_pct"] == "4.0000"


def test_a_wrong_call_is_scored_as_wrong(journal: Journal) -> None:
    provider = _FakeProvider(response=ProviderResponse(text=_json_payload(), model="m"))
    analyst = ShadowAnalyst(journal, provider, enabled=True)
    context = _context()
    analyst.assess(context)

    prices = {context.as_of: Decimal("100"), outcome_window_end(context, 12): Decimal("96")}
    counters = analyst.evaluate_outcomes(lambda symbol, ts: prices.get(ts))

    assert counters == {"evaluated": 1, "correct": 0, "incorrect": 1, "skipped": 0}


def test_an_unscoreable_assessment_is_skipped_not_guessed(journal: Journal) -> None:
    analyst = ShadowAnalyst(journal, StaticProvider(), enabled=True)
    analyst.assess(_context())

    counters = analyst.evaluate_outcomes(lambda symbol, ts: None)

    assert counters == {"evaluated": 0, "correct": 0, "incorrect": 0, "skipped": 1}
    assert len(journal.unevaluated_assessments()) == 1, "still waiting for the future"


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


def test_an_unconfigured_notifier_is_quiet(journal: Journal) -> None:
    notifier = TelegramNotifier(transport=_Transport(), env={}, journal=journal)

    assert not notifier.enabled
    assert notifier.send("hello") is False
    assert _notifications(journal) == [("telegram", "skipped", "not configured")]


def test_a_configured_notifier_posts_and_records(journal: Journal) -> None:
    transport = _Transport()
    notifier = TelegramNotifier(transport=transport, env={TOKEN_ENV: TOKEN, CHAT_ENV: CHAT}, journal=journal)

    assert notifier.enabled
    assert notifier.send("halted: reconciliation drift") is True

    method, url, body = transport.calls[0]
    assert method == "POST"
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == CHAT
    assert body["disable_web_page_preview"] is True
    assert _notifications(journal)[-1][1] == "sent"


def test_the_bot_token_never_reaches_the_journal(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    notifier = TelegramNotifier(
        transport=_Transport(), env={TOKEN_ENV: TOKEN, CHAT_ENV: CHAT}, journal=journal
    )

    notifier.send(f"debug: url was https://api.telegram.org/bot{TOKEN}/sendMessage")

    for channel, status, detail in _notifications(journal):
        assert TOKEN not in detail, "the token must be redacted before it is stored"


def test_a_transport_failure_is_recorded_and_never_raised(journal: Journal) -> None:
    transport = _Transport(error=TransportError("network error: name resolution", retryable=False))
    notifier = TelegramNotifier(transport=transport, env={TOKEN_ENV: TOKEN, CHAT_ENV: CHAT}, journal=journal)

    assert notifier.send("halted") is False
    assert _notifications(journal)[-1][1] == "failed"


def test_a_notifier_without_a_journal_still_does_not_raise() -> None:
    notifier = TelegramNotifier(transport=_Transport(error=TransportError("boom")), env={})

    assert notifier.send("anything") is False


# ---------------------------------------------------------------------------
# human-readable reporting
# ---------------------------------------------------------------------------


def test_the_status_view_answers_the_operator_questions(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    _seed_book(settings, journal, ledger, clock)
    journal.record_reconciliation("positions", "info", {"detail": "matched"})

    text = render_status(settings, journal, ledger, now=clock.now)

    assert "system state" in text
    assert "ALLOCATION" in text
    assert "ceiling         100.00 USD" in text
    assert "committed       20.00" in text
    assert "realised P&L    -0.05" in text
    assert "invariant       OK" in text
    assert "BROKER ACCOUNT" in text
    assert "POSITIONS" in text and "BTC/USD" in text
    assert "OPEN ORDERS" in text
    assert "RECENT DECISIONS" in text
    assert "RECONCILIATION (recent)" in text
    assert "COUNTERS" in text


def test_the_status_view_never_prints_a_credential(
    settings: Settings,
    journal: Journal,
    ledger: AllocationLedger,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SUPERSECRETKEYVALUE"
    monkeypatch.setenv("ALPACA_API_KEY", secret)

    text = render_status(settings, journal, ledger, now=clock.now)

    assert secret not in text


def test_the_ledger_view_lists_events_and_live_reservations(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    intent = make_intent()
    journal.record_intent(intent)
    ledger.reserve("pending-1", Decimal("30.25"), note="pending buy order")

    text = render_ledger(ledger, journal)

    assert "ALLOCATION LEDGER" in text
    assert "reserved 30.25" in text
    assert "RECENT EVENTS" in text
    assert "reserve" in text
    assert "ACTIVE RESERVATIONS" in text and "pending-1" in text


def test_the_orders_view_includes_the_broker_status(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    intent = _seed_book(settings, journal, ledger, clock)

    text = render_orders(journal)

    assert "ORDERS (most recent first)" in text
    assert intent.client_order_id[:18] in text
    assert "filled" in text


def test_an_empty_journal_renders_flat_rather_than_failing(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    text = render_status(settings, journal, ledger, now=clock.now)

    assert "(flat)" in text
    assert "(none)" in text
    assert "VIOLATED" not in text


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


def test_the_status_payload_is_complete_and_serialisable(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    _seed_book(settings, journal, ledger, clock)

    payload = status_payload(settings, journal, ledger, now=clock.now)

    assert payload["app"] == {"name": settings.app.name, "environment": "paper", "paper_only": True}
    assert payload["system"]["state"] == "active"
    assert Decimal(payload["allocation"]["ceiling"]) == Decimal("100")
    assert Decimal(payload["allocation"]["committed"]) == Decimal("20")
    assert payload["allocation"]["invariant_holds"] is True
    assert payload["broker"]["cash"] == "79.95"
    assert payload["positions"][0]["symbol"] == "BTC/USD"
    assert payload["orders"][0]["status"] == "filled"
    assert payload["limits"]["max_orders_per_day"] == settings.risk.max_orders_per_day
    assert json.loads(json.dumps(payload)) == payload, "the page fetches JSON, not reprs"


def test_the_dashboard_serves_the_page_and_the_status_json(
    settings: Settings, journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    journal.save_balance(
        cash=Decimal("100"),
        equity=Decimal("100"),
        buying_power=Decimal("100"),
        non_marginable_buying_power=Decimal("100"),
        source="broker",
    )
    server = make_server(settings, journal, ledger, host="127.0.0.1", port=0)
    dashboard = DashboardServer(server).start()
    try:
        base = dashboard.url

        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            page = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"

        with urllib.request.urlopen(f"{base}/api/status", timeout=5) as response:
            payload = json.loads(response.read())

        assert "CryptoBot" in page and "api/status" in page
        assert Decimal(payload["allocation"]["ceiling"]) == Decimal("100")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"{base}/orders/cancel", timeout=5)
        assert excinfo.value.code == 404
    finally:
        dashboard.stop()


def test_the_dashboard_offers_no_route_that_changes_state(
    settings: Settings, journal: Journal, ledger: AllocationLedger
) -> None:
    server = make_server(settings, journal, ledger, host="127.0.0.1", port=0)
    dashboard = DashboardServer(server).start()
    try:
        request = urllib.request.Request(
            f"{dashboard.url}/api/status", data=b"{}", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        # BaseHTTPRequestHandler answers 501 for any method without a do_* handler,
        # so there is nothing to call: the dashboard is a window, not a control panel.
        assert excinfo.value.code == 501
        assert journal.get_system_state()[0].value == "active"
    finally:
        dashboard.stop()
