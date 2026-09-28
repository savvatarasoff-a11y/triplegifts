"""Логирование с маскировкой токенов и ключей."""
from __future__ import annotations

import logging
import re
import sys

# Токен бота вида 123456789:AA...
_PATTERNS = [
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),
]
MASK = "***"


def redact(text: str, secrets: list[str] | None = None) -> str:
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message, self._secrets)
        record.args = None
        return True


def setup_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Библиотеки HTTP пишут URL запросов, где может оказаться токен бота
    for noisy in ("aiohttp.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
