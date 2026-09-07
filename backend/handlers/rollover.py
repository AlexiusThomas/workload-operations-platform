"""EventBridge-triggered workload rollover handler — ``wop-rollover-handler`` (Task 15.1).

Advances eligible, unclaimed WorkUnits to the next scheduled date so incomplete work is not
silently lost (FR-022). Only WorkUnits in the ``AVAILABLE`` state are eligible for automatic
rollover (BR-014, RISK-007); every other state is left untouched.

Design references: *Concurrency and Atomic Operations → Rollover — Atomic TransactWriteItems*
and *Workload Rollover* (FR-022).

Per-eligible-unit, the handler performs a single 3-item ``TransactWriteItems`` via
:func:`backend.data.repositories.write_rollover_transition` (canonical date advance +
``rollover_count`` increment, denormalized projection update, and a ``WORK_UNIT_ROLLED_OVER``
AuditEvent). The canonical write is guarded on ``state=AVAILABLE`` AND
``current_scheduled_date = expected_old_date`` so a unit claimed since the query, or already
advanced by a prior run of the same cycle, is skipped silently on
``TransactionCanceledException``.

Per-cycle idempotency (FR-022 AC-8, FR-020 AC-8, BR-015) is enforced through the idempotency
layer using a ``rollover_cycle_key`` of the form ``ROLLOVER#{YYYY-MM-DD}``: the whole cycle
runs once; a re-trigger for the same cycle replays without re-advancing dates. In addition,
each unit's conditional guard makes re-advancement impossible even if the cycle lock is
bypassed.

Rollover advances ``current_scheduled_date`` by exactly one calendar day (see
:func:`advance_scheduled_date`). The exact rollover schedule (day-of-week, time-of-day,
timezone) is a deployment/operational decision (OQ-013) delivered via the EventBridge rule;
the date-advance rule here is intentionally simple, pure, and injectable so the schedule
policy can change without touching this logic.

``completed_qty`` and ``original_scheduled_date`` are preserved across rollover (the handler
supplies an unmodified canonical copy; FR-022 AC-4, AC-5). The actor is ``SYSTEM``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from botocore.exceptions import ClientError

from backend.data import repositories as repo
from backend.data import tables
from backend.domain import state_machine as sm
from backend.idempotency import layer as idempotency
from backend.observability import logger, metrics

SYSTEM_ACTOR = "SYSTEM"
SYSTEM_ROLE = "SYSTEM"

_CONDITION_FAILED = "ConditionalCheckFailedException"

#: Type of the pure date-advance policy (injectable for tests / future schedule changes).
DateAdvancer = Callable[[str], str]


def _is_condition_failure(exc: ClientError) -> bool:
    """Return True for a conditional-check / transaction-cancelled failure."""
    code = exc.response.get("Error", {}).get("Code", "")
    if code in (_CONDITION_FAILED, "TransactionCanceledException"):
        return True
    reasons = exc.response.get("CancellationReasons") or []
    return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)


def advance_scheduled_date(current_scheduled_date: str) -> str:
    """Advance an ISO ``YYYY-MM-DD`` date string by exactly one calendar day (pure).

    The next scheduled date is ``current_scheduled_date + 1 day``. This is the V1 rollover
    policy; the precise operational cadence is deployment configuration (OQ-013) and can be
    swapped by injecting a different :data:`DateAdvancer` without changing the handler.

    Args:
        current_scheduled_date: An ISO-8601 calendar date (``YYYY-MM-DD``).

    Returns:
        The next day's ISO calendar date string.
    """
    parsed: date = datetime.strptime(current_scheduled_date, "%Y-%m-%d").date()
    return (parsed + timedelta(days=1)).isoformat()


def is_eligible(work_unit: Dict[str, Any]) -> bool:
    """Return True iff a WorkUnit is eligible for automatic rollover (AVAILABLE only).

    Only WorkUnits in the ``AVAILABLE`` state (unclaimed, not started) may be rolled over
    (FR-022 AC-2, AC-3, BR-014). Every other state is ineligible.
    """
    return str(work_unit.get("state")) == sm.AVAILABLE


def _cycle_date(event: Dict[str, Any]) -> str:
    """Resolve the rollover cycle date (``YYYY-MM-DD``) from the EventBridge event.

    Uses the EventBridge ``time`` field when present (the scheduled trigger time), else the
    current UTC date. The cycle date scopes the per-cycle idempotency key.
    """
    raw_time = event.get("time")
    if isinstance(raw_time, str) and raw_time:
        try:
            return datetime.fromisoformat(raw_time.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date().isoformat()


def _roll_one(work_unit: Dict[str, Any], advancer: DateAdvancer, correlation_id: str) -> bool:
    """Attempt to roll a single eligible WorkUnit forward. Returns True on success.

    A conditional-check failure (claimed since query, or already advanced this cycle) is
    swallowed and reported as a skip (returns False).
    """
    previous_date = str(work_unit.get("current_scheduled_date"))
    new_date = advancer(previous_date)
    new_rollover_count = int(work_unit.get("rollover_count", 0)) + 1
    try:
        repo.write_rollover_transition(
            dict(work_unit),
            previous_date=previous_date,
            new_date=new_date,
            new_rollover_count=new_rollover_count,
            actor_id=SYSTEM_ACTOR,
            actor_role=SYSTEM_ROLE,
            correlation_id=correlation_id,
        )
    except ClientError as exc:
        if _is_condition_failure(exc):
            logger.info(
                "rollover_skipped",
                correlation_id=correlation_id,
                entity_type=repo.ENTITY_WORK_UNIT,
                entity_id=work_unit.get("work_unit_id"),
            )
            return False
        raise
    metrics.increment(metrics.ROLLOVER_UNIT)
    return True


def run_rollover(cycle_date: str, *, correlation_id: str, advancer: DateAdvancer) -> Dict[str, Any]:
    """Roll all eligible AVAILABLE WorkUnits forward for one cycle (post-idempotency check).

    Queries the AVAILABLE queue via GSI-1 and advances each eligible unit. Paginates through
    every page so no eligible unit is missed at V1 volumes (AS-002).

    Returns:
        A summary dict with ``cycle_date``, ``rolled_over`` count, and ``skipped`` count.
    """
    rolled_over = 0
    skipped = 0
    next_token: Optional[str] = None
    while True:
        items, next_token = repo.query_work_units_by_state(sm.AVAILABLE, next_token=next_token)
        for work_unit in items:
            if not is_eligible(work_unit):
                continue
            if _roll_one(work_unit, advancer, correlation_id):
                rolled_over += 1
            else:
                skipped += 1
        if not next_token:
            break
    logger.info(
        "rollover_completed",
        correlation_id=correlation_id,
        action="rollover",
    )
    return {"cycle_date": cycle_date, "rolled_over": rolled_over, "skipped": skipped}


def handle(
    event: Dict[str, Any],
    context: Any = None,
    *,
    correlation_id: Optional[str] = None,
    advancer: Optional[DateAdvancer] = None,
) -> Dict[str, Any]:
    """Handle an EventBridge scheduled rollover trigger (the testable core).

    Not wrapped by the HTTP ``error_handler`` — the trigger is EventBridge, not API Gateway.
    Per-cycle idempotency uses ``ROLLOVER#{cycle_date}`` through the idempotency layer so a
    re-triggered cycle does not double-advance (FR-022 AC-8, FR-020 AC-8, BR-015).

    Args:
        event: The EventBridge scheduled event (``time`` scopes the cycle date).
        context: Unused Lambda context.
        correlation_id: Optional correlation id (generated when absent).
        advancer: Optional pure date-advance policy (defaults to +1 calendar day).

    Returns:
        A summary dict describing the cycle outcome.
    """
    correlation_id = correlation_id or str(uuid4())
    active_advancer = advancer or advance_scheduled_date

    idempotency.set_table(tables.idempotency_table())
    cycle_date = _cycle_date(event)
    cycle_key = f"ROLLOVER#{cycle_date}"
    fingerprint = idempotency.compute_fingerprint("rollover", cycle_key, {"cycle": cycle_date})

    check = idempotency.check_idempotency(cycle_key, fingerprint)
    if check.decision == idempotency.DECISION_REPLAY:
        logger.info("rollover_replayed", correlation_id=correlation_id, action="rollover")
        return (
            json.loads(check.response_body) if check.response_body else {"cycle_date": cycle_date}
        )
    if check.decision == idempotency.DECISION_IN_FLIGHT:
        logger.info("rollover_in_flight_skip", correlation_id=correlation_id, action="rollover")
        return {"cycle_date": cycle_date, "status": "in_flight"}

    if not idempotency.acquire_lock(cycle_key, "rollover", cycle_key, fingerprint):
        logger.info("rollover_lock_contended", correlation_id=correlation_id, action="rollover")
        return {"cycle_date": cycle_date, "status": "in_flight"}

    logger.info("rollover_started", correlation_id=correlation_id, action="rollover")
    summary = run_rollover(cycle_date, correlation_id=correlation_id, advancer=active_advancer)
    idempotency.store_idempotency_result(cycle_key, 200, json.dumps(summary))
    return summary
