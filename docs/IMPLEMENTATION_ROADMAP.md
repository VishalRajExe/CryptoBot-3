# Implementation roadmap

Phase order, why it is that order, and what remains. Status here mirrors
[`IMPLEMENTATION_CHECKLIST.md`](../IMPLEMENTATION_CHECKLIST.md), which carries
the per-phase evidence; this file is the map, not the ledger.

---

## Sequencing principle

Each phase produces something the next phase can *depend on without trusting it*:

- contracts before any behaviour, so money and status vocabulary cannot drift;
- the journal before execution, so "persist intent first" is a schema constraint
  rather than a promise;
- the ledger before the broker, so the ceiling exists before anything could
  breach it;
- the simulator before the paper adapter, so execution logic is proven where a
  mistake is free;
- evaluation last, because it measures the other phases and would otherwise be
  written to agree with them.

```
Phase 0  scaffolding
   │
Phase 1  core contracts · config · states · journal schema · CLI
   ├──────────────┬────────────────┐
Phase 2  data   Phase 3  strategy   │
   │              │                 │
   └──────┬───────┘                 │
          │                         │
     Phase 4  ledger + risk ◄────────┘
          │
     Phase 5  execution (simulator → paper adapter)
          │
     Phase 6  persistence · reconciliation · recovery
          │
     ┌────┴─────┬──────────┐
Phase 7      Phase 8   Phase 9      Phase 10
research     ops       shadow AI   monitoring
     └────┬─────┴──────────┘
          │
     Phase 11  evaluation · challengers
          │
     Phase 12  paper soak + acceptance   ◄── blocked: credentials + 30 days
```

Phases 7–10 are independent of one another and were built after the execution
path was stable, so none of them could tempt a shortcut in it.

---

## Phase summary

| Phase | Delivers | Status |
| --- | --- | --- |
| 0 | package layout, pyproject, config, env template | Completed and verified |
| 1 | contracts, errors, states, timeframes, logging, transport, journal schema, CLI skeleton | Completed and verified |
| 2 | Alpaca crypto bars, validation, gap detection, cache, instrument specs | Completed and verified (offline) |
| 3 | indicators, strategy interface, SMA cross, Donchian, look-ahead proof | Completed and verified |
| 4 | allocation ledger, position sizing, deterministic risk manager | Completed and verified |
| 5 | execution service, simulator, paper adapter, order lifecycle | Completed and verified (mocked) |
| 6 | journal readers/writers, reconciler, restart recovery | Completed and verified |
| 7 | datasets, stdlib backtester, suites, walk-forward, sensitivity | Completed and verified |
| 8 | preflight, retries/backoff, state machine commands, safe shutdown | Completed and verified |
| 9 | shadow analyst with no order authority | Completed and verified |
| 10 | reports, read-only dashboard, notifier | Completed and verified |
| 11 | trade evaluation, slippage measurement, challengers | Completed and verified |
| 12 | acceptance harness | Completed and verified |
| 12 | **the 30-day paper soak itself** | **Blocked** |

---

## What remains

### 1. Unblock the soak (the only thing standing between the project and Phase 12)

| Step | Command | Pass condition |
| --- | --- | --- |
| Set paper credentials | export `ALPACA_API_KEY`, `ALPACA_API_SECRET_KEY` | `cryptobot config-check` says credentials present |
| Install the SDK | `pip install -e ".[alpaca]"` | import succeeds |
| Preflight | `cryptobot preflight --mode paper` | no failures |
| Fetch real bars | `cryptobot data-refresh --lookback 500` | bars stored, freshness check passes |
| Run the soak | `cryptobot paper-run --submit` | runs for ≥30 days with ≥30 decisions |
| Accept | `cryptobot acceptance --days 30` | `ACCEPTED` |

Everything before "Run the soak" is a single afternoon. The soak is 30 days of
wall-clock time and cannot be shortened without invalidating it.

### 2. Work that is safe to do while the soak runs

These do not touch the running process's behaviour and can be developed against
the simulator:

| Item | Why it matters | Risk if skipped |
| --- | --- | --- |
| Token bucket for the trading API | the current design relies on cadence, not on an explicit budget | multi-symbol use could hit 429s |
| Multi-symbol risk tuning | limits are calibrated for one symbol at 25% | a basket would breach `max_total_exposure_pct` unpredictably |
| `stop_limit` support | requires a `stop_price` field on `OrderIntent` and its own tests | none today; it is simply unsupported |
| Prometheus/structured metrics export | operators currently read text and SQLite | none for a single-operator setup |

Removed from this table on 2026-10-29: fee-currency accounting. It was a real
defect rather than a limitation, and it is fixed — a buy's fee is recorded in the
base asset, the credited quantity is netted, and the broker's posted `CFEE`/`FEE`
activity corrects the estimate. See `docs/API_CAPABILITIES.md` §4 and
`tests/test_fees.py`.

Each of these needs its own checklist entry with evidence before it is believed.
None of them should be merged into the running soak's code path mid-flight.

### 3. Explicitly out of scope, permanently

- Live trading, in any form.
- Short selling and margin (Alpaca does not offer them for crypto).
- Bracket/OCO orders.
- WebSocket streaming market data.
- Giving the shadow AI any authority over orders.

---

## Definition of done for the project

The project is complete when all of the following are true:

1. `pytest` is green. *(true today: 510 passed in `.venv`, 477 passed + 1 skipped
   without `alpaca-py`)*
2. `cryptobot preflight --mode paper` reports no failures against a real paper
   account. *(true today: 10 checks, 0 failures)*
3. One order has been placed, filled, reconciled and reported end to end against
   the real paper API. *(true today, once: 0.000248437 BTC, one fee, clean
   reconciliation; a second order would be better evidence than one)*
4. A paper soak of at least 30 days has run with no critical reconciliation
   drift and no unresolved orders left behind. *(not started: it needs elapsed
   time, and clock time cannot be compressed honestly)*
5. `cryptobot acceptance --days 30` prints `ACCEPTED`, and the report's criteria
   are all evidenced by the journal. *(blocked on 4)*
6. `IMPLEMENTATION_CHECKLIST.md` records those results. *(done for 1–3,
   pending 4–5)*

Until then, the honest status is: **the platform is built and tested, and has
placed one real paper order. It has not soaked for 30 days and has never traded
live.**

---

## Re-verification checklist for future changes

Before claiming any change is complete:

```powershell
python -m pytest                                              # expect 510 passed
cryptobot config-check                                        # no secrets, paper only
cryptobot env-hint                                            # variable names, never values
cryptobot preflight --mode simulated                          # failures? warnings are fine
cryptobot run-once --mode simulated                           # decisions recorded, no orders
```

Then update `IMPLEMENTATION_CHECKLIST.md` with the actual output. A phase is not
complete because a file exists, and a test is not passing because it was written.
