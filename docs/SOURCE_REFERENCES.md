# Source references

The workspace this project lives in contains seventeen other trading projects.
This file records **which one informed which decision**, and — more importantly —
what was *not* taken from them.

**No source code was copied from any reference repository.** Every pattern below
is an idea that was re-implemented against this project's own contracts. That
matters for two reasons: nothing here needs to be reconciled with a foreign
licence, and nothing here inherits a foreign dependency tree.

> If you later copy *any* code from these repositories, check its licence first.
> Several are copyleft (Freqtrade and backtesting.py in particular), and copying
> from them would change the licensing of this project.

---

## 1. Reference → decision map

| Reference | What it informed | What was deliberately not taken |
| --- | --- | --- |
| **Freqtrade** | the *shape* of strategy lifecycle and reconciliation: a strategy produces signals from closed candles; local state is reconciled against the exchange rather than trusted; a "dry run" mode is first-class. Also the idea of recording every decision with its reason. | Its engine, its data model, its exchange abstraction, its dependency tree. `docs/ARCHITECTURE.md` records why it is not the runtime: an engine that owns capital accounting would have to be reconciled with the $100 ledger, and the ledger is the thing this project exists to enforce. |
| **VectorBT** | research ergonomics: cost-aware vectorised exploration, and the discipline of stating fills and costs explicitly instead of letting them default. | Vectorised execution as the *authoritative* path. `research/backtest.py` is an explicit event loop so that the live loop and the backtest share `Strategy.generate` and differ only in the adapter. `vectorbt` remains an optional extra, never a runtime dependency. |
| **Hummingbot** | order-lifecycle vocabulary: the distinction between submitted / partially filled / cancelled / lost, and the rule that an order in an unknown state must be resolved, not retried. | Its connectors, its market-making model, its bot framework. Only the lifecycle states influenced `core/contracts.py::OrderStatus`. |
| **Vibe-Trading** | the *existence* of an Alpaca crypto paper flow worth checking: which endpoints exist, that paper crypto is a real thing to verify against rather than assume. | Its Alpaca connector. The adapter in `execution/alpaca_paper.py` was written against the verified API facts in `docs/API_CAPABILITIES.md`, not against another project's wrapper. Importing a connector unchanged is how unverified assumptions get inherited. |
| **day-trader-skill** | the framing of deterministic risk validation: named checks, each with a pass/fail and a detail, all evaluated so a rejection reports every reason. | Its skill/prompt packaging. `risk/manager.py` is fifteen named checks in a fixed order returning a `RiskVerdict`. |
| **TradingAgents** | the isolation requirement for AI components: an analysis agent that reads market context and publishes an opinion, with no route to the broker. | Any multi-agent orchestration, and any shared process with credentials. `ai/shadow.py` is isolated structurally: a test asserts it cannot import the execution, sizing, risk, reconciliation, engine, or runtime modules. |
| **Kronos** | confirmed that a forecasting model is *optional research*, not a system dependency. | Everything. Not imported, not required, not present in `pyproject.toml`. |
| **backtesting.py** | a sanity check on event-loop backtest semantics (when a signal becomes a fill). | Its engine and its fill model. Using it would have meant a second execution semantics alongside the live one. |
| **Lean / NautilusTrader** | data-model vocabulary (order states, fill records, position snapshots) and the reminder that reconciliation is a first-class concern. | Both as a runtime. The master plan explicitly forbids making them mandatory dependencies without demonstrated need, and a $100 paper-only bot does not demonstrate that need. |
| **OpenBB / OpenTerminalUI** | nothing at runtime. They are data/UI projects with no bearing on a single-symbol paper bot. | Everything. |
| **Paperclip** | nothing at runtime. | Everything. |
| **skfolio** | nothing at runtime. Portfolio optimisation is meaningless for one symbol under a $100 ceiling. | Everything. |
| **beebots / bitcoin_trading_bot_python** | a reminder of how small a useful bot can be, and of the failure mode where a bot has no ledger and therefore no bound. | Everything. The specific gap these illustrate — capital accounting as an afterthought — is what `sizing/ledger.py` exists to prevent. |
| **awesome-systematic-trading** | reading list, nothing more. | Everything. |

---

## 2. What was borrowed versus written

| Concern | Borrowed as | Written here |
| --- | --- | --- |
| Strategy lifecycle | pattern (signals from closed bars) | `strategy/base.py`, `strategy/sma_cross.py`, `strategy/donchian.py` |
| Order lifecycle | vocabulary | `core/contracts.py::OrderStatus`, `execution/service.py` |
| Reconciliation | idea (local vs broker) | `reconciliation/reconciler.py`, `Drift`, severities |
| Risk validation | pattern (named checks) | `risk/manager.py`, fifteen checks |
| Cost-aware research | discipline | `research/backtest.py`, `CostModel` |
| AI isolation | requirement | `ai/shadow.py` + structural tests |
| Capital accounting | **nothing** | `sizing/ledger.py` — no reference repository has an application-enforced ceiling independent of the broker balance |
| Paper-only enforcement | **nothing** | `config/settings.py` allowlist + `execution/alpaca_paper.py` four-layer guard |
| Acceptance harness | **nothing** | `evaluation/acceptance.py` |

The last three rows are the project's actual contribution: the references
describe how to trade, not how to *bound* trading.

---

## 3. Dependency audit

The rule from the plan: avoid making LEAN, NautilusTrader, Hummingbot, OpenBB,
Paperclip, and backtesting.py mandatory runtime dependencies without a
demonstrated need. Current state:

```
dependencies            = PyYAML>=6.0
optional: alpaca        = alpaca-py>=0.30      (order submission only)
optional: research      = vectorbt>=0.28       (exploration only)
optional: dev           = pytest>=7.4
```

| Reference | Present in `pyproject.toml` | Why or why not |
| --- | --- | --- |
| Freqtrade | no | engine not used; pattern only |
| VectorBT | optional only | research convenience; the stdlib backtester is authoritative and reproducible |
| Hummingbot | no | lifecycle vocabulary only |
| Vibe-Trading | no | connector rewritten against verified facts |
| day-trader-skill | no | risk checker written here |
| TradingAgents | no | AI is isolated and provider-agnostic |
| Kronos | no | optional research, not used |
| backtesting.py | no | second execution semantics would be a liability |
| LEAN | no | .NET runtime; no demonstrated need |
| NautilusTrader | no | engine not used; vocabulary only |
| OpenBB | no | no data need beyond Alpaca bars |
| OpenTerminalUI | no | UI is a stdlib HTTP server |
| Paperclip | no | not applicable |
| skfolio | no | single symbol |
| beebots / bitcoin_trading_bot_python | no | illustrative only |
| awesome-systematic-trading | no | a list, not a dependency |

Every entry above is checkable with `grep` on `pyproject.toml`; that is
intentional. A dependency list that can only be verified by reading a document
is not verified.

---

## 4. If you extend this project

When adding a capability that one of the references already has, decide in this
order:

1. **Do we need it at all?** Most features in a full trading platform exist to
   serve multiple exchanges, multiple strategies, or multiple users. This bot has
   one of each.
2. **Does it touch the order path?** If yes, it belongs in `execution/` and must
   not be reachable from `ai/` or `evaluation/`. The structural tests will tell
   you immediately.
3. **Does it need a dependency?** If a stdlib implementation is under ~300 lines,
   writing it keeps the paper-only guarantee auditable in one repository.
4. **Is the behaviour verified?** If it makes a claim about Alpaca, it belongs in
   `docs/API_CAPABILITIES.md` with a source. If it cannot be verified offline, it
   is marked as an assumption and no test may depend on it.
5. **Is there evidence?** Add the test, run the suite, and record the output in
   `IMPLEMENTATION_CHECKLIST.md`. A phase is not complete because a file exists.

---

## 5. Attribution

The reference repositories remain the property of their authors and are not
distributed with this project. Where a design pattern was inspired by one of
them, the pattern is named above. No licence header from any of them applies to
this code, because no code was taken from them.

If that ever changes, this section must change with it — and so must the
project's licence.
