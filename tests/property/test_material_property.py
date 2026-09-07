"""Property-based test for the MaterialRequirement total-meters invariant (Property 8)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.domain import material


@settings(max_examples=200)
@given(
    cable_length_m=st.floats(
        min_value=0.01, max_value=10000.0, allow_nan=False, allow_infinity=False
    ),
    qty_required=st.integers(min_value=1, max_value=1000),
)
def test_total_meters_invariant(cable_length_m: float, qty_required: int) -> None:
    # Feature: workload-operations-platform, Property 8: MaterialRequirement Total Meters Invariant
    # Validates: Requirements FR-003 AC-3, BR-017
    mr = material.create_material_requirement(
        material_type=material.FIBER,
        cable_length_m=cable_length_m,
        qty_required=qty_required,
    )
    expected = cable_length_m * qty_required
    # total_meters_required equals cable_length_m * qty_required.
    assert abs(mr.total_meters_required - expected) < 1e-6 * max(1.0, abs(expected))
    # Initial delivered values are zero (FR-003 AC-2).
    assert mr.qty_delivered == 0
    assert mr.meters_delivered == 0.0
    assert mr.status == material.MATERIAL_REQUIRED
