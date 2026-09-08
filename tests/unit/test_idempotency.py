"""Unit tests for the idempotency layer: fingerprint conflict and IN_FLIGHT recovery.

Covers FR-020 AC-4: fingerprint match -> replay; mismatch -> 409 IDEMPOTENCY_CONFLICT;
and the deterministic 30-second IN_FLIGHT crash-recovery procedure (mark COMPLETED on
entity-state match, else delete orphaned record and re-execute).

Uses a moto-emulated DynamoDB table so tests are hermetic (no AWS credentials).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import pytest
from moto import mock_aws

from backend.idempotency import layer
from backend.observability.errors import IdempotencyConflictError

_TABLE = "wop-idempotency-table"


@pytest.fixture()
def idem_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table(_TABLE)
        layer.set_table(table)
        try:
            yield table
        finally:
            layer.set_table(None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_compute_fingerprint_is_order_independent() -> None:
    fp1 = layer.compute_fingerprint("op", "res-1", {"a": 1, "b": 2})
    fp2 = layer.compute_fingerprint("op", "res-1", {"b": 2, "a": 1})
    assert fp1 == fp2


def test_compute_fingerprint_differs_on_body() -> None:
    fp1 = layer.compute_fingerprint("op", "res-1", {"a": 1})
    fp2 = layer.compute_fingerprint("op", "res-1", {"a": 2})
    assert fp1 != fp2


def test_check_no_record_proceeds(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("op", "res", {"x": 1})
    result = layer.check_idempotency("key-1", fp)
    assert result.decision == layer.DECISION_PROCEED


def test_completed_matching_fingerprint_replays(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("verify", "wu-1", {"x": 1})
    layer.acquire_lock("key-2", "verify", "wu-1", fp)
    layer.store_idempotency_result("key-2", status_code=200, response_body=json.dumps({"ok": True}))

    result = layer.check_idempotency("key-2", fp)
    assert result.decision == layer.DECISION_REPLAY
    assert result.status_code == 200
    assert json.loads(result.response_body)["ok"] is True


def test_completed_mismatched_fingerprint_conflicts(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("verify", "wu-1", {"x": 1})
    layer.acquire_lock("key-3", "verify", "wu-1", fp)
    layer.store_idempotency_result("key-3", status_code=200, response_body="{}")

    other_fp = layer.compute_fingerprint("verify", "wu-1", {"x": 999})
    with pytest.raises(IdempotencyConflictError):
        layer.check_idempotency("key-3", other_fp)


def test_acquire_lock_second_attempt_fails(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("claim", "wu-9", {})
    token = layer.acquire_lock("key-4", "claim", "wu-9", fp)
    assert token is not None  # lock acquired -> a lock token is returned
    # A second concurrent request with the same key cannot re-acquire the lock.
    assert layer.acquire_lock("key-4", "claim", "wu-9", fp) is None


def test_check_in_flight_decision(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("claim", "wu-9", {})
    layer.acquire_lock("key-5", "claim", "wu-9", fp)
    result = layer.check_idempotency("key-5", fp)
    assert result.decision == layer.DECISION_IN_FLIGHT


def test_recover_fresh_in_flight_keeps_waiting(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("claim", "wu-9", {})
    layer.acquire_lock("key-6", "claim", "wu-9", fp)
    # created_at is "now"; not stale yet -> keep waiting.
    result = layer.recover_in_flight(
        "key-6",
        expected_state_matches=lambda: True,
        build_response=lambda: "{}",
    )
    assert result.decision == layer.DECISION_IN_FLIGHT


def _force_created_at(table: Any, key: str, created_at_iso: str) -> None:
    table.update_item(
        Key={"pk": f"IDMP#{key}"},
        UpdateExpression="SET created_at = :c",
        ExpressionAttributeValues={":c": created_at_iso},
    )


def test_recover_stale_in_flight_entity_match_marks_completed(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("claim", "wu-9", {})
    layer.acquire_lock("key-7", "claim", "wu-9", fp)
    stale = datetime.now(timezone.utc) - timedelta(seconds=60)
    _force_created_at(idem_table, "key-7", _iso(stale))

    result = layer.recover_in_flight(
        "key-7",
        expected_state_matches=lambda: True,  # entity committed
        build_response=lambda: json.dumps({"recovered": True}),
        status_code=200,
    )
    assert result.decision == layer.DECISION_REPLAY
    assert json.loads(result.response_body)["recovered"] is True

    # Record is now COMPLETED and will replay on subsequent checks.
    check = layer.check_idempotency("key-7", fp)
    assert check.decision == layer.DECISION_REPLAY


def test_recover_stale_in_flight_entity_mismatch_deletes_and_proceeds(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("claim", "wu-9", {})
    layer.acquire_lock("key-8", "claim", "wu-9", fp)
    stale = datetime.now(timezone.utc) - timedelta(seconds=60)
    _force_created_at(idem_table, "key-8", _iso(stale))

    result = layer.recover_in_flight(
        "key-8",
        expected_state_matches=lambda: False,  # entity did NOT commit
        build_response=lambda: "{}",
    )
    assert result.decision == layer.DECISION_PROCEED

    # The orphaned record was deleted; a fresh check proceeds, and the lock is re-acquirable.
    assert layer.check_idempotency("key-8", fp).decision == layer.DECISION_PROCEED
    assert layer.acquire_lock("key-8", "claim", "wu-9", fp) is not None


def test_store_result_sets_ttl_at_least_24h(idem_table: Any) -> None:
    fp = layer.compute_fingerprint("op", "res", {})
    layer.acquire_lock("key-9", "op", "res", fp)
    before = int(time.time())
    layer.store_idempotency_result("key-9", status_code=200, response_body="{}")
    item = idem_table.get_item(Key={"pk": "IDMP#key-9"})["Item"]
    assert int(item["ttl"]) >= before + layer.DEFAULT_TTL_SECONDS


# --------------------------------------------------------------- Task 23 issue 8 (idempotency)


def test_in_flight_different_fingerprint_is_conflict(idem_table: Any) -> None:
    """8A: an IN_FLIGHT record with a DIFFERENT fingerprint is a key-reuse mismatch (409),
    not an ordinary duplicate."""
    fp = layer.compute_fingerprint("claim", "wu-1", {"x": 1})
    layer.acquire_lock("key-8a", "claim", "wu-1", fp)  # still IN_FLIGHT (no store)
    other_fp = layer.compute_fingerprint("claim", "wu-1", {"x": 2})
    with pytest.raises(IdempotencyConflictError):
        layer.check_idempotency("key-8a", other_fp)
    # Same fingerprint while in-flight is still an ordinary in-flight (not a conflict).
    assert layer.check_idempotency("key-8a", fp).decision == layer.DECISION_IN_FLIGHT


def test_stale_execution_cannot_complete_newer_lock(idem_table: Any) -> None:
    """8B: a stale execution cannot overwrite a newer lock's record.

    Owner A acquires the lock (token A). The record is then re-acquired by owner B (after A's
    record is cleared), producing token B. A late store from A (token A) must NOT clobber B's
    IN_FLIGHT record; only B's token may complete it."""
    fp = layer.compute_fingerprint("verify", "wu-2", {})
    token_a = layer.acquire_lock("key-8b", "verify", "wu-2", fp)
    assert token_a is not None

    # Simulate A's lock being recovered/freed and B acquiring a fresh lock.
    layer.delete_orphaned_in_flight("key-8b")
    token_b = layer.acquire_lock("key-8b", "verify", "wu-2", fp)
    assert token_b is not None and token_b != token_a

    # Stale A tries to complete using its old token -> no-op (condition fails, swallowed).
    layer.store_idempotency_result("key-8b", 200, '{"owner": "A"}', lock_token=token_a)
    # The record is still IN_FLIGHT (B has not completed) - A did not clobber it.
    assert layer.check_idempotency("key-8b", fp).decision == layer.DECISION_IN_FLIGHT

    # B completes with its own token -> success, and the stored body is B's.
    layer.store_idempotency_result("key-8b", 200, '{"owner": "B"}', lock_token=token_b)
    replay = layer.check_idempotency("key-8b", fp)
    assert replay.decision == layer.DECISION_REPLAY
    assert json.loads(replay.response_body)["owner"] == "B"
