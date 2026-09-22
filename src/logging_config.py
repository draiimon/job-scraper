from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_SENSITIVE_PARTS = ("token", "secret", "password", "authorization", "api_key", "database_url", "webhook")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            payload[key] = "[REDACTED]" if any(part in key.lower() for part in _SENSITIVE_PARTS) else value
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
