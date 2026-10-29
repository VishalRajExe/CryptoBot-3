"""Operational states.

Phase 8 requires that these be kept strictly distinct:

* **pausing entries** - stop opening new positions, leave existing ones alone.
* **cancelling orders** - an explicit, separate action.
* **stopping the process** - a lifecycle concern, not a trading decision.
* **closing positions** - never automatic; requires an explicit operator order.

:class:`Capabilities` encodes exactly which of those a given state permits so
that no caller has to re-derive the policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SystemState(str, Enum):
    ACTIVE = "active"
    """Normal operation; new entries allowed if risk approves."""

    PAUSE_ENTRIES = "pause_entries"
    """Reversible operator pause. No new entries. Cancels allowed. Positions held."""

    HALTED = "halted"
    """Unsafe/untrustworthy state (reconciliation drift, repeated errors).

    No new entries, no cancellations, no automatic anything. Requires a human.
    """

    KILLED = "killed"
    """Kill switch engaged. No new entries, but cancelling open orders is allowed
    so that the operator can flatten risk deliberately."""


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the current state permits.

    ``close_positions`` is intentionally absent: closing a position is always an
    explicit operator action routed through the normal risk + execution path,
    never an automatic consequence of a state change.
    """

    allow_new_entries: bool
    allow_cancel_orders: bool
    allow_process_restart: bool
    requires_manual_intervention: bool

    @classmethod
    def for_state(cls, state: SystemState) -> "Capabilities":
        return _CAPABILITIES[state]


_CAPABILITIES: dict[SystemState, Capabilities] = {
    SystemState.ACTIVE: Capabilities(True, True, True, False),
    SystemState.PAUSE_ENTRIES: Capabilities(False, True, True, False),
    SystemState.HALTED: Capabilities(False, False, False, True),
    SystemState.KILLED: Capabilities(False, True, True, True),
}


def capabilities_for(state: SystemState | str) -> Capabilities:
    try:
        # ``str(SystemState.KILLED)`` is "SystemState.KILLED" on a str-Enum, so an
        # enum argument has to be taken as-is rather than round-tripped through str.
        key = state if isinstance(state, SystemState) else SystemState(str(state))
        return _CAPABILITIES[key]
    except (ValueError, KeyError) as exc:  # unknown persisted state
        raise ValueError(f"unknown system state: {state!r}") from exc
