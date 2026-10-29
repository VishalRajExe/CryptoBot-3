"""The persistent allocation ledger.

Semantics (this is the part most systems get wrong, so it is spelled out):

``ceiling``
    Fixed capital the bot is allowed to deploy. Comes from configuration
    (default $100). It does **not** grow when the bot makes money: realised P&L
    is tracked and reported in ``realized_pnl`` but never added back to
    ``ceiling``. This is what "realised profits must not raise the allocation
    ceiling" means in practice.

``committed``
    Cost basis currently tied up in open positions: ``quantity * price``, fees
    excluded. Fees are expensed to ``realized_pnl`` when they are paid, because
    they are a cost of trading rather than capital still deployed. Two reasons
    this matters: the reconciler can compare ``committed`` directly with the
    broker's ``avg_entry_price`` basis, and a closed position releases every cent
    it committed instead of leaving amortised fees behind.

``reserved``
    Cash earmarked for buy orders that have been sent but not yet filled,
    including a fee/slippage buffer.

``available``
    ``max(0, ceiling - committed - reserved)``.

The aggregate constraint ``committed + reserved <= ceiling`` therefore applies
across *all* positions and *all* pending orders at once, not per order.

State is rebuilt from the journal on startup, so a crash cannot lose or invent
capital. Every mutation appends an event row; ``rebuild()`` replays them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Callable

from cryptobot.core.contracts import AllocationState, to_decimal
from cryptobot.core.errors import ContractError, DuplicateOrderError, ReconciliationError, RiskRejected
from cryptobot.journal.store import Journal

#: Signed ledger event kinds. ``committed`` = commit - decommit + adjust.
COMMIT = "commit"
DECOMMIT = "decommit"
REALIZE = "realize"
ADJUST = "adjust"
RESERVE = "reserve"
RELEASE = "release"


class AllocationLedger:
    def __init__(self, journal: Journal, ceiling: Decimal) -> None:
        self._journal = journal
        self._ceiling = to_decimal(ceiling, "ceiling")
        if self._ceiling <= 0:
            raise ContractError("allocation ceiling must be positive")
        self._committed = Decimal("0")
        self._realized = Decimal("0")
        self._unrealized = Decimal("0")
        self._reserved: dict[str, Decimal] = {}
        self.rebuild()

    # -- construction -------------------------------------------------------

    def rebuild(self) -> AllocationState:
        """Recompute all state from persisted events and reservations.

        A reservation larger than the whole ceiling is refused: :meth:`reserve`
        cannot produce one, so its presence means the journal was edited or
        restored from a backup that never described this account, and starting
        on top of it would invent a state that never existed. Committed capital
        above the ceiling is treated differently - see :meth:`_assert_invariant`
        - because it is a fact about money already spent rather than a claim.
        """
        committed = Decimal("0")
        realized = Decimal("0")
        for event in self._journal.ledger_events():
            amount = to_decimal(event["amount"], "amount")
            kind = event["kind"]
            if kind == COMMIT:
                committed += amount
            elif kind == DECOMMIT:
                committed -= amount
            elif kind == REALIZE:
                realized += amount
            elif kind == ADJUST:
                committed += amount
        self._committed = committed if committed > 0 else Decimal("0")
        self._realized = realized
        self._reserved = self._journal.reservations()
        state = self.state()
        if state.reserved > state.ceiling + AllocationState.EPSILON:
            raise ReconciliationError(
                f"allocation ledger invariant violated after rebuild: "
                f"committed={state.committed} reserved={state.reserved} ceiling={state.ceiling}"
            )
        return state

    # -- inspection ---------------------------------------------------------

    @property
    def ceiling(self) -> Decimal:
        return self._ceiling

    def state(self) -> AllocationState:
        return AllocationState(
            ceiling=self._ceiling,
            committed=self._committed,
            reserved=sum(self._reserved.values(), Decimal("0")),
            realized_pnl=self._realized,
            unrealized_pnl=self._unrealized,
        )

    def available(self) -> Decimal:
        return self.state().available

    def reservation_for(self, ref: str) -> Decimal | None:
        return self._reserved.get(ref)

    @property
    def reservations(self) -> dict[str, Decimal]:
        return dict(self._reserved)

    def set_unrealized(self, amount: Decimal) -> None:
        """Reporting only: P&L of open positions. Never affects the ceiling."""
        self._unrealized = to_decimal(amount, "unrealized")

    # -- mutations ----------------------------------------------------------

    def reserve(self, ref: str, amount: Decimal, note: str | None = None) -> Decimal:
        """Earmark capital for a pending buy order. Idempotent per ``ref``.

        The capacity check is repeated inside the write lock. ``available()``
        above reflects only this process's view; a second process, or one that
        restarted with orphaned rows, may hold reservations this instance has
        not seen, and those claims are just as real. ``BEGIN IMMEDIATE``
        serialises the two writers, so the second one refuses instead of both
        quietly oversubscribing the ceiling.
        """
        value = to_decimal(amount, "reserve amount")
        if value <= 0:
            raise ContractError("reservation amount must be positive")
        if ref in self._reserved:
            if self._reserved[ref] == value:
                return value
            raise DuplicateOrderError(
                f"reservation {ref} already exists with {self._reserved[ref]}, not {value}"
            )
        available = self.available()
        if value > available:
            raise RiskRejected(
                f"cannot reserve {value}: only {available} of the {self._ceiling} "
                f"allocation is available"
            )
        with self._journal.transaction():
            persisted = self._journal.reservations()
            if ref in persisted:
                if persisted[ref] == value:
                    self._reserved = dict(persisted)
                    return value
                raise DuplicateOrderError(
                    f"reservation {ref} already exists with {persisted[ref]}, not {value}"
                )
            already = sum(persisted.values(), Decimal("0"))
            projected = self._committed + already + value
            if projected > self._ceiling + AllocationState.EPSILON:
                raise RiskRejected(
                    f"cannot reserve {value}: {already} is already reserved in the "
                    f"journal against a {self._ceiling} ceiling"
                )
            self._journal.set_reservation(ref, value)
            self._journal.append_ledger_event(
                RESERVE, value, ref=ref, note=note, allocated_after=projected
            )
        self._reserved = dict(persisted)
        self._reserved[ref] = value
        self._assert_invariant()
        return value

    def release(self, ref: str, note: str | None = None) -> bool:
        """Release a reservation (order cancelled, rejected, or filled)."""
        amount = self._reserved.get(ref)
        if amount is None:
            return False
        projected = self._committed + sum(self._reserved.values(), Decimal("0")) - amount
        with self._journal.transaction():
            self._journal.delete_reservation(ref)
            self._journal.append_ledger_event(
                RELEASE, amount, ref=ref, note=note, allocated_after=projected
            )
        del self._reserved[ref]
        return True

    def commit_buy(self, ref: str, quantity: Decimal, price: Decimal, fee_quote: Decimal) -> Decimal:
        """Convert a filled buy into committed capital. Returns the basis added.

        The fee is expensed rather than capitalised: see the module docstring.
        Both movements are recorded as separate events so ``rebuild()`` replays
        exactly the same numbers.
        """
        quantity = to_decimal(quantity, "quantity")
        price = to_decimal(price, "price")
        fee_quote = to_decimal(fee_quote, "fee")
        if quantity <= 0 or price <= 0:
            raise ContractError("commit_buy requires positive quantity and price")
        basis = quantity * price
        reservation = self._reserved.pop(ref, None)
        projected_reserved = sum(self._reserved.values(), Decimal("0"))
        projected_committed = self._committed + basis
        with self._journal.transaction():
            if reservation is not None:
                self._journal.delete_reservation(ref)
            self._journal.append_ledger_event(
                COMMIT,
                basis,
                ref=ref,
                note=f"buy fill {quantity} @ {price}",
                allocated_after=projected_committed + projected_reserved,
            )
            if fee_quote:
                self._journal.append_ledger_event(
                    REALIZE,
                    -fee_quote,
                    ref=ref,
                    note="buy fee expensed",
                    allocated_after=projected_committed + projected_reserved,
                )
        self._committed = projected_committed
        self._realized -= fee_quote
        self._assert_invariant()
        return basis

    def apply_sell(
        self,
        ref: str,
        quantity: Decimal,
        price: Decimal,
        fee_quote: Decimal,
        average_entry_price: Decimal,
    ) -> Decimal:
        """Book a filled sell. Returns realised P&L for this fill.

        ``cost_basis`` is fee-free on both sides: the buy fee was already
        expensed, and the sell fee is deducted from the proceeds here. A closed
        position therefore leaves exactly nothing behind in ``committed``.
        """
        quantity = to_decimal(quantity, "quantity")
        price = to_decimal(price, "price")
        fee_quote = to_decimal(fee_quote, "fee")
        average_entry_price = to_decimal(average_entry_price, "average_entry_price")
        if quantity <= 0 or price <= 0:
            raise ContractError("apply_sell requires positive quantity and price")

        cost_basis = min(quantity * average_entry_price, self._committed)
        proceeds = quantity * price - fee_quote
        realized = proceeds - cost_basis

        reservation = self._reserved.pop(ref, None)
        projected_reserved = sum(self._reserved.values(), Decimal("0"))
        projected_committed = self._committed - cost_basis
        if projected_committed < 0:
            projected_committed = Decimal("0")
        with self._journal.transaction():
            if reservation is not None:
                self._journal.delete_reservation(ref)
            self._journal.append_ledger_event(
                DECOMMIT,
                cost_basis,
                ref=ref,
                note=f"sell fill {quantity} @ {price}",
                allocated_after=projected_committed + projected_reserved,
            )
            self._journal.append_ledger_event(
                REALIZE,
                realized,
                ref=ref,
                note="realised P&L after fees",
                allocated_after=projected_committed + projected_reserved,
            )
        self._committed = projected_committed
        self._realized += realized
        self._assert_invariant()
        return realized

    def expense_fee(self, amount: Decimal, *, ref: str, note: str) -> Decimal:
        """Book a fee discovered after the fact.

        Used when the broker posts the actual charge for a trade whose fee was an
        estimate. A positive ``amount`` is money spent, so it reduces realised P&L.
        It never touches ``committed``: the fee was never capital deployed.
        """
        value = to_decimal(amount, "fee")
        if value == 0:
            return Decimal("0")
        projected = self._committed + sum(self._reserved.values(), Decimal("0"))
        with self._journal.transaction():
            self._journal.append_ledger_event(
                REALIZE, -value, ref=ref, note=note, allocated_after=projected
            )
        self._realized -= value
        self._assert_invariant()
        return value

    def adjust(self, amount: Decimal, *, ref: str, note: str) -> None:
        """Operator correction. Requires an explicit reason."""
        if not note:
            raise ContractError("ledger adjustments require a note")
        value = to_decimal(amount, "adjust amount")
        projected = self._committed + value
        with self._journal.transaction():
            self._journal.append_ledger_event(
                ADJUST, value, ref=ref, note=note, allocated_after=projected
            )
        self._committed = projected if projected > 0 else Decimal("0")
        self._assert_invariant()

    def realized_since(self, since: datetime) -> Decimal:
        return self._journal.sum_ledger(REALIZE, since=since)

    # -- invariants ---------------------------------------------------------

    def _assert_invariant(self) -> None:
        """Refuse an impossible reservation; *record* an over-deployment.

        ``reserved > ceiling`` cannot arise from correct operation, because
        :meth:`reserve` refuses it inside the write lock. Seeing it means this
        instance's view has diverged from the journal, and continuing to mutate
        would be guesswork, so it raises.

        ``committed > ceiling`` is a different kind of event. A fill that costs
        more than the reservation predicted - a price that gapped up between
        sizing and execution - has already spent real money. The ledger's job is
        to record money that was spent, not to reject it. Raising here would
        leave the fill row written, the order record stale, and the ledger
        permanently unable to book the sell that unwinds the position, because
        every later mutation would raise on the same condition. The breach is
        recorded and the system halted instead: see
        :meth:`cryptobot.execution.service.OrderService._flag_over_deployment`,
        and the ``allocation_invariant`` check that preflight and acceptance run
        over ``state().invariant_holds``.
        """
        state = self.state()
        if state.reserved > state.ceiling + AllocationState.EPSILON:
            raise ReconciliationError(
                "allocation reservation above the ceiling: "
                f"reserved={state.reserved} > ceiling={self._ceiling}"
            )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = self.state()
        return (
            f"AllocationLedger(ceiling={state.ceiling}, committed={state.committed}, "
            f"reserved={state.reserved}, available={state.available})"
        )
