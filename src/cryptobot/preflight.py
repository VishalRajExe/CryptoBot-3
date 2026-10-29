"""Startup preflight.

Run before anything can place an order. Every check is read-only and every
failure is explicit: the application fails closed (no entries) rather than
starting in a state it cannot justify.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from cryptobot.config.settings import Settings
from cryptobot.core.errors import HaltedError
from cryptobot.core.states import Severity, SystemState
from cryptobot.data.alpaca_data import MarketDataService
from cryptobot.data.validation import assert_fresh
from cryptobot.execution.alpaca_paper import assert_paper_base_url
from cryptobot.execution.base import ExecutionAdapter
from cryptobot.journal.schema import SCHEMA_VERSION
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    severity: Severity = Severity.CRITICAL


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.severity is Severity.CRITICAL)

    @property
    def failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(c for c in self.checks if not c.ok and c.severity is Severity.CRITICAL)

    @property
    def warnings(self) -> tuple[PreflightCheck, ...]:
        return tuple(c for c in self.checks if not c.ok and c.severity is not Severity.CRITICAL)

    def summary(self) -> str:
        failed = len(self.failures)
        return (
            f"preflight {'passed' if self.ok else 'FAILED'} "
            f"({len(self.checks)} checks, {failed} failure(s), {len(self.warnings)} warning(s))"
        )

    def raise_if_failed(self) -> None:
        if self.ok:
            return
        details = "; ".join(f"{c.name}: {c.detail}" for c in self.failures)
        raise HaltedError(f"preflight failed - refusing to trade: {details}")


def run_preflight(
    settings: Settings,
    journal: Journal,
    ledger: AllocationLedger,
    *,
    adapter: ExecutionAdapter | None = None,
    data_service: MarketDataService | None = None,
    require_broker: bool = False,
    now: datetime | None = None,
) -> PreflightReport:
    now = now or datetime.now(UTC)
    checks: list[PreflightCheck] = []

    def add(name: str, ok: bool, detail: str, severity: Severity = Severity.CRITICAL) -> None:
        checks.append(PreflightCheck(name, ok, detail, severity))

    add(
        "paper_environment",
        settings.app.environment == "paper",
        f"environment={settings.app.environment}",
    )

    try:
        base = assert_paper_base_url(settings.alpaca.trading_base_url)
        add("paper_endpoint", True, base)
    except Exception as exc:  # noqa: BLE001
        add("paper_endpoint", False, str(exc))

    add(
        "database_schema",
        journal.schema_version == SCHEMA_VERSION,
        f"version={journal.schema_version} expected={SCHEMA_VERSION} path={journal.path}",
    )

    state = ledger.state()
    add(
        "allocation_invariant",
        state.invariant_holds,
        f"committed={state.committed} reserved={state.reserved} ceiling={state.ceiling}",
    )

    system_state, reason, updated = journal.get_system_state()
    if system_state is SystemState.HALTED:
        add(
            "system_state",
            False,
            f"HALTED since {updated.isoformat() if updated else 'unknown'}: {reason}; "
            "run `cryptobot unhalt --reason ...` after investigating",
        )
    elif system_state is SystemState.KILLED:
        add("system_state", False, f"kill switch engaged: {reason}")
    else:
        add("system_state", True, system_state.value)

    add(
        "clock",
        2024 <= now.year <= 2100,
        f"system time {now.isoformat()}",
        Severity.WARNING if now.year > 2100 else Severity.CRITICAL,
    )

    unresolved = journal.unresolved_orders()
    if unresolved:
        names = ", ".join(o.client_order_id for o in unresolved[:5])
        add(
            "unresolved_orders",
            False,
            f"{len(unresolved)} order(s) awaiting broker confirmation: {names}",
            Severity.CRITICAL if require_broker else Severity.WARNING,
        )
    else:
        add("unresolved_orders", True, "none")

    missing = settings.alpaca.missing_credential_names()
    if require_broker:
        add(
            "credentials",
            not missing,
            "present" if not missing else f"missing env vars: {', '.join(missing)}",
        )
    else:
        add(
            "credentials",
            not missing,
            "present" if not missing else f"not set ({', '.join(missing)}) - local modes only",
            Severity.WARNING,
        )

    if require_broker and adapter is not None:
        try:
            info = adapter.preflight() if hasattr(adapter, "preflight") else {}
            add(
                "broker_connection",
                True,
                f"{adapter.name} ok; account_status={info.get('account_status', 'n/a')} "
                f"equity={info.get('equity', 'n/a')}",
            )
        except Exception as exc:  # noqa: BLE001
            add("broker_connection", False, str(exc)[:300])

    if data_service is not None:
        for symbol in settings.market.symbols:
            bar = journal.latest_bar(symbol, settings.market.timeframe)
            if bar is None:
                add(
                    "data_freshness",
                    False,
                    f"{symbol}: no bars",
                    Severity.CRITICAL if require_broker else Severity.WARNING,
                )
                continue
            try:
                assert_fresh(bar, now, settings.market.max_bar_age_seconds)
                add("data_freshness", True, f"{symbol} @ {bar.ts.isoformat()}")
            except Exception as exc:  # noqa: BLE001
                add(
                    "data_freshness",
                    False,
                    f"{symbol}: {exc}",
                    Severity.CRITICAL if require_broker else Severity.WARNING,
                )

    return PreflightReport(tuple(checks))
