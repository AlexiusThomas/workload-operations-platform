"""Integration tests for the WorkUnit handler — full state lifecycle (Task 11).

Exercises claim, start, quantity, prep-complete, label, tote-assign, ready-to-verify,
verify (+ProductionEvent), fail-verify (rework), rework re-claim (qty preserved), complete,
exception assignment, and the queue/detail/stale reads — against moto DynamoDB with the
synthetic auth provider. Validates the atomic transitions, ownership rules, conflict
handling, and idempotency required by FR-006..FR-016, FR-005, FR-022 AC-7, BR-001, BR-003,
BR-007, BR-010.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest

from backend.auth.provider import SyntheticAuthProvider
from backend.data import repositories as repo, tables
from backend.handlers import common, workunit
from backend.idempotency import layer as idempotency

TECH1 = "token-tech-001"  # TECH-001
TECH2 = "token-tech-002"  # TECH-002
RUNNER = "token-runner-001"  # RUNNER-001
LEAD = "token-verify-001"  # VERIFY-001
ADMIN = "token-admin-001"  # ADMIN-001


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


def _seed_work_unit(
    work_unit_id: str, state: str = "AVAILABLE", **overrides: Any
) -> Dict[str, Any]:
    wu: Dict[str, Any] = {
        "work_unit_id": work_unit_id,
        "work_package_id": overrides.get("work_package_id", "wp-1"),
        "site": "SITE-A",
        "work_type": "Fiber",
        "state": state,
        "sk_gsi1": f"SITE-A#Fiber#{overrides.get('current_scheduled_date', '2024-01-15')}",
        "claimed_by": overrides.get("claimed_by"),
        "required_qty": overrides.get("required_qty", 10),
        "completed_qty": overrides.get("completed_qty", 0),
        "tote_id": overrides.get("tote_id"),
        "rework_count": overrides.get("rework_count", 0),
        "current_scheduled_date": overrides.get("current_scheduled_date", "2024-01-15"),
        "original_scheduled_date": "2024-01-15",
        "rollover_count": 0,
        "version": 0,
        "created_at": "2024-01-14T10:00:00.000Z",
        "updated_at": "2024-01-14T10:00:00.000Z",
    }
    # Seed the parent WorkPackage as committed so the queue visibility gate passes.
    if repo.get_work_package(wu["work_package_id"]) is None:
        repo.create_work_package(
            {
                "work_package_id": wu["work_package_id"],
                "site": "SITE-A",
                "rack_position": "RACK-001",
                "work_type": "Fiber",
                "original_scheduled_date": "2024-01-15",
                "current_scheduled_date": "2024-01-15",
                "rollover_count": 0,
                "committed": True,
                "state": "AVAILABLE",
                "created_at": "2024-01-14T10:00:00.000Z",
                "updated_at": "2024-01-14T10:00:00.000Z",
            },
            actor_id="ADMIN-001",
            actor_role="MANAGER_ADMIN",
            correlation_id="seed-wp",
        )
    repo.write_work_unit_transition(
        wu,
        action_type="WORK_UNIT_CREATED",
        actor_id="ADMIN-001",
        actor_role="MANAGER_ADMIN",
        before_state=None,
        after_state=state,
        correlation_id="seed",
    )
    return wu


def _event(
    method: str,
    *,
    token: str,
    action: Optional[str] = None,
    work_unit_id: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, str]] = None,
    idem_key: Optional[str] = None,
) -> Dict[str, Any]:
    headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    path: Dict[str, str] = {}
    if work_unit_id is not None:
        path["work_unit_id"] = work_unit_id
    if action is not None:
        path["action"] = action
    return {
        "httpMethod": method,
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": path or None,
        "queryStringParameters": query,
        "requestContext": {"requestId": "req-test"},
    }


def _post(token: str, action: str, wid: str, **kw: Any) -> Dict[str, Any]:
    return workunit.handle(_event("POST", token=token, action=action, work_unit_id=wid, **kw))


def test_claim_success_and_conflict(wired: Any) -> None:
    _seed_work_unit("wu-claim")
    ok = _post(TECH1, "claim", "wu-claim", idem_key="c1")
    assert ok["statusCode"] == 200
    assert json.loads(ok["body"])["claimed_by"] == "TECH-001"

    # A second claim by a different technician conflicts (already CLAIMED).
    conflict = _post(TECH2, "claim", "wu-claim", idem_key="c2")
    assert conflict["statusCode"] == 409
    assert json.loads(conflict["body"])["error"]["code"] == "CLAIM_CONFLICT"


def test_start_requires_owner(wired: Any) -> None:
    _seed_work_unit("wu-start", state="CLAIMED", claimed_by="TECH-001")
    # Non-owner technician is rejected.
    denied = _post(TECH2, "start", "wu-start", idem_key="s0")
    assert denied["statusCode"] == 403
    # Owner succeeds.
    ok = _post(TECH1, "start", "wu-start", idem_key="s1")
    assert ok["statusCode"] == 200
    assert json.loads(ok["body"])["state"] == "IN_PROGRESS"


def test_quantity_update_and_exceed(wired: Any) -> None:
    _seed_work_unit("wu-qty", state="IN_PROGRESS", claimed_by="TECH-001", required_qty=10)
    ok = workunit.handle(
        _event(
            "PATCH",
            token=TECH1,
            action="quantity",
            work_unit_id="wu-qty",
            body={"completed_qty": 7},
            idem_key="q1",
        )
    )
    assert ok["statusCode"] == 200
    assert json.loads(ok["body"])["completed_qty"] == 7

    exceed = workunit.handle(
        _event(
            "PATCH",
            token=TECH1,
            action="quantity",
            work_unit_id="wu-qty",
            body={"completed_qty": 99},
            idem_key="q2",
        )
    )
    assert exceed["statusCode"] == 400
    assert json.loads(exceed["body"])["error"]["code"] == "QUANTITY_EXCEEDED"


def test_prep_complete_requires_full_quantity(wired: Any) -> None:
    _seed_work_unit(
        "wu-prep", state="IN_PROGRESS", claimed_by="TECH-001", required_qty=10, completed_qty=4
    )
    incomplete = _post(TECH1, "prep-complete", "wu-prep", idem_key="p1")
    assert incomplete["statusCode"] == 400
    assert json.loads(incomplete["body"])["error"]["code"] == "INCOMPLETE_WORK"


def test_full_happy_path_to_verified(wired: Any) -> None:
    _seed_work_unit(
        "wu-flow", state="IN_PROGRESS", claimed_by="TECH-001", required_qty=5, completed_qty=5
    )
    assert _post(TECH1, "prep-complete", "wu-flow", idem_key="f1")["statusCode"] == 200
    assert _post(TECH1, "label", "wu-flow", idem_key="f2")["statusCode"] == 200
    tote = _post(TECH1, "tote-assign", "wu-flow", body={"tote_id": "TOTE-1"}, idem_key="f3")
    assert tote["statusCode"] == 200
    assert _post(TECH1, "ready-to-verify", "wu-flow", idem_key="f4")["statusCode"] == 200

    verify = _post(LEAD, "verify", "wu-flow", idem_key="f5")
    assert verify["statusCode"] == 200
    payload = json.loads(verify["body"])
    assert payload["state"] == "VERIFIED"
    assert "production_event_id" in payload

    # ProductionEvent persisted.
    events, _ = repo.query_production_events_by_week(
        __import__("backend.domain.reporting", fromlist=["compute_week_key"]).compute_week_key(
            repo.get_work_unit("wu-flow")["updated_at"]
        )
    )
    assert any(e["work_unit_id"] == "wu-flow" for e in events)

    assert _post(LEAD, "complete", "wu-flow", idem_key="f6")["statusCode"] == 200
    assert repo.get_work_unit("wu-flow")["state"] == "COMPLETE"


def test_ready_to_verify_requires_tote(wired: Any) -> None:
    # TOTE_ASSIGNED but tote_id missing (edge case) -> TOTE_REQUIRED.
    _seed_work_unit("wu-nt", state="TOTE_ASSIGNED", claimed_by="TECH-001")
    resp = _post(TECH1, "ready-to-verify", "wu-nt", idem_key="nt1")
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "TOTE_REQUIRED"


def test_fail_verify_and_rework_preserves_quantity(wired: Any) -> None:
    _seed_work_unit(
        "wu-rw",
        state="READY_TO_VERIFY",
        claimed_by="TECH-001",
        required_qty=10,
        completed_qty=10,
        tote_id="TOTE-9",
    )
    fail = _post(
        LEAD, "fail-verify", "wu-rw", body={"failure_reason": "bad routing"}, idem_key="r1"
    )
    assert fail["statusCode"] == 200
    unit = repo.get_work_unit("wu-rw")
    assert unit["state"] == "REWORK_REQUIRED"
    assert unit["rework_count"] == 1
    assert unit["completed_qty"] == 10  # preserved (BR-003)

    # Rework re-claim by a technician -> IN_PROGRESS, qty preserved (BR-010).
    reclaim = _post(TECH1, "claim", "wu-rw", idem_key="r2")
    assert reclaim["statusCode"] == 200
    unit2 = repo.get_work_unit("wu-rw")
    assert unit2["state"] == "IN_PROGRESS"
    assert unit2["completed_qty"] == 10


def test_verify_requires_lead_role(wired: Any) -> None:
    _seed_work_unit("wu-auth", state="READY_TO_VERIFY", claimed_by="TECH-001", tote_id="T")
    denied = _post(TECH1, "verify", "wu-auth", idem_key="a1")
    assert denied["statusCode"] == 403


def test_exception_assignment_by_lead(wired: Any) -> None:
    _seed_work_unit("wu-assign", state="CLAIMED", claimed_by="TECH-001")
    resp = _post(LEAD, "assign", "wu-assign", body={"technician_id": "TECH-002"}, idem_key="as1")
    assert resp["statusCode"] == 200
    assert repo.get_work_unit("wu-assign")["claimed_by"] == "TECH-002"


def test_queue_excludes_uncommitted(wired: Any) -> None:
    _seed_work_unit("wu-visible")
    # Seed an AVAILABLE unit under an uncommitted package.
    repo.create_work_package(
        {
            "work_package_id": "wp-unc",
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
        },
        actor_id="ADMIN-001",
        actor_role="MANAGER_ADMIN",
        correlation_id="seed-unc",
    )
    _seed_work_unit("wu-hidden", work_package_id="wp-unc")

    resp = workunit.handle(_event("GET", token=TECH1))
    ids = {u["work_unit_id"] for u in json.loads(resp["body"])["items"]}
    assert "wu-visible" in ids
    assert "wu-hidden" not in ids


def test_idempotent_claim_replays(wired: Any) -> None:
    _seed_work_unit("wu-idem")
    first = _post(TECH1, "claim", "wu-idem", idem_key="dup")
    second = _post(TECH1, "claim", "wu-idem", idem_key="dup")
    assert first["statusCode"] == second["statusCode"] == 200
    # Only one WORK_UNIT_CLAIMED audit despite two calls.
    audits, _ = repo.query_audit_events(repo.ENTITY_WORK_UNIT, "wu-idem")
    claimed = [a for a in audits if a["action_type"] == "WORK_UNIT_CLAIMED"]
    assert len(claimed) == 1


def test_stale_active_work_view(wired: Any) -> None:
    _seed_work_unit(
        "wu-stale", state="IN_PROGRESS", claimed_by="TECH-001", current_scheduled_date="2024-01-01"
    )
    resp = workunit.handle(
        _event(
            "GET", token=LEAD, action="stale", work_unit_id="stale", query={"today": "2024-01-15"}
        )
    )
    assert resp["statusCode"] == 200
    ids = {u["work_unit_id"] for u in json.loads(resp["body"])["items"]}
    assert "wu-stale" in ids
