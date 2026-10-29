"""Acceptance harness for the paper-trading soak.

The starting point for Phase 12 of the plan is "run paper trading for at least
30 days". That sentence is only checkable if the evidence it needs is named up
front, so this module states the criteria and evaluates them against the
journal:

* the soak actually lasted the required number of days,
* activity was continuous rather than clustered on one afternoon,
* enough decisions and enough *closed* trades exist for the numbers to mean
  anything,
* the allocation invariant held,
* no order was left unresolved,
* no critical reconciliation drift was ever recorded,
* measured slippage stayed inside its budget.

Duration is reported separately as ``pending`` rather than as a failure: on day
three the project is not broken, it is simply not finished. Every other
criterion that fails is a real failure. The report never claims readiness it
cannot evidence.

Duration is measured from soak heartbeats, not from the wall clock. A journal
whose first decision is thirty days old proves that the bot *once* ran; it does
not prove that it ran for thirty days. ``cryptobot.soak`` supplies the covered
interval, and time during which the process was not running counts as zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from cryptobot.config.settings import Settings
from cryptobot.core.errors import CryptobotError, ReconciliationError
from cryptobot.core.states import SystemState
from cryptobot.evaluation.trades import evaluate_execution, evaluate_trades
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger
from cryptobot.soak import SoakCoverage, compute_coverage

ZERO = Decimal("0")
UTC = timezone.utc


@dataclass(frozen=True)
class AcceptanceCriteria:
    """Thresholds an operator sets before the soak, not after seeing results."""

    required_days: Decimal = Decimal("30")
    min_days_with_decisions: int = 20
    min_decisions: int = 50
    min_trades: int = 10
    min_reconciliations: int = 30
    max_critical_drifts: int = 0
    max_unresolved_orders: int = 0
    max_average_slippage_bps: Decimal = Decimal("50")
    #: Gaps longer than the heartbeat threshold. Zero means the soak must be
    #: continuous; a single outage is a finding, not a rounding error.
    max_gaps: int = 0
    max_restarts: int = 10

    def __post_init__(self) -> None:
        if self.required_days <= ZERO:
            raise ValueError("required_days must be positive")
        for name in (
            "min_days_with_decisions",
            "min_decisions",
            "min_trades",
            "min_reconciliations",
            "max_critical_drifts",
            "max_unresolved_orders",
            "max_gaps",
            "max_restarts",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class Criterion:
    name: str
    ok: bool
    detail: str
    #: A non-blocking criterion can be unmet without the soak being wrong;
    #: duration is the only such criterion, and it is reported as pending.
    blocking: bool = True

    def line(self) -> str:
        state = "PASS" if self.ok else ("PENDING" if not self.blocking else "FAIL")
        return f"  {state:<8}{self.name:<26}{self.detail}"


@dataclass(frozen=True)
class AcceptanceReport:
    criteria: tuple[Criterion, ...]
    elapsed_days: Decimal
    required_days: Decimal
    generated_at: datetime
    state: SystemState = SystemState.ACTIVE
    notes: tuple[str, ...] = field(default_factory=tuple)
    covered_days: Decimal = ZERO
    observed_days: Decimal = ZERO
    coverage: SoakCoverage = field(default_factory=SoakCoverage)

    @property
    def ok(self) -> bool:
        return all(criterion.ok for criterion in self.criteria)

    @property
    def failures(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if not c.ok and c.blocking)

    @property
    def pending(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if not c.ok and not c.blocking)

    @property
    def remaining_days(self) -> Decimal:
        return max(ZERO, self.required_days - self.elapsed_days)

    def summary(self) -> str:
        progress = (
            f"{self.elapsed_days:.1f} of {self.required_days:.0f} days"
            f" ({self.remaining_days:.1f} remaining)"
            if self.remaining_days > ZERO
            else f"{self.elapsed_days:.1f} of {self.required_days:.0f} days (complete)"
        )
        lines = [
            "ACCEPTANCE REPORT",
            f"  generated        {self.generated_at.isoformat()}",
            f"  system state     {self.state.value}",
            f"  soak             {progress}",
        ]
        if self.observed_days != self.elapsed_days:
            lines.append(
                f"  observed         {self.observed_days:.1f} day(s) of wall clock; "
                f"only {self.elapsed_days:.1f} day(s) evidenced by heartbeats"
            )
        lines.append("")
        lines.extend(criterion.line() for criterion in self.criteria)
        lines.append("")
        if self.ok:
            lines.append("  VERDICT: ACCEPTED - every criterion is evidenced by the journal")
        elif self.failures:
            lines.append(f"  VERDICT: NOT ACCEPTED - {len(self.failures)} criterion(a) failed")
        else:
            lines.append(
                f"  VERDICT: NOT ACCEPTED - the soak needs {self.remaining_days:.1f} more day(s)"
            )
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "generated_at": self.generated_at.isoformat(),
            "state": self.state.value,
            "elapsed_days": str(self.elapsed_days.quantize(Decimal("0.01"))),
            "required_days": str(self.required_days),
            "remaining_days": str(self.remaining_days.quantize(Decimal("0.01"))),
            "covered_days": str(self.covered_days.quantize(Decimal("0.01"))),
            "observed_days": str(self.observed_days.quantize(Decimal("0.01"))),
            "coverage": self.coverage.as_dict(),
            "criteria": [
                {
                    "name": criterion.name,
                    "ok": criterion.ok,
                    "blocking": criterion.blocking,
                    "detail": criterion.detail,
                }
                for criterion in self.criteria
            ],
        }

    def raise_if_not_ready(self) -> None:
        if not self.ok:
            raise CryptobotError(self.summary())


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def run_acceptance(
    journal: Journal,
    settings: Settings,
    *,
    criteria: AcceptanceCriteria | None = None,
    now: datetime | None = None,
) -> AcceptanceReport:
    """Evaluate the soak criteria against the journal as it stands."""
    criteria = criteria or AcceptanceCriteria()
    now = now or datetime.now(UTC)
    ceiling = settings.allocation.ceiling_usd

    state, _reason, _updated = journal.get_system_state()
    health = journal.health()
    first, _last = journal.activity_span()
    days = journal.decision_days()

    # Duration is coverage, not elapsed time. ``observed`` stays in the report so
    # an operator can see how much wall clock the heartbeats fail to explain.
    coverage = compute_coverage(journal, now=now)
    elapsed = coverage.covered_days

    checks: list[Criterion] = []

    checks.append(
        Criterion(
            "journal_present",
            first is not None,
            (
                f"first decision {first.isoformat()}"
                if first
                else "no decisions recorded; nothing can be evidenced"
            ),
        )
    )

    checks.append(
        Criterion(
            "soak_instrumented",
            coverage.instrumented,
            (
                f"{coverage.checkpoints} heartbeat(s) across "
                f"{coverage.processes} process(es)"
                if coverage.window_id is not None
                else "no soak window; run the bot with `cryptobot paper-run` to record one"
            ),
        )
    )

    checks.append(
        Criterion(
            "soak_continuity",
            len(coverage.gaps) <= criteria.max_gaps,
            f"{len(coverage.gaps)} gap(s) longer than "
            f"{coverage.gap_threshold_seconds}s within budget {criteria.max_gaps}",
        )
    )

    checks.append(
        Criterion(
            "restart_budget",
            coverage.restarts <= criteria.max_restarts,
            f"{coverage.restarts} restart(s) within budget {criteria.max_restarts}",
        )
    )

    checks.append(
        Criterion(
            "soak_duration",
            elapsed >= criteria.required_days,
            f"{elapsed.quantize(Decimal('0.01'))} of {criteria.required_days} day(s) covered",
            blocking=False,
        )
    )

    checks.append(
        Criterion(
            "continuous_coverage",
            len(days) >= criteria.min_days_with_decisions,
            f"{len(days)} day(s) with decisions; {criteria.min_days_with_decisions} required",
        )
    )

    checks.append(
        Criterion(
            "decision_volume",
            health["decisions"] >= criteria.min_decisions,
            f"{health['decisions']} decision(s); {criteria.min_decisions} required",
        )
    )

    report = evaluate_trades(journal, ceiling=ceiling)
    checks.append(
        Criterion(
            "closed_trades",
            report.count >= criteria.min_trades,
            f"{report.count} closed trade(s); {criteria.min_trades} required",
        )
    )

    # ``AllocationLedger`` refuses to construct when the invariant is already
    # broken. That refusal is the finding, not an excuse to abandon the report:
    # every other criterion still has to be evaluated.
    try:
        allocation = AllocationLedger(journal, ceiling).state()
    except ReconciliationError as exc:
        checks.append(Criterion("allocation_invariant", False, str(exc)))
    else:
        deployed = allocation.committed + allocation.reserved
        checks.append(
            Criterion(
                "allocation_invariant",
                deployed <= ceiling + allocation.EPSILON,
                f"committed {_fmt(allocation.committed)} + reserved "
                f"{_fmt(allocation.reserved)} <= ceiling {_fmt(ceiling)}",
            )
        )

    unresolved = journal.unresolved_orders()
    checks.append(
        Criterion(
            "unresolved_orders",
            len(unresolved) <= criteria.max_unresolved_orders,
            f"{len(unresolved)} unresolved order(s) within budget "
            f"{criteria.max_unresolved_orders}",
        )
    )

    critical = journal.count_reconciliations("critical")
    checks.append(
        Criterion(
            "reconciliation_critical",
            critical <= criteria.max_critical_drifts,
            f"{critical} critical drift(s) within budget {criteria.max_critical_drifts}",
        )
    )

    total_reconciliations = journal.count_reconciliations()
    checks.append(
        Criterion(
            "reconciliation_volume",
            total_reconciliations >= criteria.min_reconciliations,
            f"{total_reconciliations} reconciliation(s); "
            f"{criteria.min_reconciliations} required",
        )
    )

    execution = evaluate_execution(journal)
    # No fills means slippage is undefined, not bad: the ``closed_trades``
    # criterion is what requires there to have been trading at all.
    checks.append(
        Criterion(
            "execution_slippage",
            execution.count == 0
            or execution.average_slippage_bps <= criteria.max_average_slippage_bps,
            (
                f"{execution.count} filled order(s), average "
                f"{execution.average_slippage_bps:+.2f} bps within budget "
                f"{criteria.max_average_slippage_bps} bps"
                if execution.count
                else "no filled orders to measure"
            ),
        )
    )

    healthy_state = state in (SystemState.ACTIVE, SystemState.PAUSE_ENTRIES)
    checks.append(
        Criterion(
            "operational_state",
            healthy_state,
            f"{state.value}"
            + ("" if healthy_state else " - a halted or killed bot cannot complete a soak"),
        )
    )

    notes: list[str] = []
    if report.unbalanced:
        notes.append(
            f"{len(report.unbalanced)} fill(s) could not be matched to a position; "
            "the trade statistics above are incomplete"
        )
    if execution.skipped:
        notes.append(f"{len(execution.skipped)} filled order(s) had no usable reference price")
    if coverage.api_errors:
        notes.append(f"{coverage.api_errors} API error(s) recorded during the soak")

    return AcceptanceReport(
        criteria=tuple(checks),
        elapsed_days=elapsed,
        required_days=criteria.required_days,
        generated_at=now,
        state=state,
        notes=tuple(notes),
        covered_days=coverage.covered_days,
        observed_days=coverage.observed_days,
        coverage=coverage,
    )


__all__ = [
    "AcceptanceCriteria",
    "AcceptanceReport",
    "Criterion",
    "run_acceptance",
]
