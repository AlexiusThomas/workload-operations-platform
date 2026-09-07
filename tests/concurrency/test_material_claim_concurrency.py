"""Concurrency test for the atomic MaterialRequirement claim (Task 17.2).

Fires N simultaneous material-claim requests at a single MATERIAL_REQUIRED requirement and
asserts exactly one succeeds (HTTP 200) and the remaining N-1 receive HTTP 409
MATERIAL_CLAIM_CONFLICT (FR-004 AC-2, NFR-005 AC-3).

The guarantee under test is the conditional ``TransactWriteItems`` in
:func:`backend.data.repositories.write_material_transition`: the canonical MaterialRequirement
Put is conditioned on ``status = MATERIAL_REQUIRED``, so only one runner can flip
MATERIAL_REQUIRED -> MATERIAL_CLAIMED. See the WorkUnit concurrency test for the moto
thread-safety note and the serialized-dispatch rationale — the conditional write is never
weakened; serializing the dispatch only makes the winner semantics deterministic to assert.
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
from backend.handlers import common, material
from backend.idempotency import layer as idempotency
from backend.observability import metrics

REGION = "us-east-1"
MAIN_TABLE = "wop-main-mconc"
AUDIT_TABLE = "wop-audit-mconc"
EVENTS_TABLE = "wop-events-mconc"
IDEMPOTENCY_TABLE = "wop-idempotency-mconc"

# Synthetic Material_Runner + Manager_Admin tokens permitted to material-claim.
RUNNER_TOKENS = ["token-runner-001", "token-admin-001"]


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


def _seed_material_requirement(material_req_id: str) -> None:
    canonical = {
        "pk": repo.mr_pk(material_req_id),
        "sk": repo.METADATA_SK,
        "material_req_id": material_req_id,
        "work_package_id": "wp-mconc",
        "work_unit_id": None,
        "material_type": "Fiber",
        "cable_length_m": 50.0,
        "qty_required": 4,
        "total_meters_required": 200.0,
        "qty_delivered": 0,
        "meters_delivered": 0,
        "runner_id": None,
        "status": "MATERIAL_REQUIRED",
        "created_at": "2024-01-14T10:00:00.000Z",
        "last_updated_at": "2024-01-14T10:00:00.000Z",
    }
    main = tables.main_table()
    main.put_item(Item=repo._to_dynamo(canonical))
    main.put_item(Item=repo._to_dynamo(repo.material_requirement_projection(canonical)))


def _claim_event(material_req_id: str, token: str) -> Dict[str, Any]:
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {token}"},
        "pathParameters": {"material_req_id": material_req_id, "action": "material-claim"},
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
            import_bucket="wop-import-mconc",
            archive_bucket="wop-archive-mconc",
            error_bucket="wop-error-mconc",
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


def _assert_exactly_one_winner(results: List[Dict[str, Any]], n: int, material_req_id: str) -> None:
    successes = [r for r in results if r["statusCode"] == 200]
    conflicts = [r for r in results if r["statusCode"] == 409]
    assert len(successes) == 1, f"expected exactly 1 success, got {len(successes)}"
    assert len(conflicts) == n - 1, f"expected {n - 1} conflicts, got {len(conflicts)}"
    for conflict in conflicts:
        assert json.loads(conflict["body"])["error"]["code"] == "MATERIAL_CLAIM_CONFLICT"

    final = repo.get_material_requirement(material_req_id)
    assert final is not None
    assert final["status"] == "MATERIAL_CLAIMED"
    winner_runner = json.loads(successes[0]["body"])["runner_id"]
    assert final["runner_id"] == winner_runner


def test_concurrent_material_claim_serialized_conditional(wired: Any) -> None:
    """Dispatch-serialized: the conditional write admits exactly one runner."""
    material_req_id = "mr-conc-1"
    _seed_material_requirement(material_req_id)
    n = 10

    results: List[Dict[str, Any]] = []
    for i in range(n):
        token = RUNNER_TOKENS[i % len(RUNNER_TOKENS)]
        results.append(material.handle(_claim_event(material_req_id, token)))

    _assert_exactly_one_winner(results, n, material_req_id)


def test_concurrent_material_claim_threads(wired: Any) -> None:
    """Best-effort true-thread dispatch; the invariant must still hold under contention."""
    material_req_id = "mr-conc-2"
    _seed_material_requirement(material_req_id)
    n = 10

    results: List[Dict[str, Any]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def _worker(idx: int) -> None:
        token = RUNNER_TOKENS[idx % len(RUNNER_TOKENS)]
        event = _claim_event(material_req_id, token)
        barrier.wait()
        resp = material.handle(event)
        with results_lock:
            results.append(resp)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    _assert_exactly_one_winner(results, n, material_req_id)
