"""Integration tests for the WorkPackage handler (Task 10.2).

Covers creation validation (missing mandatory attribute -> 400 VALIDATION_ERROR), the
derived-state response, and list pagination (FR-002 AC-4, FR-021 AC-3, FR-005 AC-5). Runs
against moto DynamoDB (shared ``wop_tables`` fixture) with the synthetic auth provider.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest

from backend.auth.provider import SyntheticAuthProvider
from backend.data import repositories as repo, tables
from backend.handlers import common, workpackage
from backend.idempotency import layer as idempotency

ADMIN_TOKEN = "token-admin-001"
TECH_TOKEN = "token-tech-001"


@pytest.fixture()
def wired(wop_tables: Any) -> Any:
    """Wire the synthetic auth provider and idempotency table for handler tests."""
    common.set_auth_provider(SyntheticAuthProvider())
    idempotency.set_table(tables.idempotency_table())
    try:
        yield wop_tables
    finally:
        common.set_auth_provider(None)
        idempotency.set_table(None)


def _event(
    method: str,
    *,
    token: str = ADMIN_TOKEN,
    body: Optional[Dict[str, Any]] = None,
    path: Optional[Dict[str, str]] = None,
    query: Optional[Dict[str, str]] = None,
    idem_key: Optional[str] = None,
) -> Dict[str, Any]:
    headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    return {
        "httpMethod": method,
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": path,
        "queryStringParameters": query,
        "requestContext": {"requestId": "req-test"},
    }


def test_create_work_package_success(wired: Any) -> None:
    body = {
        "site": "SITE-A",
        "rack_position": "RACK-001",
        "work_type": "Fiber",
        "scheduled_date": "2024-01-15",
        "work_units": [{"required_qty": 10, "work_type": "Fiber"}],
    }
    resp = workpackage.handle(_event("POST", body=body, idem_key="k1"))
    assert resp["statusCode"] == 201
    payload = json.loads(resp["body"])
    assert payload["state"] == "AVAILABLE"
    assert len(payload["work_units"]) == 1
    # Persisted with an AuditEvent per entity.
    audits, _ = repo.query_audit_events(repo.ENTITY_WORK_PACKAGE, payload["work_package_id"])
    assert len(audits) == 1 and audits[0]["action_type"] == "WORK_PACKAGE_CREATED"


def test_create_missing_mandatory_attribute_returns_400(wired: Any) -> None:
    body = {"site": "SITE-A", "work_type": "Fiber", "scheduled_date": "2024-01-15"}
    resp = workpackage.handle(_event("POST", body=body, idem_key="k2"))
    assert resp["statusCode"] == 400
    payload = json.loads(resp["body"])
    assert payload["error"]["code"] == "VALIDATION_ERROR"


def test_create_requires_admin_role(wired: Any) -> None:
    body = {
        "site": "SITE-A",
        "rack_position": "RACK-001",
        "work_type": "Fiber",
        "scheduled_date": "2024-01-15",
    }
    resp = workpackage.handle(_event("POST", token=TECH_TOKEN, body=body, idem_key="k3"))
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"]["code"] == "AUTHORIZATION_DENIED"


def test_get_work_package_derived_state(wired: Any) -> None:
    body = {
        "site": "SITE-A",
        "rack_position": "RACK-001",
        "work_type": "Fiber",
        "scheduled_date": "2024-01-15",
        "work_units": [
            {"required_qty": 10, "work_type": "Fiber"},
            {"required_qty": 5, "work_type": "Fiber"},
        ],
    }
    created = json.loads(workpackage.handle(_event("POST", body=body, idem_key="k4"))["body"])
    wp_id = created["work_package_id"]

    resp = workpackage.handle(_event("GET", token=TECH_TOKEN, path={"work_package_id": wp_id}))
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    # All WorkUnits AVAILABLE -> derived AVAILABLE.
    assert payload["derived_state"] == "AVAILABLE"
    assert len(payload["work_units"]) == 2


def test_get_missing_work_package_returns_404(wired: Any) -> None:
    resp = workpackage.handle(_event("GET", path={"work_package_id": "nope"}))
    assert resp["statusCode"] == 404
    assert json.loads(resp["body"])["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_list_work_packages_pagination(wired: Any) -> None:
    for i in range(3):
        body = {
            "site": "SITE-A",
            "rack_position": f"RACK-00{i}",
            "work_type": "Fiber",
            "scheduled_date": "2024-01-15",
        }
        workpackage.handle(_event("POST", body=body, idem_key=f"list-{i}"))

    resp = workpackage.handle(_event("GET", token=TECH_TOKEN))
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["count"] == 3
    assert len(payload["items"]) == 3


def test_idempotent_create_replays(wired: Any) -> None:
    body = {
        "site": "SITE-A",
        "rack_position": "RACK-001",
        "work_type": "Fiber",
        "scheduled_date": "2024-01-15",
    }
    first = workpackage.handle(_event("POST", body=body, idem_key="dup"))
    second = workpackage.handle(_event("POST", body=body, idem_key="dup"))
    assert first["statusCode"] == second["statusCode"] == 201
    # Same work_package_id replayed (no second package created).
    assert (
        json.loads(first["body"])["work_package_id"]
        == json.loads(second["body"])["work_package_id"]
    )
    items, _ = repo.list_work_packages()
    assert len(items) == 1
