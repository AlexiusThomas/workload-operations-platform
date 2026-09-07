"""Property-based test for idempotency key deduplication (Property 4).

Uses a moto-emulated DynamoDB idempotency table so the property test is hermetic (no AWS
credentials or network). It exercises the full check -> lock -> execute -> store lifecycle
and asserts that a second identical request replays the first response and creates no
additional side effects (FR-020 AC-2, AC-5, AC-6).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import boto3
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from backend.idempotency import layer

_IDEMPOTENCY_TABLE = "wop-idempotency-table"


def _create_idempotency_table() -> Any:
    """Create the idempotency table in moto and return the table resource."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=_IDEMPOTENCY_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(_IDEMPOTENCY_TABLE)


def _execute_once(
    events_sink: List[str],
    key: str,
    operation: str,
    resource_id: str,
    body: Dict[str, Any],
) -> Tuple[int, str]:
    """Run one idempotent operation.

    Emulates a mutating handler: check idempotency, and only on PROCEED perform the
    "side effect" (append to ``events_sink``, standing in for AuditEvent/ProductionEvent/
    MaterialEvent writes) and store the result. On REPLAY, return the stored response
    without re-executing.
    """
    fingerprint = layer.compute_fingerprint(operation, resource_id, body)
    result = layer.check_idempotency(key, fingerprint)

    if result.decision == layer.DECISION_REPLAY:
        assert result.status_code is not None and result.response_body is not None
        return result.status_code, result.response_body

    # PROCEED: acquire the lock, perform the side effect exactly once, store the result.
    acquired = layer.acquire_lock(key, operation, resource_id, fingerprint)
    assert acquired  # single-threaded test: the first PROCEED always wins the lock

    events_sink.append(f"{operation}:{resource_id}")  # the one-time side effect
    response = {"resource_id": resource_id, "operation": operation, "event_index": len(events_sink)}
    status_code = 200
    body_json = json.dumps(response, sort_keys=True)
    layer.store_idempotency_result(key, status_code=status_code, response_body=body_json)
    return status_code, body_json


@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    key=st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != ""),
    resource_id=st.text(min_size=1, max_size=20).filter(lambda s: s.strip() != ""),
    n_extra=st.integers(min_value=0, max_value=5),
)
def test_idempotency_key_deduplication(key: str, resource_id: str, n_extra: int) -> None:
    # Feature: workload-operations-platform, Property 4: Idempotency Key Deduplication
    # Validates: Requirements FR-020 AC-2, FR-020 AC-5, FR-020 AC-6
    with mock_aws():
        table = _create_idempotency_table()
        layer.set_table(table)
        try:
            events: List[str] = []
            operation = "verify_work_unit"
            body = {"resource_id": resource_id}

            first_status, first_body = _execute_once(events, key, operation, resource_id, body)
            events_after_first = len(events)

            # Replay the same request 1 + n_extra additional times with the same key.
            for _ in range(1 + n_extra):
                status, resp = _execute_once(events, key, operation, resource_id, body)
                # Identical status and body on every replay (FR-020 AC-2).
                assert status == first_status
                assert resp == first_body
                # The side-effect count never increases after the first call
                # (FR-020 AC-5, AC-6).
                assert len(events) == events_after_first
        finally:
            layer.set_table(None)
