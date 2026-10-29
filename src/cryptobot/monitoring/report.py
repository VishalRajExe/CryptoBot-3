"""Human-readable status and reports.

The bar for this module: the operator can answer "what is my exposure, what is
pending, why did the bot not trade, and is anything inconsistent?" without
opening SQLite. It never prints credentials or full account numbers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from cryptobot.config.settings import Settings
from cryptobot.core.contracts import OrderStatus, quote_currency, to_decimal
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger

UTC = timezone.utc


def _money(value: Decimal | str) -> str:
    return f"{to_decimal(value, 'money'):,.2f}"


def _table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> str:
    if not rows:
        return "  (none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def render_status(
    settings: Settings,
    journal: Journal,
    ledger: AllocationLedger,
    *,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    state, reason, updated = journal.get_system_state()
    allocation = ledger.state()
    health = journal.health()

    lines: list[str] = []
    lines.append(f"CRYPTOBOT status  ({settings.app.name} / {settings.app.environment})")
    lines.append(f"  time            {now.isoformat()}")
    lines.append(f"  system state    {state.value}" + (f" - {reason}" if reason else ""))
    if updated:
        lines.append(f"  state since     {updated.isoformat()}")
    lines.append(f"  database        {journal.path} (schema {health['schema_version']})")
    lines.append("")

    lines.append("ALLOCATION")
    quote = quote_currency(settings.market.symbols[0])
    lines.append(f"  ceiling         {_money(allocation.ceiling)} {quote}")
    lines.append(f"  committed       {_money(allocation.committed)}")
    lines.append(f"  reserved        {_money(allocation.reserved)}")
    lines.append(f"  available       {_money(allocation.available)}")
    lines.append(f"  deployment      {allocation.deployment_pct}%")
    lines.append(f"  realised P&L    {_money(allocation.realized_pnl)}")
    lines.append(f"  unrealised P&L  {_money(allocation.unrealized_pnl)}")
    lines.append(f"  invariant       {'OK' if allocation.invariant_holds else 'VIOLATED'}")

    balance = journal.latest_balance()
    if balance:
        lines.append("")
        lines.append("BROKER ACCOUNT (last snapshot)")
        lines.append(f"  cash            {_money(balance['cash'])}")
        lines.append(f"  equity          {_money(balance['equity'])}")
        lines.append(f"  buying power    {_money(balance['buying_power'])}")
        lines.append(f"  as of           {balance['ts']}")
    lines.append("")

    positions = journal.latest_positions()
    lines.append("POSITIONS")
    if positions:
        lines.append(
            _table(
                [
                    (
                        p.symbol,
                        format(p.quantity, "f"),
                        format(p.avg_entry_price, "f"),
                        _money(p.market_value),
                        _money(p.unrealized_pnl),
                    )
                    for p in positions
                ],
                ("symbol", "quantity", "avg entry", "market value", "unrealised"),
            )
        )
    else:
        lines.append("  (flat)")

    open_statuses = [s for s in OrderStatus if s.is_open]
    open_orders = journal.list_orders(open_statuses, limit=20)
    lines.append("")
    lines.append("OPEN ORDERS")
    lines.append(
        _table(
            [
                (
                    o.client_order_id[:18],
                    o.symbol,
                    o.side.value,
                    format(o.quantity, "f"),
                    o.status.value,
                    o.updated_at.strftime("%Y-%m-%d %H:%M"),
                )
                for o in open_orders
            ],
            ("client_order_id", "symbol", "side", "qty", "status", "updated"),
        )
    )

    lines.append("")
    lines.append("RECENT DECISIONS")
    lines.append(
        _table(
            [
                (
                    d["ts"][:16].replace("T", " "),
                    d["symbol"],
                    d["action"],
                    (d["reason"] or "")[:44],
                    d["strategy"][:20],
                )
                for d in journal.recent_decisions(6)
            ],
            ("time", "symbol", "action", "reason", "strategy"),
        )
    )

    reconciliations = journal.recent_reconciliations(3)
    lines.append("")
    lines.append("RECONCILIATION (recent)")
    lines.append(
        _table(
            [
                (r["ts"][:19].replace("T", " "), r["severity"], r["kind"], r["detail_json"][:60])
                for r in reconciliations
            ],
            ("time", "severity", "kind", "detail"),
        )
    )

    lines.append("")
    lines.append("COUNTERS")
    lines.append(
        "  "
        + "  ".join(
            f"{key}={health[key]}"
            for key in ("bars", "decisions", "risk_verdicts", "order_intents", "orders", "fills")
        )
    )
    return "\n".join(lines)


def render_ledger(ledger: AllocationLedger, journal: Journal, *, limit: int = 20) -> str:
    state = ledger.state()
    lines = [
        "ALLOCATION LEDGER",
        f"  ceiling {_money(state.ceiling)}  committed {_money(state.committed)}  "
        f"reserved {_money(state.reserved)}  available {_money(state.available)}",
        f"  realised {_money(state.realized_pnl)}  unrealised {_money(state.unrealized_pnl)}",
        "",
        "RECENT EVENTS",
        _table(
            [
                (e["ts"][:19].replace("T", " "), e["kind"], _money(e["amount"]), (e["ref"] or "")[:16])
                for e in journal.ledger_events(limit=limit)
            ],
            ("time", "kind", "amount", "ref"),
        ),
        "",
        "ACTIVE RESERVATIONS",
        _table(
            [(ref[:24], _money(amount)) for ref, amount in ledger.reservations.items()],
            ("ref", "amount"),
        ),
    ]
    return "\n".join(lines)


def render_orders(journal: Journal, *, limit: int = 20) -> str:
    orders = journal.list_orders(limit=limit)
    return "\n".join(
        [
            "ORDERS (most recent first)",
            _table(
                [
                    (
                        o.client_order_id[:18],
                        o.symbol,
                        o.side.value,
                        format(o.quantity, "f"),
                        format(o.filled_quantity, "f"),
                        o.status.value,
                        (o.raw_status or "")[:14],
                    )
                    for o in orders
                ],
                ("client_order_id", "symbol", "side", "qty", "filled", "status", "broker status"),
            ),
        ]
    )
