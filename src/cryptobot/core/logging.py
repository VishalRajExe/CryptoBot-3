"""Structured logging with credential redaction.

Two protections, both cheap and both tested:

1. A filter that replaces any value of a known secret environment variable if it
   ever appears in a log message (e.g. an HTTP library echoing the auth header).
2. A filter that rewrites ``Apca-Api-Key-Id: <value>`` style substrings.

Nothing in this module ever prints a secret; it only ever removes them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

REDACTED = "***REDACTED***"

#: Environment variables whose *values* must never reach a log line.
SECRET_ENV_VARS = (
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET_KEY",
    "CRYPTOBOT_TELEGRAM_BOT_TOKEN",
)

_HEADER_PATTERNS = (
    re.compile(r"(?i)(apca-api-key-id|apca-api-secret-key)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(bot)[0-9]{6,}:[A-Za-z0-9_-]{20,}"),
)


def collect_secrets(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = os.environ if env is None else env
    values = {env.get(name, "") for name in SECRET_ENV_VARS}
    # Ignore short/placeholder values so we never redact ordinary words.
    return tuple(sorted(v for v in values if v and len(v) >= 8))


def redact_text(text: str, secrets: Iterable[str] | None = None) -> str:
    secrets = collect_secrets() if secrets is None else tuple(secrets)
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    for pattern in _HEADER_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", out)
    return out


class RedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self._secrets = tuple(secrets) if secrets is not None else None

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = collect_secrets() if self._secrets is None else self._secrets
        try:
            original = record.getMessage()
        except Exception:  # malformed format args must not crash logging
            return True
        cleaned = redact_text(original, secrets)
        if cleaned != original:
            record.msg = cleaned
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, Mapping):
            payload.update({k: v for k, v in context.items() if k not in payload})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = getattr(record, "context", None)
        suffix = ""
        if isinstance(context, Mapping) and context:
            suffix = " " + " ".join(f"{k}={v}" for k, v in context.items())
        return super().format(record) + suffix


_CONFIGURED = False


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        JsonFormatter() if json_output else TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, **context: Any) -> None:
    """Log with a machine-readable context mapping attached."""
    safe = {k: redact_text(str(v)) if isinstance(v, str) else v for k, v in context.items()}
    logger.log(level, event, extra={"context": safe}, stacklevel=2)
