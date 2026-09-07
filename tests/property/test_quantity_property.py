"""Property-based tests for WorkUnit quantity invariants (Properties 1 and 11)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.domain import quantity


@settings(max_examples=200)
@given(
    required_qty=st.integers(min_value=1, max_value=1000),
    completed_qty=st.integers(min_value=0, max_value=1000),
)
def test_quantity_invariant(required_qty: int, completed_qty: int) -> None:
    # Feature: workload-operations-platform, Property 1: WorkUnit Quantity Invariant
    # Validates: Requirements FR-002 AC-3, FR-009 AC-2, BR-002, BR-003
    result = quantity.validate_quantity_update(
        required_qty=required_qty, new_completed_qty=completed_qty
    )
    if completed_qty > required_qty:
        assert result.is_error
        assert result.error_code == quantity.QUANTITY_EXCEEDED
    else:
        # 0 <= completed_qty <= required_qty with required_qty > 0 is always valid.
        assert not result.is_error
        assert result.error_code is None


@settings(max_examples=200)
@given(
    required_qty=st.integers(min_value=1, max_value=1000),
    excess=st.integers(min_value=1, max_value=1000),
)
def test_quantity_update_rejection_above_required(required_qty: int, excess: int) -> None:
    # Feature: workload-operations-platform, Property 11: Quantity Update Rejection Above Required
    # Validates: Requirements FR-009 AC-2, FR-009 AC-3, BR-002
    proposed = required_qty + excess  # strictly greater than required_qty
    result = quantity.validate_quantity_update(
        required_qty=required_qty, new_completed_qty=proposed
    )
    # Any completed_qty strictly greater than required_qty is rejected with
    # QUANTITY_EXCEEDED; the domain layer never mutates state, so the WorkUnit's
    # completed_qty is inherently unchanged.
    assert result.is_error
    assert result.error_code == quantity.QUANTITY_EXCEEDED
