"""Unit tests for WorkPackage aggregate state derivation.

Covers every row of the design's "Aggregate State Derivation Examples" table
(FR-021 AC-3).
"""

from __future__ import annotations

from typing import List

import pytest

from backend.domain import aggregate_state as ag
from backend.domain import state_machine as sm

# Each case: (work_unit_states, material_statuses, expected_derived_state, reason).
# Mirrors the design's Aggregate State Derivation Examples table row-for-row.
CASES = [
    ([sm.AVAILABLE, sm.AVAILABLE], [], ag.WP_AVAILABLE, "Priority 8: all AVAILABLE"),
    ([sm.COMPLETE, sm.COMPLETE], [], ag.WP_COMPLETE, "Priority 6: all COMPLETE"),
    ([sm.VERIFIED, sm.VERIFIED], [], ag.WP_VERIFIED, "Priority 7: all VERIFIED"),
    (
        [sm.COMPLETE, sm.AVAILABLE],
        [],
        ag.WP_PARTIALLY_COMPLETE,
        "Priority 5a: some COMPLETE, remainder AVAILABLE",
    ),
    (
        [sm.VERIFIED, sm.AVAILABLE],
        [],
        ag.WP_PARTIALLY_COMPLETE,
        "Priority 5a: some VERIFIED, remainder AVAILABLE",
    ),
    (
        [sm.COMPLETE, sm.VERIFIED, sm.AVAILABLE],
        [],
        ag.WP_PARTIALLY_COMPLETE,
        "Priority 5a: some terminal, rest AVAILABLE",
    ),
    (
        [sm.IN_PROGRESS, sm.COMPLETE],
        [],
        ag.WP_IN_PROGRESS,
        "Priority 3: any IN_PROGRESS beats 5a",
    ),
    (
        [sm.READY_TO_VERIFY, sm.COMPLETE],
        [],
        ag.WP_PENDING_VERIFICATION,
        "Priority 2: any READY_TO_VERIFY",
    ),
    (
        [sm.REWORK_REQUIRED, sm.COMPLETE],
        [],
        ag.WP_REWORK_REQUIRED,
        "Priority 1: any REWORK_REQUIRED",
    ),
    (
        [sm.PREP_COMPLETE, sm.AVAILABLE],
        [],
        ag.WP_PREP_IN_PROGRESS,
        "Priority 5: any PREP_COMPLETE",
    ),
    (
        [sm.AVAILABLE, sm.AVAILABLE],
        [ag.MATERIAL_REQUIRED],
        ag.WP_AWAITING_MATERIAL,
        "Priority 4: any open material requirement",
    ),
    (
        [sm.AVAILABLE, sm.AVAILABLE],
        [ag.MATERIAL_DELIVERED, ag.READY_FOR_PREP],
        ag.WP_AVAILABLE,
        "All materials delivered; Priority 8: all AVAILABLE",
    ),
    (
        [sm.IN_PROGRESS, sm.AVAILABLE],
        [ag.MATERIAL_CLAIMED],
        ag.WP_IN_PROGRESS,
        "Priority 3 (IN_PROGRESS) beats Priority 4 (open material)",
    ),
    (
        [sm.PREP_COMPLETE, sm.AVAILABLE],
        [ag.MATERIAL_CLAIMED],
        ag.WP_AWAITING_MATERIAL,
        "Priority 4 (open material) beats Priority 5 (PREP_COMPLETE)",
    ),
    (
        [sm.COMPLETE, sm.VERIFIED],
        [],
        ag.WP_VERIFIED,
        "Mixed terminal (COMPLETE + VERIFIED, no AVAILABLE): not all COMPLETE, "
        "all at least VERIFIED -> VERIFIED",
    ),
    ([], [], ag.WP_IMPORTED, "Priority 9: no WorkUnits"),
]


@pytest.mark.parametrize(
    "work_units,materials,expected,reason",
    CASES,
    ids=[c[3] for c in CASES],
)
def test_aggregate_state_examples(
    work_units: List[str], materials: List[str], expected: str, reason: str
) -> None:
    assert ag.compute_work_package_state(work_units, materials) == expected, reason
