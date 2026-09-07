"""MaterialRequirement domain helpers — pure logic (no I/O).

Provides :func:`create_material_requirement`, which computes the immutable
``total_meters_required`` (cable_length_m x qty_required) at creation time and rejects
non-positive cable length or quantity (FR-003 AC-3, FR-003 AC-4, BR-017).

Pure function library: no I/O, no AWS calls, no environment reads. It returns a plain
dataclass describing the derived MaterialRequirement values; persistence happens in a
later task's data-access layer.
"""

from __future__ import annotations

from dataclasses import dataclass

MATERIAL_REQUIRED = "MATERIAL_REQUIRED"

#: Material types supported in V1 (AS-009).
FIBER = "Fiber"
COPPER = "Copper"


class MaterialValidationError(ValueError):
    """Raised when a MaterialRequirement is created with invalid inputs.

    Non-positive ``cable_length_m`` or ``qty_required`` triggers this error
    (FR-003 AC-4). Callers map this to an HTTP 400 VALIDATION_ERROR in a later task.
    """


@dataclass(frozen=True)
class MaterialRequirementValues:
    """Derived, immutable MaterialRequirement values produced at creation time.

    Attributes:
        material_type: The material type (Fiber or Copper).
        cable_length_m: Cable length in metres (positive).
        qty_required: Quantity required (positive integer).
        total_meters_required: cable_length_m * qty_required — immutable after creation
            (FR-003 AC-3, BR-017).
        qty_delivered: Initially 0 (FR-003 AC-2).
        meters_delivered: Initially 0 (FR-003 AC-2).
        status: Initial status, MATERIAL_REQUIRED (FR-003 AC-2).
    """

    material_type: str
    cable_length_m: float
    qty_required: int
    total_meters_required: float
    qty_delivered: int
    meters_delivered: float
    status: str


def create_material_requirement(
    material_type: str,
    cable_length_m: float,
    qty_required: int,
) -> MaterialRequirementValues:
    """Compute the derived values for a new MaterialRequirement.

    Validates that ``cable_length_m`` and ``qty_required`` are strictly positive
    (FR-003 AC-4) and computes ``total_meters_required = cable_length_m * qty_required``
    which is immutable thereafter (FR-003 AC-3, BR-017).

    Args:
        material_type: The material type (Fiber or Copper).
        cable_length_m: Cable length in metres. Must be > 0.
        qty_required: Quantity required. Must be > 0.

    Returns:
        A :class:`MaterialRequirementValues` with the computed total and initial fields.

    Raises:
        MaterialValidationError: If ``cable_length_m <= 0`` or ``qty_required <= 0``.
    """
    if cable_length_m <= 0:
        raise MaterialValidationError("cable_length_m must be a positive value")
    if qty_required <= 0:
        raise MaterialValidationError("qty_required must be a positive integer")

    total_meters_required = cable_length_m * qty_required
    return MaterialRequirementValues(
        material_type=material_type,
        cable_length_m=cable_length_m,
        qty_required=qty_required,
        total_meters_required=total_meters_required,
        qty_delivered=0,
        meters_delivered=0.0,
        status=MATERIAL_REQUIRED,
    )
