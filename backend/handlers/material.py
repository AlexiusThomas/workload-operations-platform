"""MaterialRequirement API handler and Material Runner workflow (Task 13).

Implements the MaterialRequirement endpoints from the design's API Design section:

- ``GET  /v1/work-packages/{id}/material-requirements`` — list by WorkPackage.
- ``GET  /v1/material-requirements``                     — list by status (Runner queue).
- ``POST /v1/work-packages/{id}/material-requirements``  — Manager_Admin creation with a
  computed, immutable ``total_meters_required`` (FR-003 AC-3, BR-017).
- ``POST /v1/material-requirements/{id}/material-claim``  — atomic claim, 409 on race
  (FR-004 AC-1, AC-2).
- ``POST /v1/material-requirements/{id}/material-deliver``— idempotent delivery that emits a
  MaterialEvent and auto-evaluates READY_FOR_PREP (FR-004 AC-4..AC-7, BR-011, BR-012).

Every status transition is written atomically with its AuditEvent (delivery adds a
MaterialEvent) via a single ``TransactWriteItems`` (FR-017 AC-5). Runner identity is
resolved from server-side session context only (FR-004 AC-9).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from uuid import uuid4

from botocore.exceptions import ClientError

from backend.auth import middleware as auth
from backend.data import repositories as repo
from backend.domain import material as material_domain
from backend.domain import reporting
from backend.handlers import common, validation
from backend.handlers.middleware import error_handler
from backend.observability import errors, metrics

MATERIAL_REQUIRED = "MATERIAL_REQUIRED"
MATERIAL_CLAIMED = "MATERIAL_CLAIMED"
MATERIAL_DELIVERED = "MATERIAL_DELIVERED"
READY_FOR_PREP = "READY_FOR_PREP"

_CONDITION_FAILED = "ConditionalCheckFailedException"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _is_condition_failure(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    if code in (_CONDITION_FAILED, "TransactionCanceledException"):
        return True
    reasons = exc.response.get("CancellationReasons") or []
    return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)


def _load(material_req_id: str) -> Dict[str, Any]:
    material_req = repo.get_material_requirement(material_req_id)
    if material_req is None:
        raise errors.ResourceNotFoundError("material requirement not found")
    return material_req


def _mr_response(material_req: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "material_req_id": material_req.get("material_req_id"),
        "work_package_id": material_req.get("work_package_id"),
        "work_unit_id": material_req.get("work_unit_id"),
        "material_type": material_req.get("material_type"),
        "cable_length_m": material_req.get("cable_length_m"),
        "qty_required": material_req.get("qty_required"),
        "total_meters_required": material_req.get("total_meters_required"),
        "qty_delivered": material_req.get("qty_delivered"),
        "meters_delivered": material_req.get("meters_delivered"),
        "runner_id": material_req.get("runner_id"),
        "status": material_req.get("status"),
    }


# ---------------------------------------------------------------------------
# Create (FR-003)
# ---------------------------------------------------------------------------
def _create(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "create_material_requirement")
    work_package_id = common.required_path_param(event, "work_package_id")
    body = common.parse_body(event)
    validation.validate_operation("material_create", body)

    # Domain validation computes the immutable total_meters_required (FR-003 AC-3/AC-4).
    try:
        values = material_domain.create_material_requirement(
            material_type=body["material_type"],
            cable_length_m=float(body["cable_length_m"]),
            qty_required=int(body["qty_required"]),
        )
    except material_domain.MaterialValidationError as exc:
        raise errors.ValidationError(str(exc)) from exc

    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        material_req_id = str(uuid4())
        ts = _iso_now()
        canonical = {
            "material_req_id": material_req_id,
            "work_package_id": work_package_id,
            "work_unit_id": body.get("work_unit_id"),
            "material_type": values.material_type,
            "cable_length_m": values.cable_length_m,
            "qty_required": values.qty_required,
            "total_meters_required": values.total_meters_required,
            "qty_delivered": values.qty_delivered,
            "meters_delivered": values.meters_delivered,
            "runner_id": None,
            "status": values.status,
            "created_at": ts,
            "last_updated_at": ts,
        }
        repo.write_material_transition(
            canonical,
            action_type="MATERIAL_REQ_CREATED",
            actor_id=user_ctx.user_id,
            actor_role=user_ctx.role,
            before_state=None,
            after_state=values.status,
            correlation_id=correlation_id,
        )
        return 201, _mr_response(canonical)

    return common.run_idempotent(
        operation="create_material_requirement",
        resource_id=work_package_id,
        request_body=body,
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Claim (FR-004 AC-1, AC-2)
# ---------------------------------------------------------------------------
def _claim(event: Dict[str, Any], material_req_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "material_claim")
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        material_req = _load(material_req_id)
        current = str(material_req.get("status"))
        if current != MATERIAL_REQUIRED:
            # Already claimed / delivered — concurrent claim is a conflict (FR-004 AC-2/AC-3).
            metrics.increment(metrics.MATERIAL_CLAIM_CONFLICT)
            raise errors.MaterialClaimConflictError("material requirement is already claimed")

        updated = dict(material_req)
        updated["status"] = MATERIAL_CLAIMED
        updated["runner_id"] = user_ctx.user_id
        updated["last_updated_at"] = _iso_now()
        try:
            repo.write_material_transition(
                updated,
                action_type="MATERIAL_REQ_CLAIMED",
                actor_id=user_ctx.user_id,
                actor_role=user_ctx.role,
                before_state=MATERIAL_REQUIRED,
                after_state=MATERIAL_CLAIMED,
                correlation_id=correlation_id,
                condition_expression="#status = :required",
                expression_attribute_names={"#status": "status"},
                expression_attribute_values={":required": MATERIAL_REQUIRED},
            )
        except ClientError as exc:
            if _is_condition_failure(exc):
                metrics.increment(metrics.MATERIAL_CLAIM_CONFLICT)
                raise errors.MaterialClaimConflictError("material requirement is already claimed")
            raise
        return 200, _mr_response(updated)

    return common.run_idempotent(
        operation="material_claim",
        resource_id=f"{material_req_id}:{user_ctx.user_id}",
        request_body={},
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Deliver (FR-004 AC-4..AC-7)
# ---------------------------------------------------------------------------
def _build_material_event(material_req: Dict[str, Any], runner_id: str) -> Dict[str, Any]:
    ts = _iso_now()
    week_key = reporting.compute_week_key(ts)
    return {
        "material_event_id": str(uuid4()),
        "event_type#week_key": f"ME#{week_key}",
        "material_req_id": material_req.get("material_req_id"),
        "work_package_id": material_req.get("work_package_id"),
        "work_unit_id": material_req.get("work_unit_id"),
        "material_type": material_req.get("material_type"),
        "cable_length_m": material_req.get("cable_length_m"),
        "qty_delivered": material_req.get("qty_delivered"),
        "meters_delivered": material_req.get("meters_delivered"),
        "runner_id": runner_id,
        "timestamp": ts,
        "week_key": week_key,
        "week_key#timestamp": f"{week_key}#{ts}",
    }


def _sibling_scope(material_req: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the MaterialRequirement projections that share this one's package/unit scope.

    Auto READY_FOR_PREP evaluates all requirements for the WorkPackage, or for the WorkUnit
    when the delivered requirement is unit-scoped (FR-004 AC-5).
    """
    work_package_id = str(material_req.get("work_package_id"))
    siblings = repo.list_material_requirements_by_package(work_package_id)
    work_unit_id = material_req.get("work_unit_id")
    if work_unit_id is not None:
        siblings = [s for s in siblings if s.get("work_unit_id") == work_unit_id]
    return siblings


def _deliver(event: Dict[str, Any], material_req_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "material_deliver")
    body = common.parse_body(event)
    validation.validate_operation("material_deliver", body)
    qty_delivered = int(body["qty_delivered"])
    meters_delivered = float(body["meters_delivered"])
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        material_req = _load(material_req_id)
        current = str(material_req.get("status"))
        if current != MATERIAL_CLAIMED:
            raise errors.InvalidStateTransitionError("delivery requires MATERIAL_CLAIMED status")
        # Ownership: the delivering runner must be the runner who claimed (Admin bypasses).
        auth.require_runner_match(user_ctx, material_req.get("runner_id"))

        updated = dict(material_req)
        updated["status"] = MATERIAL_DELIVERED
        updated["qty_delivered"] = qty_delivered
        updated["meters_delivered"] = meters_delivered
        updated["runner_id"] = material_req.get("runner_id")
        updated["last_updated_at"] = _iso_now()
        material_event = _build_material_event(updated, str(material_req.get("runner_id")))
        try:
            repo.write_material_transition(
                updated,
                action_type="MATERIAL_REQ_DELIVERED",
                actor_id=user_ctx.user_id,
                actor_role=user_ctx.role,
                before_state=MATERIAL_CLAIMED,
                after_state=MATERIAL_DELIVERED,
                correlation_id=correlation_id,
                condition_expression="#status = :claimed",
                expression_attribute_names={"#status": "status"},
                expression_attribute_values={":claimed": MATERIAL_CLAIMED},
                material_event=material_event,
            )
        except ClientError as exc:
            if _is_condition_failure(exc):
                raise errors.InvalidStateTransitionError(
                    "delivery requires MATERIAL_CLAIMED status"
                )
            raise
        metrics.increment(metrics.MATERIAL_DELIVERY)

        # Auto READY_FOR_PREP when every requirement in scope is now delivered (FR-004 AC-5).
        _maybe_ready_for_prep(updated, correlation_id)

        # Reflect the final persisted state (possibly READY_FOR_PREP) in the response.
        final = repo.get_material_requirement(material_req_id) or updated
        return 200, {
            "material_requirement": _mr_response(final),
            "material_event_id": material_event["material_event_id"],
        }

    return common.run_idempotent(
        operation="material_deliver",
        resource_id=material_req_id,
        request_body=body,
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


def _maybe_ready_for_prep(delivered: Dict[str, Any], correlation_id: str) -> None:
    """Transition the delivered requirement to READY_FOR_PREP if its whole scope is delivered.

    The scope is delivered when every sibling MaterialRequirement (package- or unit-scoped)
    is in MATERIAL_DELIVERED (or already READY_FOR_PREP). The transition is applied to the
    just-delivered requirement's canonical item (design: MaterialRequirement State Machine).
    """
    siblings = _sibling_scope(delivered)
    statuses = [str(s.get("proj_status") or s.get("status")) for s in siblings]
    all_delivered = statuses and all(s in (MATERIAL_DELIVERED, READY_FOR_PREP) for s in statuses)
    if not all_delivered:
        return

    ready = dict(repo.get_material_requirement(str(delivered["material_req_id"])) or delivered)
    ready["status"] = READY_FOR_PREP
    ready["last_updated_at"] = _iso_now()
    try:
        repo.write_material_transition(
            ready,
            action_type="MATERIAL_REQ_READY_FOR_PREP",
            actor_id="SYSTEM",
            actor_role="SYSTEM",
            before_state=MATERIAL_DELIVERED,
            after_state=READY_FOR_PREP,
            correlation_id=correlation_id,
            condition_expression="#status = :delivered",
            expression_attribute_names={"#status": "status"},
            expression_attribute_values={":delivered": MATERIAL_DELIVERED},
        )
    except ClientError as exc:
        # Already advanced (idempotent re-eval) — ignore a conditional miss.
        if not _is_condition_failure(exc):
            raise


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _list_by_package(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "view_material_requirements")
    work_package_id = common.required_path_param(event, "work_package_id")
    items = repo.list_material_requirements_by_package(work_package_id)
    summaries = [
        {
            "material_req_id": item.get("material_req_id"),
            "material_type": item.get("material_type"),
            "qty_required": item.get("qty_required"),
            "qty_delivered": item.get("qty_delivered"),
            "meters_delivered": item.get("meters_delivered"),
            "status": item.get("proj_status") or item.get("status"),
        }
        for item in items
    ]
    return common.success(200, {"items": summaries}, correlation_id)


def _list_by_status(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "view_material_requirements")
    status = common.query_param(event, "status") or MATERIAL_REQUIRED
    items, next_token = repo.query_material_requirements_by_status(
        status, next_token=common.query_param(event, "next_token")
    )
    summaries = [_mr_response(item) for item in items]
    body = {"items": summaries, "next_token": next_token, "count": len(summaries)}
    return common.success(200, body, correlation_id)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
@error_handler
def handle(
    event: Dict[str, Any], context: Any = None, *, correlation_id: str = ""
) -> Dict[str, Any]:
    """Route a MaterialRequirement request by method, path, and ``action``."""
    method = (event.get("httpMethod") or "GET").upper()
    action = common.path_param(event, "action")
    material_req_id = common.path_param(event, "material_req_id")
    work_package_id = common.path_param(event, "work_package_id")

    if method == "POST" and action == "material-claim" and material_req_id:
        return _claim(event, material_req_id, correlation_id)
    if method == "POST" and action == "material-deliver" and material_req_id:
        return _deliver(event, material_req_id, correlation_id)
    if method == "POST" and work_package_id is not None:
        return _create(event, correlation_id)

    if method == "GET":
        if work_package_id is not None:
            return _list_by_package(event, correlation_id)
        return _list_by_status(event, correlation_id)

    raise errors.ValidationError(f"unsupported material route: {method} {action}")
