# Alpaca API capabilities — verified facts

The rule this file exists to enforce: **no invented capabilities.** If a
behaviour is not recorded here with a source, it is not implemented. Where the
documentation was ambiguous, the code does the conservative thing and this file
says so.

**How to use this file.** When changing anything that talks to Alpaca, check the
claim here first and re-verify it against the linked documentation. Alpaca
changes its API; this table is a snapshot, not a promise.

**Verification method.** Each fact below was checked against the Alpaca docs at
the stated URL and, where the behaviour is observable without credentials, by a
unit test. Facts that *require* credentials are marked **unverified here** — they
have never been exercised against a live paper account in this environment, and
must not be described as working until they are.

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

- `build_order_payload` sends `qty` as a **decimal string** and never a float,
  and sends `client_order_id` always (determinism, not convenience).
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

| Fact | Value | Source |
| --- | --- | --- |
| Tier 1 maker | 15 bps | [Crypto fees](https://docs.alpaca.markets/docs/crypto-fees) |
| Tier 1 taker | 25 bps | same |
| Fee currency | charged against the received asset for buys | same |

**Implemented as:** `risk.fee_reserve_bps: 25` (taker) and
`research.backtest.CostModel(fee_bps=25, slippage_bps=25)`. Tier 2+ pricing
depends on 30-day volume and is *not* assumed; the default is the worst tier-1
case.

**Known limitation.** The ledger expenses fees in quote currency **at fill
time** using the recorded fee. For a buy, Alpaca charges the fee in the received
(base) asset, so the exact quote-currency cost depends on the price at which
that fee is later valued. The reserve (25 bps of notional) is a conservative
upper bound; the ledger records what was actually reported. This has not been
verified against a real fill because no fill has been placed in this
environment — **unverified here**.

---

## 5. Status vocabulary

| Fact | Value | Source |
| --- | --- | --- |
| Order statuses | `new`, `partially_filled`, `filled`, `done_for_day`, `canceled`, `expired`, `replaced`, `pending_cancel`, `pending_replace`, `accepted`, `pending_new`, `accepted_for_bidding`, `stopped`, `rejected`, `suspended`, `calculated` | [Orders](https://docs.alpaca.markets/reference/getallorders) |

**Implemented as:** `core/contracts.py::map_broker_status` maps the broker
vocabulary onto a smaller local vocabulary (`PENDING_SUBMIT`, `UNCERTAIN`,
`ACCEPTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REPLACED`, `REJECTED`,
`EXPIRED`, `LOST`, `UNKNOWN`). An unrecognised string maps to `UNKNOWN`, which
`needs_broker_confirmation` treats as unresolved. Guessing would be worse than
admitting ignorance: `UNKNOWN` blocks a paper run until it is resolved.

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
| `alpaca-py` exposes a by-client-order-id lookup for crypto orders (`get_order_by_client_id`) | it is the documented REST route `GET /v2/orders:by_client_order_id` surfaced by the SDK, and the code calls it in `get_order` | recovery would need a paginated order scan instead |
| Paper fills are simulated without real slippage | paper trading by definition | measured slippage on paper will be optimistic; the acceptance harness therefore treats slippage as a *floor*, not a forecast |
| `Retry-After` is present on 429 | documented as typical | the transport falls back to exponential backoff, which is slower but still safe |

Nothing in the test suite depends on these assumptions being true: every one of
them is exercised only through the simulator or a mock. That is the point — the
suite proves the code does what it says, not that Alpaca behaves as expected.

---

## 8. What is deliberately absent

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
