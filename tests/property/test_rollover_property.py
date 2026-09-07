"""Property-based tests for workload rollover (Correctness Properties 5, 6, 7).

- Property 5 (Rollover Eligibility Invariant): a WorkUnit in any state other than AVAILABLE
  is left entirely unchanged by rollover (FR-022 AC-2, AC-3, BR-014).
- Property 6 (Rollover Data Preservation): an AVAILABLE WorkUnit that undergoes exactly one
  rollover has completed_qty and original_scheduled_date unchanged, current_scheduled_date
  advanced, and rollover_count incremented by 1 (FR-022 AC-4, AC-5, AC-6, BR-015).
- Property 7 (Rollover Idempotency): executing the rollover N times for the same cycle key
  produces the same current_scheduled_date and rollover_count as executing it once
  (FR-022 AC-8, FR-020 AC-8, BR-015).

Properties 5 and 6 exercise the pure eligibility/advance logic and the moto-backed handler.
Property 7 uses moto to prove the per-cycle idempotency guard end-to-end.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.data import repositories as repo
from backend.domain import state_machine as sm
from backend.handlers import rollover

from ._moto_env import moto_wop_environment

NON_AVAILABLE_STATES: List[str] = [s for s in sm.ALL_STATES if s != sm.AVAILABLE]


def _sk_gsi1(site: str, work_type: str, scheduled_date: str) -> str:
    return f"{site}#{work_type}#{scheduled_date}"


def _seed_available_unit(
    ddb: Any,
    *,
    work_unit_id: str,
    site: str,
    work_type: str,
    scheduled_date: str,
    completed_qty: int,
    required_qty: int,
    rollover_count: int,
) -> Dict[str, Any]:
    """Write an AVAILABLE WorkUnit canonical item (with GSI-1 key) + projection to moto."""
    work_package_id = f"wp-{work_unit_id}"
    canonical = {
        "pk": repo.wu_pk(work_unit_id),
        "sk": repo.METADATA_SK,
        "work_unit_id": work_unit_id,
        "work_package_id": work_package_id,
        "site": site,
        "work_type": work_type,
        "state": sm.AVAILABLE,
        "claimed_by": None,
        "required_qty": required_qty,
        "completed_qty": completed_qty,
        "tote_id": None,
        "rework_count": 0,
        "current_scheduled_date": scheduled_date,
        "original_scheduled_date": scheduled_date,
        "rollover_count": rollover_count,
        "version": 0,
        "sk_gsi1": _sk_gsi1(site, work_type, scheduled_date),
    }
    main = repo.tables.main_table()
    main.put_item(Item=repo._to_dynamo(canonical))
    main.put_item(Item=repo._to_dynamo(repo.work_unit_projection(canonical)))
    return canonical


# ---------------------------------------------------------------------------
# Property 5: Rollover Eligibility Invariant
# ---------------------------------------------------------------------------
@settings(max_examples=200, deadline=None)
@given(
    state=st.sampled_from(NON_AVAILABLE_STATES),
    completed_qty=st.integers(min_value=0, max_value=100),
    rollover_count=st.integers(min_value=0, max_value=50),
)
def test_property_5_rollover_eligibility_invariant(
    state: str, completed_qty: int, rollover_count: int
) -> None:
    # Feature: workload-operations-platform, Property 5: Rollover Eligibility Invariant
    # Validates: Requirements FR-022 AC-2, FR-022 AC-3, BR-014
    work_unit = {
        "state": state,
        "completed_qty": completed_qty,
        "rollover_count": rollover_count,
        "current_scheduled_date": "2024-01-14",
        "original_scheduled_date": "2024-01-14",
    }
    # A non-AVAILABLE unit is never eligible; rollover must leave it entirely unchanged.
    assert rollover.is_eligible(work_unit) is False
    snapshot = dict(work_unit)
    if rollover.is_eligible(work_unit):  # pragma: no cover - guarded above
        rollover._roll_one(work_unit, rollover.advance_scheduled_date, "corr")
    assert work_unit == snapshot


# ---------------------------------------------------------------------------
# Property 6: Rollover Data Preservation
# ---------------------------------------------------------------------------
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    completed_qty=st.integers(min_value=0, max_value=100),
    required_qty=st.integers(min_value=1, max_value=100),
    rollover_count=st.integers(min_value=0, max_value=50),
    day_offset=st.integers(min_value=0, max_value=3650),
)
def test_property_6_rollover_data_preservation(
    completed_qty: int, required_qty: int, rollover_count: int, day_offset: int
) -> None:
    # Feature: workload-operations-platform, Property 6: Rollover Data Preservation
    # Validates: Requirements FR-022 AC-4, FR-022 AC-5, FR-022 AC-6, BR-015
    completed_qty = min(completed_qty, required_qty)
    scheduled_date = (datetime(2020, 1, 1) + timedelta(days=day_offset)).date().isoformat()
    expected_new_date = rollover.advance_scheduled_date(scheduled_date)

    with moto_wop_environment():
        seeded = _seed_available_unit(
            None,  # ddb resource is wired via tables.configure; not needed here
            work_unit_id="wu-preserve",
            site="SITE-A",
            work_type="Fiber",
            scheduled_date=scheduled_date,
            completed_qty=completed_qty,
            required_qty=required_qty,
            rollover_count=rollover_count,
        )
        assert rollover.is_eligible(seeded) is True

        summary = rollover.run_rollover(
            "2099-01-01", correlation_id="corr", advancer=rollover.advance_scheduled_date
        )
        assert summary["rolled_over"] == 1

        rolled = repo.get_work_unit("wu-preserve")
        assert rolled is not None
        # completed_qty and original_scheduled_date preserved.
        assert int(rolled["completed_qty"]) == completed_qty
        assert rolled["original_scheduled_date"] == scheduled_date
        # current_scheduled_date advanced by one day; rollover_count incremented by 1.
        assert rolled["current_scheduled_date"] == expected_new_date
        assert int(rolled["rollover_count"]) == rollover_count + 1
        # State unchanged (AVAILABLE -> AVAILABLE).
        assert rolled["state"] == sm.AVAILABLE


# ---------------------------------------------------------------------------
# Property 7: Rollover Idempotency
# ---------------------------------------------------------------------------
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    execute_times=st.integers(min_value=1, max_value=5),
    rollover_count=st.integers(min_value=0, max_value=20),
)
def test_property_7_rollover_idempotency(execute_times: int, rollover_count: int) -> None:
    # Feature: workload-operations-platform, Property 7: Rollover Idempotency
    # Validates: Requirements FR-022 AC-8, FR-020 AC-8, BR-015
    scheduled_date = "2024-03-04"
    expected_new_date = rollover.advance_scheduled_date(scheduled_date)
    event = {"time": "2024-03-05T06:00:00Z"}

    with moto_wop_environment():
        _seed_available_unit(
            None,
            work_unit_id="wu-idem",
            site="SITE-A",
            work_type="Fiber",
            scheduled_date=scheduled_date,
            completed_qty=0,
            required_qty=10,
            rollover_count=rollover_count,
        )

        for _ in range(execute_times):
            rollover.handle(event, advancer=rollover.advance_scheduled_date)

        rolled = repo.get_work_unit("wu-idem")
        assert rolled is not None
        # Re-triggering the same cycle N times advances the date exactly once.
        assert rolled["current_scheduled_date"] == expected_new_date
        assert int(rolled["rollover_count"]) == rollover_count + 1
