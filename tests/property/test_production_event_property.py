"""Property-based test: ProductionEvents are created only on verification (Property 12).

For any WorkUnit driven through a sequence of transitions that does NOT include the
READY_TO_VERIFY -> VERIFIED transition, zero ProductionEvents exist for that WorkUnit.

The lifecycle is deterministic (AVAILABLE -> ... -> READY_TO_VERIFY -> VERIFIED); the
property samples a stop index strictly before the VERIFIED step and asserts no
ProductionEvent was written. It also exercises the fail-verify (rework) branch, which must
likewise produce no ProductionEvent.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.data import repositories as repo
from backend.handlers import workunit
from tests.property._moto_env import moto_wop_environment

TECH = "token-tech-001"
LEAD = "token-verify-001"

# The ordered pre-VERIFIED lifecycle steps: (token, action, body). Each advances the
# WorkUnit one state. VERIFIED is intentionally excluded — the property forbids it.
_PRE_VERIFY_STEPS: List[Dict[str, Any]] = [
    {"token": TECH, "action": "claim", "body": None},
    {"token": TECH, "action": "start", "body": None},
    {"token": TECH, "action": "prep-complete", "body": None},
    {"token": TECH, "action": "label", "body": None},
    {"token": TECH, "action": "tote-assign", "body": {"tote_id": "TOTE-1"}},
    {"token": TECH, "action": "ready-to-verify", "body": None},
]


def _seed_available_unit(ddb: Any, work_unit_id: str, required_qty: int) -> None:
    repo.create_work_package(
        {
            "work_package_id": "wp-p",
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
        correlation_id="seed",
    )
    repo.write_work_unit_transition(
        {
            "work_unit_id": work_unit_id,
            "work_package_id": "wp-p",
            "site": "SITE-A",
            "work_type": "Fiber",
            "state": "AVAILABLE",
            "sk_gsi1": "SITE-A#Fiber#2024-01-15",
            "claimed_by": None,
            "required_qty": required_qty,
            "completed_qty": required_qty,  # pre-filled so prep-complete is allowed
            "tote_id": None,
            "rework_count": 0,
            "current_scheduled_date": "2024-01-15",
            "original_scheduled_date": "2024-01-15",
            "rollover_count": 0,
            "version": 0,
            "created_at": "2024-01-14T10:00:00.000Z",
            "updated_at": "2024-01-14T10:00:00.000Z",
        },
        action_type="WORK_UNIT_CREATED",
        actor_id="ADMIN-001",
        actor_role="MANAGER_ADMIN",
        before_state=None,
        after_state="AVAILABLE",
        correlation_id="seed",
    )


def _event(token: str, action: str, wid: str, body: Optional[Dict[str, Any]], n: int) -> Dict:
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {token}", "Idempotency-Key": f"{action}-{n}"},
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": {"work_unit_id": wid, "action": action},
        "requestContext": {"requestId": "req"},
    }


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    stop_index=st.integers(min_value=0, max_value=len(_PRE_VERIFY_STEPS)),
    do_rework=st.booleans(),
    required_qty=st.integers(min_value=1, max_value=20),
)
def test_production_events_only_on_verification(
    stop_index: int, do_rework: bool, required_qty: int
) -> None:
    # Feature: workload-operations-platform, Property 12: ProductionEvents Created Only
    # on Verification
    # Validates: Requirements FR-014 AC-5, BR-007
    with moto_wop_environment():
        wid = "wu-prop"
        _seed_available_unit(None, wid, required_qty)

        # Advance through the pre-VERIFIED lifecycle up to (but not including) stop_index.
        for n, step in enumerate(_PRE_VERIFY_STEPS[:stop_index]):
            resp = workunit.handle(_event(step["token"], step["action"], wid, step["body"], n))
            assert resp["statusCode"] == 200

        # Optionally exercise fail-verify (rework) when we reached READY_TO_VERIFY.
        if do_rework and stop_index == len(_PRE_VERIFY_STEPS):
            resp = workunit.handle(
                _event(LEAD, "fail-verify", wid, {"failure_reason": "defect"}, 99)
            )
            assert resp["statusCode"] == 200

        # No VERIFIED transition happened -> zero ProductionEvents for this WorkUnit.
        events, _ = repo.query_production_events_by_week(
            __import__("backend.domain.reporting", fromlist=["compute_week_key"]).compute_week_key(
                "2024-01-15T00:00:00.000Z"
            )
        )
        assert all(e.get("work_unit_id") != wid for e in events)
