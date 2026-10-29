# Operations runbook

For running CRYPTOBOT day to day: how to start it, what the states mean, what to
do when something looks wrong, and how to stop it without leaving a mess.

Read `docs/API_CAPABILITIES.md` §7 before trusting anything a paper run reports
about fills and slippage.

---

## 1. Before you start anything

```powershell
cryptobot config-check          # effective config, names of missing variables only
cryptobot preflight --mode paper
```

`preflight` runs ten checks. **Failures block; warnings do not.** A run with
warnings is normal (for example: no credentials configured while you are working
offline). A run with failures must not be started.

| Check | Fails when | What it means |
| --- | --- | --- |
| `paper_environment` | `app.environment` is not `paper` | configuration is wrong; nothing else matters |
| `paper_endpoint` | the trading URL is not on the paper allowlist | refuse to run |
| `database_schema` | the journal cannot be opened or migrated | the journal is the state; no journal, no trading |
| `allocation_invariant` | `committed + reserved > ceiling` | the ledger cannot explain itself — resolve manually |
| `system_state` | the kill switch is engaged | `cryptobot resume` after understanding why |
| `clock` | the system clock looks wrong | timestamps drive everything; fix the clock first |
| `unresolved_orders` | an order is `UNCERTAIN`/`ACCEPTED`/`LOST` | resolve with `cryptobot reconcile` before trading again |
| `credentials` | a named environment variable is absent | a warning offline, a failure for a paper run |
| `broker_connection` | the broker is unreachable or rejects the key | network or credential problem |
| `data_freshness` | the latest cached bar is older than `market.max_bar_age_seconds` | refresh data |

If a check fails, the command prints the failing check's **detail**, not just
"FAILED". Read it; it usually names the fix.

---

## 2. Operational states

```powershell
cryptobot pause --reason "news window"     # stop new entries, keep positions
cryptobot resume --reason "window closed"  # back to active
cryptobot kill --reason "flash crash"      # kill switch: no entries, no cancels
```

| State | New entries | Cancel orders | Keep running | Needs a human |
| --- | --- | --- | --- | --- |
| `ACTIVE` | yes | yes | yes | no |
| `PAUSE_ENTRIES` | no | yes | yes | no |
| `HALTED` | no | yes | yes | yes |
| `KILLED` | no | no | yes | yes |

Two things this table deliberately does **not** include: closing positions, and
stopping the process. Pausing entries is not liquidating; `Ctrl-C` is not a
state. There is no capability to close a position automatically, because a
"pause" that sold your position would be a catastrophic surprise.

`pause` requires `--reason`, and the reason is persisted with the state change.
Future-you will want to know why past-you did it.

### Automatic transitions

| Trigger | State | Reversible by |
| --- | --- | --- |
| critical reconciliation drift | `HALTED` (entries only) | operator, after `reconcile` is clean |
| `max_consecutive_errors` reached in the loop | loop stops, state is reported | operator |
| daily loss limit / max drawdown breached (risk check) | entries rejected, state unchanged | next day, or operator |
| kill switch | `KILLED` | `cryptobot resume`, deliberately |

The risk manager rejecting an entry does **not** change the system state. It
records the verdict and moves on. Confusing "this order was refused" with "the
bot is broken" is how a working system gets halted for no reason.

---

## 3. Normal operation

```powershell
cryptobot paper-run --submit --interval 60 --reconcile-every 5
```

- One decision per configured symbol per cycle.
- Reconciles every 5th cycle.
- Submits only when `--submit` is present. Without it, decisions and risk
  verdicts are recorded and nothing is sent anywhere.
- On `Ctrl-C` the loop finishes the current step and exits; it does not abandon
  an in-flight submission. A submission that is genuinely ambiguous is recorded
  as `UNCERTAIN`, which the next start resolves by lookup.

Watch it from another terminal:

```powershell
cryptobot dashboard            # loopback only, 127.0.0.1:8787
cryptobot status               # allocation, positions, orders, decisions
cryptobot trade-report         # realised trades and measured slippage
```

The dashboard is a **viewer**. It serves `/` and `/api/status`; `POST` returns
501. There is no button, link, or endpoint that changes state, so leaving it
open cannot cause an action.

---

## 4. When something looks wrong

Work down this list in order. Each step is read-only.

```powershell
cryptobot status
cryptobot ledger
cryptobot orders --limit 50
cryptobot decisions --limit 20
cryptobot reconcile --mode paper
```

| Symptom | First look at | Usual cause |
| --- | --- | --- |
| No trades at all | `cryptobot decisions` | strategy says hold, or the state is not `ACTIVE`, or data is stale |
| Every entry rejected | the verdict reasons (`cryptobot orders`) | risk check: exposure, daily loss, drawdown, spread, order count |
| Capital "missing" | `cryptobot ledger` | a reservation that was never released — check for `UNCERTAIN` orders |
| Duplicate-looking orders | `cryptobot orders` | should be impossible: `client_order_id` is deterministic and `broker_order_id` is unique |
| Order stuck in `ACCEPTED` | `cryptobot reconcile` | broker has not filled it; past the grace period it becomes `LOST` |
| Order is `LOST` | the broker's own UI | the local record could not be confirmed; resolve by hand, then clear it |
| Preflight fails on `allocation_invariant` | `cryptobot ledger` | journal and ledger disagree — do not trade until reconciled |

### If you find an `UNCERTAIN` or `LOST` order

Do **not** re-run with `--submit` hoping it clears. Check the broker first:

1. `cryptobot reconcile --mode paper`
2. If it remains unresolved, look the order up by client order id in the Alpaca
   dashboard.
3. The bot will not resubmit it. That is by design: resubmission after ambiguity
   is how one intended order becomes two real ones.

---

## 5. Stopping

| Intent | Do this |
| --- | --- |
| Stop taking new entries, keep everything else | `cryptobot pause --reason "..."` |
| Stop the loop but keep positions and open orders | `Ctrl-C` in the `paper-run` terminal |
| Cancel open orders as well | `cryptobot cancel-all --mode paper --yes` |
| Stop everything and re-check before restart | `cryptobot kill --reason "..."` |

`cancel-all` requires `--yes` and lists what it is about to cancel. Without
`--yes` it prints the orders and refuses, so an accidental keypress is a
no-op.

**Shutdown order that never loses state:** `pause` → `Ctrl-C` → `reconcile` →
`cancel-all --yes` (only if you actually want the orders gone). The journal is
written before each step, so any order you did not cancel is still there when you
restart.

---

## 6. Restarting after a crash

```powershell
cryptobot preflight --mode paper     # unresolved_orders will tell you if anything is in doubt
cryptobot reconcile --mode paper     # resolve UNCERTAIN orders by client id
cryptobot status                     # confirm allocation and positions
cryptobot paper-run --submit
```

The journal is the source of truth, not the process. Nothing is lost by killing
the process; what can be *in doubt* is an order whose submission was in flight,
and that is exactly what `UNCERTAIN` plus reconciliation exists for.

---

## 7. Daily and weekly checks

**Daily**

```powershell
cryptobot preflight --mode paper
cryptobot status
cryptobot reconcile --mode paper
cryptobot trade-report
```

Look for: unresolved orders, critical reconciliation events, and a
`deployment_pct` that has crept toward the ceiling.

**Weekly**

```powershell
cryptobot acceptance --days 30      # progress toward the soak, and any failed criterion
cryptobot experiments --limit 10    # what research was actually run
cryptobot decisions --limit 50      # did it behave the way you think it did?
```

`acceptance` reports duration as **PENDING** until the soak is long enough. A
`FAIL` is different from a `PENDING` and needs action.

---

## 8. Things that are not emergencies

| Situation | Why it is fine |
| --- | --- |
| `credentials` warning while running offline | the simulated path needs no credentials |
| `data_freshness` warning on a fresh journal | no bars yet; run `data-refresh` or `seed-demo` |
| Risk verdict rejecting everything for a day | that is the risk manager working |
| `profit factor n/a (no losses yet)` | honest: no losing trade means no ratio |
| Challenger report says `insufficient_evidence` | honest: too few out-of-sample trades to judge |

## 9. Things that are emergencies

- `allocation_invariant` failing.
- Any `LOST` order.
- Two orders for what should have been one.
- A reconciliation `critical` event with no explanation.
- Realising you configured a live key. Stop the process and rotate the key: the
  application cannot use a live endpoint, but the credential itself is still
  exposed.

---

## 10. Where the state lives

| State | Location |
| --- | --- |
| Everything the bot knows | the SQLite journal (`journal.path`, default `var/cryptobot.db`) |
| Operational state (`ACTIVE`/…) | `system_state` table, single row |
| In-flight orders | `orders` table with `needs_broker_confirmation` statuses |
| Capital | `ledger_events` + `ledger_reservations`, replayed by `rebuild()` |
| Configuration | `config/trading.yaml` |
| Credentials | environment only, never the journal, never the logs |

Back up the journal by copying the file while the process is stopped, or use
`sqlite3 cryptobot.db ".backup backup.db"` while it runs. Deleting the journal
deletes the ledger, and the ledger is the reason the ceiling holds.
