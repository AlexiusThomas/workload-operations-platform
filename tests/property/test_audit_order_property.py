"""Property-based test: audit log chronological order and completeness (Property 9).

For any WorkUnit and any sequence of N state transitions performed on it, the audit-log
query returns exactly N transition AuditEvents (plus the creation event) sorted by SK in
ascending (chronological) order, with no gaps.

The deterministic WorkUnit lifecycle provides an ordered list of transitions; the property
samples a prefix length N, applies exactly those N transitions via the handler, and asserts
the audit count and ordering. AuditEvent SKs are ``TS#{timestamp}#{uuid}`` so same-timestamp
bursts remain unique and the ascending-SK query is stable (FR-017 AC-3, AC-4).
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

# Full deterministic lifecycle. Each entry advances the WorkUnit exactly one state and emits
# exactly one transition AuditEvent.
_LIFECYCLE: List[Dict[str, Any]] = [
    {"token": TECH, "action": "claim", "body": None, "expect": "CLAIMED"},
    {"token": TECH, "action": "start", "body": None, "expect": "IN_PROGRESS"},
    {"token": TECH, "action": "prep-complete", "body": None, "expect": "PREP_COMPLETE"},
    {"token": TECH, "action": "label", "body": None, "expect": "LABELED"},
    {"token": TECH, "action": "tote-assign", "body": {"tote_id": "T1"}, "expect": "TOTE_ASSIGNED"},
    {"token": TECH, "action": "ready-to-verify", "body": None, "expect": "READY_TO_VERIFY"},
    {"token": LEAD, "action": "verify", "body": None, "expect": "VERIFIED"},
    {"token": LEAD, "action": "complete", "body": None, "expect": "COMPLETE"},
]


def _seed(wid: str, required_qty: int) -> None:
    repo.create_work_package(
        {
            "work_package_id": "wp-a",
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
            "work_unit_id": wid,
            "work_package_id": "wp-a",
            "site": "SITE-A",
            "work_type": "Fiber",
            "state": "AVAILABLE",
            "sk_gsi1": "SITE-A#Fiber#2024-01-15",
            "claimed_by": None,
            "required_qty": required_qty,
            "completed_qty": required_qty,
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
    n_transitions=st.integers(min_value=0, max_value=len(_LIFECYCLE)),
    required_qty=st.integers(min_value=1, max_value=15),
)
def test_audit_log_chronological_and_complete(n_transitions: int, required_qty: int) -> None:
    # Feature: workload-operations-platform, Property 9: Audit Log Chronological Order
    # and Completeness
    # Validates: Requirements FR-017 AC-3, FR-017 AC-4
    with moto_wop_environment():
        wid = "wu-audit"
        _seed(wid, required_qty)

        for n, step in enumerate(_LIFECYCLE[:n_transitions]):
            resp = workunit.handle(_event(step["token"], step["action"], wid, step["body"], n))
            assert resp["statusCode"] == 200
            assert json.loads(resp["body"])["state"] == step["expect"]

        audits, _ = repo.query_audit_events(repo.ENTITY_WORK_UNIT, wid)

        # Exactly N transition events + 1 creation event, no gaps.
        assert len(audits) == n_transitions + 1

        # Ascending SK order == chronological (no gaps, stable via uuid suffix).
        sks = [a["sk"] for a in audits]
        assert sks == sorted(sks)
        timestamps = [a["timestamp"] for a in audits]
        assert timestamps == sorted(timestamps)
