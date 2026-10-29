"""Reconciliation between local state and the broker.

Purpose: make it impossible to *silently* lose track of an order or position.

The reconciler:

* adopts any broker order we have no local record for (critical - it means
  something submitted an order we do not know about);
* re-queries every locally unresolved order by ``client_order_id`` and, if the
  broker has never heard of it after the grace period, marks it ``LOST``
  (critical, requires a human - it is never resubmitted automatically);
* compares broker positions with the position implied by our own fills;
* compares ledger committed capital with the cost basis of broker positions;
* halts new entries on any critical drift, because at that point the application
  can no longer claim to know its exposure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from cryptobot.core.contracts import OrderRecord, to_decimal
from cryptobot.core.errors import ReconciliationError
from cryptobot.core.logging import get_logger
from cryptobot.core.states import Severity, SystemState
from cryptobot.execution.base import ExecutionAdapter
from cryptobot.execution.service import OrderService
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger

log = get_logger(__name__)
UTC = timezone.utc

QUANTITY_TOLERANCE = Decimal("0.00000001")
VALUE_TOLERANCE = Decimal("0.01")

#: How far a position may differ before it is treated as unexplained drift.
#: A crypto buy is charged its fee in the base asset, so the local holding and the
#: broker's reported quantity can legitimately differ by one fee. Treating that as
#: critical would halt the bot on its first real trade; tolerating *twice* the fee
#: budget still catches anything material, because a missing or duplicated fill is
#: tens of percent of the position, not 25 basis points.
DEFAULT_FEE_TOLERANCE = Decimal("0.0025")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Drift:
    kind: str
    severity: Severity
    detail: Mapping[str, Any]

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"[{self.severity.value}] {self.kind}: {dict(self.detail)}"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    ts: datetime
    drifts: tuple[Drift, ...] = ()
    recovered: int = 0
    lost: int = 0
    pending: int = 0
    halted: bool = False
    checked_orders: int = 0
    checked_positions: int = 0
    source: str = ""

    @property
    def ok(self) -> bool:
        return not self.drifts

    @property
    def critical(self) -> tuple[Drift, ...]:
        return tuple(d for d in self.drifts if d.severity is Severity.CRITICAL)

    def summary(self) -> str:
        # Naming the broker is not decoration: the same journal reconciled against
        # a fresh simulator and against the broker produces opposite answers, and
        # "clean" without a source was read as the wrong one more than once.
        against = f" against {self.source}" if self.source else ""
        if self.ok:
            return (
                f"reconciliation clean{against} ({self.checked_orders} order(s), "
                f"{self.checked_positions} position(s), {self.recovered} recovered)"
            )
        return (
            f"reconciliation found {len(self.drifts)} drift(s){against} "
            f"({len(self.critical)} critical); recovered={self.recovered} lost={self.lost} "
            f"pending={self.pending}" + ("; system HALTED" if self.halted else "")
        )


class Reconciler:
    def __init__(
        self,
        journal: Journal,
        ledger: AllocationLedger,
        adapter: ExecutionAdapter,
        order_service: OrderService,
        *,
        grace_seconds: int = 120,
        fee_tolerance_fraction: Decimal = DEFAULT_FEE_TOLERANCE,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._journal = journal
        self._ledger = ledger
        self._adapter = adapter
        self._orders = order_service
        self._grace_seconds = grace_seconds
        self._fee_tolerance = to_decimal(fee_tolerance_fraction, "fee_tolerance_fraction")
        self._clock = clock

    def reconcile(self, *, context: str = "periodic", halt_on_critical: bool = True) -> ReconciliationReport:
        self._refuse_foreign_state()
        now = self._clock()
        drifts: list[Drift] = []
        recovered = lost = pending = 0

        try:
            account = self._adapter.account()
            self._journal.save_balance(
                cash=account.cash,
                equity=account.equity,
                buying_power=account.buying_power,
                non_marginable_buying_power=account.non_marginable_buying_power,
                currency=account.currency,
                source=self._adapter.name,
            )
            broker_positions = list(self._adapter.positions())
            self._journal.save_position_snapshots(
                [p.to_snapshot(now, source=self._adapter.name) for p in broker_positions]
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never guess
            drifts.append(
                Drift("broker_unavailable", Severity.CRITICAL, {"error": str(exc)[:300], "context": context})
            )
            report = self._finish(now, drifts, 0, 0, 0, context, halt_on_critical)
            return report

        # 1. resolve locally unresolved orders
        unresolved = self._journal.unresolved_orders()
        for record in unresolved:
            outcome = self._orders.recover(record, grace_seconds=self._grace_seconds)
            if outcome == "recovered":
                recovered += 1
            elif outcome == "lost":
                lost += 1
                drifts.append(
                    Drift(
                        "order_lost",
                        Severity.CRITICAL,
                        {"client_order_id": record.client_order_id, "symbol": record.symbol},
                    )
                )
            else:
                pending += 1

        # 2. positions: ours (from fills) vs broker's
        broker_by_symbol = {p.symbol: p for p in broker_positions}
        symbols = sorted(set(broker_by_symbol) | set(self._local_symbols()))
        for symbol in symbols:
            local_qty, local_avg = self._journal.realized_position(symbol)
            broker = broker_by_symbol.get(symbol)
            broker_qty = broker.quantity if broker else Decimal("0")
            quantity_tolerance = max(
                QUANTITY_TOLERANCE, abs(broker_qty) * self._fee_tolerance * 2
            )
            if abs(local_qty - broker_qty) > quantity_tolerance:
                drifts.append(
                    Drift(
                        "position_mismatch",
                        Severity.CRITICAL,
                        {
                            "symbol": symbol,
                            "local_qty": format(local_qty, "f"),
                            "broker_qty": format(broker_qty, "f"),
                            "local_avg": format(local_avg, "f"),
                        },
                    )
                )

        # 3. ledger committed capital vs broker position cost basis
        broker_cost = sum(
            (p.reported_cost_basis for p in broker_positions), Decimal("0")
        )
        committed = self._ledger.state().committed
        value_tolerance = max(VALUE_TOLERANCE, abs(broker_cost) * self._fee_tolerance * 2)
        if abs(committed - broker_cost) > value_tolerance:
            drifts.append(
                Drift(
                    "ledger_position_mismatch",
                    Severity.CRITICAL,
                    {
                        "ledger_committed": format(committed, "f"),
                        "broker_cost_basis": format(broker_cost, "f"),
                    },
                )
            )

        # 4. broker orders we have no intent for
        try:
            broker_open = list(self._adapter.list_open_orders())
        except Exception as exc:  # noqa: BLE001
            broker_open = []
            drifts.append(
                Drift("open_orders_unavailable", Severity.CRITICAL, {"error": str(exc)[:300]})
            )
        for broker_order in broker_open:
            if self._journal.get_intent(broker_order.client_order_id) is None:
                drifts.append(
                    Drift(
                        "unknown_broker_order",
                        Severity.CRITICAL,
                        {
                            "client_order_id": broker_order.client_order_id,
                            "broker_order_id": broker_order.broker_order_id,
                            "status": broker_order.status.value,
                        },
                    )
                )

        # 5. allocation invariant
        state = self._ledger.state()
        if not state.invariant_holds:
            drifts.append(
                Drift(
                    "allocation_invariant",
                    Severity.CRITICAL,
                    {
                        "committed": format(state.committed, "f"),
                        "reserved": format(state.reserved, "f"),
                        "ceiling": format(state.ceiling, "f"),
                    },
                )
            )

        return self._finish(
            now, drifts, recovered, lost, pending, context, halt_on_critical,
            checked_orders=len(unresolved), checked_positions=len(broker_positions),
        )

    # -- internals ----------------------------------------------------------

    def _refuse_foreign_state(self) -> None:
        """Refuse to compare a journal written by one broker against another.

        A simulator starts flat and a funded paper account does not, so
        reconciling one's journal against the other reports a position mismatch
        that does not exist - and halts a healthy system. That happened: the
        default ``--mode`` was ``simulated``, so the routine command reported
        critical drift against the real account. Each position snapshot records
        which adapter produced it, so the mistake is detectable *before* anything
        is compared, and saying so beats a fabricated drift.
        """
        sources = {snapshot.source for snapshot in self._journal.latest_positions()}
        if not sources:
            return
        # Snapshots written before the source column kept the adapter name use
        # the words "broker" and "simulator". They still distinguish a real
        # account from a simulator, which is the distinction that matters here.
        ours = {self._adapter.name}
        ours.add("simulator" if getattr(self._adapter, "is_simulated", False) else "broker")
        foreign = sources - ours
        if not foreign:
            return
        raise ReconciliationError(
            f"refusing to reconcile: recorded positions came from "
            f"{', '.join(sorted(foreign))} but this run asked "
            f"{self._adapter.name}; pass the matching --mode"
        )

    def _local_symbols(self) -> set[str]:
        return {position.symbol for position in self._journal.latest_positions()}

    def _finish(
        self,
        now: datetime,
        drifts: list[Drift],
        recovered: int,
        lost: int,
        pending: int,
        context: str,
        halt_on_critical: bool,
        *,
        checked_orders: int = 0,
        checked_positions: int = 0,
    ) -> ReconciliationReport:
        critical = [d for d in drifts if d.severity is Severity.CRITICAL]
        halted = False
        if critical and halt_on_critical:
            reason = f"reconciliation drift ({context}): " + "; ".join(d.kind for d in critical)
            self._journal.set_system_state(SystemState.HALTED, reason)
            halted = True
            log.error("system halted after reconciliation drift", extra={"context": {"reason": reason}})

        for drift in drifts:
            self._journal.record_reconciliation(drift.kind, drift.severity.value, dict(drift.detail))

        report = ReconciliationReport(
            ts=now,
            drifts=tuple(drifts),
            recovered=recovered,
            lost=lost,
            pending=pending,
            halted=halted,
            checked_orders=checked_orders,
            checked_positions=checked_positions,
            source=self._adapter.name,
        )
        if report.ok:
            self._journal.record_reconciliation("clean", Severity.INFO.value, {"context": context})
        return report
