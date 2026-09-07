"""Property-based test for mandatory WorkPackage attribute validation (Property 10)."""

from __future__ import annotations

from typing import Dict, List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.handlers import validation
from backend.observability.errors import ValidationError

#: A fully valid WorkPackage create body; subsets are derived by dropping fields.
_FULL_BODY: Dict[str, object] = {
    "site": "SITE-A",
    "rack_position": "RACK-001",
    "work_type": "Fiber",
    "scheduled_date": "2024-01-15",
}

_MANDATORY = validation.WORK_PACKAGE_MANDATORY_ATTRIBUTES


def _non_empty_proper_subsets(fields: List[str]) -> List[List[str]]:
    """Return all 2^n - 1 non-empty subsets of ``fields`` (used to enumerate present keys)."""
    n = len(fields)
    subsets: List[List[str]] = []
    for mask in range(1, (1 << n)):
        subsets.append([fields[i] for i in range(n) if mask & (1 << i)])
    return subsets


# All 15 non-empty subsets of the 4 mandatory attributes.
_PRESENT_SUBSETS = _non_empty_proper_subsets(_MANDATORY)


@settings(max_examples=200)
@given(present=st.sampled_from(_PRESENT_SUBSETS))
def test_mandatory_attribute_validation(present: List[str]) -> None:
    # Feature: workload-operations-platform, Property 10: Mandatory Attribute Validation
    # Validates: Requirements FR-002 AC-4, SEC-006 AC-1
    body = {field: _FULL_BODY[field] for field in present}
    missing_any = any(field not in present for field in _MANDATORY)

    if missing_any:
        # Any subset missing at least one mandatory field is rejected; no record created.
        with pytest.raises(ValidationError):
            validation.validate_operation("work_package_create", body)
    else:
        # Only the full set of mandatory attributes is accepted.
        validation.validate_operation("work_package_create", body)


def test_full_mandatory_set_is_accepted() -> None:
    # Sanity anchor: the complete mandatory set validates without error.
    validation.validate_operation("work_package_create", dict(_FULL_BODY))
