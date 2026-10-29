"""Exception hierarchy.

Every failure mode the application can produce is a subclass of
:class:`CryptobotError` so that the CLI can render one consistent report and
callers can distinguish "expected, handled" from "unexpected bug".
"""

from __future__ import annotations


class CryptobotError(Exception):
    """Base class for all application errors."""


class ContractError(CryptobotError):
    """A typed contract was constructed with invalid data."""


class ConfigError(CryptobotError):
    """Configuration is missing, malformed, or unsafe."""


class DataError(CryptobotError):
    """Market data could not be obtained or is unusable."""


class StaleDataError(DataError):
    """The newest available bar is too old to base a decision on."""


class GapError(DataError):
    """A required candle interval is missing from the series."""


class RiskRejected(CryptobotError):
    """A deterministic risk check blocked an order intent."""


class ExecutionError(CryptobotError):
    """Base class for broker interaction failures."""


class AmbiguousSubmission(ExecutionError):
    """The submit call failed in a way that leaves the order state unknown.

    The caller MUST NOT resubmit. It must recover by querying the broker with
    the deterministic ``client_order_id``.
    """


class DefiniteRejection(ExecutionError):
    """The broker definitively refused the order; no state change occurred."""


class DuplicateOrderError(ExecutionError):
    """An order intent with this ``client_order_id`` already exists."""


class MissingDependency(CryptobotError):
    """An optional dependency required for this code path is not installed."""


class ReconciliationError(CryptobotError):
    """Local state and broker state disagree beyond tolerance."""


class HaltedError(CryptobotError):
    """The system is in a state that forbids the requested operation."""


class PaperOnlyViolation(ExecutionError):
    """A code path attempted to reach a non-paper Alpaca endpoint."""
