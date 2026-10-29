"""The single authoritative order path.

Every order in this application goes through :meth:`OrderService.submit`. The
sequence is deliberately ordered so that no failure mode can produce a duplicate
or an unaccounted order:

1. Refuse to proceed unless the risk verdict approved.
2. Persist the intent (primary key = deterministic ``client_order_id``).
3. Reserve allocation.
4. Persist an order row in ``PENDING_SUBMIT`` **before** touching the network.
5. Submit.
6. On an ambiguous failure, mark ``UNCERTAIN`` and stop. Recovery is by querying
   the broker with the same ``client_order_id`` - never by resubmitting.
7. Book fills to the ledger idempotently.

Fees: the Orders API returns no per-fill fee. Alpaca charges a crypto fee on the
*credited* asset (verified 2026-10-29, docs/API_CAPABILITIES.md §4): a buy is
charged in the base asset, a sell in the quote asset, and the charge only appears
as a ``CFEE``/``FEE`` account activity after the end of the trading day. Until
then the fee on a recorded fill is an *estimate* at the configured reserve rate.
:mod:`cryptobot.execution.fees` replaces it with the broker's actual figure when
the activity posts; the estimate is deliberately the taker rate so that a
maker-priced fill never leaves reservations under-funded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Sequence

from cryptobot.core.contracts import (
    FillRecord,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    RiskVerdict,
    Side,
    credited_currency,
    quote_currency,
    to_decimal,
)
from cryptobot.core.errors import (
    AmbiguousSubmission,
    ContractError,
    DefiniteRejection,
    DuplicateOrderError,
    ExecutionError,
    RiskRejected,
)
from cryptobot.core.logging import get_logger
from cryptobot.core.states import SystemState
from cryptobot.execution.base import BrokerOrder, ExecutionAdapter
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger

log = get_logger(__name__)

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OrderOutcome(str, Enum):
    FILLED = "filled"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    intent: OrderIntent
    outcome: OrderOutcome
    order: OrderRecord | None
    message: str
    filled_quantity: Decimal = Decimal("0")
    reserved: Decimal = Decimal("0")

    @property
    def submitted_to_broker(self) -> bool:
        return self.outcome in (OrderOutcome.SUBMITTED, OrderOutcome.FILLED)


class OrderService:
    def __init__(
        self,
        journal: Journal,
        ledger: AllocationLedger,
        adapter: ExecutionAdapter,
        *,
        fee_reserve_fraction: Decimal = Decimal("0.0025"),
        slippage_reserve_fraction: Decimal = Decimal("0.0025"),
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._journal = journal
        self._ledger = ledger
        self._adapter = adapter
        self._fee_fraction = to_decimal(fee_reserve_fraction, "fee_reserve_fraction")
        self._slippage_fraction = to_decimal(
            slippage_reserve_fraction, "slippage_reserve_fraction"
        )
        self._clock = clock

    # -- helpers ------------------------------------------------------------

    def reserve_amount(self, intent: OrderIntent) -> Decimal:
        if intent.side is not Side.BUY:
            return Decimal("0")
        return intent.notional * (
            Decimal(1) + self._fee_fraction + self._slippage_fraction
        )

    def _write_record(
        self,
        intent: OrderIntent,
        status: OrderStatus,
        *,
        broker_order_id: str | None = None,
        filled_quantity: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        reason: str | None = None,
        raw_status: str | None = None,
        submitted: bool = False,
    ) -> OrderRecord:
        now = self._clock()
        existing = self._journal.get_order(intent.client_order_id)
        record = OrderRecord(
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            status=status,
            updated_at=now,
            broker_order_id=broker_order_id
            or (existing.broker_order_id if existing else None),
            filled_quantity=(
                filled_quantity
                if filled_quantity is not None
                else (existing.filled_quantity if existing else Decimal("0"))
            ),
            avg_fill_price=(
                avg_fill_price
                if avg_fill_price is not None
                else (existing.avg_fill_price if existing else None)
            ),
            submitted_at=(
                (existing.submitted_at if existing else None)
                or (now if submitted else None)
            ),
            reason=reason,
            raw_status=raw_status,
        )
        self._journal.upsert_order(record)
        return record

    # -- submission ---------------------------------------------------------

    def submit(self, intent: OrderIntent, verdict: RiskVerdict) -> ExecutionResult:
        if not verdict.approved:
            raise RiskRejected(f"refusing to submit unapproved intent: {verdict.summary()}")

        self._journal.record_intent(intent)

        reserved = Decimal("0")
        if intent.side is Side.BUY:
            try:
                reserved = self._ledger.reserve(
                    intent.client_order_id,
                    self.reserve_amount(intent),
                    note="pending buy order",
                )
            except RiskRejected as exc:
                record = self._write_record(
                    intent, OrderStatus.REJECTED, reason=str(exc)[:240]
                )
                return ExecutionResult(
                    intent, OrderOutcome.REJECTED, record, f"reservation refused: {exc}"
                )

        self._write_record(intent, OrderStatus.PENDING_SUBMIT)

        try:
            broker_order = self._adapter.submit(intent)
        except AmbiguousSubmission as exc:
            record = self._write_record(
                intent,
                OrderStatus.UNCERTAIN,
                reason=str(exc)[:240],
                submitted=True,
            )
            log.error(
                "ambiguous order submission; recovery required by client_order_id",
                extra={"context": {"client_order_id": intent.client_order_id, "error": str(exc)[:200]}},
            )
            return ExecutionResult(
                intent,
                OrderOutcome.UNCERTAIN,
                record,
                f"submission outcome unknown; recover via client_order_id ({exc})",
                reserved=reserved,
            )
        except DefiniteRejection as exc:
            self._ledger.release(intent.client_order_id, note="broker rejected")
            record = self._write_record(
                intent, OrderStatus.REJECTED, reason=str(exc)[:240]
            )
            return ExecutionResult(
                intent, OrderOutcome.REJECTED, record, f"rejected: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 - never assume a rejection
            record = self._write_record(
                intent,
                OrderStatus.UNCERTAIN,
                reason=f"unexpected submit error: {exc}"[:240],
                submitted=True,
            )
            log.error(
                "unexpected order submission error treated as ambiguous",
                extra={"context": {"client_order_id": intent.client_order_id, "error": str(exc)[:200]}},
            )
            return ExecutionResult(
                intent,
                OrderOutcome.UNCERTAIN,
                record,
                f"unexpected error treated as unknown state: {exc}",
                reserved=reserved,
            )

        record = self.apply_broker_order(broker_order, intent=intent)
        outcome = (
            OrderOutcome.FILLED if record.status is OrderStatus.FILLED else OrderOutcome.SUBMITTED
        )
        return ExecutionResult(
            intent,
            outcome,
            record,
            f"broker status {record.raw_status or record.status.value}",
            filled_quantity=record.filled_quantity,
            reserved=reserved,
        )

    def record_rejection(self, intent: OrderIntent, verdict: RiskVerdict) -> ExecutionResult:
        """Persist a decision that never reached the broker (risk said no).

        The intent row is written first. ``orders`` has a foreign key to
        ``order_intents``, so recording a rejection without it fails, and a
        rejection is a decision worth keeping either way.
        """
        if self._journal.get_intent(intent.client_order_id) is None:
            self._journal.record_intent(intent)
        record = self._write_record(
            intent, OrderStatus.REJECTED, reason=verdict.summary()[:240]
        )
        return ExecutionResult(intent, OrderOutcome.REJECTED, record, verdict.summary())

    # -- broker state synchronisation --------------------------------------

    def apply_broker_order(
        self, broker_order: BrokerOrder, *, intent: OrderIntent | None = None
    ) -> OrderRecord:
        intent = intent or self._journal.get_intent(broker_order.client_order_id)
        if intent is None:
            raise ExecutionError(
                f"received broker order {broker_order.client_order_id} with no local intent"
            )

        self._book_fills(broker_order, intent)
        self._flag_over_deployment(intent)
        record = self._write_record(
            intent,
            broker_order.status,
            broker_order_id=broker_order.broker_order_id,
            filled_quantity=broker_order.filled_quantity,
            avg_fill_price=broker_order.avg_fill_price,
            reason=broker_order.reason,
            raw_status=broker_order.raw_status,
            submitted=True,
        )
        self.sync_reservation(record, intent)
        return record

    def _flag_over_deployment(self, intent: OrderIntent) -> None:
        """Record and halt when fills cost more than the reservation covered.

        A reservation is a forecast: notional plus a fee and slippage buffer. A
        gap up between sizing and execution can still make a fill cost more, and
        by then the money has left the account, so the ledger books it and
        ``committed`` exceeds ``ceiling``. That is a budget breach whoever caused
        it, and the response is an explicit record plus ``HALTED`` rather than a
        silently over-committed ledger: ``HALTED`` withdraws ``allow_new_entries``
        (see :class:`cryptobot.core.states.Capabilities`), so no further position
        can be opened while the operator unwinds this one.

        Firing here rather than inside the ledger keeps the ledger an accountant:
        it records what happened, and the order path decides what that means.
        """
        state = self._ledger.state()
        if state.invariant_holds:
            return

        over_by = state.allocated - state.ceiling
        detail = {
            "client_order_id": intent.client_order_id,
            "symbol": intent.symbol,
            "committed": format(state.committed, "f"),
            "reserved": format(state.reserved, "f"),
            "ceiling": format(state.ceiling, "f"),
            "over_by": format(over_by, "f"),
        }
        self._journal.record_reconciliation("allocation_breach", "critical", detail)
        self._journal.set_system_state(
            SystemState.HALTED,
            f"allocation breach on {intent.client_order_id}: "
            f"committed {detail['committed']} + reserved {detail['reserved']} "
            f"exceeds the {detail['ceiling']} ceiling by {detail['over_by']}; "
            "investigate and unwind before unhalting",
        )
        log.critical(
            "allocation ceiling breached by a fill; new entries halted",
            extra={"context": detail},
        )

    def _book_fills(self, broker_order: BrokerOrder, intent: OrderIntent) -> None:
        fills: list[FillRecord] = list(broker_order.fills)

        if not fills:
            fills = self._synthesise_fill_delta(broker_order, intent)

        if not fills:
            return

        existing = {f.fill_id for f in self._journal.fills_for(broker_order.client_order_id)}
        for fill in fills:
            if fill.fill_id in existing:
                continue
            if fill.side is Side.BUY:
                self._journal.record_fill(fill)
                self._ledger.release(fill.client_order_id, note="buy fill")
                # ``net_quantity`` is the quantity the account is credited with: a
                # crypto buy pays its fee out of the asset it just received.
                self._ledger.commit_buy(
                    fill.client_order_id, fill.net_quantity, fill.price, fill.fee_in_quote()
                )
            else:
                _, average_before = self._journal.realized_position(fill.symbol)
                self._journal.record_fill(fill)
                self._ledger.release(fill.client_order_id, note="sell fill")
                self._ledger.apply_sell(
                    fill.client_order_id,
                    fill.quantity,
                    fill.price,
                    fill.fee_in_quote(),
                    average_before,
                )
            existing.add(fill.fill_id)

    def _synthesise_fill_delta(
        self, broker_order: BrokerOrder, intent: OrderIntent
    ) -> list[FillRecord]:
        """Book whatever has been filled since the last recorded fill.

        The Orders API reports a *cumulative* ``filled_qty``; the individual fills
        are only available from a separate endpoint. Booking the cumulative figure
        on each poll would double count, so the delta against what this order has
        already recorded is booked instead.

        Booking deltas has a second effect that matters: a partially filled order
        updates positions, the ledger and reservations immediately, rather than
        waiting for a terminal status. An order that sits half-filled overnight is
        then visible to reconciliation instead of looking like unbacked exposure.

        The fee is estimated in the currency the broker actually charges it in.
        """
        cumulative = broker_order.filled_quantity
        if cumulative <= 0:
            return []

        booked = sum(
            (fill.quantity for fill in self._journal.fills_for(broker_order.client_order_id)),
            Decimal("0"),
        )
        delta = cumulative - booked
        if delta <= 0:
            return []

        price = broker_order.avg_fill_price
        if price is None:
            price = intent.notional / intent.quantity
        notional = delta * price

        fee_currency = credited_currency(broker_order.symbol, broker_order.side)
        if fee_currency == quote_currency(broker_order.symbol):
            # A fee denominated in what we receive as cash is a fraction of value.
            fee = notional * self._fee_fraction
        else:
            # A fee denominated in the asset we receive is a fraction of quantity.
            # Its quote value is the same either way: delta*f x price == notional*f.
            fee = delta * self._fee_fraction

        return [
            FillRecord(
                fill_id=f"cum-{format(cumulative, 'f')}",
                broker_order_id=broker_order.broker_order_id or broker_order.client_order_id,
                client_order_id=broker_order.client_order_id,
                symbol=broker_order.symbol,
                side=broker_order.side,
                quantity=delta,
                price=price,
                fee=fee,
                fee_currency=fee_currency,
                ts=self._clock(),
            )
        ]

    def sync_reservation(self, record: OrderRecord, intent: OrderIntent) -> None:
        ref = record.client_order_id
        if intent.side is not Side.BUY:
            self._ledger.release(ref, note="sell order")
            return
        if not record.status.is_open or record.remaining_quantity <= 0:
            self._ledger.release(ref, note=f"order {record.status.value}")
            return

        unit_price = record.avg_fill_price or (intent.notional / intent.quantity)
        needed = record.remaining_quantity * unit_price * (
            Decimal(1) + self._fee_fraction + self._slippage_fraction
        )
        current = self._ledger.reservation_for(ref)
        if current is not None and current == needed:
            return
        self._ledger.release(ref, note="resize reservation for remaining quantity")
        try:
            self._ledger.reserve(ref, needed, note="reserved for remaining quantity")
        except RiskRejected as exc:
            log.error(
                "could not re-reserve for a partially filled order; exposure is under-reserved",
                extra={"context": {"client_order_id": ref, "needed": str(needed), "error": str(exc)}},
            )

    # -- recovery -----------------------------------------------------------

    def recover(self, record: OrderRecord, *, grace_seconds: int) -> str:
        """Resolve an order whose true state is unknown.

        Returns ``"recovered"``, ``"lost"`` or ``"pending"``. Never resubmits.
        """
        if not record.status.needs_broker_confirmation:
            return "terminal"

        try:
            broker_order = self._adapter.get_order(record.client_order_id)
        except Exception as exc:  # noqa: BLE001 - broker unavailable is not "lost"
            log.warning(
                "recovery lookup failed; leaving order unresolved",
                extra={"context": {"client_order_id": record.client_order_id, "error": str(exc)[:200]}},
            )
            return "pending"

        if broker_order is not None:
            self.apply_broker_order(broker_order)
            return "recovered"

        age = (self._clock() - record.updated_at).total_seconds()
        if age < grace_seconds:
            return "pending"

        self._ledger.release(record.client_order_id, note="order not found at broker")
        self._journal.upsert_order(
            replace(
                record,
                status=OrderStatus.LOST,
                updated_at=self._clock(),
                reason=(
                    "broker has no record after the grace period; manual verification "
                    "required before any resubmission"
                ),
            )
        )
        self._journal.record_reconciliation(
            "order_lost",
            "critical",
            {"client_order_id": record.client_order_id, "age_seconds": age},
        )
        log.error(
            "order declared LOST; manual verification required",
            extra={"context": {"client_order_id": record.client_order_id, "age_seconds": age}},
        )
        return "lost"

    def release(self, client_order_id: str, note: str = "manual release") -> bool:
        return self._ledger.release(client_order_id, note=note)
