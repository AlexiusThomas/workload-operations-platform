"""Property-based test for WorkPackage aggregate state determinism (Property 13)."""

from __future__ import annotations

from typing import List

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.domain import aggregate_state as ag
from backend.domain import state_machine as sm


@settings(max_examples=200)
@given(
    work_units=st.lists(st.sampled_from(sm.ALL_STATES), min_size=1, max_size=5),
    materials=st.lists(st.sampled_from(ag.ALL_MATERIAL_STATUSES), min_size=0, max_size=3),
)
def test_aggregate_state_determinism(work_units: List[str], materials: List[str]) -> None:
    # Feature: workload-operations-platform, Property 13: WorkPackage Aggregate State Determinism
    # Validates: Requirements FR-021 AC-3
    result = ag.compute_work_package_state(work_units, materials)

    # The return value is always one of the valid derived WorkPackage states.
    assert result in ag.ALL_DERIVED_STATES

    # Consistency with the priority table: the highest-priority matching condition wins.
    unit_set = set(work_units)
    if sm.REWORK_REQUIRED in unit_set:
        assert result == ag.WP_REWORK_REQUIRED
    elif sm.READY_TO_VERIFY in unit_set:
        assert result == ag.WP_PENDING_VERIFICATION
    elif sm.IN_PROGRESS in unit_set or sm.CLAIMED in unit_set:
        assert result == ag.WP_IN_PROGRESS
    elif any(s in {ag.MATERIAL_REQUIRED, ag.MATERIAL_CLAIMED} for s in materials):
        assert result == ag.WP_AWAITING_MATERIAL
    elif any(s in {sm.LABELED, sm.TOTE_ASSIGNED, sm.PREP_COMPLETE} for s in unit_set):
        assert result == ag.WP_PREP_IN_PROGRESS

    # Mixed COMPLETE + AVAILABLE (no active/prep/material) is PARTIALLY_COMPLETE,
    # never plain AVAILABLE or COMPLETE.
    if unit_set == {sm.COMPLETE, sm.AVAILABLE} and not materials:
        assert result == ag.WP_PARTIALLY_COMPLETE

    # Referential transparency: calling twice with identical inputs returns the same value.
    assert ag.compute_work_package_state(work_units, materials) == result
