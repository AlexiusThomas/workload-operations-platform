"""Error-handling middleware for WOP Lambda handlers (NFR-009 AC-2, SEC-002 AC-3, SEC-006 AC-2).

Exposes :func:`error_handler`, a decorator that wraps a Lambda handler and:

- Extracts the API Gateway correlation id (``requestContext.requestId``) or generates one.
- Passes ``correlation_id`` to the wrapped handler as a keyword argument.
- Maps any :class:`~backend.observability.errors.WOPError` subclass to its declared
  ``http_status`` and ``code`` per the design's Error Code Reference.
- Maps any other (unhandled) exception to a sanitised HTTP 500 ``INTERNAL_ERROR``,
  emits the ``wop.unhandled_error`` metric, and logs the traceback to CloudWatch only —
  never to the response body (SEC-006 AC-2).
- Renders authorization failures with a generic message that carries no resource
  information (SEC-002 AC-3).

This module is import-safe without AWS credentials.
"""

from __future__ import annotations

import functools
import json
import traceback
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from backend.observability import errors, logger, metrics

#: The Lambda proxy-integration response shape.
LambdaResponse = Dict[str, Any]
Handler = Callable[..., LambdaResponse]


def _correlation_id(event: Dict[str, Any]) -> str:
    """Return the API Gateway request id, or a generated UUID for non-HTTP triggers."""
    request_context = event.get("requestContext") if isinstance(event, dict) else None
    if isinstance(request_context, dict):
        request_id = request_context.get("requestId")
        if request_id:
            return str(request_id)
    return str(uuid4())


def http_error(
    status_code: int,
    code: str,
    message: str,
    correlation_id: Optional[str] = None,
) -> LambdaResponse:
    """Build a Lambda proxy-integration error response with the standard error body.

    The body is JSON-serialised and includes ``X-Request-Id`` for log tracing
    (NFR-004 AC-2). No stack trace is ever included (SEC-006 AC-2).
    """
    body = errors.error_body(code=code, message=message, request_id=correlation_id)
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "X-Request-Id": correlation_id or "",
            "X-Api-Version": "1.0",
        },
        "body": json.dumps(body, default=str),
    }


def error_handler(handler_func: Handler) -> Handler:
    """Decorate a Lambda handler with centralised exception mapping.

    The wrapped handler is invoked as ``handler_func(event, context, correlation_id=...)``.
    Handlers should raise :class:`~backend.observability.errors.WOPError` subclasses for
    expected failures; the decorator translates them into the correct HTTP status/code.

    Returns:
        The wrapped handler.
    """

    @functools.wraps(handler_func)
    def wrapper(event: Dict[str, Any], context: Any = None) -> LambdaResponse:
        correlation_id = _correlation_id(event or {})
        try:
            return handler_func(event, context, correlation_id=correlation_id)
        except errors.AuthorizationError:
            # Never leak resource information in an authorization failure (SEC-002 AC-3).
            logger.warning("authorization_denied", correlation_id=correlation_id)
            return http_error(
                403,
                errors.AUTHORIZATION_DENIED,
                errors.AUTHORIZATION_DENIED_MESSAGE,
                correlation_id,
            )
        except errors.WOPError as exc:
            level = "info" if exc.http_status < 500 else "error"
            getattr(logger, level)(
                "handled_error",
                correlation_id=correlation_id,
                error=exc.code,
                http_status=exc.http_status,
            )
            return http_error(exc.http_status, exc.code, str(exc), correlation_id)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all for the boundary
            # Unhandled: log the traceback to CloudWatch (not the client), emit the alarm
            # metric, and return a sanitised 500 (NFR-009 AC-2, SEC-006 AC-2).
            logger.error(
                "unhandled_exception",
                correlation_id=correlation_id,
                error=str(exc),
                traceback=traceback.format_exc(),
                http_status=500,
            )
            metrics.increment(metrics.UNHANDLED_ERROR)
            return http_error(
                500,
                errors.INTERNAL_ERROR,
                f"An internal error occurred. Request ID: {correlation_id}",
                correlation_id,
            )

    return wrapper
