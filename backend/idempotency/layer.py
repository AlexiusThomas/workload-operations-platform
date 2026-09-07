"""Idempotency layer for WOP mutating operations (FR-020).

Implements the design's Idempotency Strategy against the ``wop-idempotency-table``:

- :func:`check_idempotency` — reads the record for a key and decides replay / conflict /
  proceed / recover.
- :func:`store_idempotency_result` — records the COMPLETED response (status/body) with a
  TTL of at least ``now + 86400`` seconds (FR-020 AC-3).
- :func:`acquire_lock` — PutItem with ``attribute_not_exists(pk)`` to win the IN_FLIGHT
  lock for concurrent same-key requests (FR-020 AC-4).
- Fingerprint handling — a COMPLETED record replays its stored response only when the
  SHA-256 ``request_fingerprint`` matches; a mismatch raises
  :class:`~backend.observability.errors.IdempotencyConflictError` (409).
- IN_FLIGHT crash recovery — the design's deterministic 30-second staleness procedure.

The layer owns the idempotency table exclusively. It uses a boto3 DynamoDB *table
resource* which is injected (:func:`set_table`) so unit/property tests can use moto or a
fake without AWS credentials. This module is import-safe: no AWS access at import time.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from backend.observability.errors import IdempotencyConflictError

# ---------------------------------------------------------------------------
# Record statuses and key layout (design: IdempotencyRecord data model)
# ---------------------------------------------------------------------------
STATUS_IN_FLIGHT = "IN_FLIGHT"
STATUS_COMPLETED = "COMPLETED"

#: Minimum TTL window in seconds (FR-020 AC-3): at least 24 hours.
DEFAULT_TTL_SECONDS = 86400

#: Staleness threshold for orphaned IN_FLIGHT records (design: IN_FLIGHT Crash Recovery).
STALENESS_THRESHOLD_SECONDS = 30

#: check_idempotency decision outcomes.
DECISION_PROCEED = "PROCEED"  # no record; caller should acquire the lock and execute
DECISION_REPLAY = "REPLAY"  # COMPLETED record with matching fingerprint; replay response
DECISION_IN_FLIGHT = "IN_FLIGHT"  # another request holds the lock; caller retries/recovers


def _pk(idempotency_key: str) -> str:
    """Return the partition key for an idempotency record."""
    return f"IDMP#{idempotency_key}"


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 ``...Z`` string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_fingerprint(operation: str, resource_id: str, request_body: Any) -> str:
    """Compute the SHA-256 request fingerprint (design: Request Fingerprint).

    The fingerprint is ``sha256(operation + resource_id + canonical_request_body_json)``
    where the body is serialised with sorted keys so semantically-identical bodies hash
    identically (FR-020 AC-4).
    """
    canonical_body = json.dumps(request_body, sort_keys=True, separators=(",", ":"), default=str)
    material = f"{operation}\x1f{resource_id}\x1f{canonical_body}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyRecord:
    """A persisted idempotency record (design: IdempotencyRecord data model)."""

    idempotency_key: str
    operation: str
    request_fingerprint: str
    resource_id: str
    status: str
    status_code: Optional[int]
    response_body: Optional[str]
    created_at: str
    ttl: int


@dataclass(frozen=True)
class CheckResult:
    """Outcome of :func:`check_idempotency`.

    Attributes:
        decision: One of ``PROCEED``, ``REPLAY``, ``IN_FLIGHT``.
        status_code: Stored HTTP status when ``decision == REPLAY``.
        response_body: Stored response body when ``decision == REPLAY``.
        record: The raw record found, if any.
    """

    decision: str
    status_code: Optional[int] = None
    response_body: Optional[str] = None
    record: Optional[IdempotencyRecord] = None


# ---------------------------------------------------------------------------
# Injectable table resource (keeps tests hermetic)
# ---------------------------------------------------------------------------
_table: Optional[Any] = None


def set_table(table: Optional[Any]) -> None:
    """Inject the DynamoDB *table resource* for the idempotency table (or a test fake)."""
    global _table
    _table = table


def _get_table() -> Any:
    """Return the injected idempotency table resource.

    Raises:
        RuntimeError: If no table has been injected. Callers must call :func:`set_table`
            (production wiring) or inject a moto/fake table (tests) before use.
    """
    if _table is None:
        raise RuntimeError("Idempotency table resource is not configured; call set_table().")
    return _table


def _to_record(item: Dict[str, Any]) -> IdempotencyRecord:
    """Map a raw DynamoDB item dict to an :class:`IdempotencyRecord`."""
    return IdempotencyRecord(
        idempotency_key=item["idempotency_key"],
        operation=item.get("operation", ""),
        request_fingerprint=item.get("request_fingerprint", ""),
        resource_id=item.get("resource_id", ""),
        status=item.get("status", ""),
        status_code=int(item["status_code"]) if item.get("status_code") is not None else None,
        response_body=item.get("response_body"),
        created_at=item.get("created_at", ""),
        ttl=int(item.get("ttl", 0)),
    )


def _get_item(idempotency_key: str) -> Optional[Dict[str, Any]]:
    """Fetch the raw idempotency item for a key, or None."""
    table = _get_table()
    resp = table.get_item(Key={"pk": _pk(idempotency_key)})
    item: Optional[Dict[str, Any]] = resp.get("Item")
    return item


def acquire_lock(
    idempotency_key: str,
    operation: str,
    resource_id: str,
    request_fingerprint: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """Attempt to win the IN_FLIGHT lock via a conditional PutItem (FR-020 AC-4).

    Returns:
        True if the lock was acquired; False if another request already holds it
        (the conditional ``attribute_not_exists(pk)`` failed).
    """
    from botocore.exceptions import ClientError  # lazy import keeps module import-safe

    table = _get_table()
    item = {
        "pk": _pk(idempotency_key),
        "idempotency_key": idempotency_key,
        "operation": operation,
        "resource_id": resource_id,
        "request_fingerprint": request_fingerprint,
        "status": STATUS_IN_FLIGHT,
        "created_at": _iso_now(),
        "ttl": int(time.time()) + max(ttl_seconds, DEFAULT_TTL_SECONDS),
    }
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def check_idempotency(
    idempotency_key: str,
    request_fingerprint: str,
) -> CheckResult:
    """Decide how to handle a request bearing ``idempotency_key`` (design: lifecycle).

    - No record found -> ``PROCEED`` (caller acquires the lock and executes).
    - COMPLETED with a matching fingerprint -> ``REPLAY`` the stored response
      (FR-020 AC-2); on a mismatch, raise :class:`IdempotencyConflictError` (409).
    - IN_FLIGHT -> ``IN_FLIGHT`` (caller retries with back-off, then recovers).

    Raises:
        IdempotencyConflictError: On a COMPLETED record whose fingerprint differs
            (key reused with different parameters, FR-020 AC-4).
    """
    item = _get_item(idempotency_key)
    if item is None:
        return CheckResult(decision=DECISION_PROCEED)

    record = _to_record(item)
    if record.status == STATUS_COMPLETED:
        if record.request_fingerprint != request_fingerprint:
            raise IdempotencyConflictError(
                "Idempotency key reused with different request parameters."
            )
        return CheckResult(
            decision=DECISION_REPLAY,
            status_code=record.status_code,
            response_body=record.response_body,
            record=record,
        )

    # IN_FLIGHT
    return CheckResult(decision=DECISION_IN_FLIGHT, record=record)


def store_idempotency_result(
    idempotency_key: str,
    status_code: int,
    response_body: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """Mark the record COMPLETED with the original response (FR-020 AC-2, AC-3).

    Updates status to COMPLETED and stores ``status_code``, ``response_body``, and a TTL
    of at least ``now + 86400`` seconds.
    """
    table = _get_table()
    ttl_value = int(time.time()) + max(ttl_seconds, DEFAULT_TTL_SECONDS)
    table.update_item(
        Key={"pk": _pk(idempotency_key)},
        UpdateExpression=(
            "SET #status = :completed, status_code = :sc, response_body = :body, #ttl = :ttl"
        ),
        ExpressionAttributeNames={"#status": "status", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":completed": STATUS_COMPLETED,
            ":sc": status_code,
            ":body": response_body,
            ":ttl": ttl_value,
        },
    )


def _is_stale(record: IdempotencyRecord, now_epoch: Optional[int] = None) -> bool:
    """Return True if an IN_FLIGHT record is older than the staleness threshold."""
    now_epoch = now_epoch if now_epoch is not None else int(time.time())
    try:
        created_dt = datetime.strptime(record.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        # Unparseable timestamp: treat as stale so recovery can proceed rather than deadlock.
        return True
    age = now_epoch - int(created_dt.timestamp())
    return age > STALENESS_THRESHOLD_SECONDS


def delete_orphaned_in_flight(idempotency_key: str) -> None:
    """Delete an orphaned IN_FLIGHT record — the ONLY permitted delete on this table.

    Scoped by a conditional expression to ``status = IN_FLIGHT`` so a COMPLETED record can
    never be deleted (design: IN_FLIGHT Crash Recovery; SEC-007 spirit).
    """
    from botocore.exceptions import ClientError  # lazy import keeps module import-safe

    table = _get_table()
    try:
        table.delete_item(
            Key={"pk": _pk(idempotency_key)},
            ConditionExpression="#status = :in_flight",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":in_flight": STATUS_IN_FLIGHT},
        )
    except ClientError as exc:
        # If the record is no longer IN_FLIGHT (completed concurrently), leave it intact.
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return
        raise


def recover_in_flight(
    idempotency_key: str,
    expected_state_matches: Callable[[], bool],
    build_response: Callable[[], str],
    status_code: int = 200,
    now_epoch: Optional[int] = None,
) -> CheckResult:
    """Run the deterministic IN_FLIGHT crash-recovery procedure (design: IN_FLIGHT Crash Recovery).

    Preconditions: the caller has already retried with back-off and still sees an IN_FLIGHT
    record. This function then:

    1. If the record is NOT older than 30 seconds, returns ``IN_FLIGHT`` (keep waiting).
    2. If it is stale (orphaned), reads the target entity via ``expected_state_matches``:
       - Match -> the original transaction committed. Mark COMPLETED with a reconstructed
         body (``build_response``) and return ``REPLAY`` (idempotent success).
       - No match -> the original transaction did not commit. Delete the orphaned record
         (the only permitted delete) and return ``PROCEED`` so the caller re-executes.

    Args:
        idempotency_key: The idempotency key.
        expected_state_matches: Callable returning True if the target entity is already in
            the expected post-operation state (i.e. the original transaction committed).
        build_response: Callable returning the reconstructed response body on a match.
        status_code: HTTP status to record on a match (default 200).
        now_epoch: Optional injected "now" for deterministic tests.

    Returns:
        A :class:`CheckResult` with decision ``IN_FLIGHT``, ``REPLAY``, or ``PROCEED``.
    """
    item = _get_item(idempotency_key)
    if item is None:
        # Record vanished (TTL/delete): safe to re-execute.
        return CheckResult(decision=DECISION_PROCEED)

    record = _to_record(item)
    if record.status == STATUS_COMPLETED:
        return CheckResult(
            decision=DECISION_REPLAY,
            status_code=record.status_code,
            response_body=record.response_body,
            record=record,
        )

    if not _is_stale(record, now_epoch=now_epoch):
        return CheckResult(decision=DECISION_IN_FLIGHT, record=record)

    if expected_state_matches():
        body = build_response()
        store_idempotency_result(idempotency_key, status_code=status_code, response_body=body)
        return CheckResult(decision=DECISION_REPLAY, status_code=status_code, response_body=body)

    delete_orphaned_in_flight(idempotency_key)
    return CheckResult(decision=DECISION_PROCEED)
