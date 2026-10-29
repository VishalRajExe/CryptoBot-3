"""Section 7: a strategy change needs a person to approve it.

The challenger report answers "is this better on out-of-sample evidence?". It
must not also answer "and shall we apply it?", because nothing in a program can
give consent. These tests pin the gate: evidence a human signs off, a digest
bound to the exact numbers they were shown, and a durable record of who decided
what and why.
"""

from __future__ import annotations

import shlex
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import yaml

from cryptobot.cli import OK, REFUSED, _promote_command, main
from cryptobot.config.settings import APP_ROOT
from cryptobot.core.errors import ContractError
from cryptobot.evaluation.challengers import (
    ChallengerReport,
    PromotionRule,
    judge,
)
from cryptobot.evaluation.promotion import (
    Approval,
    Evidence,
    Promotion,
    PromotionRefused,
    approve,
    evidence_from,
)
from cryptobot.journal.store import Journal
from cryptobot.research.backtest import BacktestConfig, BacktestResult, TradeRecord
from cryptobot.research.dataset import BarDataset

from conftest import START, make_bars

UTC = timezone.utc
APPROVED_AT = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _result(*, pnls: list[str], ceiling: str = "100") -> BacktestResult:
    trades = [
        TradeRecord(
            symbol="BTC/USD",
            entry_ts=START,
            exit_ts=START + timedelta(hours=index + 1),
            entry_price=Decimal("100"),
            exit_price=Decimal("100") + Decimal(pnl),
            quantity=Decimal("0.1"),
            fees=Decimal("0"),
            net_pnl=Decimal(pnl),
            bars_held=1,
            exit_reason="signal",
        )
        for index, pnl in enumerate(pnls)
    ]
    equity = Decimal(ceiling)
    curve = [(START, equity)]
    for pnl in pnls:
        equity += Decimal(pnl)
        curve.append((START + timedelta(hours=1), equity))
    return BacktestResult(
        strategy_name="fake",
        strategy_version="fake@test",
        params={},
        dataset=BarDataset("BTC/USD", "1h", tuple(make_bars(["100", "101", "102"]))),
        trades=trades,
        equity_curve=curve,
        config=BacktestConfig(ceiling=Decimal(ceiling)),
    )


def _report_and_verdict(*, params: dict | None = None, winning: bool = True):
    rule = PromotionRule(min_out_of_sample_trades=5)
    baseline = _result(pnls=["1"] * 10)
    challenger = _result(pnls=["2"] * 12 if winning else ["-1"] * 12)
    verdict = judge(
        baseline,
        baseline,
        challenger,
        name="Fake(fast=5)",
        params=params or {"fast": 5},
        rule=rule,
    )
    report = ChallengerReport(
        baseline_params={},
        baseline_in_sample=baseline,
        baseline_out_of_sample=baseline,
        verdicts=(verdict,),
        rule=rule,
    )
    return report, verdict


def _evidence(**overrides) -> Evidence:
    base = {
        "strategy": "sma_cross",
        "params": {"fast": 5, "slow": 20},
        "out_of_sample_return_pct": Decimal("3.10"),
        "out_of_sample_drawdown_pct": Decimal("1.20"),
        "out_of_sample_trades": 14,
        "baseline_return_pct": Decimal("2.05"),
    }
    base.update(overrides)
    return Evidence(**base)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def test_a_rejected_challenger_produces_no_evidence_to_approve() -> None:
    report, verdict = _report_and_verdict(winning=False)

    with pytest.raises(PromotionRefused) as caught:
        evidence_from(report, verdict, strategy="sma_cross")

    assert "not promote" in str(caught.value)
    assert "does not beat" in str(caught.value)


def test_insufficient_evidence_produces_no_evidence_either() -> None:
    rule = PromotionRule(min_out_of_sample_trades=500)
    baseline = _result(pnls=["1"] * 10)
    verdict = judge(
        baseline,
        baseline,
        _result(pnls=["2"] * 12),
        name="Fake",
        params={"fast": 5},
        rule=rule,
    )
    report = ChallengerReport({}, baseline, baseline, (verdict,), rule)

    with pytest.raises(PromotionRefused) as caught:
        evidence_from(report, verdict, strategy="sma_cross")

    assert "insufficient_evidence" in str(caught.value)


def test_a_promotable_challenger_becomes_evidence_with_its_real_numbers() -> None:
    report, verdict = _report_and_verdict(params={"fast": 5})

    evidence = evidence_from(report, verdict, strategy="sma_cross")

    assert evidence.strategy == "sma_cross"
    assert evidence.params == {"fast": 5}
    assert evidence.out_of_sample_trades == 12
    assert evidence.out_of_sample_return_pct == verdict.out_of_sample.total_return_pct
    assert evidence.baseline_return_pct == report.baseline_out_of_sample.total_return_pct


def test_the_digest_names_the_parameters_as_well_as_the_numbers() -> None:
    mine = _evidence()

    assert mine.digest == _evidence().digest, "equal evidence, equal digest"
    assert mine.digest != _evidence(params={"fast": 6, "slow": 20}).digest
    assert mine.digest != _evidence(out_of_sample_return_pct=Decimal("3.11")).digest
    assert mine.digest != _evidence(out_of_sample_trades=15).digest
    assert mine.digest != _evidence(strategy="donchian_breakout").digest


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------


def test_a_named_human_is_required() -> None:
    with pytest.raises(PromotionRefused) as caught:
        approve(_evidence(), approved_by="   ", reason="better", approved_at=APPROVED_AT)

    assert "name of the person" in str(caught.value)


def test_a_reason_is_required() -> None:
    with pytest.raises(PromotionRefused) as caught:
        approve(_evidence(), approved_by="Vishal", reason="  ", approved_at=APPROVED_AT)

    assert "needs a reason" in str(caught.value)


def test_approval_must_name_the_digest_the_operator_saw() -> None:
    with pytest.raises(PromotionRefused) as caught:
        approve(
            _evidence(),
            approved_by="Vishal",
            reason="better out-of-sample return",
            approved_at=APPROVED_AT,
            expect_digest="0" * 64,
        )

    assert "does not match this evidence" in str(caught.value)


def test_the_matching_digest_is_accepted_and_recorded() -> None:
    evidence = _evidence()

    promotion = approve(
        evidence,
        approved_by="Vishal",
        reason="better out-of-sample return",
        approved_at=APPROVED_AT,
        expect_digest=evidence.digest,
    )

    assert promotion.digest == evidence.digest
    assert promotion.params == {"fast": 5, "slow": 20}
    assert promotion.approval.approved_by == "Vishal"
    assert promotion.approval.approved_at == APPROVED_AT


def test_an_approval_cannot_be_reused_for_different_evidence() -> None:
    approval = Approval(
        approved_by="Vishal",
        approved_at=APPROVED_AT,
        reason="better out-of-sample return",
        evidence_digest=_evidence().digest,
    )

    with pytest.raises(PromotionRefused) as caught:
        Promotion(_evidence(params={"fast": 9, "slow": 20}), approval)

    assert "different digest" in str(caught.value)


def test_an_approval_must_be_timestamped_unambiguously() -> None:
    with pytest.raises(PromotionRefused) as caught:
        approve(
            _evidence(),
            approved_by="Vishal",
            reason="better",
            approved_at=datetime(2024, 3, 1, 12, 0),
        )

    assert "timezone-aware" in str(caught.value)


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


def _promotion() -> Promotion:
    return approve(
        _evidence(),
        approved_by="Vishal",
        reason="better out-of-sample return with a smaller drawdown",
        approved_at=APPROVED_AT,
    )


def test_recording_a_promotion_writes_one_audit_row(journal: Journal) -> None:
    promotion = _promotion()

    promotion_id = promotion.record(journal)

    rows = journal.promotions()
    assert len(rows) == 1
    row = rows[0]
    assert row["promotion_id"] == promotion_id
    assert row["strategy"] == "sma_cross"
    assert row["approved_by"] == "Vishal"
    assert row["reason"].startswith("better out-of-sample")
    assert row["evidence_digest"] == promotion.digest
    assert row["out_of_sample_trades"] == 14
    assert Decimal(row["out_of_sample_return_pct"]) == Decimal("3.10")


def test_a_recorded_promotion_can_be_found_by_its_digest(journal: Journal) -> None:
    promotion = _promotion()
    promotion.record(journal)

    assert journal.find_promotion(promotion.digest)["approved_by"] == "Vishal"
    assert journal.find_promotion("f" * 64) is None


def test_recording_does_not_change_how_the_bot_trades(journal: Journal) -> None:
    before = journal.get_system_state()

    _promotion().record(journal)

    assert journal.get_system_state() == before, "an approval is not a state change"
    assert journal.promotions(limit=1)[0]["params_json"].startswith("{")


def test_the_journal_refuses_an_anonymous_promotion(journal: Journal) -> None:
    with pytest.raises(ContractError) as caught:
        journal.record_promotion(
            strategy="sma_cross",
            params={},
            evidence_digest="a" * 64,
            approved_by=" ",
            approved_at=APPROVED_AT,
            reason="because",
            out_of_sample_return_pct=Decimal("1"),
            out_of_sample_drawdown_pct=Decimal("1"),
            out_of_sample_trades=1,
            baseline_return_pct=Decimal("0"),
        )

    assert "requires approved_by" in str(caught.value)


# ---------------------------------------------------------------------------
# the operator's round trip
# ---------------------------------------------------------------------------


def _options(command: str) -> dict[str, str]:
    """The printed command as option -> value, dropping the program and verb."""
    argv = shlex.split(command)[2:]
    return dict(zip(argv[0::2], argv[1::2]))


def test_the_printed_command_reproduces_the_digest() -> None:
    """The command challengers prints must approve exactly what it printed."""
    evidence = _evidence()

    options = _options(_promote_command(evidence))

    rebuilt = Evidence(
        strategy=options["--strategy"],
        params={"fast": 5, "slow": 20},
        out_of_sample_return_pct=Decimal(options["--oos-return-pct"]),
        out_of_sample_drawdown_pct=Decimal(options["--oos-drawdown-pct"]),
        out_of_sample_trades=int(options["--oos-trades"]),
        baseline_return_pct=Decimal(options["--baseline-return-pct"]),
    )

    assert options["--evidence"] == evidence.digest
    assert rebuilt.digest == evidence.digest


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


CONFIG_PATH = APP_ROOT / "config" / "trading.yaml"


def _config(tmp_path) -> str:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["journal"]["path"] = str(tmp_path / "promote.db")
    path = tmp_path / "trading.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return str(path)


def _arguments(evidence: Evidence, *, approve_by: str, reason: str) -> list[str]:
    """The printed command as argv for ``main`` (which omits the program name)."""
    argv = shlex.split(_promote_command(evidence))[1:]
    argv[argv.index("--approve-by") + 1] = approve_by
    argv[argv.index("--reason") + 1] = reason
    return argv


def test_the_cli_dry_run_records_nothing(tmp_path, capsys) -> None:
    evidence = _evidence()

    code = main(
        [
            *_arguments(evidence, approve_by="Vishal", reason="better"),
            "--dry-run",
            "--config",
            _config(tmp_path),
        ]
    )

    output = capsys.readouterr().out
    assert code == OK
    assert "STRATEGY PROMOTION APPROVED" in output
    assert "dry run" in output


def test_the_cli_refuses_a_digest_that_is_not_the_evidence(tmp_path, capsys) -> None:
    argv = _arguments(_evidence(), approve_by="Vishal", reason="better")
    argv[argv.index("--evidence") + 1] = "b" * 64

    code = main([*argv, "--config", _config(tmp_path)])

    assert code == REFUSED
    assert "does not match this evidence" in capsys.readouterr().err


def test_the_cli_refuses_an_unnamed_approver(tmp_path, capsys) -> None:
    code = main(
        [
            *_arguments(_evidence(), approve_by="", reason="better"),
            "--config",
            _config(tmp_path),
        ]
    )

    assert code == REFUSED
    assert "name of the person" in capsys.readouterr().err


def test_challengers_prints_an_approval_command_and_records_nothing(
    tmp_path, capsys, monkeypatch
) -> None:
    from cryptobot import cli

    report, _ = _report_and_verdict()
    monkeypatch.setattr(cli, "run_challengers", lambda *a, **k: report)
    config = _config(tmp_path)

    code = main(
        [
            "challengers",
            "--bars",
            "60",
            "--grid",
            "fast_period=5",
            "--min-oos-trades",
            "1",
            "--config",
            config,
        ]
    )

    output = capsys.readouterr().out
    assert code == OK
    assert "APPROVAL REQUIRED - nothing has been changed" in output
    assert "cryptobot promote --strategy" in output
    assert _evidence().digest not in output, "the digest is the report's, not a fixture's"
    assert not (tmp_path / "promote.db").exists(), "challengers must not open the journal"


def test_the_cli_records_the_approval_once(tmp_path, capsys) -> None:
    config = _config(tmp_path)
    argv = _arguments(_evidence(), approve_by="Vishal", reason="better out-of-sample")

    assert main([*argv, "--config", config]) == OK
    first = capsys.readouterr().out
    assert "recorded" in first

    assert main([*argv, "--config", config]) == OK
    second = capsys.readouterr().out
    assert "already approved" in second

    journal = Journal(str(tmp_path / "promote.db")).initialize()
    try:
        assert len(journal.promotions()) == 1
    finally:
        journal.close()
