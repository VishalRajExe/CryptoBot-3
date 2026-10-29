"""CRYPTOBOT - paper-only cryptocurrency trading platform.

This package is deliberately importable without ``alpaca-py``, ``vectorbt``, or a
network connection so that the deterministic core can be tested anywhere.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
