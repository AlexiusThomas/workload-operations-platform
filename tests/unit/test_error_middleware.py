"""Unit tests for the error-handling middleware (Task 3.3).

Verifies each exception type maps to the correct HTTP status and error code, that stack
traces are never included in the response body, that authorization errors carry no
resource information, and that unhandled exceptions emit the ``wop.unhandled_error``
metric (SEC-006 AC-2, NFR-009 AC-2, SEC-002 AC-3).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from backend.handlers.middleware import error_handler
from backend.observability import errors, metrics


@pytest.fixture(autouse=True)
def _capture_metrics(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Capture metric names emitted during a test without touching AWS."""
    emitted: List[str] = []
    monkeypatch.setattr(metrics, "increment", lambda name, dimensions=None: emitted.append(name))
    return emitted


def _make_handler(exc: Exception):
    @error_handler
    def handler(event: Dict[str, Any], context: Any, correlation_id: str) -> Dict[str, Any]:
        raise exc

    return handler


_EVENT = {"requestContext": {"requestId": "req-abc-123"}}


@pytest.mark.parametrize(
    "exc, expected_status, expected_code",
    [
        (errors.AuthenticationError("no token"), 401, errors.UNAUTHENTICATED),
        (errors.AuthorizationError("nope"), 403, errors.AUTHORIZATION_DENIED),
        (errors.ValidationError("bad field"), 400, errors.VALIDATION_ERROR),
        (
            errors.InvalidStateTransitionError("bad transition"),
            400,
            errors.INVALID_STATE_TRANSITION,
        ),
        (errors.ClaimConflictError("taken"), 409, errors.CLAIM_CONFLICT),
        (
            errors.MaterialClaimConflictError("taken"),
            409,
            errors.MATERIAL_CLAIM_CONFLICT,
        ),
        (errors.IdempotencyConflictError("reused"), 409, errors.IDEMPOTENCY_CONFLICT),
        (errors.ResourceNotFoundError("missing"), 404, errors.RESOURCE_NOT_FOUND),
        (errors.QuantityExceededError("too much"), 400, errors.QUANTITY_EXCEEDED),
        (errors.ToteRequiredError("no tote"), 400, errors.TOTE_REQUIRED),
        (errors.IncompleteWorkError("incomplete"), 400, errors.INCOMPLETE_WORK),
    ],
)
def test_exception_maps_to_status_and_code(
    exc: Exception, expected_status: int, expected_code: str
) -> None:
    response = _make_handler(exc)(_EVENT, None)
    assert response["statusCode"] == expected_status
    body = json.loads(response["body"])
    assert body["error"]["code"] == expected_code
    assert body["error"]["request_id"] == "req-abc-123"
    assert body["error"]["timestamp"]


def test_unhandled_exception_maps_to_500_and_emits_metric(
    _capture_metrics: List[str],
) -> None:
    response = _make_handler(RuntimeError("boom secret internals"))(_EVENT, None)
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["error"]["code"] == errors.INTERNAL_ERROR
    assert metrics.UNHANDLED_ERROR in _capture_metrics


def test_no_stack_trace_in_response_body() -> None:
    response = _make_handler(RuntimeError("boom at line 42 in secret_module"))(_EVENT, None)
    raw = response["body"]
    # A stack trace would contain a "Traceback" header or file/line markers.
    assert "Traceback" not in raw
    assert 'File "' not in raw
    assert "line 42" not in raw


def test_authorization_error_carries_no_resource_info() -> None:
    response = _make_handler(errors.AuthorizationError("user X cannot access WU-999"))(_EVENT, None)
    body = json.loads(response["body"])
    # The generic message must be used; the raised detail (WU-999) must not leak.
    assert body["error"]["message"] == errors.AUTHORIZATION_DENIED_MESSAGE
    assert "WU-999" not in response["body"]


def test_correlation_id_generated_when_absent() -> None:
    response = _make_handler(errors.ValidationError("bad"))({}, None)
    body = json.loads(response["body"])
    assert body["error"]["request_id"]  # a UUID was generated


def test_successful_handler_passes_through() -> None:
    @error_handler
    def ok_handler(event: Dict[str, Any], context: Any, correlation_id: str) -> Dict[str, Any]:
        return {"statusCode": 200, "body": json.dumps({"cid": correlation_id})}

    response = ok_handler(_EVENT, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["cid"] == "req-abc-123"
