"""Integration test for the read-only audit-query handler (Task 14).

Verifies chronological ordering, Lead/Admin-only authorization, and unknown-entity-type
handling for ``GET /v1/audit/{entity_type}/{entity_id}`` (FR-017 AC-2, AC-3, AC-4).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest

from backend.auth.provider import SyntheticAuthProvider
from backend.data import repositories as repo, tables
from backend.handlers import audit_query, common
from backend.idempotency import layer as idempotency

LEAD = "token-verify-001"
ADMIN = "token-admin-001"
TECH = "token-tech-001"


@pytest.fixture()
def wired(wop_tables: Any) -> Any:
    common.set_auth_provider(SyntheticAuthProvider())
    idempotency.set_table(tables.idempotency_table())
    try:
        yield wop_tables
    finally:
        common.set_auth_provider(None)
        idempotency.set_table(None)


def _seed_audits(work_unit_id: str) -> None:
    steps = [
        ("WORK_UNIT_CREATED", None, "AVAILABLE"),
        ("WORK_UNIT_CLAIMED", "AVAILABLE", "CLAIMED"),
        ("WORK_UNIT_STARTED", "CLAIMED", "IN_PROGRESS"),
    ]
    for idx, (action, before, after) in enumerate(steps):
        repo.write_work_unit_transition(
            {
                "work_unit_id": work_unit_id,
                "work_package_id": "wp-a",
                "site": "SITE-A",
                "work_type": "Fiber",
                "state": after,
                "sk_gsi1": "SITE-A#Fiber#2024-01-15",
                "claimed_by": "TECH-001" if after != "AVAILABLE" else None,
                "required_qty": 10,
                "completed_qty": 0,
                "current_scheduled_date": "2024-01-15",
                "original_scheduled_date": "2024-01-15",
                "rollover_count": 0,
                "rework_count": 0,
                "version": 0,
                "created_at": "2024-01-14T10:00:00.000Z",
                "updated_at": f"2024-01-15T08:0{idx}:00.000Z",
            },
            action_type=action,
            actor_id="TECH-001",
            actor_role="TECHNICIAN",
            before_state=before,
            after_state=after,
            correlation_id=f"req-{idx}",
        )


def _event(*, token: str, entity_type: Optional[str], entity_id: Optional[str]) -> Dict[str, Any]:
    path: Dict[str, str] = {}
    if entity_type is not None:
        path["entity_type"] = entity_type
    if entity_id is not None:
        path["entity_id"] = entity_id
    return {
        "httpMethod": "GET",
        "headers": {"Authorization": f"Bearer {token}"},
        "pathParameters": path or None,
        "requestContext": {"requestId": "req"},
    }


def test_audit_query_chronological(wired: Any) -> None:
    _seed_audits("wu-audit")
    resp = audit_query.handle(_event(token=LEAD, entity_type="work-unit", entity_id="wu-audit"))
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["count"] == 3
    actions = [a["action_type"] for a in payload["items"]]
    assert actions == ["WORK_UNIT_CREATED", "WORK_UNIT_CLAIMED", "WORK_UNIT_STARTED"]
    timestamps = [a["timestamp"] for a in payload["items"]]
    assert timestamps == sorted(timestamps)


def test_audit_query_requires_lead_or_admin(wired: Any) -> None:
    _seed_audits("wu-x")
    resp = audit_query.handle(_event(token=TECH, entity_type="work-unit", entity_id="wu-x"))
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"]["code"] == "AUTHORIZATION_DENIED"


def test_audit_query_unknown_entity_type(wired: Any) -> None:
    resp = audit_query.handle(_event(token=ADMIN, entity_type="widget", entity_id="x"))
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "VALIDATION_ERROR"


def test_audit_query_empty_for_unknown_entity(wired: Any) -> None:
    resp = audit_query.handle(_event(token=ADMIN, entity_type="work-unit", entity_id="none"))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["count"] == 0
