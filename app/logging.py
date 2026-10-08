"""Structured JSON logging with request-id correlation and secret redaction.

Never log document text, questions, tokens, passwords or API keys. The redaction here is a
safety net; call sites must still avoid passing such values.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "secret",
        "question",
        "content",
        "text",
        "snippet",
        "document_text",
        "answer",
    }
)

_REDACTIONS = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "[redacted-jwt]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "[redacted-key]"),
    (re.compile(r"pa-[A-Za-z0-9_-]{20,}"), "[redacted-key]"),
    (re.compile(r"sb_secret_[A-Za-z0-9_-]{8,}"), "[redacted-key]"),
)


def redact(text: str) -> str:
    """Mask anything that looks like a bearer token, JWT or provider API key."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            str(k): ("[redacted]" if str(k).lower() in _SENSITIVE_KEYS else _clean(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(_clean(fields))
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc"] = redact("".join(traceback.format_exception(*record.exc_info)))
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configure JSON logging on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_rag_json", False):
            root.removeHandler(existing)
    handler._rag_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    # Third-party libraries log full URLs (with ids in query strings) at INFO.
    for noisy in ("httpx", "httpcore", "hpack", "anthropic", "urllib3", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True


def log_event(
    logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any
) -> None:
    """Emit a structured event. ``fields`` become top-level JSON keys."""
    logger.log(level, event, extra={"fields": fields})
