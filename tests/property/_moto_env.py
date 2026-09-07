"""Shared moto environment builder for handler-level property tests.

Property tests cannot use the ``wop_tables`` pytest fixture directly (Hypothesis re-runs the
test body many times within one fixture setup). This helper creates the four WOP tables +
three S3 buckets inside a caller-managed ``mock_aws`` context and wires the data layer, auth
provider, and idempotency table. Callers use it as a context manager per example.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import boto3

from backend.auth.provider import SyntheticAuthProvider
from backend.data import config, tables
from backend.handlers import common
from backend.idempotency import layer as idempotency
from backend.observability import metrics

REGION = "us-east-1"
MAIN_TABLE = "wop-main-prop"
AUDIT_TABLE = "wop-audit-prop"
EVENTS_TABLE = "wop-events-prop"
IDEMPOTENCY_TABLE = "wop-idempotency-prop"


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


def _create_simple_table(ddb: Any, name: str, hash_only: bool = False) -> None:
    key_schema = [{"AttributeName": "pk", "KeyType": "HASH"}]
    attrs = [{"AttributeName": "pk", "AttributeType": "S"}]
    if not hash_only:
        key_schema.append({"AttributeName": "sk", "KeyType": "RANGE"})
        attrs.append({"AttributeName": "sk", "AttributeType": "S"})
    ddb.create_table(
        TableName=name,
        KeySchema=key_schema,
        AttributeDefinitions=attrs,
        BillingMode="PAY_PER_REQUEST",
    )


@contextmanager
def moto_wop_environment() -> Iterator[Any]:
    """Yield a boto3 DynamoDB resource with all WOP tables/buckets and wiring configured.

    Metrics are disabled to keep property runs hermetic and quiet.
    """
    from moto import mock_aws

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _create_main_table(ddb)
        _create_simple_table(ddb, AUDIT_TABLE)
        _create_events_table(ddb)
        _create_simple_table(ddb, IDEMPOTENCY_TABLE, hash_only=True)

        data_config = config.DataConfig(
            main_table=MAIN_TABLE,
            audit_table=AUDIT_TABLE,
            events_table=EVENTS_TABLE,
            idempotency_table=IDEMPOTENCY_TABLE,
            import_bucket="wop-import-prop",
            archive_bucket="wop-archive-prop",
            error_bucket="wop-error-prop",
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
