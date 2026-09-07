"""Integration tests for the MaterialRequirement handler / Material Runner workflow (Task 13).

Covers creation with computed total_meters, the atomic claim (incl. 409 conflict on a
second claim), delivery with an emitted MaterialEvent, runner-ownership enforcement, and the
auto READY_FOR_PREP transition when all requirements for a package are delivered
(FR-003, FR-004, BR-011, BR-012). Runs against moto DynamoDB with the synthetic auth
provider.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest

from backend.auth.provider import SyntheticAuthProvider
from backend.data import repositories as repo, tables
from backend.handlers import common, material
from backend.idempotency import layer as idempotency

RUNNER1 = "token-runner-001"  # RUNNER-001
LEAD = "token-verify-001"
ADMIN = "token-admin-001"
TECH = "token-tech-001"


@pytest.fixture()
def wired(wop_tables: Any) -> Any:
    from backend.observability import metrics

    common.set_auth_provider(SyntheticAuthProvider())
    idempotency.set_table(tables.idempotency_table())
    metrics.disable()
    try:
        yield wop_tables
    finally:
        common.set_auth_provider(None)
        idempotency.set_table(None)
        metrics.enable()


def _event(
    method: str,
    *,
    token: str,
    action: Optional[str] = None,
    material_req_id: Optional[str] = None,
    work_package_id: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, str]] = None,
    idem_key: Optional[str] = None,
) -> Dict[str, Any]:
    headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    path: Dict[str, str] = {}
    if action is not None:
        path["action"] = action
    if material_req_id is not None:
        path["material_req_id"] = material_req_id
    if work_package_id is not None:
        path["work_package_id"] = work_package_id
    return {
        "httpMethod": method,
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": path or None,
        "queryStringParameters": query,
        "requestContext": {"requestId": "req"},
    }


def _create(token: str, wp_id: str, body: Dict[str, Any], idem: str) -> Dict[str, Any]:
    return material.handle(
        _event("POST", token=token, work_package_id=wp_id, body=body, idem_key=idem)
    )


def test_create_requires_admin(wired: Any) -> None:
    body = {"material_type": "Fiber", "cable_length_m": 50.0, "qty_required": 4}
    resp = _create(TECH, "wp-1", body, "c0")
    assert resp["statusCode"] == 403


def test_create_computes_total_meters(wired: Any) -> None:
    body = {"material_type": "Fiber", "cable_length_m": 50.0, "qty_required": 4}
    resp = _create(ADMIN, "wp-1", body, "c1")
    assert resp["statusCode"] == 201
    payload = json.loads(resp["body"])
    assert float(payload["total_meters_required"]) == 200.0
    assert payload["status"] == "MATERIAL_REQUIRED"


def test_create_rejects_non_positive(wired: Any) -> None:
    body = {"material_type": "Fiber", "cable_length_m": 0, "qty_required": 4}
    resp = _create(ADMIN, "wp-1", body, "c2")
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "VALIDATION_ERROR"


def test_claim_and_conflict(wired: Any) -> None:
    created = json.loads(
        _create(
            ADMIN, "wp-1", {"material_type": "Fiber", "cable_length_m": 10, "qty_required": 2}, "c3"
        )["body"]
    )
    mr_id = created["material_req_id"]

    ok = material.handle(
        _event(
            "POST", token=RUNNER1, action="material-claim", material_req_id=mr_id, idem_key="mc1"
        )
    )
    assert ok["statusCode"] == 200
    assert json.loads(ok["body"])["status"] == "MATERIAL_CLAIMED"
    assert json.loads(ok["body"])["runner_id"] == "RUNNER-001"

    # Second claim conflicts.
    conflict = material.handle(
        _event(
            "POST", token=RUNNER1, action="material-claim", material_req_id=mr_id, idem_key="mc2"
        )
    )
    assert conflict["statusCode"] == 409
    assert json.loads(conflict["body"])["error"]["code"] == "MATERIAL_CLAIM_CONFLICT"


def test_deliver_emits_material_event_and_ready_for_prep(wired: Any) -> None:
    created = json.loads(
        _create(
            ADMIN,
            "wp-solo",
            {"material_type": "Fiber", "cable_length_m": 50.0, "qty_required": 4},
            "c4",
        )["body"]
    )
    mr_id = created["material_req_id"]
    material.handle(
        _event("POST", token=RUNNER1, action="material-claim", material_req_id=mr_id, idem_key="d0")
    )

    resp = material.handle(
        _event(
            "POST",
            token=RUNNER1,
            action="material-deliver",
            material_req_id=mr_id,
            body={"qty_delivered": 4, "meters_delivered": 200.0},
            idem_key="d1",
        )
    )
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert "material_event_id" in payload

    # The sole requirement in the package scope is delivered -> auto READY_FOR_PREP.
    final = repo.get_material_requirement(mr_id)
    assert final["status"] == "READY_FOR_PREP"
    assert int(final["qty_delivered"]) == 4

    # MaterialEvent persisted and queryable by runner+week.
    week_key = __import__(
        "backend.domain.reporting", fromlist=["compute_week_key"]
    ).compute_week_key(final["last_updated_at"])
    events, _ = repo.query_material_events_by_week(week_key)
    assert any(e["material_req_id"] == mr_id for e in events)
    by_runner, _ = repo.query_material_events_by_runner_week("RUNNER-001", week_key)
    assert len(by_runner) >= 1


def test_deliver_requires_claiming_runner(wired: Any) -> None:
    # RUNNER-001 claims; a different runner token does not exist, so use ADMIN bypass check:
    # here we assert a non-claiming RUNNER is rejected. Only RUNNER-001 synthetic runner
    # exists, so simulate mismatch by delivering as a fresh material claimed by a manual write.
    created = json.loads(
        _create(
            ADMIN,
            "wp-mm",
            {"material_type": "Fiber", "cable_length_m": 10, "qty_required": 1},
            "c5",
        )["body"]
    )
    mr_id = created["material_req_id"]
    # Manually set it MATERIAL_CLAIMED by a different runner id.
    claimed = repo.get_material_requirement(mr_id)
    claimed["status"] = "MATERIAL_CLAIMED"
    claimed["runner_id"] = "RUNNER-999"
    repo.write_material_transition(
        claimed,
        action_type="MATERIAL_REQ_CLAIMED",
        actor_id="RUNNER-999",
        actor_role="MATERIAL_RUNNER",
        before_state="MATERIAL_REQUIRED",
        after_state="MATERIAL_CLAIMED",
        correlation_id="manual",
    )
    resp = material.handle(
        _event(
            "POST",
            token=RUNNER1,
            action="material-deliver",
            material_req_id=mr_id,
            body={"qty_delivered": 1, "meters_delivered": 10.0},
            idem_key="d2",
        )
    )
    assert resp["statusCode"] == 403


def test_list_by_status_queue(wired: Any) -> None:
    _create(
        ADMIN, "wp-q", {"material_type": "Fiber", "cable_length_m": 10, "qty_required": 1}, "c6"
    )
    resp = material.handle(_event("GET", token=RUNNER1, query={"status": "MATERIAL_REQUIRED"}))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["count"] == 1


def test_list_by_package(wired: Any) -> None:
    _create(
        ADMIN, "wp-lp", {"material_type": "Fiber", "cable_length_m": 10, "qty_required": 1}, "c7"
    )
    resp = material.handle(_event("GET", token=TECH, work_package_id="wp-lp"))
    assert resp["statusCode"] == 200
    assert len(json.loads(resp["body"])["items"]) == 1


def test_idempotent_deliver_no_double_event(wired: Any) -> None:
    created = json.loads(
        _create(
            ADMIN,
            "wp-id",
            {"material_type": "Fiber", "cable_length_m": 10, "qty_required": 1},
            "c8",
        )["body"]
    )
    mr_id = created["material_req_id"]
    material.handle(
        _event("POST", token=RUNNER1, action="material-claim", material_req_id=mr_id, idem_key="i0")
    )
    e = _event(
        "POST",
        token=RUNNER1,
        action="material-deliver",
        material_req_id=mr_id,
        body={"qty_delivered": 1, "meters_delivered": 10.0},
        idem_key="i1",
    )
    first = material.handle(e)
    second = material.handle(e)
    assert first["statusCode"] == second["statusCode"] == 200
    week_key = __import__(
        "backend.domain.reporting", fromlist=["compute_week_key"]
    ).compute_week_key(repo.get_material_requirement(mr_id)["last_updated_at"])
    events, _ = repo.query_material_events_by_week(week_key)
    mine = [ev for ev in events if ev["material_req_id"] == mr_id]
    assert len(mine) == 1  # no duplicate MaterialEvent (FR-020 AC-6)
