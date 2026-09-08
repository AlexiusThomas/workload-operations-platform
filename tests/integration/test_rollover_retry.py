"""Rollover crash/retry safety test (Task 23, issue 7).

A rollover cycle that fails after processing only some units must, on retry, finish the
remaining eligible units exactly once without re-advancing already-processed units. Anchored
to the immutable cycle key: each successful unit records last_rollover_cycle, so a retry
skips it (FR-022 AC-4/AC-5/AC-6/AC-8, FR-020 AC-8, BR-015).
"""

from __future__ import annotations

from typing import Any, Dict

import boto3
import pytest
from moto import mock_aws

from backend.auth.provider import SyntheticAuthProvider
from backend.data import config, repositories as repo, tables
from backend.handlers import common, rollover
from backend.idempotency import layer as idempotency
from backend.observability import metrics

REGION = "us-east-1"
MAIN = "wop-main-roll"
AUDIT = "wop-audit-roll"
EVENTS = "wop-events-roll"
IDEM = "wop-idem-roll"

SCHEDULED = "2024-03-04"
NEXT_DAY = "2024-03-05"
EVENT = {"time": "2024-03-05T06:00:00Z"}  # cycle date 2024-03-05


def _create_tables(ddb: Any) -> None:
    ddb.create_table(
        TableName=MAIN,
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
    for name in (AUDIT, EVENTS):
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
        TableName=IDEM,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _seed(work_unit_id: str, completed_qty: int = 3) -> None:
    canonical = {
        "pk": repo.wu_pk(work_unit_id),
        "sk": repo.METADATA_SK,
        "work_unit_id": work_unit_id,
        "work_package_id": f"wp-{work_unit_id}",
        "site": "SITE-A",
        "work_type": "Fiber",
        "state": "AVAILABLE",
        "claimed_by": None,
        "required_qty": 10,
        "completed_qty": completed_qty,
        "tote_id": None,
        "rework_count": 0,
        "current_scheduled_date": SCHEDULED,
        "original_scheduled_date": SCHEDULED,
        "rollover_count": 0,
        "version": 0,
        "sk_gsi1": f"SITE-A#Fiber#{SCHEDULED}",
    }
    main = tables.main_table()
    main.put_item(Item=repo._to_dynamo(canonical))
    main.put_item(Item=repo._to_dynamo(repo.work_unit_projection(canonical)))


@pytest.fixture()
def wired() -> Any:
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _create_tables(ddb)
        tables.configure(
            resource=ddb,
            data_config=config.DataConfig(
                main_table=MAIN,
                audit_table=AUDIT,
                events_table=EVENTS,
                idempotency_table=IDEM,
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


def test_rollover_partial_failure_then_retry_finishes_exactly_once(
    wired: Any, monkeypatch: Any
) -> None:
    for i in range(4):
        _seed(f"wu-{i}", completed_qty=i)  # varied completed_qty to prove preservation

    # First attempt fails after successfully rolling exactly 2 units.
    real_roll_one = rollover._roll_one
    calls = {"n": 0}

    def flaky_roll_one(work_unit: Dict[str, Any], advancer: Any, cycle_key: str, cid: str) -> bool:
        if calls["n"] >= 2:
            raise RuntimeError("injected mid-cycle failure")
        calls["n"] += 1
        return real_roll_one(work_unit, advancer, cycle_key, cid)

    monkeypatch.setattr(rollover, "_roll_one", flaky_roll_one)
    with pytest.raises(RuntimeError):
        rollover.handle(EVENT, advancer=rollover.advance_scheduled_date)

    # Exactly 2 units advanced so far; the rest remain on the original date.
    advanced = [
        u
        for i in range(4)
        if (u := repo.get_work_unit(f"wu-{i}"))["current_scheduled_date"] == NEXT_DAY
    ]
    assert len(advanced) == 2
    for u in advanced:
        assert int(u["rollover_count"]) == 1

    # Retry the SAME cycle with the real _roll_one. The stale IN_FLIGHT lock is recovered and
    # the remaining eligible units are finished; already-advanced units are NOT re-advanced.
    monkeypatch.setattr(rollover, "_roll_one", real_roll_one)
    # Make the orphaned IN_FLIGHT lock look stale so recovery proceeds deterministically.
    monkeypatch.setattr(idempotency, "_is_stale", lambda *_a, **_k: True)
    rollover.handle(EVENT, advancer=rollover.advance_scheduled_date)

    # Every unit is advanced exactly once now: date = NEXT_DAY, rollover_count == 1,
    # completed_qty and original_scheduled_date preserved.
    for i in range(4):
        u = repo.get_work_unit(f"wu-{i}")
        assert u["current_scheduled_date"] == NEXT_DAY
        assert int(u["rollover_count"]) == 1, f"wu-{i} advanced more than once"
        assert int(u["completed_qty"]) == i
        assert u["original_scheduled_date"] == SCHEDULED
        assert u["state"] == "AVAILABLE"
