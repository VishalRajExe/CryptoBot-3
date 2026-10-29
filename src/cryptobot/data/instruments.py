"""Documented fallback instrument metadata.

Real precision always comes from ``GET /v2/assets?asset_class=crypto`` and is
cached in the journal. These entries exist only so that offline research, dry-runs
and tests can operate without credentials, and they are the values Alpaca's own
documentation publishes for BTC/USD:

    "min_order_size": "0.0001", "min_trade_increment": "0.0001", "price_increment": "1"

Using a fallback is recorded in :class:`~cryptobot.core.contracts.RiskVerdict`
check details so a decision that relied on it is auditable. Paper trading
refreshes the real values before submitting anything.
"""

from __future__ import annotations

from cryptobot.core.contracts import InstrumentSpec

FALLBACK_INSTRUMENTS: dict[str, InstrumentSpec] = {
    "BTC/USD": InstrumentSpec(
        symbol="BTC/USD",
        min_order_size="0.0001",
        min_trade_increment="0.0001",
        price_increment="1",
        tradable=True,
        fractionable=True,
        shortable=False,
        marginable=False,
    )
}


def fallback_instrument(symbol: str) -> InstrumentSpec | None:
    return FALLBACK_INSTRUMENTS.get(symbol)
