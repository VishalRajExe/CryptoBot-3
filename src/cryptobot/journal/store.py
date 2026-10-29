"""The durable journal: every fact the application knows, in SQLite.

The journal is deliberately the only mutable store. Nothing else in the system
keeps authoritative state in memory, which is what makes restart recovery and
reconciliation meaningful rather than decorative.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from cryptobot.core import jsonutil
from cryptobot.core.contracts import (
    AllocationState,
    Bar,
    FillRecord,
    InstrumentSpec,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    PositionSnapshot,
    RiskVerdict,
    Side,
    StrategySignal,
    quote_currency,
    to_decimal,
)
from cryptobot.core.errors import ContractError, DuplicateOrderError, ExecutionError
from cryptobot.core.states import SystemState
from cryptobot.journal import schema

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _f(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


class Journal:
    def __init__(self, path: str | Path = ":memory:", *, clock: Callable[[], datetime] = _utcnow):
        self.path = str(path)
        self._clock = clock
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("journal is not open; call initialize() first")
        return self._conn

    def initialize(self) -> "Journal":
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._conn = conn
        for statement in schema.DDL:
            conn.execute(statement)
        # Every migration so far is additive DDL guarded by IF NOT EXISTS, so
        # re-stamping the version is the whole migration. It is an upsert rather
        # than INSERT OR IGNORE so an existing journal reports the schema it now
        # actually has instead of the one it was created with.
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(schema.SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('created_at', ?)",
            (self.now().isoformat(),),
        )
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Journal":
        return self.initialize()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def now(self) -> datetime:
        return self._clock()

    @property
    def schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    # -- bars ---------------------------------------------------------------

    def save_bars(self, bars: Iterable[Bar], source: str = "alpaca") -> int:
        ingested = self.now().isoformat()
        rows = [
            (b.symbol, b.timeframe, b.ts.isoformat(), _f(b.open), _f(b.high), _f(b.low), _f(b.close), _f(b.volume), source, ingested)
            for b in bars
        ]
        if not rows:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO bars (symbol, timeframe, ts, open, high, low, close, volume, source, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    source=excluded.source, ingested_at=excluded.ingested_at
                """,
                rows,
            )
        return len(rows)

    def recent_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        rows = self.conn.execute(
            """
            SELECT * FROM bars WHERE symbol=? AND timeframe=?
            ORDER BY ts DESC LIMIT ?
            """,
            (symbol, timeframe, int(limit)),
        ).fetchall()
        return [Bar.from_row(row) for row in reversed(rows)]

    def latest_bar(self, symbol: str, timeframe: str) -> Bar | None:
        row = self.conn.execute(
            "SELECT * FROM bars WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT 1",
            (symbol, timeframe),
        ).fetchone()
        return Bar.from_row(row) if row else None

    def bars_between(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Bar]:
        rows = self.conn.execute(
            """
            SELECT * FROM bars WHERE symbol=? AND timeframe=? AND ts>=? AND ts<?
            ORDER BY ts ASC
            """,
            (symbol, timeframe, start.isoformat(), end.isoformat()),
        ).fetchall()
        return [Bar.from_row(row) for row in rows]

    def bar_count(self, symbol: str, timeframe: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        return int(row["n"])

    # -- instruments --------------------------------------------------------

    def save_instrument(self, spec: InstrumentSpec) -> None:
        self.conn.execute(
            """
            INSERT INTO instruments (symbol, min_order_size, min_trade_increment, price_increment,
                                     tradable, fractionable, shortable, marginable, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                min_order_size=excluded.min_order_size,
                min_trade_increment=excluded.min_trade_increment,
                price_increment=excluded.price_increment,
                tradable=excluded.tradable, fractionable=excluded.fractionable,
                shortable=excluded.shortable, marginable=excluded.marginable,
                fetched_at=excluded.fetched_at
            """,
            (
                spec.symbol,
                _f(spec.min_order_size),
                _f(spec.min_trade_increment),
                _f(spec.price_increment),
                int(spec.tradable),
                int(spec.fractionable),
                int(spec.shortable),
                int(spec.marginable),
                self.now().isoformat(),
            ),
        )

    def get_instrument(self, symbol: str) -> InstrumentSpec | None:
        row = self.conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            return None
        return InstrumentSpec(
            symbol=row["symbol"],
            min_order_size=row["min_order_size"],
            min_trade_increment=row["min_trade_increment"],
            price_increment=row["price_increment"],
            tradable=bool(row["tradable"]),
            fractionable=bool(row["fractionable"]),
            shortable=bool(row["shortable"]),
            marginable=bool(row["marginable"]),
        )

    # -- decisions ----------------------------------------------------------

    def record_decision(
        self, signal: StrategySignal, data_fingerprint: str, decision_id: str | None = None
    ) -> str:
        identifier = decision_id or f"dec-{uuid.uuid4().hex[:16]}"
        row = signal.to_row()
        self.conn.execute(
            """
            INSERT INTO decisions (decision_id, ts, symbol, timeframe, bar_ts, strategy,
                                   strategy_version, action, reason, indicators_json, data_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                (signal.ts or signal.bar_ts).isoformat(),
                row["symbol"],
                row["timeframe"],
                row["bar_ts"],
                row["strategy_name"],
                row["strategy_version"],
                row["action"],
                row["reason"],
                row["indicators_json"],
                data_fingerprint,
            ),
        )
        return identifier

    def get_decision(self, decision_id: str) -> Mapping[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        return dict(row) if row else None

    def recent_decisions(self, limit: int = 20) -> list[Mapping[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_verdict(
        self, decision_id: str, verdict: RiskVerdict, verdict_id: str | None = None
    ) -> str:
        identifier = verdict_id or f"vrd-{uuid.uuid4().hex[:16]}"
        row = verdict.to_row()
        self.conn.execute(
            """
            INSERT INTO risk_verdicts (verdict_id, decision_id, ts, approved, reasons_json,
                                       checks_json, available_before)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                decision_id,
                row["ts"],
                row["approved"],
                row["reasons_json"],
                row["checks_json"],
                row["available_before"],
            ),
        )
        return identifier

    # -- intents and orders -------------------------------------------------

    def record_intent(self, intent: OrderIntent) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO order_intents (client_order_id, ts, symbol, side, order_type, qty,
                                           limit_price, notional, time_in_force, strategy,
                                           strategy_version, bar_ts, decision_id, intent_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.client_order_id,
                    intent.ts.isoformat(),
                    intent.symbol,
                    intent.side.value,
                    intent.order_type.value,
                    _f(intent.quantity),
                    _f(intent.limit_price),
                    _f(intent.notional),
                    intent.time_in_force.value,
                    intent.strategy_name,
                    intent.strategy_version,
                    intent.bar_ts.isoformat(),
                    intent.decision_id,
                    intent.fingerprint,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateOrderError(
                f"order intent {intent.client_order_id} already exists"
            ) from exc

    def get_intent(self, client_order_id: str) -> OrderIntent | None:
        row = self.conn.execute(
            "SELECT * FROM order_intents WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        return OrderIntent.from_row(row) if row else None

    def intent_fingerprints_since(self, since: datetime) -> set[str]:
        rows = self.conn.execute(
            "SELECT intent_fingerprint FROM order_intents WHERE ts >= ?", (since.isoformat(),)
        ).fetchall()
        return {r["intent_fingerprint"] for r in rows}

    def upsert_order(self, record: OrderRecord) -> None:
        row = record.to_row()
        try:
            self.conn.execute(
                """
                INSERT INTO orders (client_order_id, broker_order_id, status, symbol, side, qty,
                                    filled_qty, avg_fill_price, submitted_at, updated_at, reason, raw_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    broker_order_id=excluded.broker_order_id,
                    status=excluded.status,
                    filled_qty=excluded.filled_qty,
                    avg_fill_price=excluded.avg_fill_price,
                    submitted_at=COALESCE(orders.submitted_at, excluded.submitted_at),
                    updated_at=excluded.updated_at,
                    reason=excluded.reason,
                    raw_status=excluded.raw_status
                """,
                (
                    row["client_order_id"],
                    row["broker_order_id"],
                    row["status"],
                    row["symbol"],
                    row["side"],
                    row["qty"],
                    row["filled_qty"],
                    row["avg_fill_price"],
                    row["submitted_at"],
                    row["updated_at"],
                    row["reason"],
                    row["raw_status"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ExecutionError(
                f"cannot persist order {record.client_order_id}: {exc}"
            ) from exc

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        row = self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        return OrderRecord.from_row(row) if row else None

    def list_orders(
        self, statuses: Sequence[OrderStatus] | None = None, limit: int = 200
    ) -> list[OrderRecord]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self.conn.execute(
                f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
                (*[s.value for s in statuses], int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM orders ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [OrderRecord.from_row(r) for r in rows]

    def unresolved_orders(self) -> list[OrderRecord]:
        """Orders whose true state is not yet known locally."""
        open_values = [s.value for s in OrderStatus if s.needs_broker_confirmation]
        placeholders = ",".join("?" for _ in open_values)
        rows = self.conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY updated_at ASC",
            tuple(open_values),
        ).fetchall()
        return [OrderRecord.from_row(r) for r in rows]

    def count_orders_since(self, since: datetime) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM order_intents i
            LEFT JOIN orders o ON o.client_order_id = i.client_order_id
            WHERE i.ts >= ? AND (o.status IS NULL OR o.status != 'rejected')
            """,
            (since.isoformat(),),
        ).fetchone()
        return int(row["n"])

    def orders_submitted_between(self, start: datetime, end: datetime) -> list[OrderRecord]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE submitted_at >= ? AND submitted_at < ? ORDER BY submitted_at ASC",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [OrderRecord.from_row(r) for r in rows]

    # -- fills --------------------------------------------------------------

    def record_fill(self, fill: FillRecord) -> bool:
        """Insert a fill. Returns False when it was already recorded."""
        row = fill.to_row()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO fills (fill_id, broker_order_id, client_order_id, symbol, side,
                                         qty, price, fee, fee_currency, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["fill_id"],
                row["broker_order_id"],
                row["client_order_id"],
                row["symbol"],
                row["side"],
                row["qty"],
                row["price"],
                row["fee"],
                row["fee_currency"],
                row["ts"],
            ),
        )
        return cursor.rowcount > 0

    def fills_for(self, client_order_id: str) -> list[FillRecord]:
        rows = self.conn.execute(
            "SELECT * FROM fills WHERE client_order_id=? ORDER BY ts ASC", (client_order_id,)
        ).fetchall()
        return [FillRecord.from_row(r) for r in rows]

    def fills(self, limit: int = 500) -> list[FillRecord]:
        rows = self.conn.execute(
            "SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [FillRecord.from_row(r) for r in rows]

    def fills_for_symbol(
        self, symbol: str, *, since: datetime | None = None
    ) -> list[FillRecord]:
        """Every fill for one symbol, oldest first.

        Trade evaluation must see the whole history for a symbol; the paged
        :meth:`fills` reader cannot guarantee that, and a truncated history would
        silently invent a position that was never held.
        """
        query = "SELECT * FROM fills WHERE symbol=?"
        params: list[Any] = [symbol]
        if since is not None:
            query += " AND ts >= ?"
            params.append(since.isoformat())
        query += " ORDER BY ts ASC"
        rows = self.conn.execute(query, tuple(params)).fetchall()
        return [FillRecord.from_row(r) for r in rows]

    def correct_fill_fee(self, fill_id: str, fee: Decimal) -> Decimal | None:
        """Replace an estimated fee with the broker's actual figure.

        Returns the previous fee, or ``None`` when the fill is unknown. The fee on
        a fill is an estimate until the broker posts the activity, so this is a
        correction rather than a rewrite: quantity, price and timestamps are not
        touched, and the ledger receives a matching adjustment event.
        """
        row = self.conn.execute(
            "SELECT fee FROM fills WHERE fill_id=?", (fill_id,)
        ).fetchone()
        if row is None:
            return None
        previous = to_decimal(row["fee"], "fee")
        self.conn.execute(
            "UPDATE fills SET fee=? WHERE fill_id=?", (format(fee, "f"), fill_id)
        )
        self.conn.commit()
        return previous

    def fee_activity_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT activity_id FROM fee_activities").fetchall()
        return {row["activity_id"] for row in rows}

    def matched_fee_fill_ids(self) -> set[str]:
        """Fills that already have an actual fee attributed to them."""
        rows = self.conn.execute(
            "SELECT matched_fill_id FROM fee_activities WHERE matched_fill_id IS NOT NULL"
        ).fetchall()
        return {row["matched_fill_id"] for row in rows}

    def record_fee_activity(self, row: Mapping[str, Any]) -> bool:
        """Store one attributed fee. Returns False when it was already recorded."""
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO fee_activities (
                activity_id, ts, kind, symbol, qty, price, currency, quote_value,
                matched_client_order_id, matched_fill_id, delta_quote, description, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["activity_id"],
                row["ts"],
                row["kind"],
                row["symbol"],
                row["qty"],
                row["price"],
                row["currency"],
                row["quote_value"],
                row.get("matched_client_order_id"),
                row.get("matched_fill_id"),
                row["delta_quote"],
                row.get("description"),
                row["recorded_at"],
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def fee_activities(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM fee_activities ORDER BY ts DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in rows]

    def sum_fees(self, since: datetime | None = None) -> Decimal:
        """Total fees in quote currency.

        Fees are charged in the asset a trade credits, so a buy reports its fee in
        the base asset. Summing the raw column would add BTC to USD; every fee is
        valued at its fill price first.
        """
        query = "SELECT symbol, fee, fee_currency, price FROM fills"
        params: tuple[Any, ...] = ()
        if since is not None:
            query += " WHERE ts >= ?"
            params = (since.isoformat(),)
        total = Decimal("0")
        for row in self.conn.execute(query, params).fetchall():
            fee = to_decimal(row["fee"], "fee")
            if row["fee_currency"] != quote_currency(row["symbol"]):
                fee *= to_decimal(row["price"], "price")
            total += fee
        return total

    def realized_position(self, symbol: str) -> tuple[Decimal, Decimal]:
        """(quantity, average cost) implied by fills alone.

        Used as an independent cross-check of the allocation ledger against the
        broker's position. A crypto buy pays its fee out of the base asset it is
        credited with, so the holding is the net quantity even though the fill
        itself records what the broker executed.
        """
        rows = self.conn.execute(
            "SELECT qty, price, side, fee, fee_currency FROM fills WHERE symbol=? ORDER BY ts ASC",
            (symbol,),
        ).fetchall()
        quote = quote_currency(symbol)
        qty = Decimal("0")
        cost = Decimal("0")
        for row in rows:
            fill_qty = to_decimal(row["qty"], "qty")
            price = to_decimal(row["price"], "price")
            if row["side"] == Side.BUY.value:
                held = fill_qty
                if row["fee_currency"] != quote:
                    held -= to_decimal(row["fee"], "fee")
                if held <= 0:
                    continue
                fill_qty = held
                qty += fill_qty
                cost += fill_qty * price
            else:
                if qty > 0:
                    cost -= (cost / qty) * min(fill_qty, qty)
                qty -= fill_qty
                if qty < 0:
                    qty = Decimal("0")
                if cost < 0:
                    cost = Decimal("0")
        avg = (cost / qty) if qty > 0 else Decimal("0")
        return qty, avg

    # -- ledger -------------------------------------------------------------

    def append_ledger_event(
        self,
        kind: str,
        amount: Decimal,
        *,
        ref: str | None = None,
        note: str | None = None,
        allocated_after: Decimal,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ledger_events (ts, kind, amount, ref, note, allocated_after)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self.now().isoformat(), kind, _f(amount), ref, note, _f(allocated_after)),
        )

    def ledger_events(self, limit: int | None = None) -> list[Mapping[str, Any]]:
        query = "SELECT * FROM ledger_events ORDER BY event_id ASC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            query = "SELECT * FROM (SELECT * FROM ledger_events ORDER BY event_id DESC LIMIT ?) ORDER BY event_id ASC"
            params = (int(limit),)
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def set_reservation(self, ref: str, amount: Decimal) -> None:
        self.conn.execute(
            """
            INSERT INTO ledger_reservations (ref, amount, created_at) VALUES (?, ?, ?)
            ON CONFLICT(ref) DO UPDATE SET amount=excluded.amount
            """,
            (ref, _f(amount), self.now().isoformat()),
        )

    def delete_reservation(self, ref: str) -> bool:
        cursor = self.conn.execute("DELETE FROM ledger_reservations WHERE ref=?", (ref,))
        return cursor.rowcount > 0

    def reservations(self) -> dict[str, Decimal]:
        rows = self.conn.execute("SELECT ref, amount FROM ledger_reservations").fetchall()
        return {r["ref"]: to_decimal(r["amount"], "amount") for r in rows}

    def sum_ledger(self, kind: str, since: datetime | None = None) -> Decimal:
        query = "SELECT amount FROM ledger_events WHERE kind=?"
        params: list[Any] = [kind]
        if since is not None:
            query += " AND ts >= ?"
            params.append(since.isoformat())
        total = Decimal("0")
        for row in self.conn.execute(query, tuple(params)).fetchall():
            total += to_decimal(row["amount"], "amount")
        return total

    # -- system state -------------------------------------------------------

    def get_system_state(self) -> tuple[SystemState, str | None, datetime | None]:
        row = self.conn.execute("SELECT * FROM system_state WHERE id=1").fetchone()
        if not row:
            return SystemState.ACTIVE, None, None
        try:
            state = SystemState(row["state"])
        except ValueError:
            state = SystemState.HALTED
        return state, row["reason"], datetime.fromisoformat(row["updated_at"])

    def set_system_state(self, state: SystemState, reason: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO system_state (id, state, reason, updated_at) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state, reason=excluded.reason,
                                          updated_at=excluded.updated_at
            """,
            (SystemState(state).value, reason, self.now().isoformat()),
        )

    # -- promotions ---------------------------------------------------------

    def record_promotion(
        self,
        *,
        strategy: str,
        params: Mapping[str, Any],
        evidence_digest: str,
        approved_by: str,
        approved_at: datetime,
        reason: str,
        out_of_sample_return_pct: Decimal,
        out_of_sample_drawdown_pct: Decimal,
        out_of_sample_trades: int,
        baseline_return_pct: Decimal,
    ) -> int:
        """Append a human approval of one strategy configuration.

        This is an audit row, not a switch: nothing loads a promotion to decide
        how to trade. The parameters are replayed by whoever edits the
        configuration, and this row is what they can point at afterwards.
        """
        for name, value in (
            ("strategy", strategy),
            ("approved_by", approved_by),
            ("reason", reason),
            ("evidence_digest", evidence_digest),
        ):
            if not str(value).strip():
                raise ContractError(f"a promotion requires {name}")
        cursor = self.conn.execute(
            """
            INSERT INTO strategy_promotions (
                ts, strategy, params_json, evidence_digest, approved_by, approved_at,
                reason, out_of_sample_return_pct, out_of_sample_drawdown_pct,
                out_of_sample_trades, baseline_return_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.now().isoformat(),
                strategy,
                jsonutil.dumps(dict(params)),
                evidence_digest,
                approved_by,
                approved_at.isoformat(),
                reason,
                _f(to_decimal(out_of_sample_return_pct, "out_of_sample_return_pct")),
                _f(to_decimal(out_of_sample_drawdown_pct, "out_of_sample_drawdown_pct")),
                int(out_of_sample_trades),
                _f(to_decimal(baseline_return_pct, "baseline_return_pct")),
            ),
        )
        return int(cursor.lastrowid)

    def promotions(self, limit: int = 20) -> list[Mapping[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM strategy_promotions ORDER BY promotion_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_promotion(self, evidence_digest: str) -> Mapping[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM strategy_promotions WHERE evidence_digest=? "
            "ORDER BY promotion_id DESC LIMIT 1",
            (evidence_digest,),
        ).fetchone()
        return dict(row) if row else None

    # -- reconciliation and snapshots ---------------------------------------

    def record_reconciliation(self, kind: str, severity: str, detail: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO reconciliation_events (ts, kind, severity, detail_json) VALUES (?, ?, ?, ?)",
            (self.now().isoformat(), kind, severity, jsonutil.dumps(dict(detail))),
        )

    def recent_reconciliations(self, limit: int = 20) -> list[Mapping[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM reconciliation_events ORDER BY event_id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_reconciliations(self, severity: str | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM reconciliation_events"
        params: tuple[Any, ...] = ()
        if severity is not None:
            query += " WHERE severity=?"
            params = (severity,)
        return int(self.conn.execute(query, params).fetchone()["n"])

    def activity_span(self) -> tuple[datetime | None, datetime | None]:
        """(first, last) timestamp of anything the bot recorded.

        Decisions bound the analysis window for acceptance purposes, because a
        journal with fills but no decisions cannot be audited.
        """
        row = self.conn.execute(
            "SELECT MIN(ts) AS first, MAX(ts) AS last FROM decisions"
        ).fetchone()
        first, last = row["first"], row["last"]
        if first is None:
            return None, None
        return datetime.fromisoformat(first), datetime.fromisoformat(last)

    def decision_days(self) -> list[str]:
        """Distinct UTC dates on which at least one decision was recorded."""
        rows = self.conn.execute(
            "SELECT DISTINCT substr(ts, 1, 10) AS day FROM decisions ORDER BY day ASC"
        ).fetchall()
        return [str(r["day"]) for r in rows]

    def save_position_snapshots(self, snapshots: Iterable[PositionSnapshot]) -> None:
        """Persist position snapshots, keeping the source each one names.

        The source column used to be written from a parameter defaulting to
        ``"broker"``, so every row said the same thing and the reconciler could
        not tell which adapter a journal belonged to. The snapshot carries the
        adapter's own name; that is what is stored.
        """
        with self.transaction() as conn:
            for snap in snapshots:
                row = snap.to_row()
                conn.execute(
                    """
                    INSERT INTO position_snapshots (ts, symbol, qty, avg_entry_price, last_price,
                                                    market_value, unrealized_pnl, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["ts"],
                        row["symbol"],
                        row["qty"],
                        row["avg_entry_price"],
                        row["last_price"],
                        row["market_value"],
                        row["unrealized_pnl"],
                        row["source"],
                    ),
                )

    def latest_positions(self) -> list[PositionSnapshot]:
        rows = self.conn.execute(
            """
            SELECT p.* FROM position_snapshots p
            JOIN (SELECT symbol, MAX(id) AS max_id FROM position_snapshots GROUP BY symbol) m
              ON p.id = m.max_id
            ORDER BY p.symbol
            """
        ).fetchall()
        out: list[PositionSnapshot] = []
        for row in rows:
            if to_decimal(row["qty"], "qty") == 0:
                continue
            out.append(
                PositionSnapshot(
                    ts=datetime.fromisoformat(row["ts"]),
                    symbol=row["symbol"],
                    quantity=row["qty"],
                    avg_entry_price=row["avg_entry_price"],
                    last_price=row["last_price"],
                    source=row["source"],
                )
            )
        return out

    def save_balance(
        self,
        *,
        cash: Decimal,
        equity: Decimal,
        buying_power: Decimal,
        non_marginable_buying_power: Decimal,
        currency: str = "USD",
        source: str = "broker",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO balance_snapshots (ts, currency, cash, equity, buying_power,
                                           non_marginable_buying_power, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.now().isoformat(),
                currency,
                _f(cash),
                _f(equity),
                _f(buying_power),
                _f(non_marginable_buying_power),
                source,
            ),
        )

    def latest_balance(self) -> Mapping[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM balance_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def peak_equity(self) -> Decimal:
        row = self.conn.execute(
            "SELECT MAX(CAST(equity AS REAL)) AS peak FROM balance_snapshots"
        ).fetchone()
        if not row or row["peak"] is None:
            return Decimal("0")
        return Decimal(str(row["peak"]))

    def latest_market_price(self, symbol: str) -> Decimal | None:
        row = self.conn.execute(
            "SELECT close FROM bars WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
        ).fetchone()
        return to_decimal(row["close"], "close") if row else None

    # -- experiments and AI -------------------------------------------------

    def save_experiment(self, payload: Mapping[str, Any]) -> str:
        experiment_id = str(payload.get("experiment_id") or f"exp-{uuid.uuid4().hex[:16]}")
        self.conn.execute(
            """
            INSERT OR REPLACE INTO experiments (experiment_id, created_at, name, strategy,
                strategy_version, params_json, costs_json, period_start, period_end,
                data_fingerprint, code_version, metrics_json, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                str(payload.get("created_at") or self.now().isoformat()),
                payload["name"],
                payload["strategy"],
                payload["strategy_version"],
                jsonutil.dumps(payload.get("params", {})),
                jsonutil.dumps(payload.get("costs", {})),
                payload["period_start"],
                payload["period_end"],
                payload["data_fingerprint"],
                payload["code_version"],
                jsonutil.dumps(payload.get("metrics", {})),
                payload.get("notes"),
            ),
        )
        return experiment_id

    def list_experiments(self, limit: int = 50) -> list[Mapping[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    def save_ai_assessment(self, payload: Mapping[str, Any]) -> str:
        assessment_id = str(payload.get("assessment_id") or f"ai-{uuid.uuid4().hex[:16]}")
        self.conn.execute(
            """
            INSERT OR REPLACE INTO ai_assessments (assessment_id, ts, symbol, timeframe, bar_ts,
                provider, model, prompt_hash, mode, payload_json, latency_ms, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assessment_id,
                str(payload["ts"]),
                payload["symbol"],
                payload["timeframe"],
                str(payload["bar_ts"]),
                payload["provider"],
                payload["model"],
                payload["prompt_hash"],
                payload.get("mode", "shadow"),
                jsonutil.dumps(payload.get("payload", {})),
                payload.get("latency_ms"),
                _f(to_decimal(payload.get("cost_usd", "0"), "cost_usd")),
            ),
        )
        return assessment_id

    def unevaluated_assessments(self, limit: int = 200) -> list[Mapping[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM ai_assessments WHERE evaluated_at IS NULL
            ORDER BY ts ASC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_assessment_evaluated(self, assessment_id: str, outcome: Mapping[str, Any]) -> None:
        self.conn.execute(
            "UPDATE ai_assessments SET outcome_json=?, evaluated_at=? WHERE assessment_id=?",
            (jsonutil.dumps(dict(outcome)), self.now().isoformat(), assessment_id),
        )

    def record_notification(self, channel: str, status: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO notifications (ts, channel, status, detail) VALUES (?, ?, ?, ?)",
            (self.now().isoformat(), channel, status, detail),
        )

    # -- soak instrumentation ------------------------------------------------

    def open_soak_window(self, note: str = "", interval_seconds: Decimal | float = 60.0) -> Mapping[str, Any]:
        """Return the open soak window, opening one only when there is none.

        Reusing the existing window is the point rather than an optimisation: a
        restarted process resumes the same interval of evidence instead of
        quietly starting a fresh clock that would look like uninterrupted
        operation. Closing a soak is a deliberate operator action.

        The heartbeat interval is recorded with the window because coverage is
        judged against the cadence the operator declared, not against a default
        that would mislabel a slow loop as a gap.
        """
        interval = to_decimal(interval_seconds, "interval_seconds")
        row = self.conn.execute(
            "SELECT * FROM soak_windows WHERE ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            existing = to_decimal(row["interval_seconds"], "interval_seconds")
            if interval > existing:
                self.conn.execute(
                    "UPDATE soak_windows SET interval_seconds=? WHERE window_id=?",
                    (_f(interval), row["window_id"]),
                )
                self.record_soak_event(
                    row["window_id"],
                    "interval_changed",
                    f"heartbeat interval {existing} -> {interval} seconds",
                )
                return dict(self.soak_window() or row)
            return dict(row)
        window_id = f"soak-{uuid.uuid4().hex[:12]}"
        timestamp = self.now().isoformat()
        self.conn.execute(
            "INSERT INTO soak_windows "
            "(window_id, started_at, ended_at, interval_seconds, note, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?)",
            (window_id, timestamp, _f(interval), note, timestamp),
        )
        self.conn.commit()
        return self.soak_window() or {}

    def soak_window(self) -> Mapping[str, Any] | None:
        """The most recent soak window, open or closed."""
        row = self.conn.execute(
            "SELECT * FROM soak_windows ORDER BY started_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def close_soak_window(self, reason: str = "") -> int:
        """End every open window. Returns how many were closed."""
        ended = self.now().isoformat()
        note_suffix = f" (closed: {reason})" if reason else " (closed)"
        rows = self.conn.execute(
            "SELECT window_id, note FROM soak_windows WHERE ended_at IS NULL"
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "UPDATE soak_windows SET ended_at=?, note=? WHERE window_id=?",
                (ended, f"{row['note'] or ''}{note_suffix}", row["window_id"]),
            )
        self.conn.commit()
        return len(rows)

    def start_soak_process(self, window_id: str, note: str = "") -> str:
        process_id = f"run-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO soak_processes "
            "(process_id, window_id, started_at, ended_at, exit_reason, note) "
            "VALUES (?, ?, ?, NULL, NULL, ?)",
            (process_id, window_id, self.now().isoformat(), note),
        )
        self.conn.commit()
        return process_id

    def end_soak_process(self, process_id: str, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE soak_processes SET ended_at=?, exit_reason=? "
            "WHERE process_id=? AND ended_at IS NULL",
            (self.now().isoformat(), reason, process_id),
        )
        self.conn.commit()

    def soak_processes(self, window_id: str | None = None) -> list[Mapping[str, Any]]:
        query = "SELECT * FROM soak_processes"
        params: tuple[Any, ...] = ()
        if window_id is not None:
            query += " WHERE window_id=?"
            params = (window_id,)
        rows = self.conn.execute(query + " ORDER BY started_at ASC", params).fetchall()
        return [dict(row) for row in rows]

    def record_soak_checkpoint(
        self,
        window_id: str,
        process_id: str,
        *,
        uptime_seconds: Decimal,
        cycles: int,
        decisions: int,
        orders: int,
        fills: int,
        api_errors: int,
        note: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO soak_checkpoints (
                window_id, process_id, ts, uptime_seconds, cycles, decisions,
                orders, fills, api_errors, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                window_id,
                process_id,
                self.now().isoformat(),
                _f(uptime_seconds),
                int(cycles),
                int(decisions),
                int(orders),
                int(fills),
                int(api_errors),
                note,
            ),
        )
        self.conn.commit()

    def soak_checkpoints(self, window_id: str | None = None) -> list[Mapping[str, Any]]:
        query = "SELECT * FROM soak_checkpoints"
        params: tuple[Any, ...] = ()
        if window_id is not None:
            query += " WHERE window_id=?"
            params = (window_id,)
        rows = self.conn.execute(
            query + " ORDER BY ts ASC, checkpoint_id ASC", params
        ).fetchall()
        return [dict(row) for row in rows]

    def record_soak_event(self, window_id: str, kind: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO soak_events (window_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (window_id, self.now().isoformat(), kind, detail),
        )
        self.conn.commit()

    def soak_events(
        self, window_id: str | None = None, limit: int = 200
    ) -> list[Mapping[str, Any]]:
        query = "SELECT * FROM soak_events"
        params: tuple[Any, ...] = ()
        if window_id is not None:
            query += " WHERE window_id=?"
            params = (window_id,)
        rows = self.conn.execute(
            query + " ORDER BY ts ASC, event_id ASC LIMIT ?", (*params, int(limit))
        ).fetchall()
        return [dict(row) for row in rows]

    def counts_between(
        self, start: datetime, end: datetime
    ) -> dict[str, dict[str, int]]:
        """Per-UTC-day decision, order and fill counts for ``[start, end)``.

        Grouped in SQL so a daily summary is one query per table instead of
        loading every row into Python.
        """
        bounds = (start.isoformat(), end.isoformat())
        summary: dict[str, dict[str, int]] = {}
        for table, key in (("decisions", "decisions"), ("order_intents", "orders"), ("fills", "fills")):
            rows = self.conn.execute(
                f"SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM {table} "
                "WHERE ts >= ? AND ts < ? GROUP BY day",
                bounds,
            ).fetchall()
            for row in rows:
                summary.setdefault(row["day"], {"decisions": 0, "orders": 0, "fills": 0})[
                    key
                ] = int(row["n"])
        return summary

    # -- diagnostics --------------------------------------------------------

    def health(self) -> dict[str, Any]:
        tables = (
            "bars",
            "decisions",
            "risk_verdicts",
            "order_intents",
            "orders",
            "fills",
            "ledger_events",
            "reconciliation_events",
        )
        counts = {
            name: int(self.conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
            for name in tables
        }
        state, reason, updated = self.get_system_state()
        counts.update(
            {
                "schema_version": self.schema_version,
                "path": self.path,
                "system_state": state.value,
                "system_state_reason": reason,
                "system_state_updated_at": updated.isoformat() if updated else None,
            }
        )
        return counts
