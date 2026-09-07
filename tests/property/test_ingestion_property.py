"""Property-based test for ingestion idempotency (Correctness Property 3).

Ingesting the same workload payload N times produces exactly the same set of WorkPackage
and WorkUnit records (identical counts and attributes) as ingesting it once.

Runs against moto-emulated DynamoDB + S3. The moto context and hermetic table/bucket wiring
are established per-example so Hypothesis can re-run the body freely.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import boto3
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from backend.data import config, tables
from backend.handlers import ingestion
from backend.idempotency import layer as idempotency

REGION = "us-east-1"
MAIN_TABLE = "wop-main-table-prop"
AUDIT_TABLE = "wop-audit-table-prop"
EVENTS_TABLE = "wop-events-table-prop"
IDEMPOTENCY_TABLE = "wop-idempotency-table-prop"
IMPORT_BUCKET = "wop-import-prop"
ARCHIVE_BUCKET = "wop-archive-prop"
ERROR_BUCKET = "wop-error-prop"


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


def _payload(package_count: int, units_per_package: int) -> Dict[str, Any]:
    packages: List[Dict[str, Any]] = []
    for p in range(package_count):
        units = [
            {"work_unit_id": f"wu-{p}-{u}", "required_qty": u + 1, "work_type": "Fiber"}
            for u in range(units_per_package)
        ]
        packages.append(
            {
                "work_package_id": f"wp-{p}",
                "site": "SITE-A",
                "rack_position": "RACK-001",
                "work_type": "Fiber",
                "scheduled_date": "2024-01-15",
                "work_units": units,
            }
        )
    return {"work_packages": packages}


def _snapshot() -> Dict[str, Any]:
    """Capture WorkPackage + WorkUnit canonical items keyed by id for comparison."""
    main = tables.main_table()
    items = main.scan().get("Items", [])
    work_packages: Dict[str, Any] = {}
    work_units: Dict[str, Any] = {}
    for item in items:
        if item.get("sk") != "METADATA":
            continue
        pk = item["pk"]
        if pk.startswith("WP#"):
            work_packages[item["work_package_id"]] = {
                "state": item.get("state"),
                "committed": item.get("committed"),
                "current_scheduled_date": item.get("current_scheduled_date"),
            }
        elif pk.startswith("WU#"):
            work_units[item["work_unit_id"]] = {
                "state": item.get("state"),
                "required_qty": item.get("required_qty"),
                "work_package_id": item.get("work_package_id"),
            }
    return {"work_packages": work_packages, "work_units": work_units}


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    package_count=st.integers(min_value=1, max_value=3),
    units_per_package=st.integers(min_value=1, max_value=4),
    ingest_times=st.integers(min_value=1, max_value=4),
)
def test_ingestion_idempotency(
    package_count: int, units_per_package: int, ingest_times: int
) -> None:
    # Feature: workload-operations-platform, Property 3: Ingestion Idempotency
    # Validates: Requirements FR-001 AC-5, FR-020 AC-7
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _create_tables(ddb)
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (IMPORT_BUCKET, ARCHIVE_BUCKET, ERROR_BUCKET):
            s3.create_bucket(Bucket=bucket)

        data_config = config.DataConfig(
            main_table=MAIN_TABLE,
            audit_table=AUDIT_TABLE,
            events_table=EVENTS_TABLE,
            idempotency_table=IDEMPOTENCY_TABLE,
            import_bucket=IMPORT_BUCKET,
            archive_bucket=ARCHIVE_BUCKET,
            error_bucket=ERROR_BUCKET,
        )
        tables.configure(resource=ddb, data_config=data_config)
        idempotency.set_table(tables.idempotency_table())
        try:
            key = "workload/file-001.json"
            body = json.dumps(_payload(package_count, units_per_package))

            snapshot_after_one = None
            for attempt in range(ingest_times):
                # Re-create the object each attempt (archived/moved on success) so the S3
                # get_object succeeds; idempotency on the S3 key makes repeats no-ops.
                s3.put_object(Bucket=IMPORT_BUCKET, Key=key, Body=body.encode("utf-8"))
                event = {
                    "Records": [{"s3": {"bucket": {"name": IMPORT_BUCKET}, "object": {"key": key}}}]
                }
                ingestion.handle(
                    event, parser=ingestion.SyntheticJsonWorkloadParser(), s3_client=s3
                )
                if attempt == 0:
                    snapshot_after_one = _snapshot()

            snapshot_after_n = _snapshot()

            assert snapshot_after_one == snapshot_after_n
            assert len(snapshot_after_n["work_packages"]) == package_count
            assert len(snapshot_after_n["work_units"]) == package_count * units_per_package
            for wp in snapshot_after_n["work_packages"].values():
                assert wp["state"] == "AVAILABLE"
                assert wp["committed"] is True
            for wu in snapshot_after_n["work_units"].values():
                assert wu["state"] == "AVAILABLE"
        finally:
            tables.configure(resource=None, data_config=None)
            idempotency.set_table(None)
