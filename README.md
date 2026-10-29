# CRYPTOBOT

A paper-only, risk-first cryptocurrency trading platform in Python, targeting
Alpaca Crypto Markets through Alpaca's **paper** endpoint. Capital deployment is
bounded by an application-enforced allocation ceiling (default **$100**),
independent of whatever balance the paper account happens to display.

> **Live trading is out of scope.** There is no live switch, no live endpoint,
> and no fallback that can produce one. The plan does not add it later either.

## What it actually enforces

| Concern | How |
| --- | --- |
| Paper only | Four independent layers: config allowlist, re-check at adapter construction, `paper=True` hardcoded, and inspection of the constructed client on every use. |
| $100 aggregate ceiling | A journal-backed ledger tracks `committed` + `reserved`; the constraint is on the whole book, not on each order. Realised profit never raises the ceiling. |
| Small, complete risk verdicts | Fifteen named checks all run; the full list of reasons is persisted with every decision. |
| No duplicate orders | Deterministic `client_order_id`; an ambiguous submission becomes `UNCERTAIN` and is resolved by lookup, never resubmitted. |
| Restart safety | Intent is persisted before submission; recovery works from the journal, and an order unresolved past its grace period becomes `LOST`. |
| Honest research | Costs are explicit, all attempted parameter combinations are recorded, and look-ahead is proven absent by prefix invariance. |
| AI with no authority | The shadow analyst is recorded and scored; it has no import path to execution, sizing, risk, or the engine. |
| Read-only monitoring | The dashboard binds loopback and exposes only `/` and `/api/status`. `POST` returns 501. |

## Status

| | |
| --- | --- |
| Test suite | **373 passed** — `python -m pytest tests -p no:cacheprovider --tb=short -rN` |
| Runs without network | yes |
| Runs without credentials | yes |
| Runtime dependencies | standard library + `PyYAML` |
| Paper soak (≥30 days) | **not started** — blocked on credentials and elapsed time |

Phase-by-phase detail, including the evidence for each claim and the defects
found while verifying it, is in
[IMPLEMENTATION_CHECKLIST.md](IMPLEMENTATION_CHECKLIST.md). Nothing there is
marked complete on the strength of a file existing.

## Requirements

- Python 3.11+
- An Alpaca **paper** account (only for fetching real bars or submitting paper
  orders — everything else works offline)

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -e .            # core: no network, no broker SDK
pip install -e ".[dev]"     # + pytest
pip install -e ".[alpaca]"  # + alpaca-py, required only to submit paper orders
```

## Configure

Configuration lives in [`config/trading.yaml`](config/trading.yaml). It has no
live-trading field: `alpaca.trading_base_url` is validated against a paper-only
allowlist at load time, and `app.environment` accepts only `paper`.

Credentials are read from the environment, by name, and are never written to the
journal or printed:

```powershell
$env:ALPACA_API_KEY = "..."          # paper key id
$env:ALPACA_API_SECRET_KEY = "..."   # paper secret
```

[`.env.example`](.env.example) documents the variable names. The application
does not load a `.env` file itself (no `dotenv` dependency); export the values in
your shell. Optional: `CRYPTOBOT_DB` overrides the journal path, and
`CRYPTOBOT_TELEGRAM_BOT_TOKEN` / `CRYPTOBOT_TELEGRAM_CHAT_ID` enable
notifications.

Check that the configuration is what you think it is:

```powershell
cryptobot config-check
```

It prints the effective public configuration and the *names* of any missing
variables. It never prints a secret value.

## The three stages

### 1. Historical backtest

```powershell
# explicit costs matter more than the strategy
cryptobot backtest --bars 800 --fee-bps 25 --slippage-bps 25

# parameter suite with a chronological out-of-sample split
cryptobot research --bars 800 --grid fast_period=6,12,18 --grid slow_period=24,48,72

# re-select parameters on each fold instead of once
cryptobot walk-forward --bars 1200 --train-bars 400 --test-bars 150
```

Bars come from `--data FILE` (JSON/CSV), `--from-journal` (the cache), or a
seeded synthetic random walk. **Synthetic output says nothing about real
markets**, and the command says so every time it is used.

### 2. Local dry run (no broker, no credentials)

```powershell
cryptobot seed-demo --bars 600      # SYNTHETIC bars, for plumbing only
cryptobot run-once                  # one decision cycle; no orders
cryptobot run-once --submit         # route approved intents to the simulator
cryptobot status
cryptobot trade-report
```

`run-once` without `--submit` is a pure evaluation: it records the decision and
the risk verdict, and sends nothing anywhere.

### 3. Alpaca paper trading

```powershell
cryptobot preflight --mode paper    # must show no failures
cryptobot data-refresh --lookback 500
cryptobot paper-run --submit --interval 60 --reconcile-every 5
```

`paper-run` without `--submit` still evaluates and reconciles — it just never
sends an order. Watch it with `cryptobot dashboard` (loopback only) or
`cryptobot status`.

## Command reference

Every command accepts `--config PATH` and `-v`.

| Command | Purpose |
| --- | --- |
| `config-check` | validate configuration, print public values only |
| `status` | allocation, positions, orders, recent decisions |
| `ledger` | allocation ledger events and live reservations |
| `orders` | recorded orders |
| `decisions` | recorded strategy decisions |
| `preflight` | ten startup checks with a reason for each failure |
| `experiments` | recorded research experiments |
| `data-refresh` | fetch and cache bars from Alpaca |
| `data-import` / `data-export` | move bars to and from JSON/CSV |
| `seed-demo` | store **synthetic** bars (offline demo only) |
| `run-once` | evaluate one cycle; orders only with `--submit` |
| `paper-run` | continuous paper loop; orders only with `--submit` |
| `reconcile` | compare local state with the broker |
| `pause` | stop new entries, keep positions (requires `--reason`) |
| `resume` | return to active trading |
| `kill` | engage the kill switch |
| `cancel-all` | cancel open orders (requires `--yes`) |
| `dashboard` | read-only local dashboard |
| `backtest` | single backtest with explicit costs |
| `research` | parameter suite with out-of-sample split |
| `walk-forward` | chronological walk-forward folds |
| `sensitivity` | one-factor-at-a-time sweep |
| `challengers` | judge challenger parameters out-of-sample |
| `trade-report` | evaluate recorded fills and measured slippage |
| `acceptance` | soak acceptance criteria against the journal |
| `ai-review` | shadow-mode AI assessment; never trades |

Exit codes: `0` success, `1` a check failed, `2` the request was refused (for
example a live endpoint, a symbol outside the allowlist, or a missing `--yes`),
`130` interrupted.

## Project layout

```
cryptobot-app/
├── config/trading.yaml          # no live-trading field exists
├── docs/
│   ├── ARCHITECTURE.md
│   ├── API_CAPABILITIES.md      # verified Alpaca facts, each with a source
│   ├── IMPLEMENTATION_ROADMAP.md
│   ├── OPERATIONS.md
│   └── SOURCE_REFERENCES.md
├── src/cryptobot/
│   ├── core/                    # contracts, errors, states, time, logging, transport
│   ├── config/                  # settings + paper-only allowlist
│   ├── data/                    # Alpaca crypto bars, validation, instruments
│   ├── features/                # indicators
│   ├── strategy/                # batch strategy interface + implementations
│   ├── sizing/                  # allocation ledger, position sizing
│   ├── risk/                    # deterministic risk manager
│   ├── execution/               # the ONLY module that may construct a trading client
│   ├── reconciliation/          # broker vs journal
│   ├── journal/                 # SQLite schema and store
│   ├── research/                # backtest, datasets, experiments
│   ├── evaluation/              # trades, challengers, acceptance (read-only)
│   ├── ai/                      # shadow analyst, no order authority
│   ├── monitoring/              # reports, dashboard, notifier
│   ├── preflight.py
│   ├── runtime.py               # Runtime + TradingLoop
│   ├── engine.py                # decision -> risk -> execution cycle
│   └── cli.py
├── tests/                       # 373 tests, all offline
└── IMPLEMENTATION_CHECKLIST.md
```

## Testing

```powershell
pytest                                   # 373 tests, no network, no credentials
pytest tests/test_execution.py -q        # one module
pytest -m "network or broker"            # opt-in; not run by default
```

The `network` and `broker` markers exist so that anything which would contact
Alpaca is opt-in. The default suite never places an order, never fetches a bar
over the network, and never reads a real credential.

## Known limitations

- **No paper soak has run.** The acceptance harness is built and tested, but
  `cryptobot acceptance --days 30` will report `NOT ACCEPTED` until a real soak
  exists. The project does not claim otherwise.
- **`alpaca-py` is optional.** Without it, real submission raises
  `MissingDependency` with an install hint. Nothing else degrades.
- **`stop_limit` orders are not implemented.** `OrderIntent` does not model a
  stop price, and inventing one at the adapter boundary would be a silent
  change of meaning.
- **One symbol is configured by default** (`BTC/USD`, `1h`). The code is
  symbol-agnostic; the risk limits are not tuned for a basket.
- **`vectorbt` is optional.** The stdlib backtester is the reproducible path;
  `vectorbt` is available for exploration, not as a required dependency.
- **Profit is not claimed.** Synthetic results are labelled as synthetic, and
  challengers cannot be promoted without out-of-sample trades.

## Documentation

| Document | Read it for |
| --- | --- |
| [IMPLEMENTATION_CHECKLIST.md](IMPLEMENTATION_CHECKLIST.md) | what is built, what the evidence is, what is blocked |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | module boundaries and the invariant each one owns |
| [docs/API_CAPABILITIES.md](docs/API_CAPABILITIES.md) | verified Alpaca behaviour, and what is deliberately not implemented |
| [docs/IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md) | phase order and remaining work |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | runbook: states, failure handling, shutdown |
| [docs/SOURCE_REFERENCES.md](docs/SOURCE_REFERENCES.md) | which reference repo informed what, and what was rejected |

## Licence

Proprietary, personal research project. Not investment advice. Paper trading
only: results obtained here are not evidence that any strategy is profitable.
