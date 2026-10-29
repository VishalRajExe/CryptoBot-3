"""Section 5: attempt to break the $100 allocation cap and confirm it holds.

The cap is the one number the whole project rests on, so it is attacked from
every direction the plan names: concurrent orders, partial fills, cancels,
rejects, ambiguous submissions, restarts and a corrupted journal.

The invariant under attack is ``committed + reserved <= ceiling`` after *every*
mutation, not merely at the end of a tidy sequence.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal
from typing import Sequence

import pytest

from cryptobot.core.contracts import AllocationState, OrderStatus, RiskVerdict, Side
from cryptobot.core.errors import (
    AmbiguousSubmission,
    ContractError,
    DefiniteRejection,
    ReconciliationError,
    RiskRejected,
)
from cryptobot.core.states import SystemState
from cryptobot.execution.base import BrokerAccount, BrokerOrder, BrokerPosition
from cryptobot.execution.service import OrderOutcome, OrderService
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import COMMIT, DECOMMIT, AllocationLedger

from conftest import START, Clock, make_intent

PRICE = Decimal("20000")
CEILING = Decimal("100")
EPSILON = AllocationState.EPSILON


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _approved() -> RiskVerdict:
    return RiskVerdict(True, (), (), CEILING, START)


def _denied(reason: str = "cap") -> RiskVerdict:
    return RiskVerdict(False, (reason,), (), CEILING, START)


def _order(
    intent,
    *,
    status: OrderStatus = OrderStatus.ACCEPTED,
    filled: str = "0",
    price: Decimal = PRICE,
) -> BrokerOrder:
    return BrokerOrder(
        client_order_id=intent.client_order_id,
        broker_order_id=f"b-{intent.client_order_id}",
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        status=status,
        filled_quantity=Decimal(filled),
        avg_fill_price=price if Decimal(filled) > 0 else None,
        submitted_at=START,
        raw_status=status.value,
    )


class ScriptedAdapter:
    """Accepts everything unless told otherwise; never fills by itself."""

    name = "scripted"
    is_paper = True
    is_simulated = False

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.submitted: list[str] = []
        self.known: dict[str, BrokerOrder] = {}

    def submit(self, intent):
        self.submitted.append(intent.client_order_id)
        if self.error is not None:
            raise self.error
        order = _order(intent)
        self.known[intent.client_order_id] = order
        return order

    def get_order(self, client_order_id: str):
        return self.known.get(client_order_id)

    def list_open_orders(self) -> Sequence[BrokerOrder]:
        return []

    def cancel(self, client_order_id: str) -> bool:
        return False

    def positions(self) -> Sequence[BrokerPosition]:
        return []

    def account(self) -> BrokerAccount:
        return BrokerAccount(
            cash=CEILING,
            equity=CEILING,
            buying_power=CEILING,
            non_marginable_buying_power=CEILING,
        )


def _service(journal: Journal, ledger: AllocationLedger, adapter, clock: Clock):
    return OrderService(journal, ledger, adapter, clock=clock)


def _assert_cap(ledger: AllocationLedger) -> None:
    state = ledger.state()
    assert state.committed >= 0
    assert state.reserved >= 0
    assert state.committed + state.reserved <= state.ceiling + EPSILON


# ---------------------------------------------------------------------------
# concurrent orders
# ---------------------------------------------------------------------------


def test_overlapping_buy_orders_cannot_oversubscribe(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)

    first = service.submit(
        make_intent(client_order_id="cb-a", notional="60", bar_ts=START), _approved()
    )
    second = service.submit(
        make_intent(client_order_id="cb-b", notional="60", bar_ts=START), _approved()
    )

    assert first.outcome is OrderOutcome.SUBMITTED
    assert second.outcome is OrderOutcome.REJECTED
    assert "reservation refused" in second.message
    assert adapter.submitted == ["cb-a"], "a refused order must not reach the broker"
    assert journal.get_order("cb-b").status is OrderStatus.REJECTED
    state = ledger.state()
    assert state.reserved == Decimal("60.3"), "only the first order holds capital"
    assert state.available == Decimal("39.7")
    _assert_cap(ledger)


def test_a_second_process_cannot_oversubscribe_the_same_journal(
    journal: Journal,
) -> None:
    # Two instances rebuilt at the same instant, as two processes would be. Each
    # one's in-memory view says $100 is free; the journal is the arbiter.
    first = AllocationLedger(journal, CEILING)
    second = AllocationLedger(journal, CEILING)

    first.reserve("x", Decimal("60"))

    with pytest.raises(RiskRejected):
        second.reserve("y", Decimal("60"))

    assert journal.reservations() == {"x": Decimal("60")}
    # What actually fits is still allowed: no over-correction in the other direction.
    assert second.reserve("z", Decimal("40")) == Decimal("40")
    assert sum(journal.reservations().values()) == CEILING


def test_an_orphaned_reservation_survives_a_restart(journal: Journal) -> None:
    crashed = AllocationLedger(journal, CEILING)
    crashed.reserve("orphan", Decimal("60"), note="process killed before release")
    del crashed  # the process is gone; its reservation row is not

    restarted = AllocationLedger(journal, CEILING)

    assert restarted.state().reserved == Decimal("60")
    assert restarted.available() == Decimal("40")
    with pytest.raises(RiskRejected):
        restarted.reserve("next", Decimal("50"))
    assert restarted.reserve("next", Decimal("40")) == Decimal("40")
    _assert_cap(restarted)


# ---------------------------------------------------------------------------
# partial fills
# ---------------------------------------------------------------------------


def test_partial_fills_keep_the_cap_at_every_step(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)
    intent = make_intent(client_order_id="cb-partial", quantity="0.002", notional="40")
    assert service.reserve_amount(intent) == Decimal("40.2000")

    assert service.submit(intent, _approved()).outcome is OrderOutcome.SUBMITTED
    assert ledger.state().reserved == Decimal("40.2000")

    for step in range(1, 11):
        cumulative = Decimal("0.0002") * step
        status = OrderStatus.FILLED if step == 10 else OrderStatus.PARTIALLY_FILLED
        service.apply_broker_order(
            _order(intent, status=status, filled=str(cumulative))
        )
        _assert_cap(ledger)

        if step == 5:
            state = ledger.state()
            # Half filled: 0.001 * 20000 * (1 - 0.0025 fee) committed, and the
            # remainder of the order still holds its own reservation.
            assert state.committed == Decimal("19.950000")
            assert state.reserved == Decimal("20.1000")
            assert state.committed + state.reserved < Decimal("40.2")

    state = ledger.state()
    assert state.committed == Decimal("39.900000"), "fee is expensed, not capitalised"
    assert state.reserved == Decimal("0")
    assert state.available == Decimal("60.100000")
    _assert_cap(ledger)


def test_a_partial_fill_cannot_re_reserve_more_than_remains(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)
    # Sized at 20000: 95 of notional reserves 95.475, which fits under the cap.
    intent = make_intent(client_order_id="cb-big", quantity="0.00475", notional="95")
    assert service.submit(intent, _approved()).outcome is OrderOutcome.SUBMITTED
    assert ledger.state().reserved == Decimal("95.4750")

    # The fill, however, happens 10% higher at 22000. The remainder can no longer
    # be covered: 76.8075 committed + 27.6375 needed = 104.445 > 100.
    service.apply_broker_order(
        _order(
            intent,
            status=OrderStatus.PARTIALLY_FILLED,
            filled="0.0035",
            price=Decimal("22000"),
        )
    )

    state = ledger.state()
    assert state.committed == Decimal("76.807500")
    assert state.reserved == 0, "the uncovered remainder stays unreserved, not overdrawn"
    assert journal.get_order("cb-big").remaining_quantity == Decimal("0.00125")
    _assert_cap(ledger)


# ---------------------------------------------------------------------------
# cancels
# ---------------------------------------------------------------------------


def test_a_cancelled_order_returns_every_cent(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)

    for index in range(25):
        intent = make_intent(
            client_order_id=f"cb-cancel-{index}",
            notional="40",
            bar_ts=START + timedelta(hours=index),
        )
        service.submit(intent, _approved())
        service.apply_broker_order(_order(intent, status=OrderStatus.CANCELED))

        state = ledger.state()
        assert state.reserved == 0
        assert state.committed == 0
        assert state.available == CEILING
        _assert_cap(ledger)

    assert journal.reservations() == {}


def test_an_order_that_fills_nothing_and_expires_releases_its_reservation(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)
    intent = make_intent(client_order_id="cb-expire", notional="90")
    service.submit(intent, _approved())
    assert ledger.state().reserved == Decimal("90.4500")

    service.apply_broker_order(_order(intent, status=OrderStatus.EXPIRED))

    assert ledger.state().reserved == 0
    assert ledger.available() == CEILING


# ---------------------------------------------------------------------------
# rejects
# ---------------------------------------------------------------------------


def test_a_risk_rejection_never_reaches_the_ledger_or_the_broker(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)
    intent = make_intent(client_order_id="cb-risk-no", notional="80")

    result = service.record_rejection(intent, _denied("daily loss limit"))

    assert result.outcome is OrderOutcome.REJECTED
    assert adapter.submitted == []
    assert journal.reservations() == {}
    assert ledger.state().committed == 0
    assert journal.get_order("cb-risk-no").status is OrderStatus.REJECTED


def test_a_broker_rejection_releases_the_reservation(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter(error=DefiniteRejection("insufficient balance"))
    service = _service(journal, ledger, adapter, clock)
    intent = make_intent(client_order_id="cb-broker-no", notional="70")

    result = service.submit(intent, _approved())

    assert result.outcome is OrderOutcome.REJECTED
    assert journal.reservations() == {}
    assert ledger.state().reserved == 0
    assert ledger.available() == CEILING


# ---------------------------------------------------------------------------
# ambiguous submissions
# ---------------------------------------------------------------------------


def test_an_ambiguous_submission_keeps_its_reservation(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter(error=AmbiguousSubmission("read timeout"))
    service = _service(journal, ledger, adapter, clock)
    intent = make_intent(client_order_id="cb-ambiguous", quantity="0.002", notional="40")

    result = service.submit(intent, _approved())

    assert result.outcome is OrderOutcome.UNCERTAIN
    # Releasing here would let the bot spend the same $40 twice while the order
    # may still be live at the broker.
    assert ledger.reservation_for("cb-ambiguous") == Decimal("40.2000")
    assert journal.get_order("cb-ambiguous").status is OrderStatus.UNCERTAIN
    _assert_cap(ledger)


def test_a_recovered_ambiguous_order_books_its_fill_without_double_counting(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter(error=AmbiguousSubmission("gateway closed"))
    service = _service(journal, ledger, adapter, clock)
    intent = make_intent(client_order_id="cb-recover", quantity="0.002", notional="40")
    service.submit(intent, _approved())

    service.apply_broker_order(
        _order(intent, status=OrderStatus.FILLED, filled="0.002")
    )
    service.apply_broker_order(
        _order(intent, status=OrderStatus.FILLED, filled="0.002")
    )

    state = ledger.state()
    assert state.committed == Decimal("39.900000"), "a repeated poll must not rebook"
    assert state.reserved == 0
    assert len(journal.fills_for("cb-recover")) == 1
    _assert_cap(ledger)


def test_an_uncertain_order_together_with_new_entries_stays_inside_the_cap(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter(error=AmbiguousSubmission("504"))
    service = _service(journal, ledger, adapter, clock)
    uncertain = make_intent(client_order_id="cb-unc", notional="70")
    service.submit(uncertain, _approved())

    adapter.error = None
    follow_up = make_intent(client_order_id="cb-follow", notional="35", bar_ts=START)
    result = service.submit(follow_up, _approved())

    assert result.outcome is OrderOutcome.REJECTED
    assert ledger.reservation_for("cb-unc") is not None
    _assert_cap(ledger)


# ---------------------------------------------------------------------------
# restarts
# ---------------------------------------------------------------------------


def test_a_restart_replays_partial_fill_state_exactly(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)
    intent = make_intent(client_order_id="cb-restart", quantity="0.002", notional="40")
    service.submit(intent, _approved())
    service.apply_broker_order(
        _order(intent, status=OrderStatus.PARTIALLY_FILLED, filled="0.0006")
    )
    before = ledger.state()

    restarted = AllocationLedger(journal, CEILING)

    assert restarted.state().committed == before.committed
    assert restarted.state().reserved == before.reserved
    assert restarted.available() == before.available
    _assert_cap(restarted)


def test_a_rebuilt_ledger_refuses_a_journal_that_is_already_broken(
    journal: Journal,
) -> None:
    journal.set_reservation("rogue", Decimal("150"))

    with pytest.raises(ReconciliationError) as caught:
        AllocationLedger(journal, CEILING)

    assert "invariant violated" in str(caught.value)


# ---------------------------------------------------------------------------
# corrupt state
# ---------------------------------------------------------------------------


def _raw_event(journal: Journal, kind: str, amount: str) -> None:
    journal.conn.execute(
        "INSERT INTO ledger_events (ts, kind, amount, ref, note, allocated_after) "
        "VALUES (?, ?, ?, 'raw', 'corrupt row', '0')",
        (journal.now().isoformat(), kind, amount),
    )


def test_a_non_numeric_amount_is_refused_rather_than_guessed(
    journal: Journal, clock: Clock
) -> None:
    _raw_event(journal, COMMIT, "not-a-number")

    with pytest.raises(ContractError):
        AllocationLedger(journal, CEILING)


def test_an_over_large_decommit_cannot_make_committed_negative(
    journal: Journal,
) -> None:
    _raw_event(journal, COMMIT, "50")
    _raw_event(journal, DECOMMIT, "80")

    rebuilt = AllocationLedger(journal, CEILING)

    assert rebuilt.state().committed == 0, "clamped, and still a valid state"
    assert rebuilt.available() == CEILING


def test_a_negative_reservation_is_refused(
    journal: Journal,
) -> None:
    journal.set_reservation("negative", Decimal("-5"))

    with pytest.raises(ContractError):
        AllocationLedger(journal, CEILING)


def test_a_corrupt_reservation_blocks_new_entries_but_is_visible(
    journal: Journal,
) -> None:
    healthy = AllocationLedger(journal, CEILING)
    journal.set_reservation("corrupt", Decimal("150"))

    with pytest.raises(ReconciliationError):
        AllocationLedger(journal, CEILING)

    # The healthy instance still reports what the journal now claims, so the
    # problem is visible in a status read rather than hidden until the restart.
    assert healthy.state().reserved == 0
    assert sum(journal.reservations().values()) == Decimal("150")


# ---------------------------------------------------------------------------
# a fill that costs more than its reservation
# ---------------------------------------------------------------------------


def test_a_fill_above_its_reservation_halts_and_is_recorded_not_refused(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    """The reservation is a forecast; the fill is the fact.

    A gap up between sizing and execution can cost more than the ceiling allows.
    The money has already gone by then, so the ledger records it and the order
    path halts. Refusing to book it instead would leave the fill recorded, the
    order record stale, and every later mutation - including the sell that
    unwinds the position - raising on the same condition.
    """
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)
    intent = make_intent(client_order_id="cb-gap", quantity="0.00475", notional="95")
    assert service.submit(intent, _approved()).outcome is OrderOutcome.SUBMITTED

    service.apply_broker_order(
        _order(
            intent,
            status=OrderStatus.FILLED,
            filled="0.00475",
            price=Decimal("22000"),
        )
    )

    state = ledger.state()
    assert state.committed == Decimal("104.238750")
    assert not state.invariant_holds, "the breach is visible in the state, not hidden"
    assert journal.get_order("cb-gap").status is OrderStatus.FILLED, "no stale record"

    kinds = [row["kind"] for row in journal.recent_reconciliations(limit=5)]
    assert "allocation_breach" in kinds
    assert journal.recent_reconciliations(limit=1)[0]["severity"] == "critical"

    system_state, reason, _ = journal.get_system_state()
    assert system_state is SystemState.HALTED, "new entries must stop"
    assert "allocation breach" in reason

    # The position is still unwindable, which is the whole point of recording
    # rather than raising.
    exit_intent = make_intent(
        side=Side.SELL,
        client_order_id="cb-gap-exit",
        quantity="0.00475",
        notional="104.5",
    )
    journal.record_intent(exit_intent)
    service.apply_broker_order(
        _order(
            exit_intent,
            status=OrderStatus.FILLED,
            filled="0.00475",
            price=Decimal("22000"),
        )
    )

    after = ledger.state()
    assert after.committed == 0
    assert after.invariant_holds


# ---------------------------------------------------------------------------
# randomized attack
# ---------------------------------------------------------------------------


def test_a_randomized_order_path_never_breaks_the_cap(
    journal: Journal, ledger: AllocationLedger, clock: Clock
) -> None:
    rng = random.Random(5150)
    adapter = ScriptedAdapter()
    service = _service(journal, ledger, adapter, clock)
    live: list = []

    for step in range(150):
        action = rng.choice(["submit", "cancel", "partial", "fill", "reject"])
        if not live:
            action = "submit"

        if action == "submit":
            # Quantity is derived from notional at the market price so that the
            # intent is self-consistent: an order whose quantity and notional
            # disagree would be rejected outright by the risk layer, and would
            # reserve a forecast unrelated to the fill it can actually produce.
            notional = Decimal(rng.randrange(5, 45))
            intent = make_intent(
                client_order_id=f"cb-r{step}",
                quantity=format(notional / PRICE, "f"),
                notional=format(notional, "f"),
                bar_ts=START + timedelta(hours=step),
            )
            result = service.submit(intent, _approved())
            if result.outcome is OrderOutcome.SUBMITTED:
                live.append(intent)
        elif action == "reject":
            intent = make_intent(
                client_order_id=f"cb-x{step}",
                notional=str(rng.randrange(5, 45)),
                bar_ts=START + timedelta(hours=step),
            )
            service.record_rejection(intent, _denied("random"))
        else:
            intent = live.pop(rng.randrange(len(live)))
            if action == "cancel":
                service.apply_broker_order(_order(intent, status=OrderStatus.CANCELED))
            elif action == "fill":
                service.apply_broker_order(
                    _order(intent, status=OrderStatus.FILLED, filled=str(intent.quantity))
                )
            else:
                booked = journal.get_order(intent.client_order_id).filled_quantity
                step_quantity = intent.quantity / Decimal("4")
                cumulative = min(intent.quantity, booked + step_quantity)
                service.apply_broker_order(
                    _order(
                        intent,
                        status=OrderStatus.PARTIALLY_FILLED,
                        filled=str(cumulative),
                    )
                )
                live.append(intent)
        _assert_cap(ledger)
        assert sum(journal.reservations().values()) == ledger.state().reserved

    _assert_cap(AllocationLedger(journal, CEILING))
