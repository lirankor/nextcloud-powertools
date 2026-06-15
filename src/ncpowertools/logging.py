"""Structured (JSON) logging setup.

One line of JSON per record on stdout. ``setup_logging`` is idempotent and
honors ``LOG_LEVEL``; ``get_logger`` returns a namespaced logger. Extra
fields passed via ``logger.info("msg", extra={...})`` are merged into the
JSON object.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_ROOT_NAME = "ncpowertools"

# Attributes present on every LogRecord; anything else is treated as an extra.
_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Format a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = _coerce(value)
        return json.dumps(payload, default=str, ensure_ascii=False)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        # ISO-8601 UTC with milliseconds.
        ct = time.gmtime(record.created)
        base = time.strftime("%Y-%m-%dT%H:%M:%S", ct)
        return f"{base}.{int(record.msecs):03d}Z"


def _coerce(value: Any) -> Any:
    """Make a value JSON-friendly without exploding (json.dumps uses default=str too)."""
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


def setup_logging(level: str = "INFO") -> None:
    """Configure the ncpowertools root logger for JSON stdout output.

    Idempotent: clears prior handlers so repeated calls (e.g. tests) don't
    multiply output.
    """
    logger = logging.getLogger(_ROOT_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ncpowertools namespace."""
    if name == _ROOT_NAME or name.startswith(_ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
