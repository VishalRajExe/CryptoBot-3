"""Promotion: the one step an evaluation report cannot take for itself.

A challenger that clears :class:`cryptobot.evaluation.challengers.PromotionRule`
is a *candidate*. Applying it changes what the bot will do with real money, so
it needs a person to say so, on the record. This module makes that statement an
object rather than a convention:

* :class:`Evidence` names exactly what is being approved - the strategy, the
  parameter set, and the out-of-sample numbers - and reduces it to a digest.
* :class:`Approval` records who approved it, when, why, and which digest they
  were shown.
* :func:`approve` refuses anything else: no named human, no reason, or a digest
  belonging to different evidence. A :class:`Promotion` cannot be constructed
  whose approval and evidence disagree, so there is no code path that promotes
  first and asks later.

Nothing here edits configuration. ``cryptobot promote`` records the approval and
prints the parameter set; making the change stays a deliberate edit by the
operator, in a file, under version control. That separation is the point - a
program that both selects a strategy and applies it has no approval step at all,
only a log line.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from cryptobot.core import jsonutil
from cryptobot.core.errors import ContractError, CryptobotError
from cryptobot.evaluation.challengers import PROMOTE, ChallengerReport, ChallengerVerdict
from cryptobot.journal.store import Journal


class PromotionRefused(CryptobotError):
    """A promotion was attempted without evidence a human could stand behind."""


@dataclass(frozen=True)
class Evidence:
    """The numbers a promotion rests on, and their digest.

    The digest covers the parameter set as well as the statistics, so an
    approval of one challenger cannot be reused for another that happens to
    score similarly.
    """

    strategy: str
    params: Mapping[str, object]
    out_of_sample_return_pct: Decimal
    out_of_sample_drawdown_pct: Decimal
    out_of_sample_trades: int
    baseline_return_pct: Decimal

    def __post_init__(self) -> None:
        if not self.strategy.strip():
            raise ContractError("evidence must name the strategy it applies to")
        if self.out_of_sample_trades < 0:
            raise ContractError("out_of_sample_trades cannot be negative")

    @property
    def digest(self) -> str:
        """Stable across runs: key-sorted JSON, decimals as strings."""
        payload = jsonutil.dumps(self.as_dict()).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "params": {str(key): str(value) for key, value in self.params.items()},
            "out_of_sample_return_pct": format(self.out_of_sample_return_pct, "f"),
            "out_of_sample_drawdown_pct": format(self.out_of_sample_drawdown_pct, "f"),
            "out_of_sample_trades": self.out_of_sample_trades,
            "baseline_return_pct": format(self.baseline_return_pct, "f"),
        }


@dataclass(frozen=True)
class Approval:
    """A named person, a time, a reason, and the evidence they were shown."""

    approved_by: str
    approved_at: datetime
    reason: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if not self.approved_by.strip():
            raise PromotionRefused("an approval needs the name of the person who gave it")
        if not self.reason.strip():
            raise PromotionRefused(
                "an approval needs a reason; 'looks good' is not a reason you can"
                " audit in a month"
            )
        if not self.evidence_digest.strip():
            raise PromotionRefused("an approval must name the evidence it was given for")
        if self.approved_at.tzinfo is None:
            raise PromotionRefused("approved_at must be timezone-aware")

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat(),
            "reason": self.reason,
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True)
class Promotion:
    """Approved evidence. Constructing one requires a matching approval."""

    evidence: Evidence
    approval: Approval

    def __post_init__(self) -> None:
        if self.approval.evidence_digest != self.evidence.digest:
            raise PromotionRefused(
                "the approval names a different digest than this evidence; approving"
                " numbers you were not shown is not approval"
            )

    @property
    def strategy(self) -> str:
        return self.evidence.strategy

    @property
    def params(self) -> dict[str, object]:
        return dict(self.evidence.params)

    @property
    def digest(self) -> str:
        return self.evidence.digest

    def record(self, journal: Journal) -> int:
        """Persist the approval. The only write this package can make.

        It cannot place, cancel, or size anything, and it cannot change a
        setting: it appends the fact that a human approved this exact evidence.
        """
        return journal.record_promotion(
            strategy=self.evidence.strategy,
            params=self.evidence.params,
            evidence_digest=self.evidence.digest,
            approved_by=self.approval.approved_by,
            approved_at=self.approval.approved_at,
            reason=self.approval.reason,
            out_of_sample_return_pct=self.evidence.out_of_sample_return_pct,
            out_of_sample_drawdown_pct=self.evidence.out_of_sample_drawdown_pct,
            out_of_sample_trades=self.evidence.out_of_sample_trades,
            baseline_return_pct=self.evidence.baseline_return_pct,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.evidence.strategy,
            "params": dict(self.evidence.params),
            "evidence_digest": self.digest,
            "approval": self.approval.as_dict(),
            "out_of_sample_return_pct": format(
                self.evidence.out_of_sample_return_pct, "f"
            ),
            "out_of_sample_drawdown_pct": format(
                self.evidence.out_of_sample_drawdown_pct, "f"
            ),
            "out_of_sample_trades": self.evidence.out_of_sample_trades,
            "baseline_return_pct": format(self.evidence.baseline_return_pct, "f"),
        }

    def summary(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in self.evidence.params.items()) or "(defaults)"
        return "\n".join(
            [
                "STRATEGY PROMOTION APPROVED",
                f"  strategy      {self.evidence.strategy}",
                f"  parameters    {params}",
                f"  out-of-sample {self.evidence.out_of_sample_return_pct:+.2f}%"
                f" vs baseline {self.evidence.baseline_return_pct:+.2f}%"
                f"  drawdown {self.evidence.out_of_sample_drawdown_pct:.2f}%"
                f"  {self.evidence.out_of_sample_trades} trade(s)",
                f"  approved by   {self.approval.approved_by}"
                f" at {self.approval.approved_at.isoformat()}",
                f"  reason        {self.approval.reason}",
                f"  evidence      {self.digest}",
                "",
                "  Recorded, not applied: edit the strategy parameters in your"
                " configuration",
                "  and restart deliberately. Nothing was changed by this command.",
            ]
        )


def evidence_from(
    report: ChallengerReport, verdict: ChallengerVerdict, *, strategy: str
) -> Evidence:
    """Turn a promotable verdict into the evidence object a human approves.

    A verdict that is not ``promote`` has no evidence worth approving, so this
    raises rather than producing a candidate nobody should apply.
    """
    if verdict.status != PROMOTE:
        reasons = "; ".join(verdict.reasons) or "no reason recorded"
        raise PromotionRefused(
            f"{verdict.name} is {verdict.status}, not {PROMOTE}: {reasons}"
        )
    return Evidence(
        strategy=strategy,
        params=dict(verdict.params),
        out_of_sample_return_pct=verdict.out_of_sample.total_return_pct,
        out_of_sample_drawdown_pct=verdict.out_of_sample.max_drawdown_pct,
        out_of_sample_trades=len(verdict.out_of_sample.trades),
        baseline_return_pct=report.baseline_out_of_sample.total_return_pct,
    )


def approve(
    evidence: Evidence,
    *,
    approved_by: str,
    reason: str,
    approved_at: datetime,
    expect_digest: str | None = None,
) -> Promotion:
    """Approve ``evidence`` on behalf of a named human.

    ``expect_digest`` is what the operator was shown. Passing it lets a stale or
    mistyped digest fail here instead of silently approving whatever the numbers
    happen to be now, which is the failure mode that makes approval theatre.
    """
    if expect_digest is not None and expect_digest.strip().lower() != evidence.digest:
        raise PromotionRefused(
            f"digest {expect_digest.strip()} does not match this evidence"
            f" ({evidence.digest}); re-run the challengers to see the numbers you"
            " are approving"
        )
    approval = Approval(
        approved_by=approved_by,
        approved_at=approved_at,
        reason=reason,
        evidence_digest=evidence.digest,
    )
    return Promotion(evidence, approval)


__all__ = [
    "Approval",
    "Evidence",
    "Promotion",
    "PromotionRefused",
    "approve",
    "evidence_from",
]
