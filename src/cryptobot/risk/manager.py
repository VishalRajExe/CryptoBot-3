"""Deterministic risk management.

Hard rules, encoded as pure functions of explicit inputs:

* No LLM, no network, no clock side effects. Given the same
  :class:`RiskContext` and intent, the verdict is identical.
* Every check runs and is recorded, even after one fails, so the rejection reason
  is complete rather than "first failure wins".
* The aggregate allocation check is the important one: it is not "this order is
  < $100", it is "all committed capital + all reservations + this order + its
  expected costs still fit inside the ceiling".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from cryptobot.config.settings import RiskConfig
from cryptobot.core.contracts import (
    AllocationState,
    Bar,
    InstrumentSpec,
    OrderIntent,
    RiskCheck,
    RiskVerdict,
    Side,
    to_decimal,
)
from cryptobot.core.states import Capabilities, SystemState


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Everything the risk manager is allowed to consider."""

    now: datetime
    system_state: SystemState
    allocation: AllocationState
    latest_bar: Bar | None
    instrument: InstrumentSpec | None
    position_quantity: Decimal = Decimal("0")
    orders_today: int = 0
    realized_pnl_today: Decimal = Decimal("0")
    peak_equity: Decimal = Decimal("0")
    current_equity: Decimal = Decimal("0")
    recent_fingerprints: frozenset[str] = frozenset()
    max_bar_age_seconds: int = 7200
    spread_bps: Decimal | None = None

    def capabilities(self) -> Capabilities:
        return Capabilities.for_state(self.system_state)


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    # -- individual checks --------------------------------------------------

    def evaluate(self, intent: OrderIntent, ctx: RiskContext) -> RiskVerdict:
        cfg = self.config
        checks: list[RiskCheck] = []
        ceiling = ctx.allocation.ceiling

        def add(name: str, passed: bool, detail: str = "") -> None:
            checks.append(RiskCheck(name, passed, detail))

        caps = ctx.capabilities()
        add(
            "system_state",
            caps.allow_new_entries,
            f"state={ctx.system_state.value} entries_allowed={caps.allow_new_entries}",
        )

        add(
            "symbol_allowlist",
            intent.symbol in cfg.symbol_allowlist,
            f"{intent.symbol} in {list(cfg.symbol_allowlist)}",
        )

        instrument = ctx.instrument
        add(
            "instrument_available",
            instrument is not None and instrument.tradable,
            "no instrument spec cached" if instrument is None else f"tradable={instrument.tradable}",
        )

        bar = ctx.latest_bar
        if bar is None:
            add("data_freshness", False, "no closed bar available")
        else:
            closed = bar.is_closed(ctx.now)
            age = bar.age_seconds(ctx.now)
            add(
                "data_freshness",
                closed and age <= ctx.max_bar_age_seconds,
                f"bar {bar.ts.isoformat()} closed={closed} age={age:.0f}s "
                f"limit={ctx.max_bar_age_seconds}s",
            )

        if instrument is None:
            add("quantity_precision", False, "cannot validate precision without instrument spec")
        else:
            problems = instrument.qty_issues(intent.quantity)
            add("quantity_precision", not problems, "; ".join(problems) or "valid increment")

        add(
            "positive_notional",
            intent.notional > 0 and intent.quantity > 0,
            f"qty={intent.quantity} notional={intent.notional}",
        )

        max_position_value = ceiling * cfg.max_position_pct / Decimal(100)
        add(
            "max_position_value",
            intent.notional <= max_position_value,
            f"notional={intent.notional} cap={max_position_value}",
        )

        per_trade_cap = ceiling * cfg.per_trade_risk_pct / Decimal(100)
        add(
            "per_trade_risk",
            intent.notional <= per_trade_cap,
            f"notional={intent.notional} cap={per_trade_cap}",
        )

        if intent.side is Side.BUY:
            buffer = intent.notional * (cfg.fee_reserve_fraction + cfg.slippage_reserve_fraction)
            required = ctx.allocation.committed + ctx.allocation.reserved + intent.notional + buffer
            add(
                "aggregate_allocation",
                required <= ceiling,
                f"committed={ctx.allocation.committed} reserved={ctx.allocation.reserved} "
                f"order={intent.notional} costs={buffer} required={required} ceiling={ceiling}",
            )
        else:
            add("aggregate_allocation", True, "sell reduces exposure; no allocation required")

        daily_loss_cap = ceiling * cfg.daily_loss_limit_pct / Decimal(100)
        add(
            "daily_loss_limit",
            ctx.realized_pnl_today >= -daily_loss_cap,
            f"realised_today={ctx.realized_pnl_today} limit=-{daily_loss_cap}",
        )

        if ctx.peak_equity > 0:
            trough_limit = ctx.peak_equity * (Decimal(1) - cfg.max_drawdown_pct / Decimal(100))
            add(
                "max_drawdown",
                ctx.current_equity >= trough_limit,
                f"equity={ctx.current_equity} peak={ctx.peak_equity} floor={trough_limit}",
            )
        else:
            add("max_drawdown", True, "no equity history yet")

        add(
            "duplicate_intent",
            intent.fingerprint not in ctx.recent_fingerprints,
            f"fingerprint={intent.fingerprint[:12]}",
        )

        add(
            "order_rate_limit",
            ctx.orders_today < cfg.max_orders_per_day,
            f"orders_today={ctx.orders_today} limit={cfg.max_orders_per_day}",
        )

        if intent.side is Side.SELL:
            add(
                "sell_within_position",
                intent.quantity <= ctx.position_quantity,
                f"qty={intent.quantity} position={ctx.position_quantity} "
                "(crypto cannot be shorted)",
            )
        else:
            add("sell_within_position", True, "buy side")

        if ctx.spread_bps is None:
            add("spread", True, "spread not measured for bar data; not enforced")
        else:
            add(
                "spread",
                ctx.spread_bps <= cfg.max_spread_bps,
                f"spread={ctx.spread_bps}bps limit={cfg.max_spread_bps}bps",
            )

        available = ctx.allocation.available
        if all(check.passed for check in checks):
            return RiskVerdict(True, tuple(checks), (), available, ctx.now)
        return RiskVerdict.rejected(checks, available, ctx.now)

    # -- pre-flight gates ---------------------------------------------------

    def entry_gate_reasons(self, ctx: RiskContext) -> tuple[str, ...]:
        """Reasons entries are globally blocked, independent of any intent."""
        cfg = self.config
        reasons: list[str] = []
        if not ctx.capabilities().allow_new_entries:
            reasons.append(f"system state is {ctx.system_state.value}")
        if ctx.allocation.available <= 0:
            reasons.append(f"no allocation available ({ctx.allocation.available})")
        daily_loss_cap = ctx.allocation.ceiling * cfg.daily_loss_limit_pct / Decimal(100)
        if ctx.realized_pnl_today < -daily_loss_cap:
            reasons.append(
                f"daily loss limit reached ({ctx.realized_pnl_today} < -{daily_loss_cap})"
            )
        if ctx.peak_equity > 0:
            floor = ctx.peak_equity * (Decimal(1) - cfg.max_drawdown_pct / Decimal(100))
            if ctx.current_equity < floor:
                reasons.append(f"drawdown limit reached (equity {ctx.current_equity} < {floor})")
        if ctx.orders_today >= cfg.max_orders_per_day:
            reasons.append(f"daily order cap reached ({ctx.orders_today})")
        return tuple(reasons)

    def limits_summary(self, ceiling: Decimal) -> dict[str, str]:
        cfg = self.config
        ceiling = to_decimal(ceiling, "ceiling")
        return {
            "ceiling": format(ceiling, "f"),
            "max_position_value": format(ceiling * cfg.max_position_pct / Decimal(100), "f"),
            "per_trade_cap": format(ceiling * cfg.per_trade_risk_pct / Decimal(100), "f"),
            "daily_loss_cap": format(ceiling * cfg.daily_loss_limit_pct / Decimal(100), "f"),
            "max_drawdown_pct": format(cfg.max_drawdown_pct, "f"),
            "max_orders_per_day": str(cfg.max_orders_per_day),
            "allowlist": ",".join(cfg.symbol_allowlist),
        }
