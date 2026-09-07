"""WorkPackage API handler (Task 10.1).

Implements the WorkPackage endpoints from the design's API Design section:

- ``GET  /v1/work-packages/{id}``  — returns the WorkPackage with its *derived* aggregate
  state (computed from child WorkUnit states and MaterialRequirement statuses via
  :func:`backend.domain.aggregate_state.compute_work_package_state`) plus child summaries
  (FR-021 AC-3). All authenticated roles may read.
- ``GET  /v1/work-packages``       — paginated list with optional site/work_type/date
  filters (all authenticated roles).
- ``POST /v1/work-packages``        — Manager_Admin-only, idempotent creation of a
  WorkPackage and optional WorkUnits, with one AuditEvent per created entity
  (FR-002 AC-1..AC-6, FR-019 AC-2).

Actor identity is resolved exclusively from server-side session context (FR-018 AC-1) and
authorization is enforced server-side against the RBAC matrix (SEC-002 AC-1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

from backend.auth import middleware as auth
from backend.data import repositories as repo
from backend.domain import aggregate_state
from backend.handlers import common, validation
from backend.handlers.middleware import error_handler
from backend.observability import errors


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sk_gsi1(site: str, work_type: str, scheduled_date: str) -> str:
    return f"{site}#{work_type}#{scheduled_date}"


def _work_unit_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """Project a child WorkUnit item to the summary returned in a WorkPackage response."""
    return {
        "work_unit_id": item.get("work_unit_id"),
        "state": item.get("state"),
        "claimed_by": item.get("claimed_by"),
        "required_qty": item.get("required_qty"),
        "completed_qty": item.get("completed_qty"),
        "work_type": item.get("work_type"),
    }


def _material_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """Project a child MaterialRequirement item to a summary for a WorkPackage response."""
    return {
        "material_req_id": item.get("material_req_id"),
        "status": item.get("proj_status") or item.get("status"),
        "material_type": item.get("material_type"),
        "qty_required": item.get("qty_required"),
        "qty_delivered": item.get("qty_delivered"),
    }


def _derived_state(
    work_units: List[Dict[str, Any]],
    material_requirements: List[Dict[str, Any]],
    committed: bool,
) -> str:
    """Compute the derived WorkPackage state, honouring the ingestion committed gate.

    A WorkPackage that has not completed ingestion (``committed=false``) is reported as
    ``IMPORTED`` rather than deriving an operational state from staged children (design:
    package-level visibility gate).
    """
    if not committed:
        return aggregate_state.WP_IMPORTED
    unit_states = [str(u.get("state")) for u in work_units if u.get("state") is not None]
    material_statuses = [
        str(m.get("proj_status") or m.get("status"))
        for m in material_requirements
        if (m.get("proj_status") or m.get("status")) is not None
    ]
    return aggregate_state.compute_work_package_state(unit_states, material_statuses)


def _get_work_package(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "get_work_package")

    work_package_id = common.required_path_param(event, "work_package_id")
    work_package = repo.get_work_package(work_package_id)
    if work_package is None:
        raise errors.ResourceNotFoundError("work package not found")

    children = repo.list_work_units_by_package(work_package_id)
    materials = repo.list_material_requirements_by_package(work_package_id)
    committed = bool(work_package.get("committed", True))

    body = {
        "work_package_id": work_package["work_package_id"],
        "site": work_package.get("site"),
        "rack_position": work_package.get("rack_position"),
        "work_type": work_package.get("work_type"),
        "original_scheduled_date": work_package.get("original_scheduled_date"),
        "current_scheduled_date": work_package.get("current_scheduled_date"),
        "rollover_count": work_package.get("rollover_count"),
        "derived_state": _derived_state(children, materials, committed),
        "created_at": work_package.get("created_at"),
        "updated_at": work_package.get("updated_at"),
        "work_units": [_work_unit_summary(c) for c in children],
        "material_requirements": [_material_summary(m) for m in materials],
    }
    return common.success(200, body, correlation_id)


def _list_work_packages(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "get_work_package")

    items, next_token = repo.list_work_packages(
        site=common.query_param(event, "site"),
        work_type=common.query_param(event, "work_type"),
        current_scheduled_date=common.query_param(event, "current_scheduled_date"),
        next_token=common.query_param(event, "next_token"),
    )
    summaries = [
        {
            "work_package_id": item.get("work_package_id"),
            "site": item.get("site"),
            "work_type": item.get("work_type"),
            "current_scheduled_date": item.get("current_scheduled_date"),
            "state": item.get("state"),
        }
        for item in items
    ]
    body = {"items": summaries, "next_token": next_token, "count": len(summaries)}
    return common.success(200, body, correlation_id)


def _create_work_package(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "create_work_package")

    body = common.parse_body(event)
    validation.validate_operation("work_package_create", body)

    key = common.idempotency_key(event)

    def _execute() -> Any:
        work_package_id = str(uuid4())
        ts = _iso_now()
        scheduled = body["scheduled_date"]
        work_package = {
            "work_package_id": work_package_id,
            "site": body["site"],
            "rack_position": body["rack_position"],
            "work_type": body["work_type"],
            "original_scheduled_date": scheduled,
            "current_scheduled_date": scheduled,
            "rollover_count": 0,
            "committed": True,
            "state": "AVAILABLE" if body.get("work_units") else "IMPORTED",
            "created_at": ts,
            "updated_at": ts,
        }
        work_units: List[Dict[str, Any]] = []
        for raw_unit in body.get("work_units", []):
            unit_id = str(uuid4())
            work_units.append(
                {
                    "work_unit_id": unit_id,
                    "work_package_id": work_package_id,
                    "site": body["site"],
                    "work_type": raw_unit["work_type"],
                    "state": "AVAILABLE",
                    "sk_gsi1": _sk_gsi1(body["site"], raw_unit["work_type"], scheduled),
                    "claimed_by": None,
                    "required_qty": raw_unit["required_qty"],
                    "completed_qty": 0,
                    "tote_id": None,
                    "rework_count": 0,
                    "current_scheduled_date": scheduled,
                    "original_scheduled_date": scheduled,
                    "rollover_count": 0,
                    "version": 0,
                    "created_at": ts,
                    "updated_at": ts,
                }
            )
        repo.create_work_package_with_units(
            work_package,
            work_units,
            actor_id=user_ctx.user_id,
            actor_role=user_ctx.role,
            correlation_id=correlation_id,
        )
        response = {
            "work_package_id": work_package_id,
            "site": work_package["site"],
            "rack_position": work_package["rack_position"],
            "work_type": work_package["work_type"],
            "current_scheduled_date": scheduled,
            "state": work_package["state"],
            "work_units": [
                {"work_unit_id": u["work_unit_id"], "state": u["state"]} for u in work_units
            ],
        }
        return 201, response

    return common.run_idempotent(
        operation="create_work_package",
        resource_id="work-package",
        request_body=body,
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


@error_handler
def handle(
    event: Dict[str, Any], context: Any = None, *, correlation_id: str = ""
) -> Dict[str, Any]:
    """Route a WorkPackage request based on HTTP method and path.

    The router inspects the API Gateway ``httpMethod`` and whether a ``work_package_id``
    path parameter is present.
    """
    method = (event.get("httpMethod") or "GET").upper()
    if method == "POST":
        return _create_work_package(event, correlation_id)
    if common.path_param(event, "work_package_id") is not None:
        return _get_work_package(event, correlation_id)
    return _list_work_packages(event, correlation_id)
