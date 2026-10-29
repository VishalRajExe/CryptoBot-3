"""Deterministic JSON encoding.

Decimals are encoded as strings so that no float rounding ever enters persisted
state or the hashes we compute over it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from cryptobot.core.errors import ContractError


def default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def dumps(obj: Any) -> str:
    """Compact, key-sorted JSON: stable output means stable fingerprints."""
    return json.dumps(obj, default=default, sort_keys=True, separators=(",", ":"))


def loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc
