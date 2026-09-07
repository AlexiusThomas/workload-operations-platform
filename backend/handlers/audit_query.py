"""Audit-query API handler (Task 14.1).

Read-only ``GET /v1/audit/{entity_type}/{entity_id}`` returning all AuditEvents for a
WorkUnit, WorkPackage, or MaterialRequirement in chronological order with native cursor
pagination (FR-017 AC-3, AC-4). Restricted to Verifier_Lead and Manager_Admin via the RBAC
matrix ``view_audit`` operation (FR-017). This handler performs no writes.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.auth import middleware as auth
from backend.data import repositories as repo
from backend.handlers import common
from backend.handlers.middleware import error_handler
from backend.observability import errors

#: Map the URL entity-type segment to the stored AuditEvent entity_type.
_ENTITY_TYPE_MAP: Dict[str, str] = {
    "work-unit": repo.ENTITY_WORK_UNIT,
    "work-package": repo.ENTITY_WORK_PACKAGE,
    "material-requirement": repo.ENTITY_MATERIAL_REQUIREMENT,
}


def _audit_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "audit_event_id": item.get("audit_event_id"),
        "entity_type": item.get("entity_type"),
        "entity_id": item.get("entity_id"),
        "action_type": item.get("action_type"),
        "actor_id": item.get("actor_id"),
        "actor_role": item.get("actor_role"),
        "before_state": item.get("before_state"),
        "after_state": item.get("after_state"),
        "metadata": item.get("metadata"),
        "timestamp": item.get("timestamp"),
        "correlation_id": item.get("correlation_id"),
    }


@error_handler
def handle(
    event: Dict[str, Any], context: Any = None, *, correlation_id: str = ""
) -> Dict[str, Any]:
    """Return AuditEvents for the addressed entity in chronological order (Lead/Admin only)."""
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "view_audit")

    entity_type_segment = common.required_path_param(event, "entity_type")
    entity_id = common.required_path_param(event, "entity_id")

    entity_type = _ENTITY_TYPE_MAP.get(entity_type_segment)
    if entity_type is None:
        raise errors.ValidationError(f"unknown entity_type: {entity_type_segment}")

    items, next_token = repo.query_audit_events(
        entity_type,
        entity_id,
        next_token=common.query_param(event, "next_token"),
    )
    summaries = [_audit_summary(item) for item in items]
    body = {"items": summaries, "next_token": next_token, "count": len(summaries)}
    return common.success(200, body, correlation_id)
