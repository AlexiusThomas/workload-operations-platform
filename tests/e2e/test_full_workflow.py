"""End-to-end synthetic workflow test (Task 18.1, NFR-005 AC-6).

Exercises the full WOP workflow against moto-emulated DynamoDB + S3 using only synthetic
identifiers (SITE-A, TECH-001, RUNNER-001, VERIFY-001, ADMIN-001) and the
:class:`SyntheticAuthProvider`:

    ingestion
      -> material requirement calculation
      -> material claim / delivery (MaterialEvent)
      -> technician claim
      -> partial work + quantity update
      -> prep completion
      -> labeling
      -> tote assignment
      -> ready-to-verify
      -> fail-verify / rework (completed_qty preserved)
      -> re-progress
      -> verification (ProductionEvent)
      -> final completion
      -> weekly report
      -> eligible next-day rollover

Finally the weekly report is asserted to reconcile EXACTLY to the immutable ProductionEvents
and MaterialEvents produced during the run (FR-024 AC-4, BR-016).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import boto3
import pytest
from moto import mock_aws

from backend.auth.provider import SyntheticAuthProvider
from backend.data import config, repositories as repo, tables
from backend.handlers import common, ingestion, material, report, rollover, workunit
from backend.idempotency import layer as idempotency
from backend.observability import metrics

REGION = "us-east-1"
MAIN_TABLE = "wop-main-e2e"
AUDIT_TABLE = "wop-audit-e2e"
EVENTS_TABLE = "wop-events-e2e"
IDEMPOTENCY_TABLE = "wop-idempotency-e2e"
IMPORT_BUCKET = "wop-import-e2e"
ARCHIVE_BUCKET = "wop-archive-e2e"
ERROR_BUCKET = "wop-error-e2e"

ADMIN = "token-admin-001"
RUNNER = "token-runner-001"
TECH = "token-tech-001"
LEAD = "token-verify-001"

SCHEDULED_DATE = "2024-01-15"


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
    for name in (AUDIT_TABLE,):
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
        TableName=EVENTS_TABLE,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "event_type#week_key", "AttributeType": "S"},
            {"AttributeName": "timestamp", "AttributeType": "S"},
            {"AttributeName": "technician_id", "AttributeType": "S"},
            {"AttributeName": "runner_id", "AttributeType": "S"},
            {"AttributeName": "week_key#timestamp", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "production-week-index",
                "KeySchema": [
                    {"AttributeName": "event_type#week_key", "KeyType": "HASH"},
                    {"AttributeName": "timestamp", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "material-week-index",
                "KeySchema": [
                    {"AttributeName": "event_type#week_key", "KeyType": "HASH"},
                    {"AttributeName": "timestamp", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "technician-week-index",
                "KeySchema": [
                    {"AttributeName": "technician_id", "KeyType": "HASH"},
                    {"AttributeName": "week_key#timestamp", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "runner-week-index",
                "KeySchema": [
                    {"AttributeName": "runner_id", "KeyType": "HASH"},
                    {"AttributeName": "week_key#timestamp", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    ddb.create_table(
        TableName=IDEMPOTENCY_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture()
def wired() -> Any:
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
        common.set_auth_provider(SyntheticAuthProvider())
        idempotency.set_table(tables.idempotency_table())
        metrics.disable()
        try:
            yield s3
        finally:
            tables.configure(resource=None, data_config=None)
            common.set_auth_provider(None)
            idempotency.set_table(None)
            metrics.enable()


# ---------------------------------------------------------------------------
# Event/request builders
# ---------------------------------------------------------------------------
def _wu_event(
    work_unit_id: str,
    action: str,
    token: str,
    body: Optional[Dict[str, Any]] = None,
    method: str = "POST",
) -> Dict[str, Any]:
    return {
        "httpMethod": method,
        "headers": {"Authorization": f"Bearer {token}"},
        "pathParameters": {"work_unit_id": work_unit_id, "action": action},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"requestId": f"req-{action}"},
    }


def _mr_event(
    material_req_id: str, action: str, token: str, body: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {token}"},
        "pathParameters": {"material_req_id": material_req_id, "action": action},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"requestId": f"req-{action}"},
    }


def _mr_create_event(work_package_id: str, token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {token}"},
        "pathParameters": {"work_package_id": work_package_id},
        "body": json.dumps(body),
        "requestContext": {"requestId": "req-mr-create"},
    }


def _ok(resp: Dict[str, Any], expected: int = 200) -> Dict[str, Any]:
    assert (
        resp["statusCode"] == expected
    ), f"expected {expected}, got {resp['statusCode']}: {resp['body']}"
    return json.loads(resp["body"])


def _find_work_unit_id(work_package_id: str) -> str:
    units = repo.list_work_units_by_package(work_package_id)
    assert units, "ingestion produced no work units"
    return str(units[0]["work_unit_id"])


# ---------------------------------------------------------------------------
# The full workflow
# ---------------------------------------------------------------------------
def test_full_synthetic_workflow(wired: Any) -> None:
    s3 = wired

    # 1) Ingestion — drop a synthetic workload file and process it.
    work_package_id = "WP-E2E-001"
    payload = {
        "work_packages": [
            {
                "work_package_id": work_package_id,
                "site": "SITE-A",
                "rack_position": "RACK-001",
                "work_type": "Fiber",
                "scheduled_date": SCHEDULED_DATE,
                "work_units": [
                    {"work_unit_id": "WU-E2E-001", "required_qty": 10, "work_type": "Fiber"}
                ],
            }
        ]
    }
    s3.put_object(Bucket=IMPORT_BUCKET, Key="workload/e2e.json", Body=json.dumps(payload).encode())
    ingestion.handle(
        {
            "Records": [
                {"s3": {"bucket": {"name": IMPORT_BUCKET}, "object": {"key": "workload/e2e.json"}}}
            ]
        },
        parser=ingestion.SyntheticJsonWorkloadParser(),
        s3_client=s3,
    )
    work_unit_id = _find_work_unit_id(work_package_id)
    unit = repo.get_work_unit(work_unit_id)
    assert unit is not None and unit["state"] == "AVAILABLE"
    wp = repo.get_work_package(work_package_id)
    assert wp is not None and wp["committed"] is True

    # 2) Material requirement calculation (ADMIN creates a Fiber requirement).
    mr = _ok(
        material.handle(
            _mr_create_event(
                work_package_id,
                ADMIN,
                {
                    "material_type": "Fiber",
                    "cable_length_m": 50.0,
                    "qty_required": 4,
                },
            )
        ),
        expected=201,
    )
    material_req_id = mr["material_req_id"]
    assert float(mr["total_meters_required"]) == 200.0  # 50 * 4 (BR-017)

    # 3) Material claim + delivery (RUNNER-001) -> MaterialEvent + READY_FOR_PREP.
    _ok(material.handle(_mr_event(material_req_id, "material-claim", RUNNER)))
    delivered = _ok(
        material.handle(
            _mr_event(
                material_req_id,
                "material-deliver",
                RUNNER,
                {"qty_delivered": 4, "meters_delivered": 200.0},
            )
        )
    )
    assert delivered["material_requirement"]["status"] == "READY_FOR_PREP"

    # 4) Technician claim (AVAILABLE -> CLAIMED).
    claimed = _ok(workunit.handle(_wu_event(work_unit_id, "claim", TECH)))
    assert claimed["state"] == "CLAIMED" and claimed["claimed_by"] == "TECH-001"

    # 5) Start + partial quantity update (preserved across rework later).
    _ok(workunit.handle(_wu_event(work_unit_id, "start", TECH)))
    partial = _ok(
        workunit.handle(
            _wu_event(work_unit_id, "quantity", TECH, {"completed_qty": 4}, method="PATCH")
        )
    )
    assert int(partial["completed_qty"]) == 4

    # 6) Complete quantity then prep-complete.
    _ok(
        workunit.handle(
            _wu_event(work_unit_id, "quantity", TECH, {"completed_qty": 10}, method="PATCH")
        )
    )
    _ok(workunit.handle(_wu_event(work_unit_id, "prep-complete", TECH)))

    # 7) Labeling -> tote assignment -> ready-to-verify.
    _ok(workunit.handle(_wu_event(work_unit_id, "label", TECH)))
    _ok(workunit.handle(_wu_event(work_unit_id, "tote-assign", TECH, {"tote_id": "TOTE-9921"})))
    _ok(workunit.handle(_wu_event(work_unit_id, "ready-to-verify", TECH)))

    # 8) Fail-verify (rework) — completed_qty preserved, rework_count incremented.
    _ok(
        workunit.handle(
            _wu_event(work_unit_id, "fail-verify", LEAD, {"failure_reason": "Incorrect routing"})
        )
    )
    reworked = repo.get_work_unit(work_unit_id)
    assert reworked is not None
    assert reworked["state"] == "REWORK_REQUIRED"
    assert int(reworked["completed_qty"]) == 10  # preserved (BR-003, BR-010)
    assert int(reworked["rework_count"]) == 1

    # 9) Re-claim rework (REWORK_REQUIRED -> IN_PROGRESS), re-progress through the pipeline.
    reclaimed = _ok(workunit.handle(_wu_event(work_unit_id, "claim", TECH)))
    assert reclaimed["state"] == "IN_PROGRESS"
    assert int(reclaimed["completed_qty"]) == 10  # preserved across rework
    _ok(workunit.handle(_wu_event(work_unit_id, "prep-complete", TECH)))
    _ok(workunit.handle(_wu_event(work_unit_id, "label", TECH)))
    _ok(workunit.handle(_wu_event(work_unit_id, "tote-assign", TECH, {"tote_id": "TOTE-9921"})))
    _ok(workunit.handle(_wu_event(work_unit_id, "ready-to-verify", TECH)))

    # 10) Verification (READY_TO_VERIFY -> VERIFIED) -> ProductionEvent.
    verified = _ok(workunit.handle(_wu_event(work_unit_id, "verify", LEAD)))
    assert verified["state"] == "VERIFIED"
    assert "production_event_id" in verified

    # 11) Final completion (VERIFIED -> COMPLETE), excluded from active queue.
    completed = _ok(workunit.handle(_wu_event(work_unit_id, "complete", LEAD)))
    assert completed["state"] == "COMPLETE"

    # ---- Immutable events actually written (source of truth for reconciliation) ----
    # ProductionEvent/MaterialEvent week_key is derived from the server-side event timestamp
    # (verification/delivery time), so read the actual week_key from the persisted events
    # rather than assuming the scheduled date's week.
    all_events = _scan_events()
    production_events = [e for e in all_events if e["pk"].startswith("PE#")]
    material_events = [e for e in all_events if e["pk"].startswith("ME#")]
    assert len(production_events) == 1  # exactly one, only on verification (BR-007)
    assert len(material_events) == 1
    event_week_key = str(production_events[0]["week_key"])
    assert str(material_events[0]["week_key"]) == event_week_key

    # 12) Weekly report — reconciles EXACTLY to the immutable events.
    report_body = _ok(
        report.handle(
            {
                "httpMethod": "GET",
                "headers": {"Authorization": f"Bearer {LEAD}"},
                "queryStringParameters": {"week_key": event_week_key},
                "requestContext": {"requestId": "req-report"},
            }
        )
    )

    expected_tech_qty = sum(int(pe["completed_qty"]) for pe in production_events)
    techs = {row["technician_id"]: row for row in report_body["technician_production"]}
    assert techs["TECH-001"]["total_verified_qty"] == expected_tech_qty == 10

    expected_meters = sum(float(me["meters_delivered"]) for me in material_events)
    runners = {row["runner_id"]: row for row in report_body["material_runner_deliveries"]}
    assert runners["RUNNER-001"]["total_meters_delivered"] == expected_meters == 200.0
    assert runners["RUNNER-001"]["material_runs_completed"] == len(material_events) == 1

    # Verified-unit counts reconcile to the production events (one Fiber unit at SITE-A).
    assert report_body["verified_units_by_work_type"] == {"Fiber": 1}
    assert report_body["verified_units_by_site"] == {"SITE-A": 1}
    assert report_body["total_meters_by_material_type"] == {"Fiber": 200.0}

    # Technician and Runner figures are in separate sections (BR-016).
    assert "technician_production" in report_body
    assert "material_runner_deliveries" in report_body
    assert "combined" not in json.dumps(report_body).lower()

    # 13) Eligible next-day rollover: a fresh AVAILABLE unit dated "yesterday" advances by 1 day;
    #     the COMPLETE unit is NOT rolled over.
    _seed_available_unit("WU-E2E-ROLL", work_package_id, scheduled_date="2024-01-14")
    summary = rollover.handle({"time": "2024-01-15T06:00:00Z"})
    assert summary["rolled_over"] == 1  # only the AVAILABLE unit

    rolled = repo.get_work_unit("WU-E2E-ROLL")
    assert rolled is not None
    assert rolled["current_scheduled_date"] == "2024-01-15"
    assert int(rolled["rollover_count"]) == 1

    complete_unit = repo.get_work_unit(work_unit_id)
    assert complete_unit is not None
    assert complete_unit["state"] == "COMPLETE"  # untouched by rollover (BR-014)


def _scan_events() -> List[Dict[str, Any]]:
    """Return all ProductionEvent/MaterialEvent items from the events table."""
    resp = tables.events_table().scan()
    return list(resp.get("Items", []))


def _seed_available_unit(work_unit_id: str, work_package_id: str, *, scheduled_date: str) -> None:
    canonical = {
        "pk": repo.wu_pk(work_unit_id),
        "sk": repo.METADATA_SK,
        "work_unit_id": work_unit_id,
        "work_package_id": work_package_id,
        "site": "SITE-A",
        "work_type": "Fiber",
        "state": "AVAILABLE",
        "claimed_by": None,
        "required_qty": 5,
        "completed_qty": 0,
        "tote_id": None,
        "rework_count": 0,
        "current_scheduled_date": scheduled_date,
        "original_scheduled_date": scheduled_date,
        "rollover_count": 0,
        "version": 0,
        "sk_gsi1": f"SITE-A#Fiber#{scheduled_date}",
    }
    main = tables.main_table()
    main.put_item(Item=repo._to_dynamo(canonical))
    main.put_item(Item=repo._to_dynamo(repo.work_unit_projection(canonical)))
