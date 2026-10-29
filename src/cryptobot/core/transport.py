"""A minimal, injectable HTTP transport.

Why not ``requests``: the core must stay dependency-light, and every network
call in this project needs to be substitutable in tests. This module provides a
single ``request`` seam plus bounded, header-aware retries.

It never logs headers: Alpaca authentication headers are secrets.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from cryptobot.core.errors import CryptobotError
from cryptobot.core.logging import get_logger, redact_text

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class TransportError(CryptobotError):
    """A network call failed and retrying did not help."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"malformed JSON response: {exc}", status=self.status) from exc

    def text(self, limit: int = 400) -> str:
        try:
            return self.body.decode("utf-8", errors="replace")[:limit]
        except Exception:  # pragma: no cover - defensive
            return "<unreadable>"


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Default transport. Standard library only."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> HttpResponse:
        if params:
            encoded = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True, safe="/:"
            )
            url = f"{url}{'&' if '?' in url else '?'}{encoded}"
        body = None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=body, headers=dict(request_headers), method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers=dict(response.headers),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                body=exc.read() or b"",
                headers=dict(exc.headers or {}),
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise TransportError(f"network error: {exc}", retryable=True) from exc


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return None
    return None


def request_with_retry(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    timeout: float = 20.0,
    max_retries: int = 3,
    backoff_seconds: float = 1.5,
    max_backoff_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpResponse:
    """Perform a request, retrying only on transient failures.

    Non-idempotent requests must not use this helper for state-changing calls
    without an idempotency key; order submission handles retries itself.
    """
    attempt = 0
    last_error: Exception | None = None
    while attempt <= max_retries:
        try:
            response = transport.request(
                method, url, headers=headers, params=params, json_body=json_body, timeout=timeout
            )
        except TransportError as exc:
            last_error = exc
            if not exc.retryable or attempt == max_retries:
                raise
            delay = min(backoff_seconds * (2**attempt), max_backoff_seconds)
            log.warning("transport retry after network error", extra={"context": {"attempt": attempt + 1, "delay_s": delay}})
            sleep(delay)
            attempt += 1
            continue

        if response.status < 400:
            return response

        if response.status in RETRYABLE_STATUS and attempt < max_retries:
            delay = _retry_after_seconds(response.headers)
            if delay is None:
                delay = min(backoff_seconds * (2**attempt), max_backoff_seconds)
            log.warning(
                "transport retry after HTTP error",
                extra={"context": {"status": response.status, "attempt": attempt + 1, "delay_s": delay}},
            )
            sleep(delay)
            attempt += 1
            continue

        detail = redact_text(response.text(200))
        if response.status in (401, 403):
            raise TransportError(
                f"HTTP {response.status}: authentication failed. Check that ALPACA_API_KEY "
                f"and ALPACA_API_SECRET_KEY are paper-account keys. Body: {detail}",
                status=response.status,
            )
        raise TransportError(
            f"HTTP {response.status} from {url.split('?')[0]}: {detail}",
            status=response.status,
            retryable=response.status in RETRYABLE_STATUS,
        )

    raise TransportError(f"request failed after {max_retries + 1} attempts: {last_error}", retryable=True)
