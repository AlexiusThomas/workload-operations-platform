"""Shared moto DynamoDB fixtures for data-layer integration tests (Task 7.4).

Creates the four WOP tables with the GSIs from the design's DynamoDB Table Design, wires
the :mod:`backend.data.tables` accessors to the moto resource, and yields the boto3
resource. All fixtures are hermetic — no AWS credentials or network required.
"""

from __future__ import annotations

from typing import Any, Iterator

import boto3
import pytest
from moto import mock_aws

from backend.data import config, tables

REGION = "us-east-1"
MAIN_TABLE = "wop-main-table-test"
AUDIT_TABLE = "wop-audit-table-test"
EVENTS_TABLE = "wop-events-table-test"
IDEMPOTENCY_TABLE = "wop-idempotency-table-test"


def _create_main_table(ddb: Any) -> None:
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


def _create_audit_table(ddb: Any) -> None:
    ddb.create_table(
        TableName=AUDIT_TABLE,
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


def _create_events_table(ddb: Any) -> None:
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


def _create_idempotency_table(ddb: Any) -> None:
    ddb.create_table(
        TableName=IDEMPOTENCY_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture()
def wop_tables() -> Iterator[Any]:
    """Create all four WOP tables in moto and wire the data-layer accessors."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _create_main_table(ddb)
        _create_audit_table(ddb)
        _create_events_table(ddb)
        _create_idempotency_table(ddb)

        data_config = config.DataConfig(
            main_table=MAIN_TABLE,
            audit_table=AUDIT_TABLE,
            events_table=EVENTS_TABLE,
            idempotency_table=IDEMPOTENCY_TABLE,
            import_bucket="wop-import-test",
            archive_bucket="wop-archive-test",
            error_bucket="wop-error-test",
        )
        tables.configure(resource=ddb, data_config=data_config)
        try:
            yield ddb
        finally:
            tables.configure(resource=None, data_config=None)
