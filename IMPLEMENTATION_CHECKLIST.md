# CRYPTOBOT — Implementation Checklist

This is the authoritative record of what has been built and what the evidence for
it is. Nothing here is `COMPLETED AND VERIFIED` because a file exists: it is
verified when a command in the *Evidence* row has been run and its output
recorded below.

**Scope, stated once:** paper trading only. There is no live-trading phase and
none will be added. See [ARCHITECTURE.md](docs/ARCHITECTURE.md) §2.

---

## Status vocabulary

Exactly five statuses are used in this document. They are not interchangeable
and there are no synonyms.

| Status | Means | How it is earned |
| --- | --- | --- |
| `COMPLETED AND VERIFIED` | built, and a named command reproduces the claim | the evidence row names a test or CLI run whose output is quoted in this file |
| `IMPLEMENTED BUT NOT FULLY VERIFIED` | the code exists and its own tests pass, but part of the claim depends on something that has not been exercised | the unexercised part is named explicitly, field by field |
| `IN PROGRESS` | partially built; the gaps are known | the gaps are listed |
| `BLOCKED BY EXTERNAL PREREQUISITE` | cannot proceed here; the blocker is outside this repository | the blocker names the missing prerequisite and what it unblocks |
| `NOT IMPLEMENTED` | deliberately absent | the reason it is absent is stated |

**A passing mocked suite does not establish that real Alpaca order execution
works.** Mocked transport proves our request construction and response parsing
against the published schema. It cannot prove that the broker accepts the order,
that a fill arrives, that fees post as modelled, or that a real rejection maps to
the status we expect. Those are separate rows, and they are marked separately.

---

## Evidence baseline

Recorded 2026-10-29. Both interpreters, from `d:\CRYPTOBOT\cryptobot-app`.

| Item | Value |
| --- | --- |
| Command | `python -m pytest` (no extra `-q`: `addopts` already sets it, and `-qq` suppresses the summary line) |
| `.venv` (with `alpaca` extra) | **510 passed**, 1 warning, 14.00s |
| System Python 3.14 (no `alpaca-py`) | **477 passed, 1 skipped**, 12.66s — the skip is `tests/test_sdk_integration.py`, which skips itself via `importorskip("alpaca")` |
| Network required by the suite | none |
| Credentials required by the suite | none |
| Runtime dependencies | standard library + `PyYAML` |
| Optional extras | `alpaca` (`alpaca-py`, for SDK-level verification), `research` (`vectorbt`), `dev` (`pytest`) |

Per-module collection counts (`pytest --collect-only -q`):

| Test module | Scope | Tests |
| --- | --- | --- |
| `tests/test_core.py` | contracts, timeframes, states, journal schema, logging | 45 |
| `tests/test_data_risk.py` | data validation/gaps/cache, ledger, risk manager | 56 |
| `tests/test_strategy_research.py` | indicators, strategies, backtest, walk-forward, no look-ahead | 54 |
| `tests/test_execution.py` | order lifecycle, simulator, paper adapter, recovery | 46 |
| `tests/test_fees.py` | crypto fee attribution, currency side, partial fills | 22 |
| `tests/test_system.py` | preflight, runtime, engine, loop, safe shutdown | 54 |
| `tests/test_ai_monitoring.py` | shadow AI, notifier, reports, dashboard | 44 |
| `tests/test_cli.py` | every CLI command as the operator meets it | 46 |
| `tests/test_evaluation.py` | trade evaluation, challengers, acceptance harness | 49 |
| `tests/test_soak.py` | soak windows, heartbeats, gaps, restarts, daily summaries | 21 |
| `tests/test_promotion.py` | the human approval gate and its persistence | 20 |
| `tests/test_ledger_stress.py` | the ceiling under concurrency, partials, cancels, corruption | 20 |
| `tests/test_sdk_integration.py` | `alpaca-py` models and client against a mocked transport | 33 |
| **Total** | | **510** |

## Verification levels

The four levels are kept apart on purpose. Mixing them is how "the tests pass"
turns into a claim nobody checked.

| Level | What it proves | Performed? | Where the result is |
| --- | --- | --- | --- |
| **Unit, offline** | our logic against fixtures we control | **yes** — 477 tests, no network | this file, per phase |
| **SDK-mocked** | our requests and parsing against `alpaca-py`'s own models, over a fake transport | **yes** — 33 tests | Phase 5 |
| **Authenticated paper** | the broker accepts our orders, fills them, and reports fees | **yes, once** — one buy submitted, filled, reconciled and reported against `paper-api.alpaca.markets`; no sell, no cancel, no uncertain recovery | Phase 5 |
| **Soak** | continuous paper operation over a declared interval, with coverage evidence | **NO** — nothing has run yet | Phase 12, `BLOCKED BY EXTERNAL PREREQUISITE` |

---

## Phase 0 — Workspace and scaffolding

**Objective:** a self-contained application that does not modify or import the
17 reference repositories in the workspace root.

**Status:** `COMPLETED AND VERIFIED`

| | |
| --- | --- |
| Deliverables | `cryptobot-app/` layout, `pyproject.toml`, `.gitignore`, `.env.example`, `config/trading.yaml` |
| Dependencies | none |
| Tests | `tests/test_core.py::test_default_config_is_paper_and_capped`, `tests/test_cli.py::test_config_check_never_prints_a_secret` |
| Acceptance | `cryptobot config-check` prints effective public configuration and never a credential; `app.environment: live` is refused |
| Evidence | `pytest tests/test_core.py tests/test_cli.py` green; `cryptobot config-check` on the shipped config prints `validation: ok` and `credentials: NOT SET -> ... (not required for simulated mode, tests, or backtests)` |

`.env.example` is a template only. Nothing loads it: there is no dotenv
dependency, and the application reads the two variable *names* configured in
`config/trading.yaml` from the process environment. `.env` and `.env.*` are
gitignored (`!.env.example` re-includes the template); `git check-ignore -v .env`
confirms it.

---

## Phase 1 — Core foundation

**Objective:** one set of domain contracts, one configuration model, one state
model, one logging setup, and a journal schema that structurally prevents the
mistakes the plan forbids.

**Status:** `COMPLETED AND VERIFIED`

| | |
| --- | --- |
| Deliverables | `core/contracts.py`, `core/errors.py`, `core/states.py`, `core/timeframes.py`, `core/logging.py`, `core/transport.py`, `core/jsonutil.py`, `journal/schema.py`, `journal/store.py`, `cli.py` |
| Dependencies | Phase 0 |
| Tests | `tests/test_core.py` (45) |
| Acceptance | every money value is `Decimal`, never `float`; timestamps are timezone-aware UTC; the schema refuses an order row without a persisted intent |
| Evidence | `pytest tests/test_core.py` → 45 passed; `orders.client_order_id` is a foreign key to `order_intents`, proven negatively by `tests/test_evaluation.py::test_a_filled_order_cannot_exist_without_an_intent` |

**Design points worth keeping.** `SystemState` is
`ACTIVE | PAUSE_ENTRIES | HALTED | KILLED`, each mapping to a `Capabilities`
record with no `close_positions` capability — pausing entries must never be
confused with liquidating. `OrderStatus` mirrors the broker vocabulary and
`needs_broker_confirmation` marks the subset that blocks a paper run.

The journal schema is now **version 4**: v3 added the soak tables, v4 added
`strategy_promotions`. Migrations are additive `CREATE TABLE IF NOT EXISTS` run
on every open, so an older database gains the tables without losing rows.

---

## Phase 2 — Market data

**Objective:** fetch and cache Alpaca crypto bars, validate them, and make a
stale or gapped series impossible to trade on silently.

**Status:** `COMPLETED AND VERIFIED`, including one live fetch against the real
`data.alpaca.markets` endpoint on 2026-10-29 (`data-refresh --lookback 500`
stored real BTC/USD hourly bars, and preflight's `data_freshness` check passed
against them).

| | |
| --- | --- |
| Deliverables | `data/alpaca_data.py`, `data/validation.py`, `data/instruments.py`, bar cache in the journal, `data-refresh` / `data-import` / `data-export` / `seed-demo` |
| Dependencies | Phase 1 |
| Tests | `tests/test_data_risk.py` (data subset), `tests/test_cli.py` (import/export/seed) |
| Acceptance | pagination honoured via `next_page_token`; bars older than `market.max_bar_age_seconds` are refused by the risk manager; a gap is reported as a gap, never interpolated; zero volume from a quote-mid bar is accepted as legitimate; instrument metadata comes from `/v2/assets`, not a constant |
| Evidence | `pytest tests/test_data_risk.py` → 56 passed; `cryptobot seed-demo --bars 120` stores 120 rows and prints `SYNTHETIC DATA`; `cryptobot data-refresh` without credentials exits `2` with `refusing to fetch: missing credentials`; with credentials it stored 500 real bars and `cryptobot run-once` reported no fallback warning, with `journal.get_instrument("BTC/USD")` showing the broker's `min_order_size=0.000012531` rather than the constant |

Endpoint facts used here are recorded with sources in
[docs/API_CAPABILITIES.md](docs/API_CAPABILITIES.md): `GET
/v1beta3/crypto/{loc}/bars`, and `time_in_force` accepting only `gtc`/`ioc` for
crypto.

---

## Phase 3 — Indicators and strategy engine

**Objective:** deterministic indicators and a batch strategy interface shared by
the backtester and the live loop, with look-ahead proven absent.

**Status:** `COMPLETED AND VERIFIED`

| | |
| --- | --- |
| Deliverables | `features/indicators.py`, `strategy/base.py`, `strategy/sma_cross.py`, `strategy/donchian.py`, `strategy/__init__.py` registry |
| Dependencies | Phases 1–2 |
| Tests | `tests/test_strategy_research.py` (54) |
| Acceptance | `generate(bars) -> list[StrategySignal]` is the only entry point; prefix invariance holds (`verify_no_lookahead`); the backtest fills at the next bar open with `execution_delay_bars >= 1`; a strategy version string is persisted with every decision |
| Evidence | `pytest tests/test_strategy_research.py` → 54 passed, including the prefix-invariance cases |

---

## Phase 4 — Allocation ledger and risk manager

**Objective:** enforce a $100 ceiling across the aggregate of positions *and*
pending orders, deterministically, from a journal that can be replayed.

**Status:** `COMPLETED AND VERIFIED`

| | |
| --- | --- |
| Deliverables | `sizing/ledger.py`, `sizing/position.py`, `risk/manager.py` |
| Dependencies | Phase 1 |
| Tests | `tests/test_data_risk.py` (ledger + risk), `tests/test_execution.py` (unfundable reservation), `tests/test_ledger_stress.py` (20) |
| Acceptance | `committed + reserved <= ceiling` holds after every operation and after `rebuild()`; realised profit never raises the ceiling; the ceiling is a constant from configuration and is never derived from a broker balance; every risk check runs and the full rejection reason is persisted |
| Evidence | `pytest tests/test_ledger_stress.py` → 20 passed; `pytest tests/test_execution.py::test_a_reservation_that_cannot_be_funded_never_touches_the_broker` proves a $10 ceiling rejects a $20 notional with `reserved == 0` |

**The invariant is split in two, deliberately.**

- `reserved > ceiling` is *impossible* — `reserve()` refuses it inside the write
  lock — so `rebuild()` and `_assert_invariant()` still raise
  `ReconciliationError` on it.
- `committed > ceiling` is a *fact*: a real fill that cost more than its
  reservation. Refusing to record it would leave the fill row written, the order
  record stale, and every later mutation raising — including the sell needed to
  unwind the position. So the ledger books it and `OrderService` reacts: a
  critical `allocation_breach` reconciliation, `SystemState.HALTED`, a
  `log.critical`, and new entries withdrawn. Evidence:
  `tests/test_ledger_stress.py::test_a_fill_above_its_reservation_halts_and_is_recorded_not_refused`
  asserts the committed figure, the FILLED record, the reconciliation, the halt,
  and that a subsequent sell restores the invariant.

**Accounting decision (deliberate, and unchanged by the fee work).** `committed`
is a fee-free cost basis; fees are expensed into `realized_pnl`. That lets the
reconciler compare a position basis against the broker exactly, and lets a closed
position release every cent it reserved.

---

## Phase 5 — Execution

**Objective:** a single authoritative execution path, paper-only by construction,
with deterministic ids, explicit uncertainty, recovery, and fee accounting that
matches how the broker actually charges.

**Status:** `IMPLEMENTED BUT NOT FULLY VERIFIED`

- `COMPLETED AND VERIFIED` against the simulator and against `alpaca-py` over a
  mocked transport (33 SDK tests).
- `COMPLETED AND VERIFIED` **once, against the real paper API** (2026-10-29):
  preflight passed all 10 checks, a `market` buy of 0.000248437 BTC was submitted
  with `client_order_id=cb-f6a4712fefb896e`, filled at 80123.59, booked as
  0.0002478159075 net of a base-asset fee, and reconciled clean against the
  broker's own position and cost basis. `trade-report` measured +2.88 bps of
  slippage and 0.0498 USD of fee.
- Still unexercised against the real API, and therefore still unverified: a sell
  (so a round trip, realised P&L, and a quote-denominated `FEE`), a cancel, an
  `UNCERTAIN` submission recovered by polling, a partial fill, and the actual fee
  activity — Alpaca posts those after the end of the trading day, so the 0.0498 is
  still our estimate at the reserve rate.
- `NOT IMPLEMENTED` by design: live trading, and any order type other than
  `market`.

| | |
| --- | --- |
| Deliverables | `execution/base.py` (adapter protocol), `execution/alpaca_paper.py`, `execution/service.py`, `execution/orders.py`, `execution/simulator.py`, `execution/fees.py` |
| Dependencies | Phases 1, 4 |
| Tests | `tests/test_execution.py` (46), `tests/test_fees.py` (22), `tests/test_sdk_integration.py` (33), `tests/test_system.py` (engine/loop subset) |
| Acceptance (verified) | the trading client is constructed in exactly one module and always with `paper=True`; `client_order_id` is deterministic; an ambiguous submission becomes `UNCERTAIN` and is never retried; a rejection is recorded with its reason; risk-rejected intents never reach the broker; a real paper account accepts a `market` buy, reports it filled, and its position and cost basis agree with the ledger |
| Acceptance (not verified) | that a real paper account accepts a **sell**, a **cancel**, or a **partial** fill; that a posted fee activity matches the estimate; and that an `UNCERTAIN` submission can be resolved against the live API |
| Evidence | `pytest tests/test_execution.py tests/test_fees.py tests/test_sdk_integration.py` green in the `.venv`; the same invocation on system Python skips the SDK module (1 skip) because `alpaca-py` is absent. Live: `cryptobot preflight --mode paper` → 10 checks, 0 failures; `cryptobot reconcile` → `reconciliation clean against alpaca-paper (0 order(s), 1 position(s), 0 recovered)`; `cryptobot trade-report` → 1 filled order, +2.88 bps, fees (quote) 0.0498 |

**Order-lifecycle order of operations** (`OrderService.apply_broker_order`):
resolve intent → book fills → flag over-deployment → write the order record →
record reconciliation. Fills are booked before the record so that a crash between
the two replays identically; the over-deployment check runs *after* booking so it
sees the post-fill ledger, and *before* the record so a breach cannot be recorded
as an ordinary success.

**Crypto fee convention** (verified 2026-10-29, `docs/API_CAPABILITIES.md` §4):
the fee is charged on the asset the trade *credits* — a buy in the base asset, a
sell in the quote asset. Therefore `commit_buy` uses `fill.net_quantity` (the fee
was taken out of what we received) and `fill.fee_in_quote()` values a
base-denominated charge at the fill price. The Orders API exposes no fee at all;
actual charges arrive after the end of the trading day as `CFEE`/`FEE`
activities. `execution/fees.py` replaces the estimate with the broker's figure
exactly once per activity id, expenses unattributable fees rather than dropping
them, and moves the cost basis when a base-asset fee changes the credited
quantity. Evidence: `tests/test_fees.py` (22 passed), including
`test_a_base_currency_fee_reduces_the_quantity_actually_credited`,
`test_a_quote_currency_fee_is_a_cost_not_a_quantity_reduction`,
`test_a_round_trip_conserves_capital`,
`test_a_partial_fill_is_booked_before_the_order_completes` and
`test_replaying_a_cumulative_partial_fill_does_not_double_book`.

**Four independent paper-only layers** (`execution/alpaca_paper.py`): the config
loader refuses a `trading_base_url` outside `PAPER_TRADING_HOSTS`;
`assert_paper_base_url` re-checks at construction; the client is always built
with `paper=True` and there is no parameter that can change it;
`assert_paper_only_client` inspects the constructed client on every
`_require_client`. None of these was relaxed to make the SDK tests pass — the SDK
tests are driven through a fake transport instead.

---

## Phase 6 — Persistence, reconciliation, restart recovery

**Objective:** after a crash, the bot knows exactly what it had, what it
submitted, and what it still does not know.

**Status:** `COMPLETED AND VERIFIED` (against the simulator and the mocked
adapter)

| | |
| --- | --- |
| Deliverables | `journal/store.py` readers/writers, `reconciliation/reconciler.py`, restart recovery in `runtime.py` |
| Dependencies | Phases 1, 5 |
| Tests | `tests/test_execution.py` (recovery), `tests/test_system.py` (loop + reconcile cadence), `tests/test_ledger_stress.py` (restart with orphaned reservations) |
| Acceptance | an `UNCERTAIN` order is resolved by client order id, never by resubmission; an order unresolved past `execution.lost_order_grace_seconds` becomes `LOST`; drift is recorded with a severity and a critical drift halts entries; account balances and position snapshots are stored as evidence |
| Evidence | `pytest tests/test_execution.py tests/test_system.py` green; `cryptobot reconcile --mode simulated` reports a clean state on a fresh journal |

Reconciliation compares, in order: order resolution, position quantity per
symbol, `committed` against the broker's reported cost basis, open broker orders
with no local intent, and the allocation invariant. It stores cash/equity
snapshots but never derives the ceiling from them — the ceiling is a
configuration constant, so a large broker balance cannot enlarge the risk budget.

---

## Phase 7 — Research environment

**Objective:** reproducible experiments with explicit costs, an out-of-sample
split that is stated rather than implied, and a runner that never touches the
broker.

**Status:** `COMPLETED AND VERIFIED` (`vectorbt` is optional; the stdlib engine
is the reproducible path)

| | |
| --- | --- |
| Deliverables | `research/backtest.py`, `research/dataset.py`, `research/experiment.py`, `research/vectorbt_support.py`, `backtest` / `research` / `walk-forward` / `sensitivity` commands |
| Dependencies | Phase 3 |
| Tests | `tests/test_strategy_research.py` (54) |
| Acceptance | every attempted combination is recorded, including invalid and degenerate ones; costs are explicit (`fee_bps`, `slippage_bps`); buy-and-hold is shown on the same window; results carry a data fingerprint and code version |
| Evidence | `pytest tests/test_strategy_research.py` → 54 passed; `cryptobot backtest` / `research --grid ...` exercised in `tests/test_cli.py` |

No result from this phase is a profitability claim. Synthetic bars stay labelled
synthetic everywhere they are reported.

---

## Phase 8 — Operations

**Objective:** preflight that fails closed, a kill switch, an auditable exit from
every stop state, and a shutdown that does not abandon open orders.

**Status:** `COMPLETED AND VERIFIED`

| | |
| --- | --- |
| Deliverables | `preflight.py`, `runtime.py` (`Runtime`, `TradingLoop`, `LoopReport`), `core/transport.py` retry/backoff, `pause` / `resume` / `kill` / `unhalt` / `cancel-all` / `env-hint` commands |
| Dependencies | Phases 1–7 |
| Tests | `tests/test_system.py` (50), `tests/test_cli.py` (45) |
| Acceptance | preflight runs 10 named checks and prints the reason for every failure; `HALTED`/`KILLED` stop new entries but keep the loop alive; `cancel-all` requires `--yes`; an operator stop is reported as an operator stop |
| Evidence | `pytest tests/test_system.py tests/test_cli.py` green; `cryptobot preflight --mode paper` on this machine exits `1` with `preflight FAILED (10 checks, 3 failure(s), 0 warning(s))`, naming `credentials`, `broker_connection` and `data_freshness`, printing variable names only; while the journal was `HALTED` from a credential-less start it reported 4, adding `system_state` — then `cryptobot unhalt` + `cryptobot resume` returned it to `active` (`halted -> pause_entries`, `pause_entries -> active`, both exit `0`) |

**Leaving a stop state is now guarded, and `resume` is not a bypass.**

`resume` moves `PAUSE_ENTRIES -> ACTIVE` and refuses to leave `HALTED` or
`KILLED`. Both of those mean trading should stop and both can be set by code, so
the exit is `cryptobot unhalt --reason ...`, which refuses when:

- no reason is given;
- the allocation invariant is still broken (it prints committed/reserved/ceiling);
- any order still `needs_broker_confirmation` (it names up to five).

It then moves to `PAUSE_ENTRIES`, not `ACTIVE`, and records an
`operator_state_change` reconciliation including the original halt reason.
Resuming trading is a second, deliberate command. Evidence:
`tests/test_cli.py::test_resume_cannot_undo_a_halt`,
`test_resume_cannot_undo_the_kill_switch`,
`test_unhalt_refuses_while_the_allocation_invariant_is_broken`,
`test_unhalt_refuses_while_an_order_is_unresolved`,
`test_unhalt_can_release_the_kill_switch`.

`env-hint` prints the four credential variable names with `set`/`NOT SET` and the
shell syntax to populate them. It inspects presence only and never reads a value;
`tests/test_cli.py::test_env_hint_names_the_variables_without_reading_the_values`
plants sentinel values and asserts they never appear in the output.

---

## Phase 9 — Shadow-mode AI

**Objective:** an AI opinion that is recorded, scored, and structurally unable to
influence an order.

**Status:** `COMPLETED AND VERIFIED` for the shadow contract. No real provider is
wired (`NullProvider`/`StaticProvider` for tests) — that is a scope decision, not
a gap: the module has no order authority either way.

| | |
| --- | --- |
| Deliverables | `ai/shadow.py`, `ai-review` command |
| Dependencies | Phases 1, 3 |
| Tests | `tests/test_ai_monitoring.py` (AI subset) |
| Acceptance | disabled by default; imports nothing from execution, sizing, risk, reconciliation, engine or runtime; strict JSON parsing; latency and cost ceilings; every failure swallowed and recorded; no broker credentials reach the provider |
| Evidence | `pytest tests/test_ai_monitoring.py` green; `test_the_shadow_module_cannot_reach_the_trading_path` inspects the module source for forbidden imports |

---

## Phase 10 — Monitoring

**Objective:** an operator can see allocation, positions, orders, decisions and
health without the monitoring path being able to change anything.

**Status:** `COMPLETED AND VERIFIED`

| | |
| --- | --- |
| Deliverables | `monitoring/report.py`, `monitoring/dashboard.py`, `monitoring/notify.py`, `status` / `ledger` / `orders` / `decisions` / `dashboard` commands |
| Dependencies | Phases 1, 4 |
| Tests | `tests/test_ai_monitoring.py` (monitoring subset), `tests/test_cli.py` |
| Acceptance | the dashboard binds loopback and serves only `/` and `/api/status`; `POST` returns 501; the status payload is natively JSON-serialisable; a misbehaving notifier cannot break the loop |
| Evidence | `pytest tests/test_ai_monitoring.py` green, including a real loopback request and a 404 for `/orders/cancel` |

---

## Phase 11 — Trade evaluation, challengers, and the promotion gate

**Objective:** measure what happened rather than what was predicted, and make
"should we change the strategy?" an evidence question with a stated rule **and a
named human**.

**Status:** `COMPLETED AND VERIFIED`

| | |
| --- | --- |
| Deliverables | `evaluation/trades.py`, `evaluation/challengers.py`, `evaluation/promotion.py`, `evaluation/acceptance.py`, `trade-report` / `challengers` / `promote` commands, `strategy_promotions` table |
| Dependencies | Phases 1, 4, 5, 6, 7 |
| Tests | `tests/test_evaluation.py` (47), `tests/test_promotion.py` (20), `tests/test_cli.py` (evaluation subset) |
| Acceptance | a round trip is reconstructed with average-cost accounting and its fees, in the currency each fee was actually charged; a partially closed lot releases only its share of the fees; an unmatched sell is reported, never dropped; slippage is measured against the intent's own reference price; a challenger is promotable only on out-of-sample evidence beating the baseline by a stated margin within a drawdown budget and not look-ahead-contaminated; too few trades reports `insufficient_evidence` |
| Acceptance (promotion) | no path promotes a configuration without a named approver, a reason, a tz-aware timestamp, and the digest of the exact evidence the person saw; recording an approval changes no trading state |
| Evidence | `pytest tests/test_evaluation.py tests/test_promotion.py` → 67 passed |

**How the gate binds the person.** `Evidence.digest` is a SHA-256 over the
key-sorted JSON of the strategy name, parameters and out-of-sample numbers
(decimals as exact strings). `Approval` refuses a blank approver, a blank reason,
a blank digest, or a naive datetime. `Promotion.__post_init__` refuses an approval
whose digest differs from the evidence, so there is no code path that can promote
`best()` before asking. `Promotion.record(journal)` is the only write the
evaluation package can make, and it writes one audit row. `cryptobot challengers`
prints the exact `cryptobot promote` command with the digest baked in;
`tests/test_promotion.py::test_the_printed_command_reproduces_the_digest` runs
that printed command and checks it reproduces the digest, and
`test_an_approval_cannot_be_reused_for_different_evidence` proves a pasted
approval cannot be redirected at different numbers.

---

## Phase 12 — Paper soak and acceptance

**Objective:** run paper trading for at least 30 days and accept or refuse it
against criteria fixed in advance.

**Status:** harness `COMPLETED AND VERIFIED`; **the soak itself is
`BLOCKED BY EXTERNAL PREREQUISITE`** (credentials and elapsed time). No soak has
run and none is claimed.

| | |
| --- | --- |
| Deliverables | `soak.py`, `evaluation/acceptance.py`, `soak` and `acceptance` commands |
| Dependencies | Phases 2–11 |
| Tests | `tests/test_soak.py` (21), `tests/test_evaluation.py` (acceptance subset) |
| Acceptance | the window start is persisted, not inferred; heartbeats are recorded per cycle; a restart continues the same window; a gap is reported as a gap and never counted as continuous operation; daily summaries are derived from rows, never invented; the acceptance report evaluates only an explicit interval and returns `PENDING` (not `PASS`) while evidence is missing |
| Evidence | `pytest tests/test_soak.py` → 21 passed. `cryptobot acceptance --days 30 --min-trades 0 --min-decisions 1 --min-days 1` on a journal with one decision exits `1` with `PENDING` and a `remaining` line (see `tests/test_cli.py::test_acceptance_says_how_long_is_left`); `cryptobot soak` with no declared window reports that, rather than a fabricated one |

**Defect fixed during harness work.** `AllocationLedger.__init__` raises
`ReconciliationError` when the invariant is already broken, which made
`run_acceptance` crash on exactly the case it exists to report. The harness now
catches it, records a failed criterion, and continues evaluating the rest.

---

## Cross-cutting verification

| Rule | How it is enforced | Evidence |
| --- | --- | --- |
| One authoritative execution path | structural tests assert the shadow-AI and evaluation modules cannot import the order path; the trading client is constructed in one module | `test_the_shadow_module_cannot_reach_the_trading_path`, `test_evaluation_cannot_reach_the_order_path` |
| Paper-only by construction | four independent layers; no live switch exists; credentials are never needed by research or evaluation | `tests/test_execution.py` paper-guard cases, `test_a_config_with_a_live_environment_is_refused` |
| Aggregate allocation, not per-order | the ledger owns `committed` and `reserved`; a reservation above the ceiling is refused inside the write lock | `tests/test_data_risk.py`, `tests/test_ledger_stress.py` |
| Money over the ceiling is recorded, not hidden | an over-cost fill books, halts, and stays reversible | `test_a_fill_above_its_reservation_halts_and_is_recorded_not_refused` |
| No invented API capability | every Alpaca fact carries a source and a date in `docs/API_CAPABILITIES.md`; unverified behaviour stays unimplemented | `docs/API_CAPABILITIES.md` |
| No secret in any output | `config-check`, `env-hint` and the logger redact by construction; sentinel tests plant values and assert absence | `test_config_check_never_prints_a_secret`, `test_env_hint_names_the_variables_without_reading_the_values` |
| No real orders in the suite | the simulator is the default; `--submit` is required to reach a broker; SDK tests use a fake transport | every CLI trading test, `tests/test_sdk_integration.py` |
| No stopping at a halting state | `resume` refuses `HALTED`/`KILLED`; `unhalt` validates before releasing | `tests/test_cli.py` (leaving a halt) |

---

## Defects found by the first authenticated run

Four, all fixed with regression tests. Each is a case where a green test suite
was hiding a wrong answer, which is the reason this section exists.

| Defect | What it did | Fix, and the test that now holds it |
| --- | --- | --- |
| `reconcile` and `run-once` defaulted to `--mode simulated` | the routine commands compared the funded paper account against an empty in-memory simulator, reported `position_mismatch` + `ledger_position_mismatch`, and halted a healthy journal — twice, before it was noticed | both default to `paper`, like `paper-run` and `cancel-all`; the report names the broker it asked — `test_reconcile_asks_the_paper_broker_unless_told_otherwise` |
| `position_snapshots.source` was written from a parameter that defaulted to `"broker"`, ignoring the snapshot's own source | every row said the same thing, so a journal could not be attributed to the adapter that wrote it and a paper-vs-simulator mix-up was undetectable | the store writes the snapshot's own source; the reconciler refuses a foreign one — `test_position_snapshots_remember_which_adapter_wrote_them`, `test_reconciling_against_a_different_broker_is_refused` |
| `trade-report` valued execution fees in the charged asset | a BTC-denominated buy fee printed as `0.0000` beside USD P&L while the ledger had expensed 0.05 — two numbers for one fee, and the smaller one looked like "no fee" | every fee is valued in quote and the total says `fees (quote)` — `test_a_base_asset_buy_fee_is_reported_in_the_quote_currency` |
| the engine never called `MarketDataService.instrument()` | sizing used hard-coded min-order and increment constants that the broker may have changed, disclosed only as a warning | fetched once per symbol from `/v2/assets` and cached in the journal, with the documented fallback only when the broker cannot be asked — `test_instrument_metadata_comes_from_the_broker_then_from_the_journal` |

---

## External blockers

| Blocker | Blocks | Effect today | What is still possible |
| --- | --- | --- | --- |
| No paper credentials were available until 2026-10-29 | Phases 2 (live fetch), 5 (authenticated paper), 12 (the soak) | **partly lifted:** credentials were supplied in the shell, real bars were fetched and one real order was placed; they are not stored in the repository and are never printed | a second order, and every soak-day that follows |
| No elapsed soak time | Phase 12 | acceptance reports `PENDING`, never `PASS` | running the harness, and every criterion it evaluates |
| No `alpaca-py` in the system interpreter | Phase 5 (SDK-level) | the SDK module skips itself; nothing else degrades | the 477 tests that need no SDK |
| The paper account holds an open position closed only by a signal | a verified round trip | the ledger shows committed capital and an unrealised mark; no realisation has been booked against the broker yet | placing a sell when the strategy says so, or flattening by hand and reconciling |

---

## Not implemented, by design

| Item | Why it is absent |
| --- | --- |
| Live trading | out of scope permanently; four layers refuse it |
| `stop_limit` orders | `OrderIntent` has no stop price, and inventing one at the adapter boundary would be a silent change of meaning |
| Automated position closing | a "pause" that sold the position would be a catastrophic surprise; there is no capability for it |
| Cash-ledger reconciliation of equity | the ceiling is a configuration constant, not a balance; balances are stored as evidence only |
| Applying a promoted configuration automatically | a promotion is an audit record; changing parameters stays a manual edit |
| A real AI provider | the shadow analyst has no order authority, so a provider would add cost and risk without changing decisions |

---

## How to unblock Phases 5 and 12

Follow [docs/PAPER_TRADING_SETUP.md](docs/PAPER_TRADING_SETUP.md) in order. In
short:

1. Export the paper credentials in the shell that will run the bot, then check
   with `cryptobot env-hint` (names only) — never paste them anywhere.
2. `cryptobot config-check` — `validation: ok` and credentials present.
3. `cryptobot preflight --mode paper` — no failures.
4. `cryptobot data-refresh --lookback 500` — real bars stored.
5. `cryptobot paper-run --submit --iterations 1`, then `cryptobot reconcile`,
   `cryptobot ledger`, `cryptobot trade-report` — inspect before widening.
6. `cryptobot soak start --note "first soak" --interval 60`, then
   `cryptobot paper-run --submit --interval 60` for 30+ days, restarting when
   needed (gaps are recorded, not smoothed).
7. `cryptobot acceptance --days 30` — until that prints `ACCEPTED`, Phase 12 is
   incomplete and this file must keep saying `BLOCKED BY EXTERNAL PREREQUISITE`.

Steps 1-4 have been performed against the real paper account. Step 5 was performed
with `run-once --submit` and one order, not the loop. Steps 6-7 have not been
started.

---

## Document map

| Document | Purpose |
| --- | --- |
| `README.md` | what the project is, how to install and run it |
| `docs/ARCHITECTURE.md` | module boundaries and the invariant each one owns |
| `docs/API_CAPABILITIES.md` | verified Alpaca facts, with sources and dates |
| `docs/IMPLEMENTATION_ROADMAP.md` | phase order, dependencies, remaining work |
| `docs/OPERATIONS.md` | day-to-day runbook: states, failure handling, shutdown |
| `docs/PAPER_TRADING_SETUP.md` | first run: credentials, preflight, soak, acceptance |
| `docs/SOURCE_REFERENCES.md` | which reference repository informed which decision |
| `IMPLEMENTATION_CHECKLIST.md` | this file |
