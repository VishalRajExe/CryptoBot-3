# Architecture

How the pieces fit, why the boundaries are where they are, and which invariant
each module owns. When this document and the code disagree, the code is right —
and this document is a bug.

---

## 1. Shape

```
                       ┌────────────────────────────────────────────┐
   CLI (cli.py) ──────►│  Runtime  (runtime.py, preflight.py)       │
                       │  assemblies, loop, operational state       │
                       └───────┬───────────────┬────────────────────┘
                               │               │
                    ┌──────────▼──────┐   ┌────▼──────────────┐
                    │  engine.py      │   │ reconciliation/   │
                    │  decision cycle │   │ reconciler.py     │
                    └──┬───┬───┬───┬──┘   └────┬──────────────┘
                       │   │   │   │           │
        ┌──────────────┘   │   │   └──────┐    │
        │                  │   │          │    │
  ┌─────▼──────┐   ┌───────▼───▼──┐  ┌────▼────▼────┐
  │ strategy/  │   │ risk/        │  │ journal/     │
  │ features/  │   │ sizing/      │  │ store.py     │
  └─────┬──────┘   └──────┬───────┘  └────▲─────────┘
        │                 │               │
  ┌─────▼──────┐   ┌──────▼───────┐       │
  │ data/      │   │ execution/   │───────┘
  │ alpaca_    │   │ alpaca_paper │
  │ data.py    │   │ simulator    │
  └─────┬──────┘   └──────┬───────┘
        │                 │
        ▼                 ▼
   Alpaca DATA       Alpaca PAPER
   (read-only)       (orders)
```

Two arrows cross into external systems, and only two: the data client fetches
bars, and the paper broker submits orders. Nothing else opens a socket.

---

## 2. Non-negotiable invariants

These are the properties the design exists to protect. Each one names the module
that owns it and the test that would fail if it were broken.

### 2.1 Paper-only, in four layers

1. `config/settings.py` refuses a `trading_base_url` outside `PAPER_TRADING_HOSTS`
   at load time.
2. `execution/alpaca_paper.py::assert_paper_base_url` re-checks at adapter
   construction.
3. `_default_client_factory` always passes `paper=True`; no parameter or
   environment variable can change it.
4. `AlpacaPaperBroker.assert_paper_only_client` inspects the constructed client
   on **every** `_require_client` call.

There is no `live` value anywhere. `app.environment` accepts only `paper`, and a
config saying otherwise is a hard error, not a warning.
*Test:* `tests/test_cli.py::test_a_config_with_a_live_environment_is_refused`,
`tests/test_execution.py` paper-guard cases.

### 2.2 One authoritative execution path

Only `execution/` may construct a trading client or submit an order. This is
enforced structurally rather than by discipline:

- `ai/shadow.py` is inspected at test time for imports of `cryptobot.execution`,
  `cryptobot.sizing`, `cryptobot.risk`, `cryptobot.reconciliation`,
  `cryptobot.engine`, `cryptobot.runtime`.
- `evaluation/` is inspected for imports of `cryptobot.execution` and
  `cryptobot.risk`.

*Tests:* `test_the_shadow_module_cannot_reach_the_trading_path`,
`test_evaluation_cannot_reach_the_order_path`.

### 2.3 The aggregate ceiling, not a per-order cap

`sizing/ledger.py` owns a single number: available capital is
`ceiling - committed - reserved`. `commit_buy` converts a reservation into
committed capital; `apply_sell` releases it. `rebuild()` replays the journal, so
the number survives a crash.

`AllocationLedger.__init__` calls `rebuild()` and **raises**
`ReconciliationError` if `reserved > ceiling`. A reservation above the whole
ceiling is impossible, because `reserve()` refuses one inside the write lock, so
seeing it means the journal is internally inconsistent and the ledger must not be
used to trade.

`committed > ceiling` is treated differently on purpose: it is a *fact* about
money already spent - a fill that cost more than its reservation, typically after
a gap. Raising on it would leave the fill row written, the order record stale and
every later mutation failing, including the sell that unwinds the position. So the
ledger books it and the order path reacts: a critical `allocation_breach`
reconciliation, `SystemState.HALTED`, and new entries withdrawn. The invariant is
still the gate - `invariant_holds` is what preflight, the reconciler and `unhalt`
all check - but a breach is *recorded and escalated*, never refused into a state
that cannot be repaired.

Realised P&L is reported, never re-deployed: the ceiling is a constant from
configuration.

*Tests:* `tests/test_data_risk.py` ledger suite,
`tests/test_evaluation.py::test_a_reservation_above_the_ceiling_fails_the_invariant`,
`tests/test_ledger_stress.py` (the ceiling under concurrency, partials, cancels,
corruption, and a fill above its own reservation).

### 2.4 Fees are expensed, not capitalised

No fee amount is added to `committed`. Fees are recorded as separate ledger events
into `realized_pnl`.

Crypto fees are charged on the asset the trade *credits*: a buy in the base asset,
a sell in the quote asset. So a buy's base-denominated fee is taken out of the
quantity received, and the cost basis is `net_quantity × price` - the position is
honestly smaller rather than silently over-valued. A quote-denominated fee on a
sell is deducted from proceeds. `fill.fee_in_quote()` values either form in the
quote currency so quantities, costs and fees never mix units.

Why the basis stays free of the fee *amount*: the reconciler compares the local
basis with the broker's `avg_entry_price` basis. If fees were capitalised, every
position would show a small permanent discrepancy that no amount of correct
trading would clear, and a closed position would never release the last cent of
its reservation.

*Tests:* `tests/test_fees.py` (22), the ledger and reconciliation suites in
`tests/test_data_risk.py` and `tests/test_execution.py`.

### 2.5 Intent before submission

`orders.client_order_id` is a foreign key to `order_intents`. An order row
therefore cannot exist without a persisted intent — the schema, not a code
review, guarantees "persist intent before submission".

`OrderService.record_rejection` writes the intent row first for exactly this
reason: a risk rejection is a decision worth keeping, and writing the order row
alone violates the key.

*Test:* `test_a_filled_order_cannot_exist_without_an_intent`.

### 2.6 Ambiguity is not success

A submission that raises after the request may have left the wire becomes
`UNCERTAIN`. It is never retried. Recovery looks the order up by
`client_order_id`. If no answer arrives within
`execution.lost_order_grace_seconds`, the order becomes `LOST` and blocks a paper
run until a human resolves it.

`fills` is keyed by broker fill id with a unique natural index, so duplicate
fill notifications are absorbed instead of double-counted.

### 2.7 Operations are distinct

`core/states.py` maps each `SystemState` to explicit `Capabilities`:

| State | New entries | Cancel orders | Process restart | Manual intervention |
| --- | --- | --- | --- | --- |
| `ACTIVE` | yes | yes | yes | no |
| `PAUSE_ENTRIES` | no | yes | yes | no |
| `HALTED` | no | yes | yes | yes |
| `KILLED` | no | no | no | yes |

`Capabilities` has no `close_positions` field. Pausing entries must never be
silently reinterpreted as liquidating a position; if that capability is ever
needed it will be added deliberately, with its own tests.

*Test:* `test_each_state_permits_exactly_what_the_spec_says`,
`test_capabilities_have_no_way_to_close_a_position`.

### 2.8 One strategy interface, no look-ahead

`Strategy.generate(bars) -> list[StrategySignal]` is the only entry point, used
unchanged by the backtester and the live loop. A strategy sees a bar sequence and
nothing else — no clock, no broker, no journal.

Look-ahead is proven, not asserted: `verify_no_lookahead` checks that a signal at
bar *i* is identical when computed from the prefix `bars[:i+1]`. The backtester
fills at the next bar's open with `execution_delay_bars >= 1`.

*Tests:* `tests/test_strategy_research.py`.

---

## 3. Module responsibilities

### `core/`

| Module | Owns | Does not |
| --- | --- | --- |
| `contracts.py` | every dataclass crossing a module boundary; `Decimal` money; broker-status mapping | hold policy |
| `errors.py` | the domain error vocabulary (`PaperOnlyViolation`, `AmbiguousSubmission`, `ReconciliationError`, …) | decide what to do about them |
| `states.py` | system states and their capabilities | perform transitions |
| `timeframes.py` | bar durations, UTC alignment, age arithmetic | fetch anything |
| `transport.py` | HTTP with timeout, retry, backoff, and a seam for tests | know what it is fetching |
| `logging.py` | one configuration, one secret-redacting formatter | decide log levels per module |
| `jsonutil.py` | `Decimal`-safe serialisation | persist anything |

### `config/`

`Settings` is an immutable dataclass tree. `load_settings` validates loudly:
unknown keys are refused (a typo in `risk.max_position_pct` must not silently
leave the default in place), the paper allowlist is enforced, and
`symbol_allowlist` is checked.

### `data/`

`alpaca_data.py` fetches crypto bars from
`/v1beta3/crypto/{loc}/bars`, following `next_page_token` to completion.
`validation.py` decides whether a series may be traded: monotonic timestamps,
OHLC consistency, staleness, and gaps reported explicitly. `instruments.py`
holds per-symbol order constraints (`min_order_size`, `min_trade_increment`,
`price_increment`).

A zero-volume bar is legitimate for crypto because bars are built from quote
mids; treating it as "no trading" would reject good data.

### `features/` and `strategy/`

`indicators.py` contains pure functions over sequences. `strategy/base.py`
defines `Strategy` (`name`, `version`, `min_bars`, `generate`), and the registry
in `strategy/__init__.py` maps a configured name to a class.

### `sizing/`

- `ledger.py` — the allocation ceiling (see §2.3, §2.4).
- `position.py` — turning an approved signal into a quantity that respects
  `min_order_size`, `min_trade_increment`, and the notional budget.

### `risk/`

`RiskContext` in, `RiskVerdict` out. Fifteen named checks run in a fixed order
and **all** of them run, so a rejection reports every reason at once rather than
the first one. `entry_gate_reasons` covers the state-dependent gates.

### `execution/`

- `base.py` — the `ExecutionAdapter` protocol and broker value objects. It is a
  protocol, not a base class, so a test double does not inherit production
  behaviour.
- `alpaca_paper.py` — the only SDK-facing module. `build_order_payload` is a
  pure function and is unit-tested without the SDK; the request/response
  marshalling is checked against the real `alpaca-py` in
  `tests/test_sdk_integration.py` with every HTTP call intercepted, so no test in
  this suite opens a connection or places an order.
- `fees.py` — attribution of the fees the broker posts after the fact, since the
  Orders API reports none (`docs/API_CAPABILITIES.md` §4).
- `service.py` — the lifecycle: risk verdict → intent → submit → poll →
  reconcile → persist. Enforces "never resubmit an ambiguous order".
- `simulator.py` — a deterministic adapter for offline runs and tests.
- `orders.py` — deterministic client order ids and intent construction.

### `reconciliation/`

Compares positions, open orders and the ledger's `committed` against the broker,
and snapshots cash and equity as evidence. There is no local cash model to compare
against: the ceiling is a configuration constant, so a large broker balance cannot
enlarge the risk budget. Emits `Drift` records with a severity; a critical drift
records a `reconciliation_events` row and, by default, halts entries.

### `journal/`

`schema.py` is the DDL and the reasoning behind each constraint. `store.py` is
the only module that speaks SQL. Every reader is typed and returns contracts, not
rows — except the few diagnostic readers (`ledger_events`,
`recent_reconciliations`) that return mappings on purpose, because their shape
is not part of the domain.

### `research/`

`dataset.py` (validated bars, fingerprinting, train/test split, walk-forward
windows), `backtest.py` (a cost-aware event loop, `TradeRecord`, metrics),
`experiment.py` (suites, walk-forward, sensitivity, all attempts recorded),
`vectorbt_support.py` (optional bridge, imported lazily).

Nothing here can submit an order.

### `evaluation/`

Read-only, and deliberately separate from `research/`: research asks "what would
have happened", evaluation asks "what did happen".

- `trades.py` — reconstructs round trips from fills with average-cost
  accounting; measures slippage against the intent's own reference price.
- `challengers.py` — applies a stated `PromotionRule` to out-of-sample results.
- `promotion.py` — the human approval gate. `challengers.py` only *recommends*;
  nothing here can change a configuration. `Evidence.digest` is a SHA-256 over the
  strategy name, parameters and out-of-sample numbers; `Approval` binds a named
  person, a reason and a tz-aware timestamp to that digest; `Promotion` refuses a
  digest mismatch, so no code path can promote before asking. `record(journal)`,
  the only write this package can make, adds one audit row to
  `strategy_promotions` (schema v4) and changes no trading state.
- `acceptance.py` — evaluates the soak criteria. Duration is *pending* while the
  soak runs; anything else that fails is a failure.

### `ai/`

`ShadowAnalyst` builds a prompt from closed bars, parses a strict JSON response,
and records the assessment with its latency and cost. It cannot import the order
path (§2.2). Every failure — timeout, malformed JSON, oversize response, cost
overrun — is swallowed and recorded; an AI problem must never become a trading
problem.

### `monitoring/`

`report.py` renders text for the terminal. `dashboard.py` is a stdlib
`http.server` bound to loopback serving `/` and `/api/status`; it is a viewer,
not a control surface (`POST` → 501). `notify.py` sends Telegram messages and
records the outcome; the loop guards the call so a misbehaving notifier cannot
stop a halt.

### `runtime.py`, `preflight.py`, `engine.py`, `soak.py`

- `preflight.py` runs ten named checks and returns a `PreflightReport` whose
  failures are blocking and whose warnings are not. It prints *why*, not just
  *that*, something failed.
- `runtime.py` assembles the objects (`Runtime.build`), exposes the cycle
  operations (`refresh_data`, `sync_prices`, `capture_account`, `reconcile`,
  `set_state`), and runs the loop (`TradingLoop`).
- `engine.py` is one decision cycle: gather bars → strategy → risk → maybe
  execute → journal. It returns a `CycleResult` describing what it decided and
  what it did, which is what the CLI prints and the tests assert on.
- `soak.py` reads the journal back and answers one question: for exactly which
  intervals can this journal prove the system was running? It opens and closes a
  window, records heartbeats and lifecycle events, derives daily summaries, and
  reports gaps as gaps. It writes only soak rows and never invents history to fill
  a hole; `evaluation/acceptance.py` consumes its coverage figure.

---

## 4. Data flow of one cycle

```
1. loop tick
2. runtime.refresh_data()        (optional, on cadence)
3. engine.evaluate(symbol)
   a. read closed bars from the journal        → no data? return "no_data"
   b. strategy.generate(bars)                  → hold? return "hold"
   c. build OrderIntent (deterministic coid)
   d. risk.manager.evaluate(context)           → rejected? persist and return
   e. submit is off by default                 → return "approved"
   f. execution.service.submit(intent)         → accepted / uncertain / rejected
   g. persist decision, verdict, intent, order
4. runtime.reconcile()           (on cadence)
5. notifier (guarded)
```

Every step writes to the journal before the next one depends on it. A crash
between (f) and (g) is recoverable because the intent already exists.

---

## 5. Storage

SQLite with WAL and `foreign_keys=ON`. Tables: `schema_meta`, `bars`,
`instruments`, `decisions`, `risk_verdicts`, `order_intents`, `orders`, `fills`,
`position_snapshots`, `balance_snapshots`, `ledger_events`, `ledger_reservations`,
`system_state`, `reconciliation_events`, `experiments`, `ai_assessments`,
`notifications`.

Three constraints carry weight:

- `orders.client_order_id REFERENCES order_intents` — no orphan orders.
- `orders.broker_order_id UNIQUE` — one broker order, one local intent.
- `fills(fill_id)` primary key plus `ux_fills_natural` — duplicate notifications
  are absorbed.

`system_state` is a single row (`CHECK (id = 1)`): there is exactly one bot.

---

## 6. Deliberate non-choices

| Rejected | Why |
| --- | --- |
| A live-trading flag "for later" | A switch that can be flipped is a switch that will be. Four layers and no flag is stronger. |
| Capitalising fees | Breaks basis comparison with the broker (§2.4). |
| Retrying an ambiguous submission | The only safe response to "did that order go through?" is to ask, not to send it again. |
| Making Freqtrade/LEAN/Nautilus the runtime | Each brings an engine, a data model, and a dependency tree that would have to be reconciled with the $100 ledger. Patterns were borrowed; the engine was written. |
| `float` anywhere near money | Binary floating point cannot represent 0.1, and a $100 ceiling makes every rounding error visible. |
| A dashboard with buttons | A read-only viewer cannot be the reason a position changed. |
| Claiming a profitable strategy | Synthetic data says nothing about real markets, and three trades say nothing about a strategy. |

---

## 7. Where to look first

| Question | File |
| --- | --- |
| What exactly is a `OrderIntent`? | `core/contracts.py` |
| Why did my order get rejected? | `risk/manager.py`, then `cryptobot orders` |
| Why is capital unavailable? | `sizing/ledger.py`, then `cryptobot ledger` |
| Why did it not trade today? | `cryptobot decisions`, then `engine.py` |
| Is this safe to run with real keys? | `preflight.py`, then `docs/OPERATIONS.md` |
| Can the AI trade? | `ai/shadow.py` docstring and §2.2 |
| What did the soak prove? | `evaluation/acceptance.py`, then `IMPLEMENTATION_CHECKLIST.md` |
