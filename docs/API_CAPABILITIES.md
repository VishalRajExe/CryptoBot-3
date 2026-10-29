# Alpaca API capabilities — verified facts

The rule this file exists to enforce: **no invented capabilities.** If a
behaviour is not recorded here with a source, it is not implemented. Where the
documentation was ambiguous, the code does the conservative thing and this file
says so.

**How to use this file.** When changing anything that talks to Alpaca, check the
claim here first and re-verify it against the linked documentation. Alpaca
changes its API; this table is a snapshot, not a promise.

**Verification method.** Each fact below was checked against the Alpaca docs at
the stated URL on the stated date and, where the behaviour is observable without
credentials, by a unit test. Facts that *require* credentials are marked
**unverified here** — they have never been exercised against a live paper account
in this environment, and must not be described as working until they are.

### Verification log

| Date | What was checked | How |
| --- | --- | --- |
| 2026-10-29 | Crypto fee currency, tiers, posting time, `CFEE`/`FEE` activity types | read [Crypto fees](https://docs.alpaca.markets/docs/crypto-fees) (page reports `updatedAt` 2025-09-24) |
| 2026-10-29 | Order types, `time_in_force`, 24/7 hours, margin/short restrictions | read [Crypto trading](https://docs.alpaca.markets/docs/crypto-trading) |
| 2026-10-29 | Activities endpoint, pagination, `date` filter semantics | read [Get account activities](https://docs.alpaca.markets/reference/getaccountactivities) |
| 2026-10-29 | SDK surface: request models, enums, models, exceptions | installed `alpaca-py` 0.44.0 and introspected it; pinned by `tests/test_sdk_integration.py` |

No real broker request was made to establish any of these. Every SDK check runs
against the installed package, not against the network.

---

## 1. Endpoints

| Fact | Value | Source |
| --- | --- | --- |
| Paper trading base URL | `https://paper-api.alpaca.markets` | [Paper trading](https://docs.alpaca.markets/docs/paper-trading) |
| Live trading base URL | `https://api.alpaca.markets` | [API reference](https://docs.alpaca.markets/reference) |
| Market data base URL | `https://data.alpaca.markets` | [Market data](https://docs.alpaca.markets/docs/about-market-data-api) |
| Crypto bars path | `GET /v1beta3/crypto/{loc}/bars` | [Crypto bars](https://docs.alpaca.markets/reference/cryptobars) |
| Crypto location values | `us` (the only value this project accepts) | same |

**Implemented as:** `PAPER_TRADING_HOSTS = {"https://paper-api.alpaca.markets"}`
in `config/settings.py`. The live URL is a named constant in
`execution/alpaca_paper.py` purely so it can be *explicitly rejected* with a
clear message rather than merely being absent from the allowlist.

**Consequence:** there is no configuration value, environment variable, or CLI
flag that produces a live client. See `docs/ARCHITECTURE.md` §2.1.

---

## 2. Crypto bars

| Fact | Value | Source |
| --- | --- | --- |
| Response shape | `{"bars": {"<symbol>": [bar, ...]}, "next_page_token": "..."}` | [Crypto bars](https://docs.alpaca.markets/reference/cryptobars) |
| Pagination | follow `next_page_token` until it is absent/null | same |
| Timeframe strings | `1Min`, `5Min`, `15Min`, `30Min`, `1Hour`, `6Hour`, `1Day` (this project's `1h` maps to `1Hour`) | [Historical bars](https://docs.alpaca.markets/docs/historical-api) |
| Symbol format | `BTC/USD` (slash-separated base/quote) | same |
| Empty window | the symbol key may be absent entirely, and that is not an error | same |

**Consequence:** `data/alpaca_data.py` treats a missing symbol key as "no bars
in this window", not as a failure, and follows `next_page_token` to completion.
A partially fetched series would look perfectly valid to the strategy, so
truncation is not an acceptable failure mode.

**Zero volume.** Crypto bars are constructed from quote mid prices rather than
from executions. A bar with `volume: 0` is therefore legitimate and is accepted
by `data/validation.py`. Treating it as "no trading occurred" would reject good
data.

---

## 3. Crypto orders

| Fact | Value | Source |
| --- | --- | --- |
| Supported order types for crypto | `market`, `limit`, `stop_limit` | [Crypto orders](https://docs.alpaca.markets/docs/crypto-orders) |
| `time_in_force` for crypto | `gtc`, `ioc` only — **`day` is invalid** | [Crypto orders](https://docs.alpaca.markets/docs/crypto-orders) |
| Crypto is marginable | no | same |
| Crypto is shortable | no | same |
| Trading hours | 24/7, no market close | same |
| Per-asset minimums | `min_order_size`, `min_trade_increment`, `price_increment` from the asset record | [Assets](https://docs.alpaca.markets/reference/get-v2-assets-symbol_or_asset_id) |

**Implemented as:**

- `build_order_payload` builds `qty` as a **decimal string** with `format(qty, "f")`,
  so `0.00001` never becomes `1E-5`. **Caveat, verified 2026-10-29:** the
  `alpaca-py` request models type `qty` and `limit_price` as numbers and coerce the
  string on construction (`MarketOrderRequest(qty="0.001").qty == 0.001`). The
  string formatting still matters for our own hashing and logs, and the round trip
  through `float` is exact for the increments this project trades, but the SDK does
  not carry the string end to end. Pinned in `tests/test_sdk_integration.py`.
- `time_in_force` comes from configuration and is validated; `day` cannot be
  configured because it would be rejected by the broker.
- `stop_limit` is **not implemented**. `OrderIntent` has no `stop_price` field,
  and inventing a stop price at the adapter boundary would silently change what
  the operator asked for. `build_order_payload` raises a clear `ExecutionError`
  rather than approximating it.
- Quantity is validated against `min_order_size` and `min_trade_increment`
  *before* submission, so an avoidable rejection never consumes an API call.
- Because crypto is not shortable, a `SELL` larger than the current position is
  rejected locally by the `sell_within_position` risk check.

**Deliberately not used:** notional-denominated orders. This project sends
`qty` only, so the question of notional support for crypto does not arise and
the code makes no claim about it.

---

## 4. Fees

This section was rewritten on 2026-10-29. The previous version stated the fee
currency as "charged against the received asset for buys" and then left the
accounting as a known limitation. The limitation was real and the ledger was
wrong, so it was fixed rather than documented.

| Fact | Value | Source |
| --- | --- | --- |
| Tier 1 maker | 15 bps | [Crypto fees](https://docs.alpaca.markets/docs/crypto-fees) |
| Tier 1 taker | 25 bps | same |
| Currency the fee is charged in | the **credited** asset: "the crypto fee will be charged on the credited crypto asset/fiat (what you receive) per trade" | same |
| Consequence for a buy | `BUY ETH/USD` is charged in **ETH** | same |
| Consequence for a sell | `SELL ETH/USD` is charged in **USD** | same |
| When the fee appears | posted at the end of the trading day, after the fill | same |
| Activity type, non-USD charge | `CFEE` | same |
| Activity type, USD charge | `FEE` | same |
| Where to read them | `GET /v2/account/activities` with `activity_types` | [Get account activities](https://docs.alpaca.markets/reference/getaccountactivities) |
| Fee on the order object | **none**: `alpaca-py` 0.44.0 `Order.model_fields` has no fee field | SDK introspection, 2026-10-29 |
| Activities method in the SDK | **none**: `TradingClient` has no activities method in 0.44.0 | SDK introspection, 2026-10-29 |

**Implemented as:**

- `core/contracts.py::credited_currency` is the single definition of the rule, and
  `FillRecord.net_quantity` nets a base-asset fee out of the buy quantity. The
  holding a fill creates is `filled_qty - fee`, because the fee is taken from the
  asset that was just credited.
- `committed` stays **fee-free**: `commit_buy` expenses the fee in quote to
  `realized_pnl` and commits `net_quantity * price`. Therefore
  `committed + fee_in_quote == gross_notional` for every buy, which is what makes a
  reconciliation against the broker's `avg_entry_price` basis meaningful rather
  than approximately right.
- The fee recorded on a fill at fill time is an **estimate** at the reserve rate,
  in the currency the broker will actually charge it in. The Orders API cannot do
  better, so the estimate is labelled as one.
- `execution/fees.py::attribute_fees` replaces the estimate with the broker's
  figure once the activity posts. It is idempotent by activity id, matches on
  symbol + side + fee currency inside a 3-day window, corrects the fill, adjusts the
  ledger by the difference, and moves `committed` when a base-asset correction
  changes the credited quantity. A fee it cannot attribute is still **expensed**
  and reported as `UNATTRIBUTED` — money left the account either way.
- `reconciliation/reconciler.py` tolerates a difference of up to twice the fee
  budget on quantity and cost basis. A fee-sized difference is expected; a missing
  or duplicated fill is tens of percent and still raises a critical drift.

**Residual uncertainties.**

| Uncertainty | How the code copes |
| --- | --- |
| Whether the broker reports `filled_qty` gross or already net of the fee | the ledger books the net quantity *and* reconciliation tolerates two fee budgets, so either convention reconciles without halting |
| `CFEE` is documented on the fees page but is **absent from the enum** in the activities OpenAPI reference (`FEE` is listed, `CFEE` is not) | both types are requested and the response is treated as authoritative; a `CFEE` row is parsed by the same code path as a `FEE` row |
| The `date` filter matches `created_at`, and the docs note that "for non-trade activities such as fees, the creation date is typically the day after the trade date (in UTC)" | callers must pass an `after` date **at least one day before** the first fill they want covered; the matching window is widened rather than the filter trusted |
| Maker vs taker rate per fill | the estimate uses the taker rate, so a maker-priced fill is over-reserved, never under-reserved |


---

## 5. Status vocabulary

| Fact | Value | Source |
| --- | --- | --- |
| Order statuses (18, `alpaca-py` 0.44.0) | `new`, `partially_filled`, `filled`, `done_for_day`, `canceled`, `expired`, `replaced`, `pending_cancel`, `pending_replace`, `pending_review`, `accepted`, `pending_new`, `accepted_for_bidding`, `stopped`, `rejected`, `suspended`, `calculated`, `held` | [Orders](https://docs.alpaca.markets/reference/getallorders); the enum was confirmed member-for-member against the SDK on 2026-10-29 |

**Implemented as:** `core/contracts.py::map_broker_status` maps the broker
vocabulary onto a smaller local vocabulary (`PENDING_SUBMIT`, `UNCERTAIN`,
`ACCEPTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REPLACED`, `REJECTED`,
`EXPIRED`, `LOST`, `UNKNOWN`). An unrecognised string maps to `UNKNOWN`, which
`needs_broker_confirmation` treats as unresolved. Guessing would be worse than
admitting ignorance: `UNKNOWN` blocks a paper run until it is resolved.

Of the 18 broker statuses, 17 map to a definite local state and `pending_review`
deliberately maps to `UNKNOWN` — it means Alpaca is still reviewing the order, so
there is nothing safe to assume about it. `tests/test_sdk_integration.py` iterates
the SDK enum so a status added in a future release fails the suite rather than
silently becoming `UNKNOWN` by accident.

---

## 6. Rate limits and error handling

| Fact | Value | Source |
| --- | --- | --- |
| Trading API limit | 200 requests per minute per account | [Rate limits](https://docs.alpaca.markets/docs/rate-limit) |
| Throttled response | HTTP 429 | same |
| Retry hint | `Retry-After` header, when present | same |

**Implemented as:** `core/transport.py` defines
`RETRYABLE_STATUS = {429, 500, 502, 503, 504}`, honours `Retry-After` when
present, otherwise backs off exponentially (`backoff_seconds * 2**attempt`,
capped at 30s). Only transient failures are retried. `401`/`403` are **not**
retried and produce a message naming the credential variables, because retrying
a bad key just burns the rate limit.

**Not implemented:** proactive client-side throttling to 200/min. The loop's
cadence (`execution` retries, `--interval` seconds between cycles, and at most
`risk.max_orders_per_day` orders) keeps the observed rate far below the limit
for a single symbol. If the symbol list grows, a token bucket is the right
addition and is not present today.

---

## 7. Behaviours this project assumes but has not verified against a real account

Listed explicitly so no one mistakes them for tested facts:

| Assumption | Why it is assumed | Risk if wrong |
| --- | --- | --- |
| A `client_order_id` sent to the broker is returned on the order record | standard Alpaca behaviour, and the basis of the whole recovery path | recovery by lookup would fail; orders would become `LOST` and require manual resolution |
| Sandbox market orders fill promptly for `BTC/USD` | observed behaviour is widely reported | an order could sit `ACCEPTED` indefinitely; the grace period then marks it `LOST` |
| Paper fills are simulated without real slippage | paper trading by definition | measured slippage on paper will be optimistic; the acceptance harness therefore treats slippage as a *floor*, not a forecast |
| `Retry-After` is present on 429 | documented as typical | the transport falls back to exponential backoff, which is slower but still safe |
| Whether `filled_qty` is gross or net of a base-asset fee | the docs describe the fee as taken from the credited asset without saying whether the order's `filled_qty` reflects it | the ledger books the net figure and reconciliation tolerates two fee budgets, so a real account is expected to reconcile either way; the first live fill is where this gets settled |

Removed from this table on 2026-10-29: the claim that `alpaca-py` exposes a
by-client-order-id lookup. It is now **verified** — `TradingClient.get_order_by_client_id`
exists in 0.44.0 and `tests/test_sdk_integration.py` asserts it.

Nothing in the test suite depends on these assumptions being true: every one of
them is exercised only through the simulator or a mock. That is the point — the
suite proves the code does what it says, not that Alpaca behaves as expected.

---

## 8. What the SDK actually does (`alpaca-py` 0.44.0)

Observed by introspecting the installed package on 2026-10-29, not read from
documentation. Each row is pinned by a test in `tests/test_sdk_integration.py`,
so a version bump that changes one of them fails the suite.

| Observation | Why it matters |
| --- | --- |
| `BaseURL` subclasses `str`, so `TradingClient(...)._base_url` compares equal to the intended host | the paper-only check works without unwrapping; an earlier version of this project assumed otherwise, and that assumption is now recorded as wrong rather than left in a comment |
| `MarketOrderRequest.qty` and `LimitOrderRequest.limit_price` are typed as numbers and coerce the decimal strings we pass | precision is the SDK's, not ours, after construction; harmless at 4 decimal places, but the claim "sent as a string" would be inaccurate |
| `Order.model_fields` contains no fee field, and `TradingClient` has no activities method | the broker's actual fee is unreachable through the order API, which is the entire reason `execution/fees.py` exists |
| `APIError.code` and `.message` are properties that `json.loads` the body and index a key, so they raise `KeyError`/`JSONDecodeError` on an unexpected payload | a classifier that reads `.code` inside an `except` block converts a definite rejection into a crash; the adapter reads `status_code` defensively instead |
| `APIError.status_code` is `None` when no `requests` response is wrapped | "no status" must be classified as *ambiguous*, never as a rejection |
| `OrderStatus` has 18 members, including `pending_review` and `held` | the local map covers 17 of them and leaves `pending_review` as `UNKNOWN` on purpose |
| `CryptoBarsRequest` takes `symbol_or_symbols`, not `symbols`, and `TimeFrame.Hour` builds a new object per access with no `__eq__` | comparing timeframes by identity silently fails; compare `amount` and `unit` |

---

## 9. What is deliberately absent

| Not implemented | Reason |
| --- | --- |
| Live trading of any kind | out of scope, permanently |
| Short selling | crypto is not shortable at Alpaca |
| Margin | crypto is not marginable at Alpaca |
| `stop_limit` orders | `OrderIntent` has no stop price; approximating one would change the operator's meaning |
| Notional orders | `qty` is always sent, so no claim is made about notional support |
| Bracket / OCO orders | the lifecycle model in `execution/service.py` has no notion of a parent/child link; adding it without reconciliation support would be unsafe |
| Streaming / websocket market data | bars only; a stream would need its own reconnect and gap-fill story |
| Proactive rate-limit token bucket | see §6 |
| Multiple simultaneous symbols | supported by the code, not tuned or tested by the risk limits |
