"""The CLI as the operator actually meets it.

Two properties matter more than any individual command:

* reading is free and changing state is explicit - ``--submit``, ``--yes`` and
  ``--reason`` are required before anything happens;
* nothing printed ever contains a credential.

Each test drives ``main(argv)`` exactly as a shell would, against a temporary
configuration and database, and asserts both the exit code and the message.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
import yaml

from cryptobot.cli import FAILED, OK, REFUSED, main
from cryptobot.config.settings import APP_ROOT
from cryptobot.core.contracts import OrderRecord, OrderStatus, Side
from cryptobot.core.states import SystemState
from cryptobot.journal.store import Journal

CONFIG_PATH = APP_ROOT / "config" / "trading.yaml"


def _config(tmp_path, mutate=None) -> str:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["journal"]["path"] = str(tmp_path / "cli.db")
    if mutate is not None:
        mutate(raw)
    path = tmp_path / "trading.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return str(path)


def _run(arguments: str | list[str], config: str) -> int:
    argv = arguments.split() if isinstance(arguments, str) else list(arguments)
    return main([*argv, "--config", config])


def _journal(tmp_path) -> Journal:
    return Journal(str(tmp_path / "cli.db")).initialize()


def _store_open_order(tmp_path) -> str:
    journal = _journal(tmp_path)
    try:
        from conftest import make_intent

        intent = make_intent(quantity="0.001", notional="20")
        journal.record_intent(intent)
        journal.upsert_order(
            OrderRecord(
                client_order_id=intent.client_order_id,
                broker_order_id="broker-1",
                symbol=intent.symbol,
                side=Side.BUY,
                quantity=intent.quantity,
                status=OrderStatus.ACCEPTED,
                updated_at=journal.now(),
                submitted_at=journal.now(),
                raw_status="new",
            )
        )
        return intent.client_order_id
    finally:
        journal.close()


# ---------------------------------------------------------------------------
# configuration and inspection
# ---------------------------------------------------------------------------


def test_config_check_never_prints_a_secret(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "PKSUPERSECRETVALUE")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "SKSUPERSECRETVALUE")

    code = _run("config-check", _config(tmp_path))
    output = capsys.readouterr().out

    assert code == OK
    assert "config file:" in output
    assert "credentials: present in environment (values not printed)" in output
    assert "PKSUPERSECRETVALUE" not in output
    assert "SKSUPERSECRETVALUE" not in output
    assert "sma_cross" in output


def test_config_check_names_the_missing_variables_instead_of_failing(
    tmp_path, capsys
) -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["journal"]["path"] = str(tmp_path / "cli.db")
    raw["alpaca"]["api_key_env"] = "CRYPTOBOT_TEST_ABSENT_KEY"
    raw["alpaca"]["api_secret_env"] = "CRYPTOBOT_TEST_ABSENT_SECRET"
    path = tmp_path / "trading.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert main(["config-check", "--config", str(path)]) == OK
    output = capsys.readouterr().out
    assert "CRYPTOBOT_TEST_ABSENT_KEY" in output
    assert "not required for simulated mode" in output


def test_a_config_with_a_live_environment_is_refused(tmp_path, capsys) -> None:
    config = _config(tmp_path, lambda raw: raw["app"].update(environment="live"))

    code = _run("config-check", config)

    assert code == REFUSED
    assert "paper-only by construction" in capsys.readouterr().err


def test_a_symbol_outside_the_allowlist_is_refused(tmp_path, capsys) -> None:
    config = _config(tmp_path, lambda raw: raw["market"].update(symbols=["DOGE/USD"]))

    code = _run("status", config)

    assert code == REFUSED
    assert "symbol_allowlist" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["status", "ledger", "orders", "decisions", "experiments"])
def test_read_only_commands_work_on_an_empty_journal(tmp_path, capsys, command) -> None:
    assert _run(command, _config(tmp_path)) == OK
    assert capsys.readouterr().out.strip(), "a reader still has something to say"


def test_an_unknown_command_is_a_usage_error(tmp_path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["teleport", "--config", _config(tmp_path)])
    assert excinfo.value.code != 0


def test_no_command_prints_help(tmp_path, capsys) -> None:
    assert main([]) == OK
    assert "usage" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# data commands
# ---------------------------------------------------------------------------


def test_data_refresh_refuses_without_credentials(tmp_path, capsys) -> None:
    config = _config(
        tmp_path,
        lambda raw: raw["alpaca"].update(
            api_key_env="CRYPTOBOT_TEST_ABSENT_KEY",
            api_secret_env="CRYPTOBOT_TEST_ABSENT_SECRET",
        ),
    )

    code = _run("data-refresh", config)

    assert code == REFUSED
    error = capsys.readouterr().err
    assert "refusing to fetch: missing credentials" in error
    assert "never paste them here" in error


def test_seed_demo_labels_its_data_as_synthetic(tmp_path, capsys) -> None:
    code = _run("seed-demo --bars 120", _config(tmp_path))
    output = capsys.readouterr().out

    assert code == OK
    assert "SYNTHETIC DATA" in output
    assert "nothing about real markets" in output

    journal = _journal(tmp_path)
    try:
        assert journal.health()["bars"] == 120
    finally:
        journal.close()


def test_data_export_round_trips_through_import(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run("seed-demo --bars 40", config) == OK
    export = tmp_path / "bars.json"

    assert main(["data-export", str(export), "--config", config]) == OK
    capsys.readouterr()

    imported = _config(tmp_path, lambda raw: raw["journal"].update(path=str(tmp_path / "b.db")))
    assert main(["data-import", str(export), "--config", imported]) == OK
    assert "imported 40/40 bars" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# trading commands
# ---------------------------------------------------------------------------


def test_run_once_analyses_without_submitting(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run("seed-demo --bars 400", config) == OK
    capsys.readouterr()

    code = _run("run-once", config)
    output = capsys.readouterr().out

    assert code == OK
    assert "preflight passed" in output
    assert "submit=no" in output
    assert "BTC/USD:" in output

    journal = _journal(tmp_path)
    try:
        assert journal.health()["decisions"] == 1
        assert journal.list_orders() == [], "run-once must not place an order"
    finally:
        journal.close()


def test_run_once_refuses_when_the_system_is_halted(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    journal = _journal(tmp_path)
    try:
        journal.set_system_state(SystemState.HALTED, "reconciliation drift")
    finally:
        journal.close()

    code = _run("run-once", config)

    assert code == FAILED
    assert "preflight FAILED" in capsys.readouterr().out


def test_pause_requires_a_reason_and_changes_nothing(tmp_path, capsys) -> None:
    config = _config(tmp_path)

    assert _run("pause", config) == REFUSED
    assert "refusing to pause: --reason is required" in capsys.readouterr().err

    journal = _journal(tmp_path)
    try:
        assert journal.get_system_state()[0] is SystemState.ACTIVE
    finally:
        journal.close()


def test_pause_then_resume_is_visible_in_the_status_view(tmp_path, capsys) -> None:
    config = _config(tmp_path)

    assert _run("pause --reason maintenance", config) == OK
    capsys.readouterr()
    assert _run("status", config) == OK
    assert "pause_entries - maintenance" in capsys.readouterr().out

    assert _run(["resume", "--reason", "window closed"], config) == OK
    capsys.readouterr()
    assert _run("status", config) == OK
    assert "active" in capsys.readouterr().out


def test_the_kill_switch_blocks_preflight_and_run_once(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run("seed-demo --bars 400", config) == OK
    assert _run(["kill", "--reason", "flash crash"], config) == OK
    capsys.readouterr()

    assert _run("preflight", config) == FAILED
    assert "kill switch engaged: flash crash" in capsys.readouterr().out

    assert _run("run-once", config) == FAILED
    assert "preflight FAILED" in capsys.readouterr().out


def test_every_state_change_is_recorded_as_a_reconciliation_event(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run("kill --reason manual", config) == OK
    capsys.readouterr()

    journal = _journal(tmp_path)
    try:
        events = [row["kind"] for row in journal.recent_reconciliations(5)]
    finally:
        journal.close()

    assert "operator_state_change" in events


def test_cancel_all_refuses_while_halted(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    journal = _journal(tmp_path)
    try:
        journal.set_system_state(SystemState.HALTED, "drift")
    finally:
        journal.close()

    code = _run("cancel-all --mode simulated", config)

    assert code == REFUSED
    assert "refusing to cancel" in capsys.readouterr().err


def test_cancel_all_needs_yes_before_it_touches_an_order(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    client_order_id = _store_open_order(tmp_path)

    assert _run("cancel-all --mode simulated", config) == REFUSED
    output = capsys.readouterr()
    assert client_order_id in output.out
    assert "refusing to cancel without --yes" in output.err

    # The confirmed run reaches the adapter, which does not know this order and
    # therefore reports the refusal instead of pretending it cancelled.
    assert _run("cancel-all --mode simulated --yes", config) == OK
    assert "broker refused" in capsys.readouterr().out


def test_reconcile_reports_a_clean_offline_state(tmp_path, capsys) -> None:
    assert _run("reconcile --mode simulated", _config(tmp_path)) == OK
    assert "clean" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# shadow AI from the command line
# ---------------------------------------------------------------------------


def test_ai_review_needs_cached_bars_first(tmp_path, capsys) -> None:
    code = _run("ai-review", _config(tmp_path))

    assert code == REFUSED
    assert "no cached bars" in capsys.readouterr().err


def test_ai_review_is_off_unless_explicitly_enabled(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run("seed-demo --bars 120", config) == OK
    capsys.readouterr()

    assert _run("ai-review", config) == OK
    assert "shadow AI is disabled" in capsys.readouterr().out


def test_ai_review_records_an_assessment_without_trading(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run("seed-demo --bars 120", config) == OK
    capsys.readouterr()

    assert _run("ai-review --enable --provider static", config) == OK
    output = capsys.readouterr().out

    assert "SHADOW ASSESSMENT (recorded; cannot influence orders)" in output
    assert "prompt_hash" in output

    journal = _journal(tmp_path)
    try:
        assert len(journal.unevaluated_assessments()) == 1
        assert journal.list_orders() == [], "an assessment is not an order"
    finally:
        journal.close()


def test_ai_review_rejects_a_provider_that_cannot_be_pinned(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run("seed-demo --bars 40", config) == OK
    capsys.readouterr()

    payload = json.dumps({"regime": "sideways", "bias": "up"})
    code = main(
        ["ai-review", "--enable", "--provider", "static", "--payload", payload, "--config", config]
    )
    assert code == FAILED
    assert "no assessment recorded" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# evaluation from the command line
# ---------------------------------------------------------------------------


def _store_closed_trade(tmp_path) -> None:
    journal = _journal(tmp_path)
    try:
        from conftest import START, make_intent
        from cryptobot.core.contracts import FillRecord

        for index, (side, price, fee) in enumerate(
            [(Side.BUY, "1000", "0.25"), (Side.SELL, "1100", "0.275")]
        ):
            intent = make_intent(
                side=side, quantity="0.01", notional="10", client_order_id=f"cb-{index}"
            )
            journal.record_intent(intent)
            journal.record_fill(
                FillRecord(
                    fill_id=f"fill-{index}",
                    broker_order_id=f"broker-{index}",
                    client_order_id=intent.client_order_id,
                    symbol="BTC/USD",
                    side=side,
                    quantity=intent.quantity,
                    price=Decimal(price),
                    ts=START + timedelta(hours=index),
                    fee=Decimal(fee),
                )
            )
    finally:
        journal.close()


def test_trade_report_evaluates_recorded_fills(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _store_closed_trade(tmp_path)

    assert _run("trade-report", config) == OK
    output = capsys.readouterr().out

    assert "TRADE EVALUATION" in output
    assert "closed trades    1" in output
    assert "+0.4750" in output, "gross 1.00 less 0.525 of fees"
    assert "EXECUTION QUALITY" in output


def test_trade_report_json_is_machine_readable(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _store_closed_trade(tmp_path)

    assert _run("trade-report --json", config) == OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["trades"]["count"] == 1
    assert payload["trades"]["net_pnl"] == "0.4750"
    assert "execution" in payload


def test_acceptance_refuses_to_certify_an_empty_journal(tmp_path, capsys) -> None:
    code = _run("acceptance --days 30", _config(tmp_path))
    output = capsys.readouterr().out

    assert code == FAILED
    assert "NOT ACCEPTED" in output
    assert "journal_present" in output
    assert "soak" in output


def test_acceptance_says_how_long_is_left(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run("seed-demo --bars 120", config) == OK
    assert _run("run-once", config) == OK
    capsys.readouterr()

    code = _run("acceptance --days 30 --min-trades 0 --min-decisions 1 --min-days 1", config)
    output = capsys.readouterr().out

    assert code == FAILED
    assert "PENDING" in output
    assert "remaining" in output


def test_challengers_refuses_a_sweep_with_nothing_to_compare(tmp_path, capsys) -> None:
    code = _run("challengers --bars 200", _config(tmp_path))

    assert code == REFUSED
    assert "nothing to compare" in capsys.readouterr().err


def test_challengers_never_promotes_on_synthetic_data(tmp_path, capsys) -> None:
    code = _run(
        "challengers --bars 300 --baseline fast_period=3 --baseline slow_period=8 "
        "--grid fast_period=3,5 --grid slow_period=8,13 --min-oos-trades 999",
        _config(tmp_path),
    )
    output = capsys.readouterr().out

    assert code == OK
    assert "CHALLENGER EVALUATION" in output
    assert "no challenger may replace the baseline" in output
    assert "SYNTHETIC" in output


# ---------------------------------------------------------------------------
# leaving a halt
# ---------------------------------------------------------------------------


def _halt(tmp_path, reason: str = "drift") -> None:
    journal = _journal(tmp_path)
    try:
        journal.set_system_state(SystemState.HALTED, reason)
    finally:
        journal.close()


def _state(tmp_path) -> SystemState:
    journal = _journal(tmp_path)
    try:
        return journal.get_system_state()[0]
    finally:
        journal.close()


def test_unhalt_requires_a_reason_and_does_nothing_without_one(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _halt(tmp_path)

    assert main(["unhalt", "--config", config]) == REFUSED
    assert "--reason is required" in capsys.readouterr().err
    assert _state(tmp_path) is SystemState.HALTED


def test_unhalt_returns_to_paused_entries_not_active(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _halt(tmp_path)

    code = _run(["unhalt", "--reason", "checked reconcile output"], config)
    output = capsys.readouterr().out

    assert code == OK
    assert "halted -> pause_entries" in output
    assert "cryptobot resume" in output
    assert _state(tmp_path) is SystemState.PAUSE_ENTRIES


def test_unhalt_can_enable_entries_when_asked_explicitly(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _halt(tmp_path)

    assert _run("unhalt --reason checked --active", config) == OK

    assert _state(tmp_path) is SystemState.ACTIVE


def test_unhalt_refuses_while_the_allocation_invariant_is_broken(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _halt(tmp_path)
    journal = _journal(tmp_path)
    try:
        journal.set_reservation("rogue", Decimal("150"))
    finally:
        journal.close()

    code = _run(["unhalt", "--reason", "checked the ledger"], config)
    error = capsys.readouterr().err

    assert code == REFUSED
    assert "refusing to unhalt" in error
    assert "invariant" in error
    assert _state(tmp_path) is SystemState.HALTED


def test_unhalt_refuses_while_an_order_is_unresolved(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _halt(tmp_path)
    journal = _journal(tmp_path)
    try:
        from conftest import make_intent

        intent = make_intent(client_order_id="cb-uncertain", quantity="0.001")
        journal.record_intent(intent)
        journal.upsert_order(
            OrderRecord(
                client_order_id=intent.client_order_id,
                symbol=intent.symbol,
                side=Side.BUY,
                quantity=intent.quantity,
                status=OrderStatus.UNCERTAIN,
                updated_at=journal.now(),
            )
        )
    finally:
        journal.close()

    code = _run(["unhalt", "--reason", "checked the ledger"], config)
    error = capsys.readouterr().err

    assert code == REFUSED
    assert "need broker confirmation" in error
    assert "cb-uncertain" in error
    assert _state(tmp_path) is SystemState.HALTED


def test_unhalt_on_a_running_system_changes_nothing(tmp_path, capsys) -> None:
    config = _config(tmp_path)

    code = _run("unhalt --reason nothing-to-do", config)

    assert code == OK
    assert "not halted" in capsys.readouterr().out
    assert _state(tmp_path) is SystemState.ACTIVE


def test_unhalt_is_recorded_for_the_audit_trail(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _halt(tmp_path, reason="allocation breach")
    assert _run(["unhalt", "--reason", "reconciled by hand"], config) == OK
    capsys.readouterr()

    journal = _journal(tmp_path)
    try:
        events = [row["kind"] for row in journal.recent_reconciliations(5)]
    finally:
        journal.close()

    assert "operator_state_change" in events


def test_resume_cannot_undo_a_halt(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    _halt(tmp_path, reason="allocation breach")

    code = _run("resume --reason done", config)
    error = capsys.readouterr().err

    assert code == REFUSED
    assert "refusing to resume" in error
    assert "halted" in error
    assert "cryptobot unhalt" in error
    assert _state(tmp_path) is SystemState.HALTED, "a refusal changes nothing"


def test_resume_cannot_undo_the_kill_switch(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run(["kill", "--reason", "flash crash"], config) == OK
    capsys.readouterr()

    code = _run(["resume", "--reason", "looks calm now"], config)

    assert code == REFUSED
    assert "killed" in capsys.readouterr().err
    assert _state(tmp_path) is SystemState.KILLED


def test_unhalt_can_release_the_kill_switch(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    assert _run(["kill", "--reason", "flash crash"], config) == OK
    capsys.readouterr()

    code = _run(["unhalt", "--reason", "market reopened"], config)
    output = capsys.readouterr().out

    assert code == OK
    assert "killed -> pause_entries" in output
    assert _state(tmp_path) is SystemState.PAUSE_ENTRIES


# ---------------------------------------------------------------------------
# credential setup
# ---------------------------------------------------------------------------


def test_env_hint_names_the_variables_without_reading_the_values(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "PKNEVERPRINTED")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "SKNEVERPRINTED")

    assert _run("env-hint --shell powershell", _config(tmp_path)) == OK
    output = capsys.readouterr().out

    assert "ALPACA_API_KEY" in output
    assert "ALPACA_API_SECRET_KEY" in output
    assert "$env:ALPACA_API_KEY" in output
    assert "set" in output
    assert "PKNEVERPRINTED" not in output
    assert "SKNEVERPRINTED" not in output


def test_env_hint_reports_unset_credentials_and_bash_syntax(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)

    assert _run("env-hint --shell bash", _config(tmp_path)) == OK
    output = capsys.readouterr().out

    assert "NOT SET" in output
    assert "required variable(s) unset" in output
    assert 'export ALPACA_API_KEY="..."' in output

