"""WorkUnit API handler — all state transitions (Task 11).

Implements every WorkUnit route from the design's API Design and WorkUnit State Machine
sections. Each state transition is written atomically with its AuditEvent (and, on
verification, a ProductionEvent) via a single DynamoDB ``TransactWriteItems`` using
:func:`backend.data.repositories.write_work_unit_transition` (FR-017 AC-5, NFR-009 AC-1).

Concurrency (BR-001, FR-006 AC-2): claim / assign carry a conditional write on the current
state; a lost race raises ``TransactionCanceledException`` which is mapped to
409 ``CLAIM_CONFLICT``.

Identity (FR-018 AC-1, BR-008): the actor is resolved exclusively from server-side session
context. Authorization is enforced server-side against the RBAC matrix; ownership rules use
the ``require_*`` helpers (design: Ownership Rules).

Routes (dispatched by the ``action`` path parameter + HTTP method):
    GET   /v1/work-units                      -> queue
    GET   /v1/work-units/stale                -> stale active work (Lead/Admin)
    GET   /v1/work-units/{id}                 -> detail
    POST  /v1/work-units/{id}/claim           -> AVAILABLE->CLAIMED or REWORK_REQUIRED->IN_PROGRESS
    POST  /v1/work-units/{id}/assign          -> exception assignment (Lead/Admin)
    POST  /v1/work-units/{id}/start           -> CLAIMED->IN_PROGRESS (owner)
    PATCH /v1/work-units/{id}/quantity        -> update completed_qty (owner)
    POST  /v1/work-units/{id}/prep-complete   -> IN_PROGRESS->PREP_COMPLETE (owner)
    POST  /v1/work-units/{id}/label           -> PREP_COMPLETE->LABELED
    POST  /v1/work-units/{id}/tote-assign     -> LABELED->TOTE_ASSIGNED
    POST  /v1/work-units/{id}/ready-to-verify -> TOTE_ASSIGNED->READY_TO_VERIFY
    POST  /v1/work-units/{id}/verify          -> READY_TO_VERIFY->VERIFIED (+ProductionEvent)
    POST  /v1/work-units/{id}/fail-verify     -> READY_TO_VERIFY->REWORK_REQUIRED
    POST  /v1/work-units/{id}/complete        -> VERIFIED->COMPLETE
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple
from uuid import uuid4

from botocore.exceptions import ClientError

from backend.auth import middleware as auth
from backend.data import repositories as repo
from backend.domain import quantity as quantity_domain
from backend.domain import reporting
from backend.domain import state_machine as sm
from backend.handlers import common, validation
from backend.handlers.ingestion import suppress_uncommitted
from backend.handlers.middleware import error_handler
from backend.observability import errors, metrics

_CONDITION_FAILED = "ConditionalCheckFailedException"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sk_gsi1(site: str, work_type: str, scheduled_date: str) -> str:
    return f"{site}#{work_type}#{scheduled_date}"


def _is_condition_failure(exc: ClientError) -> bool:
    """Return True if a ClientError is a conditional-check / transaction-cancelled failure."""
    code = exc.response.get("Error", {}).get("Code", "")
    if code in (_CONDITION_FAILED, "TransactionCanceledException"):
        return True
    reasons = exc.response.get("CancellationReasons") or []
    return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)


def _load_work_unit(work_unit_id: str) -> Dict[str, Any]:
    """Fetch a WorkUnit canonical item or raise 404."""
    work_unit = repo.get_work_unit(work_unit_id)
    if work_unit is None:
        raise errors.ResourceNotFoundError("work unit not found")
    return work_unit


def _next_state_or_reject(current_state: str, transition: str) -> str:
    """Return the new state for a transition or raise INVALID_STATE_TRANSITION (FR-021 AC-2)."""
    result = sm.apply_transition(current_state, transition)
    if not result.is_valid:
        raise errors.InvalidStateTransitionError(
            f"cannot apply '{transition}' from state '{current_state}'"
        )
    return result.new_state


def _wu_response(work_unit: Dict[str, Any]) -> Dict[str, Any]:
    """Build the standard WorkUnit response body."""
    return {
        "work_unit_id": work_unit.get("work_unit_id"),
        "work_package_id": work_unit.get("work_package_id"),
        "site": work_unit.get("site"),
        "work_type": work_unit.get("work_type"),
        "state": work_unit.get("state"),
        "claimed_by": work_unit.get("claimed_by"),
        "required_qty": work_unit.get("required_qty"),
        "completed_qty": work_unit.get("completed_qty"),
        "tote_id": work_unit.get("tote_id"),
        "rework_count": work_unit.get("rework_count"),
        "current_scheduled_date": work_unit.get("current_scheduled_date"),
        "original_scheduled_date": work_unit.get("original_scheduled_date"),
        "rollover_count": work_unit.get("rollover_count"),
    }


def _updated_canonical(work_unit: Dict[str, Any], new_state: str, **changes: Any) -> Dict[str, Any]:
    """Return a copy of the canonical WorkUnit with the new state, changes, and timestamp."""
    updated = dict(work_unit)
    updated["state"] = new_state
    updated["updated_at"] = _iso_now()
    # Keep the GSI-1 key populated for every operational state so both the AVAILABLE queue
    # (FR-005) and the stale-active-work query per non-terminal state (FR-022 AC-7) surface
    # the unit. Only the ingestion sentinel states (STAGING/INGESTING) are excluded from
    # GSI-1 (design: two-phase staging/commit); those are never produced here.
    updated["sk_gsi1"] = _sk_gsi1(
        str(work_unit.get("site")),
        str(work_unit.get("work_type")),
        str(work_unit.get("current_scheduled_date")),
    )
    updated.update(changes)
    return updated


# ---------------------------------------------------------------------------
# Mutating transition helper
# ---------------------------------------------------------------------------
def _apply_transition_write(
    work_unit: Dict[str, Any],
    *,
    transition: str,
    action_type: str,
    user_ctx: Any,
    correlation_id: str,
    before_state: str,
    expected_state: str,
    extra_changes: Optional[Dict[str, Any]] = None,
    extra_condition: Optional[str] = None,
    extra_names: Optional[Dict[str, str]] = None,
    extra_values: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    production_event: Optional[Dict[str, Any]] = None,
    conflict_error: Optional[Callable[[str], errors.WOPError]] = None,
) -> Dict[str, Any]:
    """Compute the new state, then atomically write canonical + projection + AuditEvent.

    The canonical Put condition requires the WorkUnit still be in ``expected_state`` (atomic
    guard); a failed condition is mapped to a conflict/invalid-state error.
    """
    new_state = _next_state_or_reject(before_state, transition)
    updated = _updated_canonical(work_unit, new_state, **(extra_changes or {}))

    condition = "#state = :expected"
    names = {"#state": "state"}
    values = {":expected": expected_state}
    if extra_condition:
        condition = f"{condition} AND {extra_condition}"
    if extra_names:
        names.update(extra_names)
    if extra_values:
        values.update(extra_values)

    try:
        repo.write_work_unit_transition(
            updated,
            action_type=action_type,
            actor_id=user_ctx.user_id,
            actor_role=user_ctx.role,
            before_state=before_state,
            after_state=new_state,
            correlation_id=correlation_id,
            condition_expression=condition,
            expression_attribute_names=names,
            expression_attribute_values=values,
            metadata=metadata,
            production_event=production_event,
        )
    except ClientError as exc:
        if _is_condition_failure(exc):
            if conflict_error is not None:
                raise conflict_error("state changed concurrently")
            raise errors.InvalidStateTransitionError("work unit is no longer in the expected state")
        raise
    return updated


# ---------------------------------------------------------------------------
# Claim / rework-claim (FR-006, FR-015 AC-3)
# ---------------------------------------------------------------------------
def _claim(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    # Authorize the base claim operation up-front (role gate). The rework-claim path adds a
    # stricter Technician-only check inside execute once the current state is known.
    auth.authorize_operation(user_ctx, "claim")
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        # Re-read the WorkUnit inside execute so an idempotent REPLAY short-circuits before
        # any state inspection (a replayed claim must not be re-evaluated as a conflict).
        work_unit = _load_work_unit(work_unit_id)
        current = str(work_unit.get("state"))

        if current == sm.REWORK_REQUIRED:
            # Rework re-claim: REWORK_REQUIRED -> IN_PROGRESS, Technician only (FR-015 AC-5),
            # preserving completed_qty and rework history (BR-010).
            auth.authorize_operation(user_ctx, "rework_claim")
            action_type, expected = "WORK_UNIT_REWORK_CLAIMED", sm.REWORK_REQUIRED
        elif current == sm.AVAILABLE:
            action_type, expected = "WORK_UNIT_CLAIMED", sm.AVAILABLE
        else:
            # Already claimed / in flight — a claim by another actor is a conflict, not a
            # generic state error (design API: 409 CLAIM_CONFLICT; FR-006 AC-2/AC-3).
            metrics.increment(metrics.CLAIM_CONFLICT)
            raise errors.ClaimConflictError("work unit is already claimed")

        try:
            updated = _apply_transition_write(
                work_unit,
                transition=sm.CLAIM,
                action_type=action_type,
                user_ctx=user_ctx,
                correlation_id=correlation_id,
                before_state=current,
                expected_state=expected,
                extra_changes={"claimed_by": user_ctx.user_id},
                conflict_error=lambda m: errors.ClaimConflictError(m),
            )
        except errors.ClaimConflictError:
            metrics.increment(metrics.CLAIM_CONFLICT)
            raise
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="claim_work_unit",
        resource_id=f"{work_unit_id}:{user_ctx.user_id}",
        request_body={},
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


def _assign(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "assign")
    body = common.parse_body(event)
    validation.validate_operation("assignment", body)
    target_tech = body["technician_id"]

    work_unit = _load_work_unit(work_unit_id)
    current = str(work_unit.get("state"))
    if current not in (sm.AVAILABLE, sm.CLAIMED):
        raise errors.InvalidStateTransitionError("assignment requires AVAILABLE or CLAIMED state")

    previous_claimant = work_unit.get("claimed_by")
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = _updated_canonical(work_unit, sm.CLAIMED, claimed_by=target_tech)
        try:
            repo.write_work_unit_transition(
                updated,
                action_type="WORK_UNIT_ASSIGNED",
                actor_id=user_ctx.user_id,
                actor_role=user_ctx.role,
                before_state=current,
                after_state=sm.CLAIMED,
                correlation_id=correlation_id,
                condition_expression="#state = :expected",
                expression_attribute_names={"#state": "state"},
                expression_attribute_values={":expected": current},
                metadata={
                    "previous_claimant": previous_claimant,
                    "new_assignee": target_tech,
                    "assigning_user": user_ctx.user_id,
                },
            )
        except ClientError as exc:
            if _is_condition_failure(exc):
                raise errors.ClaimConflictError("state changed concurrently")
            raise
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="assign_work_unit",
        resource_id=work_unit_id,
        request_body=body,
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Owner-only transitions: start, quantity, prep-complete (FR-008/009/010)
# ---------------------------------------------------------------------------
def _start(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "start")
    work_unit = _load_work_unit(work_unit_id)
    auth.require_owner(user_ctx, work_unit.get("claimed_by"))
    current = str(work_unit.get("state"))
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = _apply_transition_write(
            work_unit,
            transition=sm.START,
            action_type="WORK_UNIT_STARTED",
            user_ctx=user_ctx,
            correlation_id=correlation_id,
            before_state=current,
            expected_state=sm.CLAIMED,
        )
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="start_work_unit",
        resource_id=work_unit_id,
        request_body={},
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


def _update_quantity(
    event: Dict[str, Any], work_unit_id: str, correlation_id: str
) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "quantity")
    body = common.parse_body(event)
    validation.validate_operation("quantity_update", body)
    new_qty = int(body["completed_qty"])

    work_unit = _load_work_unit(work_unit_id)
    auth.require_owner(user_ctx, work_unit.get("claimed_by"))
    current = str(work_unit.get("state"))
    if current != sm.IN_PROGRESS:
        raise errors.InvalidStateTransitionError("quantity updates require IN_PROGRESS state")

    required_qty = int(work_unit.get("required_qty", 0))
    result = quantity_domain.validate_quantity_update(required_qty, new_qty)
    if result.is_error:
        if result.error_code == quantity_domain.QUANTITY_EXCEEDED:
            raise errors.QuantityExceededError("completed_qty exceeds required_qty")
        raise errors.ValidationError("invalid completed_qty")

    previous_qty = int(work_unit.get("completed_qty", 0))
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = dict(work_unit)
        updated["completed_qty"] = new_qty
        updated["updated_at"] = _iso_now()
        updated["sk_gsi1"] = _sk_gsi1(
            str(work_unit.get("site")),
            str(work_unit.get("work_type")),
            str(work_unit.get("current_scheduled_date")),
        )
        try:
            repo.write_work_unit_transition(
                updated,
                action_type="WORK_UNIT_QUANTITY_UPDATED",
                actor_id=user_ctx.user_id,
                actor_role=user_ctx.role,
                before_state=sm.IN_PROGRESS,
                after_state=sm.IN_PROGRESS,
                correlation_id=correlation_id,
                condition_expression=(
                    "#state = :in_progress AND :new_qty <= required_qty AND claimed_by = :actor"
                ),
                expression_attribute_names={"#state": "state"},
                expression_attribute_values={
                    ":in_progress": sm.IN_PROGRESS,
                    ":new_qty": new_qty,
                    ":actor": user_ctx.user_id,
                },
                metadata={"previous_qty": previous_qty, "new_qty": new_qty},
            )
        except ClientError as exc:
            if _is_condition_failure(exc):
                raise errors.QuantityExceededError("completed_qty exceeds required_qty")
            raise
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="update_quantity",
        resource_id=work_unit_id,
        request_body=body,
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


def _prep_complete(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "prep_complete")
    work_unit = _load_work_unit(work_unit_id)
    auth.require_owner(user_ctx, work_unit.get("claimed_by"))
    current = str(work_unit.get("state"))

    if current == sm.IN_PROGRESS and int(work_unit.get("completed_qty", 0)) < int(
        work_unit.get("required_qty", 0)
    ):
        raise errors.IncompleteWorkError("completed_qty is less than required_qty")

    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = _apply_transition_write(
            work_unit,
            transition=sm.PREP_COMPLETE_TRANSITION,
            action_type="WORK_UNIT_PREP_COMPLETE",
            user_ctx=user_ctx,
            correlation_id=correlation_id,
            before_state=current,
            expected_state=sm.IN_PROGRESS,
            extra_condition="completed_qty = required_qty",
        )
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="prep_complete",
        resource_id=work_unit_id,
        request_body={},
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Owner-or-Lead/Admin transitions: label, tote-assign, ready-to-verify
# ---------------------------------------------------------------------------
def _label(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "label")
    work_unit = _load_work_unit(work_unit_id)
    auth.require_owner_or_lead_admin(user_ctx, work_unit.get("claimed_by"))
    current = str(work_unit.get("state"))
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = _apply_transition_write(
            work_unit,
            transition=sm.LABEL,
            action_type="WORK_UNIT_LABELED",
            user_ctx=user_ctx,
            correlation_id=correlation_id,
            before_state=current,
            expected_state=sm.PREP_COMPLETE,
        )
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="label",
        resource_id=work_unit_id,
        request_body={},
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


def _tote_assign(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "tote_assign")
    body = common.parse_body(event)
    validation.validate_operation("tote_assign", body)
    tote_id = body["tote_id"]

    work_unit = _load_work_unit(work_unit_id)
    auth.require_owner_or_lead_admin(user_ctx, work_unit.get("claimed_by"))
    current = str(work_unit.get("state"))
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = _apply_transition_write(
            work_unit,
            transition=sm.TOTE_ASSIGN,
            action_type="WORK_UNIT_TOTE_ASSIGNED",
            user_ctx=user_ctx,
            correlation_id=correlation_id,
            before_state=current,
            expected_state=sm.LABELED,
            extra_changes={"tote_id": tote_id},
            metadata={"tote_id": tote_id},
        )
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="tote_assign",
        resource_id=work_unit_id,
        request_body=body,
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


def _ready_to_verify(
    event: Dict[str, Any], work_unit_id: str, correlation_id: str
) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "ready_to_verify")
    work_unit = _load_work_unit(work_unit_id)
    auth.require_owner_or_lead_admin(user_ctx, work_unit.get("claimed_by"))
    current = str(work_unit.get("state"))

    tote_id = work_unit.get("tote_id")
    if not tote_id:
        raise errors.ToteRequiredError("a tote must be recorded before ready-to-verify")

    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = _apply_transition_write(
            work_unit,
            transition=sm.READY_TO_VERIFY_TRANSITION,
            action_type="WORK_UNIT_READY_TO_VERIFY",
            user_ctx=user_ctx,
            correlation_id=correlation_id,
            before_state=current,
            expected_state=sm.TOTE_ASSIGNED,
            extra_condition="attribute_exists(tote_id)",
        )
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="ready_to_verify",
        resource_id=work_unit_id,
        request_body={},
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Verify / fail-verify / complete (FR-014, FR-015, FR-016)
# ---------------------------------------------------------------------------
def _build_production_event(work_unit: Dict[str, Any], verifier_id: str) -> Dict[str, Any]:
    """Build an immutable ProductionEvent for a verified WorkUnit (FR-014 AC-2)."""
    ts = _iso_now()
    week_key = reporting.compute_week_key(ts)
    work_package = repo.get_work_package(str(work_unit.get("work_package_id")))
    rack_position = work_package.get("rack_position") if work_package else None
    production_event_id = str(uuid4())
    return {
        "production_event_id": production_event_id,
        "event_type#week_key": f"PE#{week_key}",
        "work_unit_id": work_unit.get("work_unit_id"),
        "work_package_id": work_unit.get("work_package_id"),
        "verifier_id": verifier_id,
        "technician_id": work_unit.get("claimed_by"),
        "completed_qty": work_unit.get("completed_qty"),
        "work_type": work_unit.get("work_type"),
        "site": work_unit.get("site"),
        "rack_position": rack_position,
        "timestamp": ts,
        "week_key": week_key,
        "week_key#timestamp": f"{week_key}#{ts}",
    }


def _verify(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "verify")
    work_unit = _load_work_unit(work_unit_id)
    current = str(work_unit.get("state"))
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        production_event = _build_production_event(work_unit, user_ctx.user_id)
        updated = _apply_transition_write(
            work_unit,
            transition=sm.VERIFY,
            action_type="WORK_UNIT_VERIFIED",
            user_ctx=user_ctx,
            correlation_id=correlation_id,
            before_state=current,
            expected_state=sm.READY_TO_VERIFY,
            production_event=production_event,
        )
        metrics.increment(metrics.VERIFICATION_PASS)
        response = _wu_response(updated)
        response["production_event_id"] = production_event["production_event_id"]
        return 200, response

    return common.run_idempotent(
        operation="verify",
        resource_id=work_unit_id,
        request_body={},
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


def _fail_verify(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "fail_verify")
    body = common.parse_body(event)
    validation.validate_operation("fail_verify", body)
    failure_reason = body["failure_reason"]

    work_unit = _load_work_unit(work_unit_id)
    current = str(work_unit.get("state"))
    new_rework_count = int(work_unit.get("rework_count", 0)) + 1
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = _apply_transition_write(
            work_unit,
            transition=sm.FAIL_VERIFY,
            action_type="WORK_UNIT_REWORK_REQUIRED",
            user_ctx=user_ctx,
            correlation_id=correlation_id,
            before_state=current,
            expected_state=sm.READY_TO_VERIFY,
            extra_changes={"rework_count": new_rework_count},
            metadata={"failure_reason": failure_reason},
        )
        metrics.increment(metrics.VERIFICATION_FAIL)
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="fail_verify",
        resource_id=work_unit_id,
        request_body=body,
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


def _complete(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "complete")
    work_unit = _load_work_unit(work_unit_id)
    current = str(work_unit.get("state"))
    if current != sm.VERIFIED:
        raise errors.InvalidStateTransitionError("verification is required before completion")
    key = common.idempotency_key(event)

    def _execute() -> Tuple[int, Dict[str, Any]]:
        updated = _apply_transition_write(
            work_unit,
            transition=sm.COMPLETE_TRANSITION,
            action_type="WORK_UNIT_COMPLETE",
            user_ctx=user_ctx,
            correlation_id=correlation_id,
            before_state=current,
            expected_state=sm.VERIFIED,
        )
        return 200, _wu_response(updated)

    return common.run_idempotent(
        operation="complete",
        resource_id=work_unit_id,
        request_body={},
        key=key,
        execute=_execute,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Reads: queue, detail, stale (FR-005, FR-022 AC-7)
# ---------------------------------------------------------------------------
def _queue(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "view_queue")
    state = common.query_param(event, "state") or sm.AVAILABLE
    items, next_token = repo.query_work_units_by_state(
        state,
        site=common.query_param(event, "site"),
        work_type=common.query_param(event, "work_type"),
        current_scheduled_date=common.query_param(event, "current_scheduled_date"),
        next_token=common.query_param(event, "next_token"),
    )
    # Suppress WorkUnits whose parent WorkPackage is not yet committed (visibility gate).
    if state == sm.AVAILABLE:
        items = suppress_uncommitted(items)
    summaries = [_wu_response(item) for item in items]
    body = {"items": summaries, "next_token": next_token, "count": len(summaries)}
    return common.success(200, body, correlation_id)


def _detail(event: Dict[str, Any], work_unit_id: str, correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "get_work_unit")
    work_unit = _load_work_unit(work_unit_id)
    return common.success(200, _wu_response(work_unit), correlation_id)


def _stale(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "view_stale")
    today = common.query_param(event, "today") or _iso_now()[:10]
    items = repo.query_stale_active_work(today=today)
    summaries = [_wu_response(item) for item in items]
    return common.success(200, {"items": summaries, "count": len(summaries)}, correlation_id)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
_POST_ACTIONS: Dict[str, Callable[[Dict[str, Any], str, str], Dict[str, Any]]] = {
    "claim": _claim,
    "assign": _assign,
    "start": _start,
    "prep-complete": _prep_complete,
    "label": _label,
    "tote-assign": _tote_assign,
    "ready-to-verify": _ready_to_verify,
    "verify": _verify,
    "fail-verify": _fail_verify,
    "complete": _complete,
}


@error_handler
def handle(
    event: Dict[str, Any], context: Any = None, *, correlation_id: str = ""
) -> Dict[str, Any]:
    """Route a WorkUnit request by HTTP method, ``action``, and ``work_unit_id``.

    ``action`` is the trailing path segment (e.g. ``claim``); ``work_unit_id`` the id path
    parameter. GET without an id lists the queue; GET ``stale`` lists stale active work.
    """
    method = (event.get("httpMethod") or "GET").upper()
    action = common.path_param(event, "action")
    work_unit_id = common.path_param(event, "work_unit_id")

    if method == "GET":
        if action == "stale" or work_unit_id == "stale":
            return _stale(event, correlation_id)
        if work_unit_id is not None:
            return _detail(event, work_unit_id, correlation_id)
        return _queue(event, correlation_id)

    if method == "PATCH" and action == "quantity":
        if work_unit_id is None:
            raise errors.ValidationError("missing work_unit_id")
        return _update_quantity(event, work_unit_id, correlation_id)

    if method == "POST" and action in _POST_ACTIONS:
        if work_unit_id is None:
            raise errors.ValidationError("missing work_unit_id")
        return _POST_ACTIONS[action](event, work_unit_id, correlation_id)

    raise errors.ValidationError(f"unsupported work-unit route: {method} {action}")
