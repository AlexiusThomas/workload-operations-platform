"""Integration tests for the data-layer repositories against moto DynamoDB (Task 7.4).

Verifies dual-write consistency (canonical + projection + audit all present or none),
audit write atomicity with the state change, and pagination cursor behaviour
(FR-017 AC-5, NFR-005 AC-2, FR-005 AC-5).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from botocore.exceptions import ClientError

from backend.data import repositories as repo


def _sk_gsi1(site: str, work_type: str, date: str) -> str:
    return f"{site}#{work_type}#{date}"


def _make_work_unit(
    work_unit_id: str, work_package_id: str, state: str = "AVAILABLE"
) -> Dict[str, Any]:
    return {
        "work_unit_id": work_unit_id,
        "work_package_id": work_package_id,
        "site": "SITE-A",
        "work_type": "Fiber",
        "state": state,
        "sk_gsi1": _sk_gsi1("SITE-A", "Fiber", "2024-01-15"),
        "claimed_by": None,
        "required_qty": 10,
        "completed_qty": 0,
        "tote_id": None,
        "current_scheduled_date": "2024-01-15",
        "original_scheduled_date": "2024-01-15",
        "rollover_count": 0,
        "rework_count": 0,
        "version": 0,
        "created_at": "2024-01-14T10:00:00.000Z",
        "updated_at": "2024-01-14T10:00:00.000Z",
    }


def test_work_unit_transition_writes_canonical_projection_and_audit(wop_tables: Any) -> None:
    wu = _make_work_unit("wu-1", "wp-1", state="CLAIMED")
    wu["claimed_by"] = "TECH-001"
    repo.write_work_unit_transition(
        wu,
        action_type="WORK_UNIT_CLAIMED",
        actor_id="TECH-001",
        actor_role="TECHNICIAN",
        before_state="AVAILABLE",
        after_state="CLAIMED",
        correlation_id="req-1",
    )

    # Canonical item exists with the new state.
    canonical = repo.get_work_unit("wu-1")
    assert canonical is not None
    assert canonical["state"] == "CLAIMED"
    assert canonical["claimed_by"] == "TECH-001"

    # Denormalized projection under the parent WorkPackage PK exists and matches.
    children = repo.list_work_units_by_package("wp-1")
    assert len(children) == 1
    assert children[0]["state"] == "CLAIMED"
    assert children[0]["claimed_by"] == "TECH-001"

    # AuditEvent written atomically with the transition.
    audits, _ = repo.query_audit_events(repo.ENTITY_WORK_UNIT, "wu-1")
    assert len(audits) == 1
    assert audits[0]["action_type"] == "WORK_UNIT_CLAIMED"
    assert audits[0]["before_state"] == "AVAILABLE"
    assert audits[0]["after_state"] == "CLAIMED"
    assert audits[0]["actor_id"] == "TECH-001"


def test_failed_condition_rolls_back_entire_transaction(wop_tables: Any) -> None:
    # Seed an AVAILABLE WorkUnit.
    wu = _make_work_unit("wu-2", "wp-2", state="AVAILABLE")
    repo.write_work_unit_transition(
        wu,
        action_type="WORK_UNIT_CREATED",
        actor_id="ADMIN-001",
        actor_role="MANAGER_ADMIN",
        before_state=None,
        after_state="AVAILABLE",
        correlation_id="seed",
    )
    audits_before, _ = repo.query_audit_events(repo.ENTITY_WORK_UNIT, "wu-2")

    # Attempt a claim whose canonical condition requires state=CLAIMED (won't match): the
    # whole transaction must roll back — no extra AuditEvent, canonical unchanged.
    claimed = _make_work_unit("wu-2", "wp-2", state="IN_PROGRESS")
    with pytest.raises(ClientError):
        repo.write_work_unit_transition(
            claimed,
            action_type="WORK_UNIT_STARTED",
            actor_id="TECH-001",
            actor_role="TECHNICIAN",
            before_state="CLAIMED",
            after_state="IN_PROGRESS",
            correlation_id="req-2",
            condition_expression="#state = :expected",
            expression_attribute_names={"#state": "state"},
            expression_attribute_values={":expected": "CLAIMED"},
        )

    canonical = repo.get_work_unit("wu-2")
    assert canonical is not None
    assert canonical["state"] == "AVAILABLE"  # unchanged
    audits_after, _ = repo.query_audit_events(repo.ENTITY_WORK_UNIT, "wu-2")
    assert len(audits_after) == len(audits_before)  # no orphan audit written


def test_verify_transition_adds_production_event(wop_tables: Any) -> None:
    wu = _make_work_unit("wu-3", "wp-3", state="VERIFIED")
    wu["claimed_by"] = "TECH-001"
    wu["completed_qty"] = 10
    production_event = {
        "production_event_id": "pe-1",
        "event_type#week_key": "PE#2024-W03",
        "work_unit_id": "wu-3",
        "work_package_id": "wp-3",
        "verifier_id": "VERIFY-001",
        "technician_id": "TECH-001",
        "completed_qty": 10,
        "work_type": "Fiber",
        "site": "SITE-A",
        "rack_position": "RACK-001",
        "timestamp": "2024-01-17T14:30:00.000Z",
        "week_key": "2024-W03",
        "week_key#timestamp": "2024-W03#2024-01-17T14:30:00.000Z",
    }
    repo.write_work_unit_transition(
        wu,
        action_type="WORK_UNIT_VERIFIED",
        actor_id="VERIFY-001",
        actor_role="VERIFIER_LEAD",
        before_state="READY_TO_VERIFY",
        after_state="VERIFIED",
        correlation_id="req-3",
        production_event=production_event,
    )

    events, _ = repo.query_production_events_by_week("2024-W03")
    assert len(events) == 1
    assert events[0]["production_event_id"] == "pe-1"
    assert events[0]["technician_id"] == "TECH-001"

    by_tech, _ = repo.query_production_events_by_technician_week("TECH-001", "2024-W03")
    assert len(by_tech) == 1


def test_queue_query_by_state_and_pagination(wop_tables: Any) -> None:
    # Seed 3 AVAILABLE WorkUnits in the same package.
    for i in range(3):
        wu = _make_work_unit(f"wu-q{i}", "wp-q", state="AVAILABLE")
        # Distinct sk_gsi1 date suffix so ordering/pagination is deterministic.
        wu["sk_gsi1"] = _sk_gsi1("SITE-A", "Fiber", f"2024-01-1{i}")
        repo.write_work_unit_transition(
            wu,
            action_type="WORK_UNIT_CREATED",
            actor_id="ADMIN-001",
            actor_role="MANAGER_ADMIN",
            before_state=None,
            after_state="AVAILABLE",
            correlation_id=f"seed-{i}",
        )

    # First page of size 2, then follow the cursor.
    page1, token1 = repo.query_work_units_by_state("AVAILABLE", limit=2)
    assert len(page1) == 2
    assert token1 is not None

    page2, token2 = repo.query_work_units_by_state("AVAILABLE", limit=2, next_token=token1)
    assert len(page2) == 1
    assert token2 is None

    ids = {item["work_unit_id"] for item in page1 + page2}
    assert ids == {"wu-q0", "wu-q1", "wu-q2"}


def test_queue_filter_by_site_work_type(wop_tables: Any) -> None:
    wu = _make_work_unit("wu-f1", "wp-f", state="AVAILABLE")
    repo.write_work_unit_transition(
        wu,
        action_type="WORK_UNIT_CREATED",
        actor_id="ADMIN-001",
        actor_role="MANAGER_ADMIN",
        before_state=None,
        after_state="AVAILABLE",
        correlation_id="seed-f",
    )
    items, _ = repo.query_work_units_by_state("AVAILABLE", site="SITE-A", work_type="Fiber")
    assert len(items) == 1
    # A non-matching site prefix returns nothing.
    none_items, _ = repo.query_work_units_by_state("AVAILABLE", site="SITE-Z")
    assert none_items == []


def test_audit_events_returned_in_chronological_order(wop_tables: Any) -> None:
    states = [
        ("WORK_UNIT_CREATED", None, "AVAILABLE"),
        ("WORK_UNIT_CLAIMED", "AVAILABLE", "CLAIMED"),
        ("WORK_UNIT_STARTED", "CLAIMED", "IN_PROGRESS"),
    ]
    for idx, (action, before, after) in enumerate(states):
        wu = _make_work_unit("wu-audit", "wp-audit", state=after or "AVAILABLE")
        wu["updated_at"] = f"2024-01-15T08:0{idx}:00.000Z"
        repo.write_work_unit_transition(
            wu,
            action_type=action,
            actor_id="TECH-001",
            actor_role="TECHNICIAN",
            before_state=before,
            after_state=after,
            correlation_id=f"req-{idx}",
        )

    audits, _ = repo.query_audit_events(repo.ENTITY_WORK_UNIT, "wu-audit")
    assert [a["action_type"] for a in audits] == [
        "WORK_UNIT_CREATED",
        "WORK_UNIT_CLAIMED",
        "WORK_UNIT_STARTED",
    ]
    timestamps = [a["sk"] for a in audits]
    assert timestamps == sorted(timestamps)  # ascending SK == chronological


def test_material_transition_writes_projection_and_material_event(wop_tables: Any) -> None:
    mr = {
        "material_req_id": "mr-1",
        "work_package_id": "wp-m",
        "material_type": "Fiber",
        "cable_length_m": 50.0,
        "qty_required": 4,
        "total_meters_required": 200.0,
        "qty_delivered": 4,
        "meters_delivered": 200.0,
        "runner_id": "RUNNER-001",
        "status": "MATERIAL_DELIVERED",
        "created_at": "2024-01-15T08:00:00.000Z",
        "last_updated_at": "2024-01-17T11:00:00.000Z",
    }
    material_event = {
        "material_event_id": "me-1",
        "event_type#week_key": "ME#2024-W03",
        "material_req_id": "mr-1",
        "work_package_id": "wp-m",
        "material_type": "Fiber",
        "qty_delivered": 4,
        "meters_delivered": 200.0,
        "runner_id": "RUNNER-001",
        "timestamp": "2024-01-17T11:00:00.000Z",
        "week_key": "2024-W03",
        "week_key#timestamp": "2024-W03#2024-01-17T11:00:00.000Z",
    }
    repo.write_material_transition(
        mr,
        action_type="MATERIAL_REQ_DELIVERED",
        actor_id="RUNNER-001",
        actor_role="MATERIAL_RUNNER",
        before_state="MATERIAL_CLAIMED",
        after_state="MATERIAL_DELIVERED",
        correlation_id="req-m",
        material_event=material_event,
    )

    canonical = repo.get_material_requirement("mr-1")
    assert canonical is not None and canonical["status"] == "MATERIAL_DELIVERED"
    children = repo.list_material_requirements_by_package("wp-m")
    assert len(children) == 1 and children[0]["proj_status"] == "MATERIAL_DELIVERED"

    events, _ = repo.query_material_events_by_week("2024-W03")
    assert len(events) == 1 and events[0]["material_event_id"] == "me-1"
    by_runner, _ = repo.query_material_events_by_runner_week("RUNNER-001", "2024-W03")
    assert len(by_runner) == 1


def test_material_status_query_and_stale_active_work(wop_tables: Any) -> None:
    # Seed a MATERIAL_REQUIRED requirement.
    mr = {
        "material_req_id": "mr-s",
        "work_package_id": "wp-s",
        "material_type": "Fiber",
        "qty_required": 4,
        "qty_delivered": 0,
        "meters_delivered": 0.0,
        "status": "MATERIAL_REQUIRED",
        "last_updated_at": "2024-01-15T08:00:00.000Z",
    }
    repo.write_material_transition(
        mr,
        action_type="MATERIAL_REQ_CREATED",
        actor_id="ADMIN-001",
        actor_role="MANAGER_ADMIN",
        before_state=None,
        after_state="MATERIAL_REQUIRED",
        correlation_id="seed-mr",
    )
    by_status, _ = repo.query_material_requirements_by_status("MATERIAL_REQUIRED")
    assert len(by_status) == 1

    # Seed a stale IN_PROGRESS WorkUnit dated in the past.
    stale = _make_work_unit("wu-stale", "wp-s", state="IN_PROGRESS")
    stale["current_scheduled_date"] = "2024-01-01"
    stale["sk_gsi1"] = _sk_gsi1("SITE-A", "Fiber", "2024-01-01")
    stale["claimed_by"] = "TECH-001"
    repo.write_work_unit_transition(
        stale,
        action_type="WORK_UNIT_STARTED",
        actor_id="TECH-001",
        actor_role="TECHNICIAN",
        before_state="CLAIMED",
        after_state="IN_PROGRESS",
        correlation_id="seed-stale",
    )
    stale_items = repo.query_stale_active_work(today="2024-01-15")
    assert any(item["work_unit_id"] == "wu-stale" for item in stale_items)


def test_create_work_package_atomic_with_audit(wop_tables: Any) -> None:
    wp = {
        "work_package_id": "wp-new",
        "site": "SITE-A",
        "rack_position": "RACK-001",
        "work_type": "Fiber",
        "original_scheduled_date": "2024-01-15",
        "current_scheduled_date": "2024-01-15",
        "rollover_count": 0,
        "state": "IMPORTED",
        "created_at": "2024-01-14T10:00:00.000Z",
        "updated_at": "2024-01-14T10:00:00.000Z",
    }
    repo.create_work_package(
        wp, actor_id="ADMIN-001", actor_role="MANAGER_ADMIN", correlation_id="req-wp"
    )
    assert repo.get_work_package("wp-new") is not None
    audits, _ = repo.query_audit_events(repo.ENTITY_WORK_PACKAGE, "wp-new")
    assert len(audits) == 1 and audits[0]["action_type"] == "WORK_PACKAGE_CREATED"
