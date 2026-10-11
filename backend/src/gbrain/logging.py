from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "body",
        "content",
        "cookie",
        "csrf_token",
        "password",
        "payload",
        "secret",
        "set-cookie",
        "token",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, dict):
            record.msg = {
                key: "[REDACTED]" if key.casefold() in _SENSITIVE_KEYS else value
                for key, value in record.msg.items()
            }
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SensitiveDataFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    logging.getLogger("uvicorn.access").disabled = True
