"""Idempotency simultaneous-request contract test (Task 23, issue 8C).

FR-020 AC-4: two concurrent requests carrying the same Idempotency_Key must execute the
operation exactly once and BOTH receive the result. This exercises common.run_idempotent:
a duplicate caller arriving while a record already exists replays the original completed
response (not a 409), and the operation body runs only once.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

import boto3
import pytest
from moto import mock_aws

from backend.data import config, tables
from backend.handlers import common
from backend.idempotency import layer as idempotency

IDEM = "wop-idem-8c"


@pytest.fixture()
def wired() -> Any:
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=IDEM,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        tables.configure(
            resource=ddb,
            data_config=config.DataConfig(
                main_table="m",
                audit_table="a",
                events_table="e",
                idempotency_table=IDEM,
                import_bucket="i",
                archive_bucket="ar",
                error_bucket="er",
            ),
        )
        idempotency.set_table(tables.idempotency_table())
        try:
            yield ddb
        finally:
            tables.configure(resource=None, data_config=None)
            idempotency.set_table(None)


def test_duplicate_same_key_replays_original_result_once(wired: Any) -> None:
    runs = {"n": 0}

    def execute() -> Tuple[int, Dict[str, Any]]:
        runs["n"] += 1
        return 200, {"result": "value", "run": runs["n"]}

    key = "idem-key-1"
    body = {"a": 1}

    first = common.run_idempotent(
        operation="op",
        resource_id="res",
        request_body=body,
        key=key,
        execute=execute,
        correlation_id="c1",
    )
    second = common.run_idempotent(
        operation="op",
        resource_id="res",
        request_body=body,
        key=key,
        execute=execute,
        correlation_id="c2",
    )

    # Executed exactly once; both callers receive the same original result (FR-020 AC-4).
    assert runs["n"] == 1
    assert first["statusCode"] == 200
    assert second["statusCode"] == 200
    assert json.loads(first["body"]) == json.loads(second["body"])
    assert json.loads(second["body"])["run"] == 1


def test_duplicate_same_key_different_payload_conflicts(wired: Any) -> None:
    """Regression: reusing a completed key with a different payload is a 409 conflict."""

    def execute() -> Tuple[int, Dict[str, Any]]:
        return 200, {"ok": True}

    common.run_idempotent(
        operation="op",
        resource_id="res",
        request_body={"a": 1},
        key="idem-key-2",
        execute=execute,
        correlation_id="c1",
    )
    from backend.observability.errors import IdempotencyConflictError

    with pytest.raises(IdempotencyConflictError):
        common.run_idempotent(
            operation="op",
            resource_id="res",
            request_body={"a": 999},
            key="idem-key-2",
            execute=execute,
            correlation_id="c2",
        )
