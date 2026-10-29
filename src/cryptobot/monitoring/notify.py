"""Telegram notifications.

Hard requirement from the spec: notifications must never crash the trading loop.
Therefore :meth:`TelegramNotifier.send` swallows every exception and returns a
boolean. Failures are recorded in the journal, never raised.

The bot token is read from the environment and redacted out of every log line by
the logging filter; it is never interpolated into an exception message.
"""

from __future__ import annotations

import os
from typing import Mapping

from cryptobot.core.logging import get_logger, redact_text
from cryptobot.core.transport import HttpTransport, UrllibTransport, request_with_retry
from cryptobot.journal.store import Journal

log = get_logger(__name__)

TOKEN_ENV = "CRYPTOBOT_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "CRYPTOBOT_TELEGRAM_CHAT_ID"
API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        env: Mapping[str, str] | None = None,
        journal: Journal | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._transport = transport or UrllibTransport()
        self._env = os.environ if env is None else env
        self._journal = journal
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._env.get(TOKEN_ENV)) and bool(self._env.get(CHAT_ENV))

    def send(self, text: str) -> bool:
        if not self.enabled:
            self._record("skipped", "not configured")
            return False
        token = self._env[TOKEN_ENV]
        chat_id = self._env[CHAT_ENV]
        try:
            request_with_retry(
                self._transport,
                "POST",
                f"{API_BASE}/bot{token}/sendMessage",
                json_body={
                    "chat_id": chat_id,
                    "text": redact_text(text)[:4000],
                    "disable_web_page_preview": True,
                },
                timeout=self._timeout,
                max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001 - must never propagate
            log.warning(
                "telegram notification failed; trading continues",
                extra={"context": {"error": str(exc)[:160]}},
            )
            self._record("failed", str(exc)[:200])
            return False
        self._record("sent", text[:120])
        return True

    def _record(self, status: str, detail: str) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record_notification("telegram", status, redact_text(detail))
        except Exception:  # noqa: BLE001 - journal trouble must not break notifications
            pass
