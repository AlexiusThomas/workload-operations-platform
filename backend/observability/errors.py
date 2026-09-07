"""Domain exception types and the standard error-response schema (SEC-006, NFR-009).

Defines the exception hierarchy raised by the business/domain/data layers and the helpers
that render a sanitised error response body. The error-response schema is::

    {"error": {"code": ..., "message": ..., "request_id": ..., "timestamp": ...}}

Internal stack traces are NEVER included in error responses (SEC-006 AC-2). Authorization
error responses carry no information about the target resource (SEC-002 AC-3).

The exception → (HTTP status, error code) mapping mirrors the design's Error Code
Reference table and is applied by :mod:`backend.handlers.middleware`. This module is pure
and import-safe (no AWS, no environment reads).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Error codes (design: Error Code Reference)
# ---------------------------------------------------------------------------
UNAUTHENTICATED = "UNAUTHENTICATED"
AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
VALIDATION_ERROR = "VALIDATION_ERROR"
INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
CLAIM_CONFLICT = "CLAIM_CONFLICT"
MATERIAL_CLAIM_CONFLICT = "MATERIAL_CLAIM_CONFLICT"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
QUANTITY_EXCEEDED = "QUANTITY_EXCEEDED"
TOTE_REQUIRED = "TOTE_REQUIRED"
INCOMPLETE_WORK = "INCOMPLETE_WORK"
INGESTION_SCHEMA_ERROR = "INGESTION_SCHEMA_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: Generic authorization message that leaks no resource information (SEC-002 AC-3).
AUTHORIZATION_DENIED_MESSAGE = "You do not have permission to perform this action."


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------
class WOPError(Exception):
    """Base class for all WOP domain exceptions.

    Attributes:
        code: The stable error code returned to clients.
        http_status: The HTTP status the middleware maps this exception to.
    """

    code: str = INTERNAL_ERROR
    http_status: int = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class AuthenticationError(WOPError):
    """Bearer token absent or invalid (FR-018 AC-2). Maps to HTTP 401."""

    code = UNAUTHENTICATED
    http_status = 401


class AuthorizationError(WOPError):
    """Role insufficient or ownership check failed (FR-019 AC-3, SEC-002 AC-3).

    Maps to HTTP 403. The rendered response body carries no resource information.
    """

    code = AUTHORIZATION_DENIED
    http_status = 403


class ValidationError(WOPError):
    """Request body fails schema/business validation (SEC-006 AC-2). Maps to HTTP 400."""

    code = VALIDATION_ERROR
    http_status = 400


class InvalidStateTransitionError(WOPError):
    """Requested state transition is not in the valid table (FR-021 AC-2). HTTP 400."""

    code = INVALID_STATE_TRANSITION
    http_status = 400


class ConflictError(WOPError):
    """Base class for 409 conflicts. Subclasses set the specific ``code``."""

    code = IDEMPOTENCY_CONFLICT
    http_status = 409


class ClaimConflictError(ConflictError):
    """WorkUnit claim lost the race; state no longer AVAILABLE (FR-006 AC-2)."""

    code = CLAIM_CONFLICT


class MaterialClaimConflictError(ConflictError):
    """MaterialRequirement claim lost the race (FR-004 AC-2)."""

    code = MATERIAL_CLAIM_CONFLICT


class IdempotencyConflictError(ConflictError):
    """Idempotency key reused with a different payload, or still in-flight (FR-020 AC-4)."""

    code = IDEMPOTENCY_CONFLICT


class ResourceNotFoundError(WOPError):
    """Requested entity does not exist. Maps to HTTP 404."""

    code = RESOURCE_NOT_FOUND
    http_status = 404


class QuantityExceededError(WOPError):
    """Proposed completed_qty exceeds required_qty (FR-009 AC-3). HTTP 400."""

    code = QUANTITY_EXCEEDED
    http_status = 400


class ToteRequiredError(WOPError):
    """Tote id empty, or ready-to-verify without a tote (FR-012 AC-2, FR-013 AC-2). HTTP 400."""

    code = TOTE_REQUIRED
    http_status = 400


class IncompleteWorkError(WOPError):
    """Prep-complete attempted while completed_qty < required_qty (FR-010 AC-2). HTTP 400."""

    code = INCOMPLETE_WORK
    http_status = 400


class IngestionSchemaError(WOPError):
    """Workload file failed schema validation (FR-001 AC-4). HTTP 400."""

    code = INGESTION_SCHEMA_ERROR
    http_status = 400


class AuthConfigurationError(WOPError):
    """Auth provider is misconfigured (e.g. synthetic in prod) — fail closed (SEC-001 AC-4).

    Surfaces as HTTP 500 because a misconfigured Lambda must not serve requests.
    """

    code = INTERNAL_ERROR
    http_status = 500


class MissingConfigurationError(WOPError):
    """A required configuration value is absent at startup (NFR-007 AC-3). HTTP 500."""

    code = INTERNAL_ERROR
    http_status = 500


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 ``...Z`` string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def error_body(
    code: str,
    message: str,
    request_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the standard error-response envelope.

    Never includes stack traces or internal detail beyond ``message`` (SEC-006 AC-2).

    Returns:
        ``{"error": {"code", "message", "request_id", "timestamp"}}``.
    """
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "timestamp": timestamp or _iso_now(),
        }
    }
