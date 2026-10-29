"""SQLite schema.

Design notes that matter for correctness:

* ``orders.client_order_id`` is a foreign key to ``order_intents``, so an order
  row cannot exist without a persisted intent. This is the structural guarantee
  behind "persist intent before submission".
* ``orders.broker_order_id`` is UNIQUE, so one broker order can never be attached
  to two local intents.
* ``fills`` is keyed by broker fill id, so duplicate fill notifications (which
  brokers do send) are absorbed by ``INSERT OR IGNORE`` instead of double-counting
  a position or a fee.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bars (
        symbol      TEXT NOT NULL,
        timeframe   TEXT NOT NULL,
        ts          TEXT NOT NULL,
        open        TEXT NOT NULL,
        high        TEXT NOT NULL,
        low         TEXT NOT NULL,
        close       TEXT NOT NULL,
        volume      TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT 'alpaca',
        ingested_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instruments (
        symbol              TEXT PRIMARY KEY,
        min_order_size      TEXT NOT NULL,
        min_trade_increment TEXT NOT NULL,
        price_increment     TEXT NOT NULL,
        tradable            INTEGER NOT NULL,
        fractionable        INTEGER NOT NULL,
        shortable           INTEGER NOT NULL,
        marginable          INTEGER NOT NULL,
        fetched_at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id      TEXT PRIMARY KEY,
        ts               TEXT NOT NULL,
        symbol           TEXT NOT NULL,
        timeframe        TEXT NOT NULL,
        bar_ts           TEXT NOT NULL,
        strategy         TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        action           TEXT NOT NULL,
        reason           TEXT NOT NULL,
        indicators_json  TEXT NOT NULL,
        data_fingerprint TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_decisions_symbol_ts ON decisions(symbol, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS risk_verdicts (
        verdict_id       TEXT PRIMARY KEY,
        decision_id      TEXT NOT NULL REFERENCES decisions(decision_id),
        ts               TEXT NOT NULL,
        approved         INTEGER NOT NULL,
        reasons_json     TEXT NOT NULL,
        checks_json      TEXT NOT NULL,
        available_before TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_verdicts_decision ON risk_verdicts(decision_id)",
    """
    CREATE TABLE IF NOT EXISTS order_intents (
        client_order_id    TEXT PRIMARY KEY,
        ts                 TEXT NOT NULL,
        symbol             TEXT NOT NULL,
        side               TEXT NOT NULL,
        order_type         TEXT NOT NULL,
        qty                TEXT NOT NULL,
        limit_price        TEXT,
        notional           TEXT NOT NULL,
        time_in_force      TEXT NOT NULL,
        strategy           TEXT NOT NULL,
        strategy_version   TEXT NOT NULL,
        bar_ts             TEXT NOT NULL,
        decision_id        TEXT REFERENCES decisions(decision_id),
        intent_fingerprint TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_intents_fingerprint ON order_intents(intent_fingerprint)",
    "CREATE INDEX IF NOT EXISTS ix_intents_ts ON order_intents(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        client_order_id TEXT PRIMARY KEY REFERENCES order_intents(client_order_id),
        broker_order_id TEXT UNIQUE,
        status          TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,
        qty             TEXT NOT NULL,
        filled_qty      TEXT NOT NULL DEFAULT '0',
        avg_fill_price  TEXT,
        submitted_at    TEXT,
        updated_at      TEXT NOT NULL,
        reason          TEXT,
        raw_status      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)",
    """
    CREATE TABLE IF NOT EXISTS fills (
        fill_id         TEXT PRIMARY KEY,
        broker_order_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,
        qty             TEXT NOT NULL,
        price           TEXT NOT NULL,
        fee             TEXT NOT NULL DEFAULT '0',
        fee_currency    TEXT NOT NULL DEFAULT 'USD',
        ts              TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_natural ON fills(broker_order_id, fill_id)",
    "CREATE INDEX IF NOT EXISTS ix_fills_client ON fills(client_order_id)",
    """
    CREATE TABLE IF NOT EXISTS position_snapshots (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                TEXT NOT NULL,
        symbol            TEXT NOT NULL,
        qty               TEXT NOT NULL,
        avg_entry_price   TEXT NOT NULL,
        last_price        TEXT NOT NULL,
        market_value      TEXT NOT NULL,
        unrealized_pnl    TEXT NOT NULL,
        source            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_positions_ts ON position_snapshots(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS balance_snapshots (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                          TEXT NOT NULL,
        currency                    TEXT NOT NULL,
        cash                        TEXT NOT NULL,
        equity                      TEXT NOT NULL,
        buying_power                TEXT NOT NULL,
        non_marginable_buying_power TEXT NOT NULL,
        source                      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_balances_ts ON balance_snapshots(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS ledger_events (
        event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,
        kind            TEXT NOT NULL,
        amount          TEXT NOT NULL,
        ref             TEXT,
        note            TEXT,
        allocated_after TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ledger_ts ON ledger_events(ts)",
    """
    CREATE TABLE IF NOT EXISTS ledger_reservations (
        ref        TEXT PRIMARY KEY,
        amount     TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS system_state (
        id         INTEGER PRIMARY KEY CHECK (id = 1),
        state      TEXT NOT NULL,
        reason     TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reconciliation_events (
        event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        kind        TEXT NOT NULL,
        severity    TEXT NOT NULL,
        detail_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_recon_ts ON reconciliation_events(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS experiments (
        experiment_id    TEXT PRIMARY KEY,
        created_at       TEXT NOT NULL,
        name             TEXT NOT NULL,
        strategy         TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        params_json      TEXT NOT NULL,
        costs_json       TEXT NOT NULL,
        period_start     TEXT NOT NULL,
        period_end       TEXT NOT NULL,
        data_fingerprint TEXT NOT NULL,
        code_version     TEXT NOT NULL,
        metrics_json     TEXT NOT NULL,
        notes            TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_assessments (
        assessment_id TEXT PRIMARY KEY,
        ts            TEXT NOT NULL,
        symbol        TEXT NOT NULL,
        timeframe     TEXT NOT NULL,
        bar_ts        TEXT NOT NULL,
        provider      TEXT NOT NULL,
        model         TEXT NOT NULL,
        prompt_hash   TEXT NOT NULL,
        mode          TEXT NOT NULL,
        payload_json  TEXT NOT NULL,
        latency_ms    INTEGER,
        cost_usd      TEXT,
        outcome_json  TEXT,
        evaluated_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        channel TEXT NOT NULL,
        status  TEXT NOT NULL,
        detail  TEXT
    )
    """,
)
