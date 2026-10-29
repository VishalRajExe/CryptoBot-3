"""Attribution of the fees the broker actually charged.

Facts this module exists because of (verified 2026-10-29, see
``docs/API_CAPABILITIES.md`` §4):

* Alpaca charges a crypto fee on the **credited** asset: a buy is charged in the
  base asset, a sell in the quote asset.
* The Orders API returns **no** fee. ``alpaca-py``'s ``Order`` model has no field
  for it, and ``TradingClient`` has no activities method at all.
* Fees are posted after the end of the trading day as account activities:
  ``CFEE`` for a non-USD charge, ``FEE`` for a USD one, through
  ``GET /v2/account/activities``.

So the fee recorded on a fill is an estimate at the configured reserve rate until
the activity posts. This module replaces the estimate with the broker's figure
exactly once per activity, and adjusts the ledger by the difference.

Two rules make it trustworthy:

1. Idempotent. Activities are keyed by their own id, so re-running a sync is free
   and a retry cannot double-charge the ledger.
2. Nothing is dropped. A fee that cannot be matched to a fill is still expensed -
   the money left the account either way - and is reported as *unattributed* so a
   human can look at why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from cryptobot.core.contracts import (
    FillRecord,
    Side,
    base_currency,
    normalize_symbol,
    quote_currency,
    to_decimal,
    to_utc,
)
from cryptobot.core.logging import get_logger
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger

log = get_logger(__name__)

UTC = timezone.utc
ZERO = Decimal("0")

#: Activity types that carry a trading fee.
FEE_ACTIVITY_TYPES = ("CFEE", "FEE")

#: A fee posts up to a day after the trade, and matching is by symbol and side, so
#: look this far back for the fill it belongs to.
MATCH_WINDOW_DAYS = 3


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FeeActivity:
    activity_id: str
    kind: str
    ts: datetime
    symbol: str
    charged: Decimal
    currency: str
    quote_value: Decimal
    description: str = ""

    @property
    def side(self) -> Side:
        """Which trade produced this fee.

        A charge in a non-USD asset is taken from the base asset, which only
        happens on a buy. A USD-denominated ``FEE`` is charged on proceeds, which
        only exist on a sell.
        """
        return Side.BUY if self.currency != quote_currency(self.symbol) else Side.SELL

    def as_row(self, *, recorded_at: datetime) -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "ts": self.ts.isoformat(),
            "kind": self.kind,
            "symbol": self.symbol,
            "qty": format(self.charged, "f"),
            "price": None,
            "currency": self.currency,
            "quote_value": format(self.quote_value, "f"),
            "description": self.description,
            "recorded_at": recorded_at.isoformat(),
        }


def parse_fee_activity(payload: Mapping[str, Any]) -> FeeActivity | None:
    """Normalise one activity object, or return ``None`` if it is not a fee.

    Tolerant by design: the activities endpoint is loosely typed and reports the
    charge as ``qty`` in the charged asset plus a ``price``, or as ``net_amount``
    when the charge is already in USD.
    """
    kind = str(payload.get("activity_type") or "").strip().upper()
    if kind not in FEE_ACTIVITY_TYPES:
        return None
    symbol_raw = payload.get("symbol")
    if not symbol_raw:
        return None
    symbol = normalize_symbol(str(symbol_raw))

    quantity = to_decimal(payload.get("qty") or "0", "qty")
    charged = abs(quantity)
    price = payload.get("price")
    net_amount = payload.get("net_amount")

    currency = base_currency(symbol) if kind == "CFEE" else quote_currency(symbol)
    if price not in (None, "", "0") and currency != quote_currency(symbol):
        quote_value = charged * to_decimal(price, "price")
    elif net_amount not in (None, ""):
        quote_value = abs(to_decimal(net_amount, "net_amount"))
    elif currency == quote_currency(symbol):
        quote_value = charged
    else:
        # A non-USD charge with no price cannot be valued; treat it as unknown
        # rather than silently worth zero.
        log.warning(
            "fee activity has no price; cannot value a base-asset fee",
            extra={"context": {"activity_id": payload.get("id"), "symbol": symbol}},
        )
        return None

    return FeeActivity(
        activity_id=str(payload.get("id") or ""),
        kind=kind,
        ts=_activity_ts(payload),
        symbol=symbol,
        charged=charged,
        currency=currency,
        quote_value=quote_value,
        description=str(payload.get("description") or ""),
    )


def parse_fee_activities(payloads: Iterable[Mapping[str, Any]]) -> list[FeeActivity]:
    out: list[FeeActivity] = []
    for payload in payloads:
        parsed = parse_fee_activity(payload)
        if parsed is not None and parsed.activity_id:
            out.append(parsed)
    return sorted(out, key=lambda item: item.ts)


def _activity_ts(payload: Mapping[str, Any]) -> datetime:
    for key in ("date", "transaction_time", "created_at"):
        raw = payload.get(key)
        if not raw:
            continue
        text = str(raw)
        if len(text) == 10:  # a bare date means "some time that day"
            text += "T00:00:00+00:00"
        try:
            return to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")), "activity.ts")
        except ValueError:
            continue
    return _utcnow()


@dataclass(frozen=True, slots=True)
class FeeAttributionReport:
    activities: int = 0
    corrected: int = 0
    already_recorded: int = 0
    unmatched: tuple[FeeActivity, ...] = ()
    delta_total: Decimal = ZERO

    @property
    def ok(self) -> bool:
        return not self.unmatched

    def summary(self) -> str:
        parts = [
            f"{self.activities} fee activity(ies)",
            f"{self.corrected} matched to a fill",
            f"{self.already_recorded} already recorded",
        ]
        if self.delta_total:
            parts.append(f"ledger adjusted by {self.delta_total}")
        if self.unmatched:
            parts.append(f"{len(self.unmatched)} UNATTRIBUTED")
        return "; ".join(parts)


def attribute_fees(
    journal: Journal,
    ledger: AllocationLedger,
    activities: Sequence[FeeActivity],
    *,
    clock: Any = _utcnow,
) -> FeeAttributionReport:
    """Correct estimated fees with the broker's actual figures.

    Returns a report; never raises for a missing match, because an unattributable
    fee is information, not a reason to stop accounting for money that moved.
    """
    known = journal.fee_activity_ids()
    matched_fills = journal.matched_fee_fill_ids()
    corrected = already = 0
    delta_total = ZERO
    unmatched: list[FeeActivity] = []

    for activity in activities:
        if activity.activity_id in known:
            already += 1
            continue

        fill = _match_fill(journal, activity, matched_fills)
        previous = fill.fee_in_quote() if fill is not None else ZERO

        if fill is not None:
            charged = _write_actual_fee(journal, fill, activity)
            # The charge is now in the fill's own currency; value it in quote so
            # the ledger and the fill agree on the same number.
            actual_quote = charged * fill.price if fill.fee_is_in_base else charged
            matched_fills.add(fill.fill_id)
            corrected += 1
            ref = fill.client_order_id
        else:
            actual_quote = activity.quote_value
            unmatched.append(activity)
            ref = f"fee:{activity.activity_id}"
            log.warning(
                "fee activity could not be attributed to a fill; expensing it anyway",
                extra={
                    "context": {
                        "activity_id": activity.activity_id,
                        "symbol": activity.symbol,
                        "quote_value": format(activity.quote_value, "f"),
                    }
                },
            )

        delta = actual_quote - previous
        if delta != ZERO:
            ledger.expense_fee(
                delta,
                ref=ref,
                note=f"actual {activity.kind} fee {actual_quote} vs estimated {previous}",
            )
            delta_total += delta
            if fill is not None and fill.fee_is_in_base:
                _move_cost_basis(ledger, fill, delta, ref=ref)

        row = activity.as_row(recorded_at=clock())
        row["delta_quote"] = format(delta, "f")
        if fill is not None:
            row["matched_client_order_id"] = fill.client_order_id
            row["matched_fill_id"] = fill.fill_id
        journal.record_fee_activity(row)

    return FeeAttributionReport(
        activities=len(activities),
        corrected=corrected,
        already_recorded=already,
        unmatched=tuple(unmatched),
        delta_total=delta_total,
    )


def _write_actual_fee(journal: Journal, fill: FillRecord, activity: FeeActivity) -> Decimal:
    """Write the actual charge in the currency the fill records its fee in.

    Returns the amount written. A ``FEE`` activity carries its value in
    ``net_amount`` with ``qty`` set to zero, so a quote-denominated charge must be
    taken from ``quote_value`` rather than from the charged quantity.
    """
    if fill.fee_currency == quote_currency(fill.symbol):
        amount = activity.quote_value
    elif activity.currency == fill.fee_currency:
        amount = activity.charged
    else:
        amount = activity.quote_value / fill.price
    journal.correct_fill_fee(fill.fill_id, amount)
    return amount


def _move_cost_basis(
    ledger: AllocationLedger, fill: FillRecord, delta: Decimal, *, ref: str
) -> None:
    """Keep ``committed`` in step with a corrected base-asset fee.

    A buy pays its fee out of the asset it was credited with, so charging a
    different fee changes how much of the position was really bought. The money
    spent does not change - it just splits differently between cost basis and
    expense - so the basis moves by exactly the fee difference and the total is
    preserved. Adjusting here is what keeps ``committed`` equal to the independent
    figure derived from fills, instead of drifting by the fee on every trade.
    """
    basis_delta = -delta
    if basis_delta < ZERO:
        # Never adjust below zero: a basis larger than what is deployed would
        # silently hide an unrelated inconsistency elsewhere.
        basis_delta = max(basis_delta, -ledger.state().committed)
    if basis_delta == ZERO:
        return
    ledger.adjust(
        basis_delta, ref=ref, note=f"corrected base-asset fee {delta} changed the credited quantity"
    )


def _match_fill(
    journal: Journal, activity: FeeActivity, already_matched: set[str]
) -> FillRecord | None:
    """Find the fill a fee belongs to.

    Conservative on purpose: same symbol, same side, same fee currency, within a
    short window, not already attributed. The closest estimate wins, which for a
    bot that places at most a handful of orders a day is effectively exact, and
    ties are broken by timestamp so the choice is deterministic.
    """
    earliest = activity.ts - timedelta(days=MATCH_WINDOW_DAYS)
    candidates = [
        fill
        for fill in journal.fills_for_symbol(activity.symbol)
        if fill.side is activity.side
        and fill.fee_currency == activity.currency
        and fill.fill_id not in already_matched
        and earliest <= fill.ts <= activity.ts
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda fill: (abs(fill.fee_in_quote() - activity.quote_value), fill.ts, fill.fill_id),
    )
