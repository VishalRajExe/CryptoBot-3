# CRYPTOBOT — Implementation Checklist

This is the authoritative record of what has been built and what the evidence
for it is. A phase is not "Completed" because the source file exists; it is
completed when the acceptance tests below pass and the evidence column names a
command you can re-run.

**Status vocabulary:** `Not started` · `In progress` · `Blocked` · `Completed and verified`

**Scope, stated once:** paper trading only. There is no live-trading phase and
none will be added. See [ARCHITECTURE.md](docs/ARCHITECTURE.md) §2.

---

## Evidence baseline

| Item | Value |
| --- | --- |
| Full suite | `python -m pytest tests -p no:cacheprovider --tb=short -rN` |
| Result | **373 passed** in 9.17s — re-run after the documentation set was written; the suite is unaffected by Markdown |
| Python | 3.11+ (tested on the interpreter in the developer environment) |
| Runtime dependencies | standard library + `PyYAML` |
| Optional extras | `alpaca` (order submission), `research` (vectorbt), `dev` (pytest) |
| Network required by the suite | none |
| Credentials required by the suite | none |
| Documents verified | 7 project documents (`README.md`, this file, five under `docs/`); every relative link resolves, 0 broken |

Per-file collection counts, from `--collect-only`:

| Test module | Scope | Tests |
| --- | --- | --- |
| `tests/test_core.py` | contracts, timeframes, states, journal schema, logging | 45 |
| `tests/test_data_risk.py` | data validation/gaps/cache, ledger, risk manager | 56 |
| `tests/test_strategy_research.py` | indicators, strategies, backtest, walk-forward, no look-ahead | 54 |
| `tests/test_execution.py` | order lifecycle, simulator, paper adapter, recovery | 44 |
| `tests/test_system.py` | preflight, runtime, engine, loop, safe shutdown | 50 |
| `tests/test_ai_monitoring.py` | shadow AI, notifier, reports, dashboard | 44 |
| `tests/test_cli.py` | every CLI command as the operator meets it | 33 |
| `tests/test_evaluation.py` | trade evaluation, challengers, acceptance harness | 47 |
| **Total** | | **373** |

---

## Phase 0 — Workspace and scaffolding

**Objective:** a new, self-contained application that does not modify or import
the 17 reference repositories in the workspace root.

**Status:** `Completed and verified`

| | |
| --- | --- |
| Deliverables | `cryptobot-app/` package layout, `pyproject.toml`, `.gitignore`, `.env.example`, `config/trading.yaml` |
| Files | `pyproject.toml`, `.env.example`, `.gitignore`, `config/trading.yaml` |
| Dependencies | none |
| Tests | `tests/test_core.py::test_default_config_is_paper_and_capped`; `tests/test_cli.py::test_config_check_never_prints_a_secret` |
| Acceptance | `cryptobot config-check` prints effective public configuration and never a credential; a config with `app.environment: live` is refused |
| Risks | config drift between `config/trading.yaml` and `Settings` defaults |
| Evidence | `pytest tests/test_core.py tests/test_cli.py` green; `cryptobot config-check` output contains no secret sentinel values |

**Notes.** The core package deliberately depends on the standard library plus
PyYAML. `alpaca-py` is an optional extra imported lazily, so the entire suite
runs with no SDK installed and no network.

---

## Phase 1 — Core foundation

**Objective:** one set of domain contracts, one configuration model, one state
model, one logging setup, and a journal schema that structurally prevents the
mistakes the plan forbids.

**Status:** `Completed and verified`

| | |
| --- | --- |
| Deliverables | `core/contracts.py`, `core/errors.py`, `core/states.py`, `core/timeframes.py`, `core/logging.py`, `core/transport.py`, `core/jsonutil.py`, `journal/schema.py`, `journal/store.py`, `cli.py` |
| Dependencies | Phase 0 |
| Tests | `tests/test_core.py` (45), `tests/test_cli.py` (33) |
| Acceptance | every money value is `Decimal`, never `float`; timestamps are timezone-aware UTC; the schema refuses an order row without a persisted intent; `order_intents.intent_fingerprint` is queryable for duplicate suppression |
| Risks | Decimal/float leakage at module boundaries; naive datetimes from SQLite |
| Evidence | `pytest tests/test_core.py` → 45 passed. `orders.client_order_id` is a foreign key to `order_intents`; the negative case is proven by `test_a_filled_order_cannot_exist_without_an_intent` in `tests/test_evaluation.py` |

**Design points worth keeping.**

- `SystemState` is `ACTIVE | PAUSE_ENTRIES | HALTED | KILLED`, and each maps to
  a `Capabilities` record. There is intentionally no `close_positions`
  capability: pausing entries must never be confused with liquidating.
- `OrderStatus` mirrors the broker vocabulary (`PENDING_SUBMIT`, `UNCERTAIN`,
  `ACCEPTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REPLACED`,
  `REJECTED`, `EXPIRED`, `LOST`, `UNKNOWN`) and `needs_broker_confirmation`
  marks the subset that blocks a paper run.
- `capabilities_for` accepts a `SystemState` enum directly. Passing a
  str-Enum through `str()` yields `"SystemState.KILLED"`, which used to raise;
  that defect is fixed and covered by the state matrix test.

---

## Phase 2 — Market data

**Objective:** fetch and cache Alpaca crypto bars, validate them, and make a
stale or gapped series impossible to trade on silently.

**Status:** `Completed and verified` (offline and mocked). **Live fetch is
`Blocked`** on credentials — see *External blockers*.

| | |
| --- | --- |
| Deliverables | `data/alpaca_data.py`, `data/validation.py`, `data/instruments.py`, bar cache in the journal, `data-refresh` / `data-import` / `data-export` / `seed-demo` |
| Dependencies | Phase 1 |
| Tests | `tests/test_data_risk.py` (data subset), `tests/test_cli.py` (import/export/seed) |
| Acceptance | pagination honoured via `next_page_token`; bars older than `market.max_bar_age_seconds` are refused by the risk manager; a gap is reported as a gap rather than silently interpolated; zero volume from a quote-mid bar is accepted as legitimate |
| Risks | Alpaca returns `{"bars": {...}, "next_page_token": ...}` per location; treating a missing symbol key as an error rather than "no data" |
| Evidence | `pytest tests/test_data_risk.py` → 56 passed, including gap detection and staleness rejection |

**Verified endpoint facts** used here are recorded in
[docs/API_CAPABILITIES.md](docs/API_CAPABILITIES.md): `GET
/v1beta3/crypto/{loc}/bars`, and `time_in_force` accepts only `gtc`/`ioc` for
crypto.

---

## Phase 3 — Indicators and strategy engine

**Objective:** deterministic indicators and a batch strategy interface that the
backtester and the live loop share, with look-ahead proven absent.

**Status:** `Completed and verified`

| | |
| --- | --- |
| Deliverables | `features/indicators.py`, `strategy/base.py`, `strategy/sma_cross.py`, `strategy/donchian.py`, `strategy/__init__.py` registry |
| Dependencies | Phases 1–2 |
| Tests | `tests/test_strategy_research.py` (54) |
| Acceptance | `generate(bars) -> list[StrategySignal]` is the only entry point; prefix invariance holds (`verify_no_lookahead`); the backtest fills at the next bar open with `execution_delay_bars >= 1`; a strategy version string is persisted with every decision |
| Risks | an indicator that peeks at `bars[-1]` when the signal is meant to use only closed bars |
| Evidence | `pytest tests/test_strategy_research.py` → 54 passed, including `verify_no_lookahead` cases |

---

## Phase 4 — Allocation ledger and risk manager

**Objective:** enforce a $100 ceiling across the aggregate of positions *and*
pending orders, deterministically, from a journal that can be replayed.

**Status:** `Completed and verified`

| | |
| --- | --- |
| Deliverables | `sizing/ledger.py`, `sizing/position.py`, `risk/manager.py` |
| Dependencies | Phase 1 |
| Tests | `tests/test_data_risk.py` (ledger + risk subset), `tests/test_execution.py` (unfundable reservation) |
| Acceptance | `committed + reserved <= ceiling` holds after every operation and after `rebuild()`; realised profit never raises the ceiling; every risk check runs and the complete rejection reason is reported; a rejection is persisted |
| Risks | fee handling: capitalising fees breaks the reconciliation comparison with the broker's `avg_entry_price` basis |
| Evidence | `pytest tests/test_data_risk.py` green. `tests/test_execution.py::test_a_reservation_that_cannot_be_funded_never_touches_the_broker` proves a $10 ceiling rejects a $20 notional with `reserved == 0` |

**Accounting decision (deliberate).** `committed` is a fee-free cost basis and
fees are expensed into `realized_pnl`. This lets the reconciler compare a
position basis against the broker exactly, and lets a closed position release
every cent it reserved. It is documented in the `ledger.py` module docstring.

---

## Phase 5 — Execution

**Objective:** a single authoritative execution path, paper-only by
construction, with deterministic ids, explicit uncertainty, and recovery.

**Status:** `Completed and verified` against the simulator and the mocked SDK
payload. **Real paper submission is `Blocked`** on credentials.

| | |
| --- | --- |
| Deliverables | `execution/base.py` (adapter protocol), `execution/alpaca_paper.py`, `execution/service.py`, `execution/orders.py`, `execution/simulator.py` |
| Dependencies | Phases 1, 4 |
| Tests | `tests/test_execution.py` (44), `tests/test_system.py` (engine/loop subset) |
| Acceptance | the trading client is constructed in exactly one module and always with `paper=True`; `client_order_id` is deterministic; an ambiguous submission becomes `UNCERTAIN` and is never retried; a rejection is recorded with its reason; risk-rejected intents never reach the broker |
| Risks | a submit that raises after the broker accepted it — handled as `UNCERTAIN` followed by lookup, never by resubmission |
| Evidence | `pytest tests/test_execution.py` → 44 passed. `tests/test_ai_monitoring.py::test_the_shadow_module_cannot_reach_the_trading_path` and `tests/test_evaluation.py::test_evaluation_cannot_reach_the_order_path` enforce the separation structurally |

**Four independent paper-only layers** (see `execution/alpaca_paper.py`):

1. the configuration loader refuses any `trading_base_url` outside
   `PAPER_TRADING_HOSTS`;
2. `assert_paper_base_url` re-checks at adapter construction;
3. the client is always constructed with `paper=True`, with no parameter that
   can set it otherwise;
4. `assert_paper_only_client` inspects the constructed client on every
   `_require_client` call.

**Defect fixed during Phase 5 verification.** `OrderService.record_rejection`
wrote the `orders` row before the `order_intents` row, violating the foreign
key. Any risk-rejected intent on the `--submit` path raised `ExecutionError`.
The intent is now written first.

---

## Phase 6 — Persistence, reconciliation, restart recovery

**Objective:** after a crash, the bot knows exactly what it had, what it
submitted, and what it still does not know.

**Status:** `Completed and verified`

| | |
| --- | --- |
| Deliverables | `journal/store.py` readers/writers, `reconciliation/reconciler.py`, restart recovery in `runtime.py` |
| Dependencies | Phases 1, 5 |
| Tests | `tests/test_execution.py` (recovery subset), `tests/test_system.py` (loop + reconcile cadence) |
| Acceptance | an `UNCERTAIN` order is resolved by client order id, never by resubmission; an order unresolved past `execution.lost_order_grace_seconds` becomes `LOST`; reconciliation drift is recorded with a severity, and a critical drift halts entries |
| Risks | duplicate fills double-counting a position — absorbed by the `fills` natural unique index and `INSERT OR IGNORE` |
| Evidence | `pytest tests/test_execution.py tests/test_system.py` green; `cryptobot reconcile --mode simulated` reports a clean state on a fresh journal |

---

## Phase 7 — Research environment

**Objective:** reproducible experiments with explicit costs, an out-of-sample
split that is stated rather than implied, and a runner that never touches the
broker.

**Status:** `Completed and verified` (`vectorbt` is optional; the stdlib engine
is the reproducible path)

| | |
| --- | --- |
| Deliverables | `research/backtest.py`, `research/dataset.py`, `research/experiment.py`, `research/vectorbt_support.py`, `backtest` / `research` / `walk-forward` / `sensitivity` commands |
| Dependencies | Phase 3 |
| Tests | `tests/test_strategy_research.py` (54) |
| Acceptance | every attempted parameter combination is recorded, including invalid and degenerate ones; costs are explicit (`fee_bps`, `slippage_bps`); buy-and-hold is shown on the same test window; results are stored with a data fingerprint and code version |
| Risks | selection bias from reporting only the best combination — mitigated by recording all attempts and always printing the caveats |
| Evidence | `pytest tests/test_strategy_research.py` → 54 passed; `cryptobot backtest` and `cryptobot research --grid ...` exercised by the CLI tests |

---

## Phase 8 — Operations

**Objective:** preflight that fails closed, rate limiting, a kill switch, and a
shutdown that does not abandon open orders or leave a half-written state.

**Status:** `Completed and verified`

| | |
| --- | --- |
| Deliverables | `preflight.py`, `runtime.py` (`Runtime`, `TradingLoop`, `LoopReport`), `core/transport.py` retry/backoff, `pause` / `resume` / `kill` / `cancel-all` commands |
| Dependencies | Phases 1–7 |
| Tests | `tests/test_system.py` (50) |
| Acceptance | preflight runs 10 named checks and prints the reason for every failure and warning; `HALTED`/`KILLED` stop new entries but keep the loop alive; `cancel-all` requires `--yes`; an operator stop is reported as an operator stop, not as a normal completion |
| Risks | a "pause" that silently cancels orders would destroy state; capabilities prevent that by construction |
| Evidence | `pytest tests/test_system.py` → 50 passed, including the state-capability matrix and repeated-error halt |

**Defects fixed during Phase 8 verification.**

- `capabilities_for(SystemState.KILLED)` raised `ValueError` because `str()`
  on a str-Enum yields `"SystemState.KILLED"`. Fixed to accept the enum.
- `LoopReport.stopped_reason` defaulted to `"completed"`, so an operator stop
  was misreported. Default is now empty and the fallback is
  `self._stop_reason or "completed"`.
- `cmd_preflight` printed only `report.summary()`, hiding which check failed.
  Added `_print_preflight`, which prints every failure and warning.

---

## Phase 9 — Shadow-mode AI

**Objective:** an AI opinion that is recorded, scored, and structurally unable
to influence an order.

**Status:** `Completed and verified` (no real provider is wired; a `NullProvider`
and a `StaticProvider` exist for testing)

| | |
| --- | --- |
| Deliverables | `ai/shadow.py`, `ai-review` command |
| Dependencies | Phases 1, 3 |
| Tests | `tests/test_ai_monitoring.py` (44, AI subset) |
| Acceptance | disabled by default; the module imports nothing from execution, sizing, risk, reconciliation, engine, or runtime; strict JSON parsing; latency and cost ceilings enforced; every failure is swallowed and recorded; no broker credentials reach the provider |
| Risks | prompt injection from market commentary — the payload is parsed strictly and a malformed or oversized response is discarded |
| Evidence | `pytest tests/test_ai_monitoring.py` green; `test_the_shadow_module_cannot_reach_the_trading_path` inspects the module source for forbidden imports |

---

## Phase 10 — Monitoring

**Objective:** an operator can see allocation, positions, orders, decisions and
health without the monitoring path being able to change anything.

**Status:** `Completed and verified`

| | |
| --- | --- |
| Deliverables | `monitoring/report.py`, `monitoring/dashboard.py`, `monitoring/notify.py`, `status` / `ledger` / `orders` / `decisions` / `dashboard` commands |
| Dependencies | Phases 1, 4 |
| Tests | `tests/test_ai_monitoring.py` (monitoring subset), `tests/test_cli.py` |
| Acceptance | the dashboard binds loopback and exposes only `/` and `/api/status`; `POST` returns 501 so no route can change state; the status payload is natively JSON-serialisable; the notifier cannot break the trading loop |
| Risks | a monitoring exception taking down the loop — the loop wraps notification in a guard, covered by `test_a_misbehaving_notifier_cannot_break_a_halt` |
| Evidence | `pytest tests/test_ai_monitoring.py` green, including a real loopback HTTP request and a 404 for `/orders/cancel` |

**Defect fixed during Phase 10 verification.** `status_payload` emitted a raw
`Decimal` for `deployment_pct`, which only serialised because the HTTP handler
passed `default=str`. It is now stringified at the source.

---

## Phase 11 — Trade evaluation and challengers

**Objective:** measure what happened rather than what was predicted, and make
"should we change the strategy?" an evidence question with a stated rule.

**Status:** `Completed and verified`

| | |
| --- | --- |
| Deliverables | `evaluation/trades.py`, `evaluation/challengers.py`, `evaluation/__init__.py`, `trade-report` and `challengers` commands |
| Dependencies | Phases 1, 4, 5, 6, 7 |
| Tests | `tests/test_evaluation.py` (47), `tests/test_cli.py` (evaluation subset) |
| Acceptance | a round trip is reconstructed with average-cost accounting and its fees; a partially closed lot releases only its share of the fees; an unmatched sell is reported, never dropped; slippage is measured against the intent's own reference price; a challenger is only promotable on out-of-sample evidence, beating the baseline by a stated margin, within a drawdown budget, and only if it is not look-ahead-contaminated; too few trades reports `insufficient_evidence` rather than a verdict |
| Risks | quoting a "profit" from three trades — mitigated by refusing to judge below the out-of-sample trade floor |
| Evidence | `pytest tests/test_evaluation.py` → 47 passed. `cryptobot trade-report` and `cryptobot challengers --grid ...` exercised by the CLI tests |

**Defect fixed during Phase 11 verification.** Serialising a report whose profit
factor is infinite raised `decimal.InvalidOperation` because
`Decimal("Infinity").quantize(...)` is undefined. `_round` now leaves
non-finite values alone.

---

## Phase 12 — Paper soak and acceptance

**Objective:** run paper trading for at least 30 days and accept or refuse it
against criteria fixed in advance.

**Status:** **`Blocked`** on credentials and elapsed time. The harness is
`Completed and verified`; the soak itself has not run.

| | |
| --- | --- |
| Deliverables | `evaluation/acceptance.py`, `acceptance` command |
| Dependencies | Phases 2–11 |
| Tests | `tests/test_evaluation.py` (acceptance subset) |
| Acceptance | the harness reports duration as *pending* rather than failed while the soak is incomplete, reports a broken ledger invariant as a failed criterion instead of crashing, refuses to certify a halted bot, and never mutates the journal |
| Risks | mistaking elapsed time for evidence — the criteria require decisions, closed trades, reconciliations, and bounded slippage, not just a long window |
| Evidence | The harness itself is verified by 15 tests. The soak is **not** verified and must not be reported as such. `cryptobot acceptance --days 30` on an empty journal exits `1` with `NOT ACCEPTED` |

**Defect fixed during Phase 12 harness work.** `AllocationLedger.__init__`
raises `ReconciliationError` when the invariant is already broken, which made
`run_acceptance` crash on exactly the case it exists to report. The harness now
catches it and records a failed criterion, then continues evaluating the rest.

### How to unblock Phase 12

1. Export paper credentials (never paste them into a chat, ticket, or log):

   ```powershell
   $env:ALPACA_API_KEY = "..."
   $env:ALPACA_API_SECRET_KEY = "..."
   ```

2. `cryptobot preflight --mode paper` — must report no failures.
3. `cryptobot data-refresh --lookback 500` — must store real bars.
4. `cryptobot paper-run --submit` — leave running for 30+ days.
5. `cryptobot acceptance --days 30` — the verdict must be `ACCEPTED`.

Until step 5 produces `ACCEPTED`, the project has not completed Phase 12.

---

## External blockers

| Blocker | Phase | Effect | What is still possible |
| --- | --- | --- | --- |
| No Alpaca paper credentials in this environment | 2, 5, 12 | real bars cannot be fetched, no paper order can be placed, the soak cannot start | everything else: simulator, mocked adapter, full suite |
| No 30 days of elapsed time | 12 | the acceptance harness will report `NOT ACCEPTED` | running it, and every criterion it evaluates |
| `alpaca-py` not installed by default | 5 | real submission raises `MissingDependency` with an install hint | payload construction and the simulator |

---

## Cross-cutting verification

| Rule | How it is enforced | Evidence |
| --- | --- | --- |
| One authoritative execution path | structural tests assert the shadow-AI and evaluation modules cannot import the order path; the trading client is constructed in one module | `test_the_shadow_module_cannot_reach_the_trading_path`, `test_evaluation_cannot_reach_the_order_path` |
| Paper-only by construction | four independent layers; no live switch exists | `tests/test_execution.py` paper-guard cases, `test_a_config_with_a_live_environment_is_refused` |
| Aggregate allocation, not per-order | the ledger owns `committed` and `reserved` and refuses to build when the invariant is broken | `tests/test_data_risk.py`, `test_a_reservation_above_the_ceiling_fails_the_invariant` |
| No invented API capability | every Alpaca fact carries a source in `docs/API_CAPABILITIES.md`; unverified behaviour is left unimplemented | `docs/API_CAPABILITIES.md` |
| No real orders in the suite | the simulator is the default mode; `--submit` is required to reach a broker | every CLI trading test |
| Claims match evidence | this file | the commands above |

---

## Document map

| Document | Purpose |
| --- | --- |
| `README.md` | what the project is, how to install and run it |
| `docs/ARCHITECTURE.md` | module boundaries and the invariants each one owns |
| `docs/IMPLEMENTATION_ROADMAP.md` | phase order, dependencies, and what remains |
| `docs/API_CAPABILITIES.md` | verified Alpaca facts and the behaviours deliberately not implemented |
| `docs/OPERATIONS.md` | day-to-day runbook, states, failure handling |
| `docs/SOURCE_REFERENCES.md` | which reference repository informed which decision, and what was not reused |
| `IMPLEMENTATION_CHECKLIST.md` | this file |
