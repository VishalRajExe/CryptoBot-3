# Safe paper-trading setup

A sequential procedure for getting from a fresh clone to a running paper-trading
loop **without** ever putting a credential into a file that is committed, a log,
a report, or a chat window.

Follow the steps in order. Each one states what success looks like and what to do
when it fails. If a step fails, fix it before continuing - the later steps assume
the earlier ones passed, and skipping ahead is how a "paper" run ends up
silently doing nothing.

What this procedure **cannot** give you: proof that orders execute correctly on
Alpaca. Only an authenticated paper run establishes that. See
`docs/API_CAPABILITIES.md` for what has been verified against the official APIs
and what has only been verified against mocks.

---

## 0. Prerequisites

Python 3.11 or newer. From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The `dev` extra is optional for running the bot but required for the test suite.
Add the `alpaca` extra if you want the Alpaca SDK present for the SDK
integration tests; it is **not** required to trade - the client is written
against the documented HTTP API, and those tests skip themselves when the
package is absent.

Check the interpreter before anything else:

```powershell
.\.venv\Scripts\python.exe -m cryptobot.cli --version
```

---

## 1. Decide where the journal lives

The journal (SQLite) *is* the state: allocation ledger, orders, decisions,
reconciliations, soak windows. Losing it loses the evidence, so decide now where
it should be.

* Default: `var/cryptobot.db`, relative to the project root, from
  `journal.path` in `config/trading.yaml`.
* Override for one shell: set `CRYPTOBOT_DB=/absolute/path/cryptobot.db` (a
  relative value resolves against the project root).

Both `var/` and `*.db` are gitignored. Use a separate database for a soak run if
you want to keep an experiment's history out of your main one.

---

## 2. Create a paper account and get paper keys

Generate keys at the Alpaca **paper** dashboard:
<https://app.alpaca.markets/paper/dashboard/overview>

Two rules that are not negotiable:

* **Paper keys only.** Live keys are neither required nor supported. `config-check`
  refuses any `app.environment` other than `paper`, and `preflight` refuses a
  trading base URL that is not on the paper allowlist.
* **Do not paste the values into a chat, an issue, a report, or a commit.** This
  project never needs to see them; you only ever need to tell the bot the
  *names* of the variables that hold them.

---

## 3. Put the keys in the environment (names only, from here on)

`cryptobot` has no dotenv dependency and never reads `.env`. It reads two
environment variable *names* that are configured in `config/trading.yaml`
(`alpaca.api_key_env`, `alpaca.api_secret_env`), and their defaults are
`ALPACA_API_KEY` and `ALPACA_API_SECRET_KEY`.

Ask the CLI for the exact syntax for your shell:

```powershell
cryptobot env-hint --shell powershell
```

It prints each variable name with `set` or `NOT SET`, then the syntax for the
current session and the persistent form. It inspects only *presence*; it never
reads, echoes, logs, or stores a value. `--shell bash` prints the `export` form.

Set them in the **same shell that will run the bot**, for example:

```powershell
$env:ALPACA_API_KEY = "..."
$env:ALPACA_API_SECRET_KEY = "..."
```

`env-hint` also tells you when to set them persistently
(`[Environment]::SetEnvironmentVariable(..., "User")`): after that, start a new
terminal so the bot inherits them.

Re-run `cryptobot env-hint` and confirm both required names say `set`. If they
still say `NOT SET`, the bot will not see them - a variable set in another
terminal, or in a `User` scope that the current process predates, is invisible.

> `.env` exists only as a local note-to-self file. It is gitignored
> (`.env`, `.env.*`, `!.env.example`), and `.env.example` holds placeholders
> only. Nothing loads it, so writing your keys there and expecting them to work
> is a silent no-op - use the shell.

---

## 4. Confirm the configuration is paper and complete

```powershell
cryptobot config-check
```

Success looks like `validation: ok` plus
`credentials: present in environment (values not printed)`.

If it instead prints `credentials: NOT SET -> ALPACA_API_KEY, ...` with
`(not required for simulated mode, tests, or backtests)`, the configuration is
valid but the environment is missing the keys for step 6.

If it exits `2` with `paper-only by construction`, the configuration names a
non-paper environment. Fix `app.environment` in `config/trading.yaml`; do not
work around the refusal.

`config-check` prints the whole effective configuration, including the database
path and the allowlist. Read it - this is the moment to notice that the symbol
list, the `$100` ceiling, or the strategy parameters are not what you intended.

---

## 5. Run preflight and read the failures

```powershell
cryptobot preflight --mode paper
```

Ten checks run. **Failures block; warnings do not.** Exit code `0` means pass,
`1` means one or more failures. A failing check prints its *detail*, not just
`FAILED` - read it, it usually names the fix.

| Symptom | Meaning | Action |
| --- | --- | --- |
| `credentials` fails | a named variable is absent | repeat step 3 |
| `broker_connection` fails | network or rejected key | check the paper endpoint and that the key is a **paper** key |
| `data_freshness` fails | latest cached bar is older than `market.max_bar_age_seconds` | step 5 |
| `database_schema` fails | the journal cannot be opened or migrated | check the path and file permissions |
| `unresolved_orders` fails | an order is `UNCERTAIN`/`ACCEPTED`/`LOST` | `cryptobot reconcile`, then `cryptobot orders` |
| `allocation_invariant` fails | `committed + reserved > ceiling` | `cryptobot ledger`, then follow "Leaving HALTED" below |
| `system_state` fails | the kill switch is engaged | `cryptobot status`, then "Leaving HALTED" below |

Running preflight without credentials is expected to fail here; that failure is
the design working, not a bug to route around.

One consequence to know before you start the loop: `paper-run` runs preflight
first and, if it fails, **halts the journal** instead of running without
credentials. So a start you abort this way leaves `system_state: HALTED`, and the
next preflight reports `system_state` as a fourth failure. That is deliberate -
it is easier to explain than a bot that idled for a week pretending to trade.
Clear it once the cause is fixed:

```powershell
cryptobot unhalt --reason "credentials installed; ledger and orders clean"
cryptobot resume --reason "starting the first paper cycle"
```

---

## 6. Get data, honestly labelled

Two mutually exclusive options:

```powershell
cryptobot data-refresh            # real bars from Alpaca (needs the keys)
cryptobot seed-demo --bars 400    # SYNTHETIC bars, offline, deterministic
```

`seed-demo` prints `SYNTHETIC DATA` and says it proves nothing about real
markets. Use it to exercise plumbing, never to draw a conclusion about a
strategy. Anything derived from synthetic bars stays synthetic in every report.

---

## 7. Dry cycles first - no orders

```powershell
cryptobot run-once
```

Success: `preflight passed`, `submit=no`, one line per symbol, exit `0`, and
`cryptobot orders` still empty. This proves the whole pipeline up to - but not
including - order submission.

Then run the loop for a few cycles **without** `--submit`:

```powershell
cryptobot paper-run --iterations 3 --interval 60
```

Orders are sent only when `--submit` is present, so this is a safe rehearsal of
the loop itself: heartbeats, decision cadence, reconciliation.

---

## 8. Arm submission, one cycle at a time

Only after step 7 is clean:

```powershell
cryptobot paper-run --submit --iterations 1
```

Then inspect, before widening the blast radius:

```powershell
cryptobot status        # allocation, positions, open orders, last decisions
cryptobot ledger        # committed + reserved against the $100 ceiling
cryptobot orders        # local record of every order, most recent first
cryptobot reconcile     # compare local state with the broker
```

Check three things specifically:

1. `committed + reserved` is at or under the ceiling, and the sum matches the
   positions you can see at Alpaca.
2. Every order in `cryptobot orders` has a **resolved** status. An `UNCERTAIN`
   order means the submission left the process before the response arrived; run
   `cryptobot reconcile` until it resolves.
3. Fees appear where you expect them. Alpaca posts crypto fees as separate
   `CFEE`/`FEE` activities end-of-day and the Orders API exposes no fee, so
   `cryptobot trade-report` is where the recorded cost is checked.

If any of the three is wrong, stop here and work "When something is wrong"
below. Do not raise `--iterations` until they are right.

---

## 9. Declare the soak window and leave it running

The soak window is how the journal later proves *how long* the system actually
ran, which is not the same as how long you intended it to run:

```powershell
cryptobot soak start --note "first paper soak" --interval 60
cryptobot paper-run --submit --interval 60
```

While the loop runs it writes heartbeats into the open window. Restarting the
process is fine and expected - coverage is computed from the recorded evidence,
so a gap in the middle is visible as a gap and never counted as continuous
operation. Close the window when you stop for good:

```powershell
cryptobot soak close --reason "soak complete"
```

Inspect at any time:

```powershell
cryptobot soak status
cryptobot soak daily --days 14
```

Do not fabricate coverage. A short window reported honestly is worth more than a
long one the journal cannot support.

---

## 10. Ask for the acceptance verdict

```powershell
cryptobot acceptance --days 30 --min-decisions 1 --min-trades 1
```

Each criterion is `PASS`, `PENDING` (not yet enough evidence - the report says
how much is left), or `FAIL`. `PENDING` is not success. The overall verdict is
only `PASS` when every criterion passes, and every criterion is computed from
rows the journal actually contains.

If the interval you are claiming was interrupted, do not shorten `--days` to
make it pass: the honest answer is that the soak is incomplete.

---

## When something is wrong

### The system is `HALTED`

`HALTED` means the system no longer trusts itself - for example a fill cost more
than its reservation, so the ledger is over the ceiling. New entries are blocked
automatically. The exit is deliberately guarded:

```powershell
cryptobot status        # why it halted, and since when
cryptobot ledger        # committed + reserved + ceiling
cryptobot reconcile     # resolve every UNCERTAIN/LOST order first
cryptobot unhalt --reason "what I checked and what I changed"
```

`unhalt` **refuses** when the allocation invariant is still broken, or while any
order still needs broker confirmation, and it names the reason. When it
succeeds it moves to `PAUSE_ENTRIES`, not `ACTIVE`: clearing a halt and resuming
trading are two decisions.

```powershell
cryptobot resume --reason "ledger reconciled; resuming"   # only after a human looked
```

### The kill switch is engaged

```powershell
cryptobot kill --reason "flash crash"    # entries blocked; cancels still allowed
cryptobot cancel-all --mode paper --yes  # cancel open orders (requires --yes)
```

Leaving `KILLED` goes through the same `unhalt` path, with the same guards.

### Orders are stuck

```powershell
cryptobot reconcile                 # ask the broker, once
cryptobot orders                    # scan the status column; most recent first
cryptobot cancel-all --mode paper --yes
```

Never delete rows to "fix" a status. The journal is the audit trail; a
correction is a new reconciliation event, not an erased one.

---

## Secrets checklist

* Keys live in the environment of the shell that runs the bot, nowhere else.
* `env-hint` prints names and `set`/`NOT SET` only; `config-check` prints
  `values not printed`; both are tested to never leak a value.
* `.env` is gitignored; `.env.example` contains placeholders.
* Log output redacts the credential variables and bearer headers.
* If a key is ever exposed: rotate it in the Alpaca dashboard. Editing it out of
  a file is not a fix, because it already left the machine.
