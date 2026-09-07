"""Integration tests for the two-phase ingestion handler (Task 9.3).

Covers:
- Staged WorkUnits are invisible to the operational queue until the WorkPackage commit
  (visibility gate) completes (FR-001 AC-7).
- A schema-invalid file is moved to the error prefix and creates no WorkPackage (FR-001 AC-4).
- A mid-commit failure leaves the WorkPackage in INGESTION_ERROR, moves the file to the
  error prefix, and keeps orphaned AVAILABLE units suppressed from the queue (FR-001 AC-7).

Runs against moto DynamoDB (shared ``wop_tables`` fixture) plus moto S3 buckets created here.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import boto3
import pytest

from backend.data import repositories as repo, tables
from backend.handlers import ingestion
from backend.idempotency import layer as idempotency

IMPORT_BUCKET = "wop-import-test"
ARCHIVE_BUCKET = "wop-archive-test"
ERROR_BUCKET = "wop-error-test"
REGION = "us-east-1"


@pytest.fixture()
def s3_buckets(wop_tables: Any) -> Any:
    """Create the import/archive/error buckets inside the active moto context."""
    s3 = boto3.client("s3", region_name=REGION)
    for bucket in (IMPORT_BUCKET, ARCHIVE_BUCKET, ERROR_BUCKET):
        s3.create_bucket(Bucket=bucket)
    idempotency.set_table(tables.idempotency_table())
    try:
        yield s3
    finally:
        idempotency.set_table(None)


def _valid_payload() -> str:
    return json.dumps(
        {
            "work_packages": [
                {
                    "work_package_id": "wp-ing-1",
                    "site": "SITE-A",
                    "rack_position": "RACK-001",
                    "work_type": "Fiber",
                    "scheduled_date": "2024-01-15",
                    "work_units": [
                        {"work_unit_id": "wu-ing-1", "required_qty": 5, "work_type": "Fiber"},
                        {"work_unit_id": "wu-ing-2", "required_qty": 3, "work_type": "Fiber"},
                    ],
                }
            ]
        }
    )


def _event(key: str) -> Dict[str, Any]:
    return {"Records": [{"s3": {"bucket": {"name": IMPORT_BUCKET}, "object": {"key": key}}}]}


def _object_exists(s3: Any, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def test_successful_ingestion_commits_and_units_visible(s3_buckets: Any) -> None:
    s3 = s3_buckets
    key = "workload/ok.json"
    s3.put_object(Bucket=IMPORT_BUCKET, Key=key, Body=_valid_payload().encode("utf-8"))

    result = ingestion.handle(_event(key), s3_client=s3)
    assert result["results"][0]["status"] == "committed"

    wp = repo.get_work_package("wp-ing-1")
    assert wp is not None and wp["state"] == "AVAILABLE" and wp["committed"] is True

    # Units are AVAILABLE and pass the committed visibility gate.
    available, _ = repo.query_work_units_by_state("AVAILABLE")
    visible = ingestion.suppress_uncommitted(available)
    assert {u["work_unit_id"] for u in visible} == {"wu-ing-1", "wu-ing-2"}

    # File archived (moved out of import).
    assert not _object_exists(s3, IMPORT_BUCKET, key)
    assert _object_exists(s3, ARCHIVE_BUCKET, key)


def test_staged_units_suppressed_until_commit(s3_buckets: Any) -> None:
    # Manually stage a WorkPackage (committed=false) with a unit already flipped to
    # AVAILABLE, to model the window before the final visibility gate is set. The queue
    # suppression must hide it.
    wp_item = {
        "work_package_id": "wp-staged",
        "site": "SITE-A",
        "rack_position": "RACK-001",
        "work_type": "Fiber",
        "original_scheduled_date": "2024-01-15",
        "current_scheduled_date": "2024-01-15",
        "rollover_count": 0,
        "committed": False,
        "state": "INGESTING",
        "created_at": "2024-01-14T10:00:00.000Z",
        "updated_at": "2024-01-14T10:00:00.000Z",
    }
    staged_unit = {
        "work_unit_id": "wu-staged",
        "work_package_id": "wp-staged",
        "site": "SITE-A",
        "work_type": "Fiber",
        "state": "STAGING",
        "claimed_by": None,
        "required_qty": 5,
        "completed_qty": 0,
        "tote_id": None,
        "rework_count": 0,
        "current_scheduled_date": "2024-01-15",
        "original_scheduled_date": "2024-01-15",
        "rollover_count": 0,
        "version": 0,
        "created_at": "2024-01-14T10:00:00.000Z",
        "updated_at": "2024-01-14T10:00:00.000Z",
    }
    repo.stage_work_package_with_units(
        wp_item,
        [staged_unit],
        actor_id="SYSTEM",
        actor_role="SYSTEM",
        correlation_id="stage-test",
    )
    # Flip the unit to AVAILABLE (Phase 2) but leave the package committed=false.
    available_unit = dict(staged_unit)
    available_unit["sk_gsi1"] = "SITE-A#Fiber#2024-01-15"
    repo.commit_staged_work_unit(
        available_unit, actor_id="SYSTEM", actor_role="SYSTEM", correlation_id="stage-test"
    )

    available, _ = repo.query_work_units_by_state("AVAILABLE")
    assert any(u["work_unit_id"] == "wu-staged" for u in available)  # present in raw GSI

    # Visibility gate suppresses it because parent committed=false.
    visible = ingestion.suppress_uncommitted(available)
    assert all(u["work_unit_id"] != "wu-staged" for u in visible)


def test_schema_invalid_file_moved_to_error_prefix(s3_buckets: Any) -> None:
    s3 = s3_buckets
    key = "workload/bad.json"
    s3.put_object(Bucket=IMPORT_BUCKET, Key=key, Body=b"not-json-at-all")

    result = ingestion.handle(_event(key), s3_client=s3)
    assert result["results"][0]["status"] == "schema_error"

    # No WorkPackage created; file moved to error prefix.
    assert not _object_exists(s3, IMPORT_BUCKET, key)
    assert _object_exists(s3, ERROR_BUCKET, key)


def test_mid_commit_failure_leaves_ingestion_error(s3_buckets: Any) -> None:
    s3 = s3_buckets
    key = "workload/fail.json"
    s3.put_object(Bucket=IMPORT_BUCKET, Key=key, Body=_valid_payload().encode("utf-8"))

    # Force a failure during Phase 2 by monkeypatching commit_staged_work_unit to raise
    # after staging has already written the WorkPackage in INGESTING state.
    original = repo.commit_staged_work_unit

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated commit failure")

    repo.commit_staged_work_unit = _boom  # type: ignore[assignment]
    try:
        result = ingestion.handle(_event(key), s3_client=s3)
    finally:
        repo.commit_staged_work_unit = original  # type: ignore[assignment]

    assert result["results"][0]["status"] == "ingestion_error"

    wp = repo.get_work_package("wp-ing-1")
    assert wp is not None and wp["state"] == "INGESTION_ERROR" and wp["committed"] is False

    # Any staged/orphaned units are suppressed from the queue (committed=false).
    available, _ = repo.query_work_units_by_state("AVAILABLE")
    visible = ingestion.suppress_uncommitted(available)
    assert all(u.get("work_package_id") != "wp-ing-1" for u in visible)

    # File moved to error prefix.
    assert not _object_exists(s3, IMPORT_BUCKET, key)
    assert _object_exists(s3, ERROR_BUCKET, key)
