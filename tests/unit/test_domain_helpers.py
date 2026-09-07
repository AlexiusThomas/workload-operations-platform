"""Unit tests for domain helper edge cases: state machine, quantity, material, reporting."""

from __future__ import annotations

import pytest

from backend.domain import material, quantity, reporting
from backend.domain import state_machine as sm


class TestStateMachine:
    def test_valid_claim_transition(self) -> None:
        result = sm.apply_transition(sm.AVAILABLE, sm.CLAIM)
        assert result.is_valid
        assert result.new_state == sm.CLAIMED
        assert result.error_code is None

    def test_rework_claim_goes_to_in_progress(self) -> None:
        result = sm.apply_transition(sm.REWORK_REQUIRED, sm.CLAIM)
        assert result.is_valid
        assert result.new_state == sm.IN_PROGRESS

    def test_invalid_transition_leaves_state_unchanged(self) -> None:
        result = sm.apply_transition(sm.AVAILABLE, sm.VERIFY)
        assert not result.is_valid
        assert result.new_state == sm.AVAILABLE
        assert result.error_code == sm.INVALID_STATE_TRANSITION

    def test_unknown_state_is_invalid(self) -> None:
        result = sm.apply_transition("BOGUS", sm.CLAIM)
        assert not result.is_valid
        assert result.new_state == "BOGUS"
        assert result.error_code == sm.INVALID_STATE_TRANSITION


class TestQuantityValidation:
    def test_boundary_completed_equals_required_ok(self) -> None:
        result = quantity.validate_quantity_update(required_qty=10, new_completed_qty=10)
        assert not result.is_error

    def test_zero_completed_ok(self) -> None:
        result = quantity.validate_quantity_update(required_qty=10, new_completed_qty=0)
        assert not result.is_error

    def test_exceeded_rejected(self) -> None:
        result = quantity.validate_quantity_update(required_qty=10, new_completed_qty=11)
        assert result.is_error
        assert result.error_code == quantity.QUANTITY_EXCEEDED

    def test_non_positive_required_rejected(self) -> None:
        result = quantity.validate_quantity_update(required_qty=0, new_completed_qty=0)
        assert result.is_error
        assert result.error_code == quantity.INVALID_QUANTITY

    def test_negative_completed_rejected(self) -> None:
        result = quantity.validate_quantity_update(required_qty=10, new_completed_qty=-1)
        assert result.is_error
        assert result.error_code == quantity.INVALID_QUANTITY


class TestMaterialRequirement:
    def test_total_meters_computed(self) -> None:
        mr = material.create_material_requirement(material.FIBER, 50.0, 4)
        assert mr.total_meters_required == 200.0
        assert mr.status == material.MATERIAL_REQUIRED

    def test_non_positive_cable_length_rejected(self) -> None:
        with pytest.raises(material.MaterialValidationError):
            material.create_material_requirement(material.FIBER, 0.0, 4)

    def test_non_positive_qty_rejected(self) -> None:
        with pytest.raises(material.MaterialValidationError):
            material.create_material_requirement(material.COPPER, 50.0, 0)


class TestReporting:
    def test_week_key_with_z_suffix(self) -> None:
        assert reporting.compute_week_key("2024-01-17T14:30:00Z") == "2024-W03"

    def test_week_key_with_offset(self) -> None:
        assert reporting.compute_week_key("2024-01-17T14:30:00+00:00") == "2024-W03"

    def test_week_key_first_week(self) -> None:
        assert reporting.compute_week_key("2024-01-01T00:00:00Z") == "2024-W01"
