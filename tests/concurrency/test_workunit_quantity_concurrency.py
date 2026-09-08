"""Concurrency test for the WorkUnit quantity lost-update guard (Task 23, issue 6).

Two concurrent same-owner quantity updates that both read the same prior ``completed_qty``
must not overwrite each other with a stale value or move ``completed_qty`` backward. The
guard is the conditional write in the quantity path: the persisted ``completed_qty`` must
still equal the value the request read (``:prev_qty``); a stale second write fails the
condition and is surfaced as a 409 CLAIM_CONFLICT (FR-009, NFR-005 AC-3).
"""

from __future__ import annotations

import json
from typing import Any, Dict

import boto3
import pytest
from moto import mock_aws

from backend.auth.provider import SyntheticAuthProvider
from backend.data import config, repositories as repo, tables
from backend.handlers import common, workunit
from backend.idempotency import layer as idempotency
from backend.observability import metrics

REGION = "us-east-1"
MAIN_TABLE = "wop-main-qty"
AUDIT_TABLE = "wop-audit-qty"
EVENTS_TABLE = "wop-events-qty"
IDEMPOTENCY_TABLE = "wop-idempotency-qty"

OWNER = "token-tech-001"  # resolves to TECH-001
OWNER_ID = "TECH-001"


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


def _seed_in_progress(work_unit_id: str, completed_qty: int = 0) -> None:
    canonical = {
        "pk": repo.wu_pk(work_unit_id),
        "sk": repo.METADATA_SK,
        "work_unit_id": work_unit_id,
        "work_package_id": "wp-qty",
        "site": "SITE-A",
        "work_type": "Fiber",
        "state": "IN_PROGRESS",
        "claimed_by": OWNER_ID,
        "required_qty": 10,
        "completed_qty": completed_qty,
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


def _qty_event(work_unit_id: str, new_qty: int) -> Dict[str, Any]:
    return {
        "httpMethod": "PATCH",
        "headers": {"Authorization": f"Bearer {OWNER}"},
        "pathParameters": {"work_unit_id": work_unit_id, "action": "quantity"},
        "body": json.dumps({"completed_qty": new_qty}),
        "requestContext": {"requestId": "req"},
    }


@pytest.fixture()
def wired() -> Any:
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _create_tables(ddb)
        tables.configure(
            resource=ddb,
            data_config=config.DataConfig(
                main_table=MAIN_TABLE,
                audit_table=AUDIT_TABLE,
                events_table=EVENTS_TABLE,
                idempotency_table=IDEMPOTENCY_TABLE,
                import_bucket="i",
                archive_bucket="a",
                error_bucket="e",
            ),
        )
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


def test_stale_same_owner_quantity_write_is_rejected(wired: Any, monkeypatch: Any) -> None:
    """Two updates that both read completed_qty=0: the first (0->8) wins; the stale
    second (0->3, built from the same prior snapshot) is rejected and does not move the
    persisted value backward to 3."""
    work_unit_id = "wu-qty-1"
    _seed_in_progress(work_unit_id, completed_qty=0)

    # Snapshot both requests "read" concurrently (completed_qty=0).
    stale_snapshot = repo.get_work_unit(work_unit_id)

    # First update commits 0 -> 8 normally.
    first = workunit.handle(_qty_event(work_unit_id, 8))
    assert first["statusCode"] == 200
    assert json.loads(first["body"])["completed_qty"] == 8

    # Second update raced on the SAME prior snapshot (completed_qty=0). Force the handler to
    # observe the stale snapshot so :prev_qty=0, but the persisted value is now 8.
    monkeypatch.setattr(workunit, "_load_work_unit", lambda _wuid: dict(stale_snapshot))
    second = workunit.handle(_qty_event(work_unit_id, 3))
    assert second["statusCode"] == 409
    assert json.loads(second["body"])["error"]["code"] == "CLAIM_CONFLICT"

    # The accepted update is preserved; the stale write never moved it backward to 3.
    final = repo.get_work_unit(work_unit_id)
    assert final is not None
    assert int(final["completed_qty"]) == 8


def test_sequential_quantity_updates_still_succeed(wired: Any) -> None:
    """Regression: non-stale sequential updates (each reading the latest) still succeed."""
    work_unit_id = "wu-qty-2"
    _seed_in_progress(work_unit_id, completed_qty=0)
    assert workunit.handle(_qty_event(work_unit_id, 4))["statusCode"] == 200
    assert workunit.handle(_qty_event(work_unit_id, 7))["statusCode"] == 200
    final = repo.get_work_unit(work_unit_id)
    assert int(final["completed_qty"]) == 7
