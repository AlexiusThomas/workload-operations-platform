"""Structured JSON logger for WOP Lambda functions (NFR-004 AC-1, AC-2).

Every log entry is emitted as a single-line JSON object to stdout (CloudWatch Logs)
with a consistent field set drawn from the design's Observability Strategy section:
``timestamp``, ``level``, ``request_id`` (correlation id), ``lambda_function``,
``user_id``, ``user_role``, ``action``, ``entity_type``, ``entity_id``, ``duration_ms``,
``http_status``, and ``error``.

The logger is the only logging interface intended for business logic; direct ``print``
calls are prohibited in production code (design: Observability Strategy). This module is
import-safe without AWS credentials: it writes to a standard :mod:`logging` stream handler
and reads no environment or AWS resources at import time.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

#: Name of the underlying stdlib logger. Business code uses the helpers below, not this.
_LOGGER_NAME = "wop"

#: Fields that form the canonical structured-log envelope (design: Structured Log Format).
_ENVELOPE_FIELDS = (
    "timestamp",
    "level",
    "request_id",
    "lambda_function",
    "user_id",
    "user_role",
    "action",
    "entity_type",
    "entity_id",
    "duration_ms",
    "error",
    "http_status",
)


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _base_logger() -> logging.Logger:
    """Return the module logger, attaching a single stdout StreamHandler once.

    The handler emits the message verbatim (the message is already JSON), so no extra
    formatting is applied. Idempotent: repeated calls do not add duplicate handlers.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _build_entry(
    level: str,
    message: str,
    *,
    correlation_id: Optional[str] = None,
    lambda_function: Optional[str] = None,
    user_id: Optional[str] = None,
    user_role: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    http_status: Optional[int] = None,
    error: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Build the structured log dict for a single entry.

    ``correlation_id`` maps to the ``request_id`` field so a single request can be traced
    across Lambda invocations (NFR-004 AC-2). Any additional keyword arguments are merged
    in as extra structured fields.
    """
    entry: Dict[str, Any] = {
        "timestamp": _iso_now(),
        "level": level,
        "message": message,
        "request_id": correlation_id,
        "lambda_function": lambda_function,
        "user_id": user_id,
        "user_role": user_role,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "duration_ms": duration_ms,
        "http_status": http_status,
        "error": error,
    }
    if extra:
        entry.update(extra)
    return entry


def _emit(level: str, message: str, **fields: Any) -> Dict[str, Any]:
    """Serialize and emit a structured entry at ``level``; return the entry dict.

    Returning the dict makes the logger unit-testable without capturing stdout.
    """
    entry = _build_entry(level, message, **fields)
    log_line = json.dumps(entry, default=str, sort_keys=True)
    logger = _base_logger()
    logger.log(getattr(logging, level, logging.INFO), log_line)
    return entry


def info(message: str, **fields: Any) -> Dict[str, Any]:
    """Emit an INFO-level structured log entry."""
    return _emit("INFO", message, **fields)


def warning(message: str, **fields: Any) -> Dict[str, Any]:
    """Emit a WARNING-level structured log entry."""
    return _emit("WARNING", message, **fields)


def error(message: str, **fields: Any) -> Dict[str, Any]:
    """Emit an ERROR-level structured log entry."""
    return _emit("ERROR", message, **fields)


def debug(message: str, **fields: Any) -> Dict[str, Any]:
    """Emit a DEBUG-level structured log entry."""
    return _emit("DEBUG", message, **fields)
