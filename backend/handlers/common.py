"""Shared HTTP helpers for WOP API Lambda handlers (design: API Design).

Provides request parsing (JSON body, path/query parameters, idempotency key), a success
response builder matching the design's standard headers, and authentication wiring that
resolves a :class:`~backend.auth.provider.UserContext` from the injected auth provider.

Every mutating handler follows the same skeleton (design: Idempotency Strategy):

    1. authenticate + authorize_operation (RBAC) + ownership checks.
    2. validate the request body.
    3. check_idempotency -> REPLAY | IN_FLIGHT | PROCEED.
    4. on PROCEED: acquire_lock, execute the transactional write, store_idempotency_result.

This module is import-safe without AWS credentials.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional, Tuple

from backend.auth import provider as auth_provider
from backend.auth.provider import AuthProvider, UserContext
from backend.data import tables
from backend.idempotency import layer as idempotency
from backend.observability import errors

#: Standard response headers (design: API Design → standard response headers).
_STANDARD_HEADERS = {"Content-Type": "application/json", "X-Api-Version": "1.0"}

#: Cold-start auth provider cache (resolved fail-closed via get_auth_provider).
_auth_provider: Optional[AuthProvider] = None


def set_auth_provider(provider: Optional[AuthProvider]) -> None:
    """Inject the auth provider (tests) or clear it. Production resolves it lazily."""
    global _auth_provider
    _auth_provider = provider


def get_provider() -> AuthProvider:
    """Return the configured auth provider, resolving it fail-closed on first use."""
    global _auth_provider
    if _auth_provider is None:
        _auth_provider = auth_provider.get_auth_provider()
    return _auth_provider


def wire_idempotency() -> None:
    """Point the idempotency layer at the configured idempotency table."""
    idempotency.set_table(tables.idempotency_table())


def authenticate(event: Dict[str, Any]) -> UserContext:
    """Resolve the caller's :class:`UserContext` from the request (FR-018 AC-1)."""
    return get_provider().authenticate(event)


def parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the JSON request body, tolerating an empty/missing body as ``{}``.

    Raises:
        ValidationError: If the body is present but not valid JSON (SEC-006 AC-2).
    """
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise errors.ValidationError(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise errors.ValidationError("request body must be a JSON object")
    return parsed


def path_param(event: Dict[str, Any], name: str) -> Optional[str]:
    """Return a path parameter value, or None."""
    params = event.get("pathParameters") or {}
    value = params.get(name)
    return str(value) if value is not None else None


def required_path_param(event: Dict[str, Any], name: str) -> str:
    """Return a required path parameter, raising ValidationError when absent."""
    value = path_param(event, name)
    if value is None:
        raise errors.ValidationError(f"missing path parameter: {name}")
    return value


def query_param(event: Dict[str, Any], name: str) -> Optional[str]:
    """Return a query-string parameter value, or None."""
    params = event.get("queryStringParameters") or {}
    value = params.get(name)
    return str(value) if value is not None else None


def idempotency_key(event: Dict[str, Any]) -> Optional[str]:
    """Return the ``Idempotency-Key`` header value (case-insensitive), or None."""
    headers = event.get("headers") or {}
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "idempotency-key":
            return str(value) if value is not None else None
    return None


def success(
    status_code: int, body: Dict[str, Any], correlation_id: Optional[str]
) -> Dict[str, Any]:
    """Build a Lambda proxy-integration success response with standard headers."""
    headers = dict(_STANDARD_HEADERS)
    headers["X-Request-Id"] = correlation_id or ""
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, default=str),
    }


def run_idempotent(
    *,
    operation: str,
    resource_id: str,
    request_body: Any,
    key: Optional[str],
    execute: Callable[[], Tuple[int, Dict[str, Any]]],
    expected_state_matches: Optional[Callable[[], bool]] = None,
    build_replay_body: Optional[Callable[[], Dict[str, Any]]] = None,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a mutating operation under the idempotency protocol (FR-020).

    When ``key`` is None the operation runs directly (idempotency is optional per
    FR-020 AC-1). Otherwise the design's lifecycle is applied: replay a COMPLETED response
    with a matching fingerprint, honour an in-flight lock, or acquire the lock, execute, and
    store the result.

    Args:
        operation: Logical operation name (used in the fingerprint).
        resource_id: Primary resource id the key is scoped to.
        request_body: Canonical request body used for the fingerprint.
        key: The client-supplied idempotency key, or None.
        execute: Callable performing the write; returns ``(status_code, body)``.
        expected_state_matches: Optional callable for IN_FLIGHT crash recovery.
        build_replay_body: Optional callable building the reconstructed body on recovery.
        correlation_id: Request correlation id for the response.

    Returns:
        A Lambda proxy-integration response.
    """
    if key is None:
        status_code, body = execute()
        return success(status_code, body, correlation_id)

    wire_idempotency()
    fingerprint = idempotency.compute_fingerprint(operation, resource_id, request_body)
    check = idempotency.check_idempotency(key, fingerprint)

    if check.decision == idempotency.DECISION_REPLAY:
        return _replay(check, correlation_id)

    if check.decision == idempotency.DECISION_IN_FLIGHT:
        # FR-020 AC-4: two concurrent requests with the same key must execute exactly once and
        # BOTH receive the result. The duplicate caller waits briefly for the in-flight owner
        # to complete, then replays the stored response. If the lock turns out to be an
        # orphaned/stale one (crash), recovery either replays a committed result or frees the
        # lock so we can proceed.
        replay = _await_completion_or_recover(
            key, fingerprint, expected_state_matches, build_replay_body
        )
        if replay is not None:
            return replay
        # else: the in-flight lock was stale and freed; fall through to acquire + execute.

    token = idempotency.acquire_lock(key, operation, resource_id, fingerprint)
    if token is None:
        # Another request won the lock between our check and now: wait for its result.
        replay = _await_completion_or_recover(
            key, fingerprint, expected_state_matches, build_replay_body
        )
        if replay is not None:
            return replay
        # Still could not obtain a result and the lock is not stale-recoverable.
        raise errors.IdempotencyConflictError("operation is already in progress")

    status_code, body = execute()
    idempotency.store_idempotency_result(
        key, status_code, json.dumps(body, default=str), lock_token=token
    )
    return success(status_code, body, correlation_id)


#: Bounded wait for an in-flight idempotent request to complete (FR-020 AC-4).
_INFLIGHT_WAIT_ATTEMPTS = 5
_INFLIGHT_WAIT_SECONDS = 0.2


def _await_completion_or_recover(
    key: str,
    fingerprint: str,
    expected_state_matches: Optional[Callable[[], bool]],
    build_replay_body: Optional[Callable[[], Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Wait for an in-flight same-key request to complete, then replay its result.

    Returns a replayed success response when the original request completed (FR-020 AC-4),
    or ``None`` when the in-flight lock is stale and has been freed for re-execution. Raises
    IdempotencyConflictError only if the wait elapses with the lock still genuinely active.
    """
    for _ in range(_INFLIGHT_WAIT_ATTEMPTS):
        current = idempotency.check_idempotency(key, fingerprint)
        if current.decision == idempotency.DECISION_REPLAY:
            return _replay(current, None)
        # Try deterministic crash recovery (stale lock => REPLAY or PROCEED).
        recovered = _recover(key, expected_state_matches, build_replay_body)
        if recovered.decision == idempotency.DECISION_REPLAY:
            return _replay(recovered, None)
        if recovered.decision == idempotency.DECISION_PROCEED:
            return None  # stale lock freed; caller should acquire + execute
        time.sleep(_INFLIGHT_WAIT_SECONDS)
    # Wait elapsed with the lock still active and non-stale.
    raise errors.IdempotencyConflictError("operation is already in progress")


def _replay(check: Any, correlation_id: Optional[str]) -> Dict[str, Any]:
    """Render a replayed response from a stored idempotency record."""
    body = json.loads(check.response_body) if check.response_body else {}
    return success(check.status_code or 200, body, correlation_id)


def _recover(
    key: str,
    expected_state_matches: Optional[Callable[[], bool]],
    build_replay_body: Optional[Callable[[], Dict[str, Any]]],
) -> Any:
    """Run IN_FLIGHT crash recovery when the caller supplied recovery callbacks."""
    if expected_state_matches is None or build_replay_body is None:
        return idempotency.CheckResult(decision=idempotency.DECISION_IN_FLIGHT)
    return idempotency.recover_in_flight(
        key,
        expected_state_matches=expected_state_matches,
        build_response=lambda: json.dumps(build_replay_body(), default=str),
    )
