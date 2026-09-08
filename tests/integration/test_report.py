"""Integration test for the weekly report handler (Task 16.2).

Seeds immutable ProductionEvents and MaterialEvents into the moto events table and asserts:

- report totals reconcile EXACTLY to the seeded events (FR-024 AC-2, AC-4),
- Technician production and Material Runner figures are in separate sections with no
  combined productivity metric (BR-011, BR-016),
- regeneration is deterministic for the same events (FR-024 AC-4),
- role gating: GET requires LEAD/ADMIN, POST requires ADMIN.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from backend.auth.provider import SyntheticAuthProvider
from backend.data import repositories as repo, tables
from backend.handlers import common, report
from backend.idempotency import layer as idempotency

LEAD = "token-verify-001"
ADMIN = "token-admin-001"
TECH = "token-tech-001"

WEEK = "2024-W03"


@pytest.fixture()
def wired(wop_tables: Any) -> Any:
    common.set_auth_provider(SyntheticAuthProvider())
    idempotency.set_table(tables.idempotency_table())
    try:
        yield wop_tables
    finally:
        common.set_auth_provider(None)
        idempotency.set_table(None)


def _put_production_event(
    *,
    pe_id: str,
    technician_id: str,
    work_type: str,
    site: str,
    completed_qty: int,
    week_key: str = WEEK,
    ts: str,
) -> None:
    item = {
        "pk": f"PE#{pe_id}",
        "sk": repo.METADATA_SK,
        "production_event_id": pe_id,
        "event_type#week_key": f"PE#{week_key}",
        "work_unit_id": f"wu-{pe_id}",
        "work_package_id": f"wp-{pe_id}",
        "verifier_id": "VERIFY-001",
        "technician_id": technician_id,
        "completed_qty": completed_qty,
        "work_type": work_type,
        "site": site,
        "timestamp": ts,
        "week_key": week_key,
        "week_key#timestamp": f"{week_key}#{ts}",
    }
    tables.events_table().put_item(Item=repo._to_dynamo(item))


def _put_material_event(
    *,
    me_id: str,
    runner_id: str,
    material_type: str,
    meters_delivered: float,
    work_package_id: str,
    week_key: str = WEEK,
    ts: str,
) -> None:
    item = {
        "pk": f"ME#{me_id}",
        "sk": repo.METADATA_SK,
        "material_event_id": me_id,
        "event_type#week_key": f"ME#{week_key}",
        "material_req_id": f"mr-{me_id}",
        "work_package_id": work_package_id,
        "work_unit_id": None,
        "material_type": material_type,
        "cable_length_m": 50.0,
        "qty_delivered": 4,
        "meters_delivered": meters_delivered,
        "runner_id": runner_id,
        "timestamp": ts,
        "week_key": week_key,
        "week_key#timestamp": f"{week_key}#{ts}",
    }
    tables.events_table().put_item(Item=repo._to_dynamo(item))


def _seed() -> None:
    # Technician production: TECH-001 verifies 2 Fiber units (qty 10, 6) + 1 Copper (qty 4);
    # TECH-002 verifies 1 Fiber unit (qty 8). Sites SITE-A / SITE-B.
    _put_production_event(
        pe_id="p1",
        technician_id="TECH-001",
        work_type="Fiber",
        site="SITE-A",
        completed_qty=10,
        ts="2024-01-17T10:00:00.000Z",
    )
    _put_production_event(
        pe_id="p2",
        technician_id="TECH-001",
        work_type="Fiber",
        site="SITE-A",
        completed_qty=6,
        ts="2024-01-17T11:00:00.000Z",
    )
    _put_production_event(
        pe_id="p3",
        technician_id="TECH-001",
        work_type="Copper",
        site="SITE-B",
        completed_qty=4,
        ts="2024-01-17T12:00:00.000Z",
    )
    _put_production_event(
        pe_id="p4",
        technician_id="TECH-002",
        work_type="Fiber",
        site="SITE-A",
        completed_qty=8,
        ts="2024-01-18T09:00:00.000Z",
    )
    # Material runner deliveries: RUNNER-001 two runs (Fiber 200 + Copper 100) over 2 WPs;
    # RUNNER-002 one run (Fiber 150).
    _put_material_event(
        me_id="m1",
        runner_id="RUNNER-001",
        material_type="Fiber",
        meters_delivered=200.0,
        work_package_id="wp-p1",
        ts="2024-01-16T08:00:00.000Z",
    )
    _put_material_event(
        me_id="m2",
        runner_id="RUNNER-001",
        material_type="Copper",
        meters_delivered=100.0,
        work_package_id="wp-p3",
        ts="2024-01-16T09:00:00.000Z",
    )
    _put_material_event(
        me_id="m3",
        runner_id="RUNNER-002",
        material_type="Fiber",
        meters_delivered=150.0,
        work_package_id="wp-p1",
        ts="2024-01-16T10:00:00.000Z",
    )


def _get(token: str, week_key: str = WEEK) -> Dict[str, Any]:
    event = {
        "httpMethod": "GET",
        "headers": {"Authorization": f"Bearer {token}"},
        "queryStringParameters": {"week_key": week_key},
        "requestContext": {"requestId": "req"},
    }
    return report.handle(event)


def test_report_reconciles_exactly(wired: Any) -> None:
    _seed()
    resp = _get(LEAD)
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])

    # Verified units by work_type: Fiber = 3 (p1,p2,p4), Copper = 1 (p3).
    assert payload["verified_units_by_work_type"] == {"Fiber": 3, "Copper": 1}
    # Verified units by site: SITE-A = 3 (p1,p2,p4), SITE-B = 1 (p3).
    assert payload["verified_units_by_site"] == {"SITE-A": 3, "SITE-B": 1}
    # Meters by material type: Fiber = 350, Copper = 100.
    assert payload["total_meters_by_material_type"] == {"Fiber": 350.0, "Copper": 100.0}

    techs = {row["technician_id"]: row for row in payload["technician_production"]}
    assert techs["TECH-001"]["total_verified_qty"] == 20  # 10 + 6 + 4
    assert techs["TECH-001"]["verified_units_by_work_type"] == {"Fiber": 2, "Copper": 1}
    assert techs["TECH-002"]["total_verified_qty"] == 8
    assert techs["TECH-002"]["verified_units_by_work_type"] == {"Fiber": 1}

    runners = {row["runner_id"]: row for row in payload["material_runner_deliveries"]}
    assert runners["RUNNER-001"]["material_runs_completed"] == 2
    assert runners["RUNNER-001"]["total_meters_delivered"] == 300.0
    assert runners["RUNNER-001"]["work_packages_supplied"] == 2  # wp-p1, wp-p3
    assert runners["RUNNER-002"]["material_runs_completed"] == 1
    assert runners["RUNNER-002"]["total_meters_delivered"] == 150.0
    assert runners["RUNNER-002"]["work_packages_supplied"] == 1


def test_report_sections_separate_no_combined_metric(wired: Any) -> None:
    _seed()
    payload = json.loads(_get(LEAD)["body"])
    # Technician and Runner figures live in distinct sections (BR-011, BR-016).
    assert "technician_production" in payload
    assert "material_runner_deliveries" in payload
    # No combined productivity metric anywhere in the report.
    combined_markers = [
        "combined",
        "productivity",
        "total_productivity",
        "combined_score",
    ]
    keys_blob = json.dumps(payload).lower()
    for marker in combined_markers:
        assert marker not in keys_blob
    # Technician rows carry no runner metrics and vice versa.
    for row in payload["technician_production"]:
        assert "meters_delivered" not in row
        assert "material_runs_completed" not in row
    for row in payload["material_runner_deliveries"]:
        assert "total_verified_qty" not in row
        assert "verified_units_by_work_type" not in row


def test_report_deterministic(wired: Any) -> None:
    _seed()
    first = json.loads(_get(LEAD)["body"])
    second = json.loads(_get(LEAD)["body"])
    assert first == second


def test_report_empty_week(wired: Any) -> None:
    payload = json.loads(_get(ADMIN, week_key="2024-W52")["body"])
    assert payload["technician_production"] == []
    assert payload["material_runner_deliveries"] == []
    assert payload["verified_units_by_work_type"] == {}
    assert payload["total_meters_by_material_type"] == {}


def test_report_get_requires_lead_or_admin(wired: Any) -> None:
    resp = _get(TECH)
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"]["code"] == "AUTHORIZATION_DENIED"


def test_report_post_requires_admin(wired: Any) -> None:
    _seed()
    event = {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {LEAD}"},
        "body": json.dumps({"week_key": WEEK}),
        "requestContext": {"requestId": "req"},
    }
    resp = report.handle(event)
    assert resp["statusCode"] == 403

    event["headers"] = {"Authorization": f"Bearer {ADMIN}"}
    resp = report.handle(event)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["week_key"] == WEEK


def test_report_eventbridge_entrypoint(wired: Any) -> None:
    _seed()
    # No httpMethod -> EventBridge weekly schedule invocation.
    result = report.handle({"week_key": WEEK})
    assert result["week_key"] == WEEK
    assert len(result["technician_production"]) == 2


def test_report_missing_week_key(wired: Any) -> None:
    event = {
        "httpMethod": "GET",
        "headers": {"Authorization": f"Bearer {ADMIN}"},
        "queryStringParameters": None,
        "requestContext": {"requestId": "req"},
    }
    resp = report.handle(event)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "VALIDATION_ERROR"


def test_report_post_malformed_week_key_returns_400(wired: Any) -> None:
    # Malformed week_key fails schema validation before generation (SEC-006 AC-1).
    event = {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {ADMIN}"},
        "body": json.dumps({"week_key": "garbage"}),
        "requestContext": {"requestId": "req"},
    }
    resp = report.handle(event)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "VALIDATION_ERROR"


def test_report_get_malformed_week_key_returns_400(wired: Any) -> None:
    # GET applies the same ISO week_key format validation as POST (SEC-006 AC-1).
    resp = _get(LEAD, week_key="2024-XX")
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "VALIDATION_ERROR"


def test_report_post_well_formed_week_key_returns_200(wired: Any) -> None:
    # Regression guard: a valid ISO week_key still passes validation and generates.
    _seed()
    event = {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {ADMIN}"},
        "body": json.dumps({"week_key": WEEK}),
        "requestContext": {"requestId": "req"},
    }
    resp = report.handle(event)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["week_key"] == WEEK


def test_report_scheduled_derives_week_from_event_time(wired: Any) -> None:
    """Issue 4: a recurring EventBridge run with NO week_key derives the week from ``time``.

    2024-01-17 falls in ISO week 2024-W03. The scheduled event carries only the EventBridge
    ``time`` (not a hard-coded week_key), and the report targets that week's events.
    """
    _seed()
    result = report.handle({"time": "2024-01-17T06:00:00Z"})
    assert result["week_key"] == WEEK
    assert len(result["technician_production"]) == 2


def test_report_scheduled_without_time_uses_current_week(wired: Any) -> None:
    """Issue 4: scheduled run with neither week_key nor time falls back to the current week."""
    # No seeded events for the current week -> empty but well-formed report (no crash).
    result = report.handle({})
    assert "week_key" in result
    assert result["technician_production"] == []


def test_report_rejects_impossible_iso_weeks(wired: Any) -> None:
    """Issue 10: W00 and W54-W99 are impossible ISO weeks and must fail validation (400)."""
    for bad in ["2024-W00", "2024-W54", "2024-W99"]:
        resp = _get(ADMIN, week_key=bad)
        assert resp["statusCode"] == 400, f"expected 400 for {bad}"
        assert json.loads(resp["body"])["error"]["code"] == "VALIDATION_ERROR"


def test_report_accepts_valid_week_boundaries(wired: Any) -> None:
    """Issue 10 regression: W01 and W53 remain valid ISO weeks."""
    for good in ["2024-W01", "2024-W53"]:
        resp = _get(ADMIN, week_key=good)
        assert resp["statusCode"] == 200, f"expected 200 for {good}"
