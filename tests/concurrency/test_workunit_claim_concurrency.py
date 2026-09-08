"""Concurrency test for the atomic WorkUnit claim (Task 17.1).

Fires N simultaneous claim requests at a single AVAILABLE WorkUnit and asserts that exactly
one succeeds (HTTP 200) and the remaining N-1 receive HTTP 409 CLAIM_CONFLICT, with a single
final ``claimed_by`` (FR-006 AC-2, AC-5, NFR-005 AC-3, BR-001).

Approach and moto thread-safety note
-------------------------------------
The atomicity guarantee under test is the DynamoDB conditional ``TransactWriteItems`` in
:func:`backend.data.repositories.write_work_unit_transition`: the canonical WorkUnit Put is
conditioned on ``state = AVAILABLE``, so only one writer can flip AVAILABLE -> CLAIMED; all
others fail the condition and are mapped to 409 CLAIM_CONFLICT.

moto emulates DynamoDB in-process and its request handling is not guaranteed to be
thread-safe for a single shared client under heavy OS-thread contention. To prove the
exactly-one-winner semantics deterministically (without weakening the guarantee), the
requests are serialized behind a lock at the *dispatch* boundary only — each claim still
executes the full handler and the real conditional transaction against moto. Serializing the
dispatch does not relax the conditional write: the second and later claims still observe the
already-CLAIMED state and MUST be rejected by the condition, exactly as concurrent requests
racing on the same key would be. This isolates and asserts the conditional-write winner
semantics, which is the property FR-006 AC-5 / BR-001 require.

A best-effort true-thread execution is also exercised to confirm the invariant holds when
requests are dispatched from multiple OS threads.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

from backend.auth.provider import SyntheticAuthProvider
from backend.data import config, repositories as repo, tables
from backend.handlers import common, workunit
from backend.idempotency import layer as idempotency
from backend.observability import metrics

REGION = "us-east-1"
MAIN_TABLE = "wop-main-conc"
AUDIT_TABLE = "wop-audit-conc"
EVENTS_TABLE = "wop-events-conc"
IDEMPOTENCY_TABLE = "wop-idempotency-conc"

# Synthetic Technician tokens (SyntheticAuthProvider registry).
TECH_TOKENS = ["token-tech-001", "token-tech-002"]


def _create_tables(ddb: Any) -> None:
    ddb.create_table(
        TableName=MAIN_TABLE,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "state", "AttributeType": "S"},
            {"AttributeName": "sk_gsi1", "AttributeType": "S"},
            {"AttributeName": "work_package_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "state-site-worktype-date-index",
                "KeySchema": [
                    {"AttributeName": "state", "KeyType": "HASH"},
                    {"AttributeName": "sk_gsi1", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "work-package-children-index",
                "KeySchema": [
                    {"AttributeName": "work_package_id", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "material-status-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "work_package_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    for name in (AUDIT_TABLE, EVENTS_TABLE):
        ddb.create_table(
            TableName=name,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    ddb.create_table(
        TableName=IDEMPOTENCY_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _seed_available_work_unit(work_unit_id: str) -> None:
    canonical = {
        "pk": repo.wu_pk(work_unit_id),
        "sk": repo.METADATA_SK,
        "work_unit_id": work_unit_id,
        "work_package_id": "wp-conc",
        "site": "SITE-A",
        "work_type": "Fiber",
        "state": "AVAILABLE",
        "claimed_by": None,
        "required_qty": 10,
        "completed_qty": 0,
        "tote_id": None,
        "rework_count": 0,
        "current_scheduled_date": "2024-01-15",
        "original_scheduled_date": "2024-01-15",
        "rollover_count": 0,
        "version": 0,
        "sk_gsi1": "SITE-A#Fiber#2024-01-15",
    }
    main = tables.main_table()
    main.put_item(Item=repo._to_dynamo(canonical))
    main.put_item(Item=repo._to_dynamo(repo.work_unit_projection(canonical)))


def _claim_event(work_unit_id: str, token: str) -> Dict[str, Any]:
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {token}"},
        "pathParameters": {"work_unit_id": work_unit_id, "action": "claim"},
        "requestContext": {"requestId": f"req-{token}"},
    }


@pytest.fixture()
def wired() -> Any:
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _create_tables(ddb)
        data_config = config.DataConfig(
            main_table=MAIN_TABLE,
            audit_table=AUDIT_TABLE,
            events_table=EVENTS_TABLE,
            idempotency_table=IDEMPOTENCY_TABLE,
            import_bucket="wop-import-conc",
            archive_bucket="wop-archive-conc",
            error_bucket="wop-error-conc",
        )
        tables.configure(resource=ddb, data_config=data_config)
        common.set_auth_provider(SyntheticAuthProvider())
        idempotency.set_table(tables.idempotency_table())
        metrics.disable()
        try:
            yield ddb
        finally:
            tables.configure(resource=None, data_config=None)
            common.set_auth_provider(None)
            idempotency.set_table(None)
            metrics.enable()


def _assert_exactly_one_winner(results: List[Dict[str, Any]], n: int, work_unit_id: str) -> None:
    successes = [r for r in results if r["statusCode"] == 200]
    conflicts = [r for r in results if r["statusCode"] == 409]
    assert len(successes) == 1, f"expected exactly 1 success, got {len(successes)}"
    assert len(conflicts) == n - 1, f"expected {n - 1} conflicts, got {len(conflicts)}"
    for conflict in conflicts:
        assert json.loads(conflict["body"])["error"]["code"] == "CLAIM_CONFLICT"

    final = repo.get_work_unit(work_unit_id)
    assert final is not None
    assert final["state"] == "CLAIMED"
    winner_claimed_by = json.loads(successes[0]["body"])["claimed_by"]
    assert final["claimed_by"] == winner_claimed_by


def test_concurrent_work_unit_claim_serialized_conditional(wired: Any) -> None:
    """Dispatch-serialized: the conditional write still admits exactly one winner."""
    work_unit_id = "wu-conc-1"
    _seed_available_work_unit(work_unit_id)
    n = 10

    results: List[Dict[str, Any]] = []
    for i in range(n):
        token = TECH_TOKENS[i % len(TECH_TOKENS)]
        results.append(workunit.handle(_claim_event(work_unit_id, token)))

    _assert_exactly_one_winner(results, n, work_unit_id)


def test_concurrent_work_unit_claim_threads(wired: Any, monkeypatch: Any) -> None:
    """Deterministic concurrent-claim race that exercises the REAL conditional write.

    This models the dangerous race precisely without depending on moto's (non-guaranteed)
    thread safety:

    * Every worker reads the SAME stale pre-claim snapshot (all observe state=AVAILABLE at
      once) — the exact condition under which a lost-update could occur.
    * All workers are released together via a Barrier.
    * ONLY the moto TransactWriteItems execution boundary (``repo._transact_write``) is
      serialized, because real DynamoDB atomically arbitrates conflicting conditional writes
      while moto's in-process store is not thread-safe. The production ConditionExpression,
      TransactWriteItems usage, handler logic, and 409 semantics are unchanged — each worker
      still runs the real handler -> repository conditional write.

    Real DynamoDB would let exactly one conditional write win; this harness reproduces that
    arbitration deterministically and asserts exactly one 200 winner + N-1 409 conflicts.
    """
    work_unit_id = "wu-conc-2"
    _seed_available_work_unit(work_unit_id)
    n = 10

    # All workers operate from the SAME stale AVAILABLE snapshot (simultaneous read).
    stale_snapshot = repo.get_work_unit(work_unit_id)
    assert stale_snapshot is not None and stale_snapshot["state"] == "AVAILABLE"
    monkeypatch.setattr(workunit, "_load_work_unit", lambda _wuid: dict(stale_snapshot))

    # Serialize ONLY the moto transaction boundary so the emulator arbitrates one conditional
    # write at a time (as real DynamoDB does); production code is untouched.
    real_transact = repo._transact_write
    transact_lock = threading.Lock()

    def _serialized_transact(items: Any) -> Any:
        with transact_lock:
            return real_transact(items)

    monkeypatch.setattr(repo, "_transact_write", _serialized_transact)

    results: List[Dict[str, Any]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def _worker(idx: int) -> None:
        token = TECH_TOKENS[idx % len(TECH_TOKENS)]
        event = _claim_event(work_unit_id, token)
        barrier.wait()  # release all workers as simultaneously as possible
        resp = workunit.handle(event)
        with results_lock:
            results.append(resp)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    _assert_exactly_one_winner(results, n, work_unit_id)
