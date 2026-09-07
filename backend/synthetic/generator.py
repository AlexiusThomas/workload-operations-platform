"""Synthetic development/test data generator with a production guard (FR-023).

Factory functions build WorkPackage, WorkUnit, and MaterialRequirement dictionaries using
only synthetic identifiers (SITE-A, RACK-001, TECH-001, RUNNER-001, VERIFY-001) — never
real employee identifiers (FR-023 AC-1). :func:`generate_dataset` produces a parameterised
dataset across Work_Types (FR-023 AC-2).

Production guard (FR-023 AC-3, CON-003, RISK-006): every dataset-generating entry point
raises :class:`RuntimeError` when ``ENVIRONMENT=prod`` so the utility can never fabricate
data in production. The guard is checked inside functions (not at import time), keeping the
module import-safe.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional
from uuid import uuid4

from backend.domain import material as material_domain

# ---------------------------------------------------------------------------
# Synthetic identifiers (FR-023 AC-1). No real employee identifiers appear here.
# ---------------------------------------------------------------------------
SYNTHETIC_SITE = "SITE-A"
SYNTHETIC_RACK = "RACK-001"
SYNTHETIC_TECHNICIAN = "TECH-001"
SYNTHETIC_RUNNER = "RUNNER-001"
SYNTHETIC_VERIFIER = "VERIFY-001"

ENV_PROD = "prod"


def _guard_production() -> None:
    """Raise if running in the production environment (FR-023 AC-3, RISK-006)."""
    if os.environ.get("ENVIRONMENT", "dev") == ENV_PROD:
        raise RuntimeError("Synthetic data generation is prohibited in the production environment.")


def _today_iso() -> str:
    """Return today's date as an ISO ``YYYY-MM-DD`` string."""
    return date.today().isoformat()


def make_work_package(
    site: str = SYNTHETIC_SITE,
    rack_position: str = SYNTHETIC_RACK,
    work_type: str = material_domain.FIBER,
    scheduled_date: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build a synthetic WorkPackage dict (FR-002 AC-1 attribute set)."""
    _guard_production()
    scheduled = scheduled_date or _today_iso()
    work_package: Dict[str, Any] = {
        "work_package_id": str(uuid4()),
        "site": site,
        "rack_position": rack_position,
        "work_type": work_type,
        "original_scheduled_date": scheduled,
        "current_scheduled_date": scheduled,
        "rollover_count": 0,
        "ingestion_source": "synthetic",
        "state": "IMPORTED",
        "created_at": f"{scheduled}T00:00:00.000Z",
        "updated_at": f"{scheduled}T00:00:00.000Z",
    }
    work_package.update(overrides)
    return work_package


def make_work_unit(
    work_package_id: str,
    required_qty: int = 10,
    state: str = "AVAILABLE",
    site: str = SYNTHETIC_SITE,
    work_type: str = material_domain.FIBER,
    scheduled_date: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build a synthetic WorkUnit dict (FR-002 AC-2 attribute set)."""
    _guard_production()
    scheduled = scheduled_date or _today_iso()
    work_unit: Dict[str, Any] = {
        "work_unit_id": str(uuid4()),
        "work_package_id": work_package_id,
        "site": site,
        "work_type": work_type,
        "state": state,
        "claimed_by": None,
        "required_qty": required_qty,
        "completed_qty": 0,
        "tote_id": None,
        "rework_count": 0,
        "current_scheduled_date": scheduled,
        "original_scheduled_date": scheduled,
        "rollover_count": 0,
        "version": 0,
        "created_at": f"{scheduled}T00:00:00.000Z",
        "updated_at": f"{scheduled}T00:00:00.000Z",
    }
    work_unit.update(overrides)
    return work_unit


def make_material_requirement(
    work_package_id: str,
    material_type: str = material_domain.FIBER,
    cable_length_m: float = 50.0,
    qty_required: int = 4,
    work_unit_id: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build a synthetic MaterialRequirement dict with a computed total_meters_required.

    Uses :func:`backend.domain.material.create_material_requirement` to compute the
    immutable ``total_meters_required`` (FR-003 AC-3, BR-017).
    """
    _guard_production()
    values = material_domain.create_material_requirement(
        material_type=material_type,
        cable_length_m=cable_length_m,
        qty_required=qty_required,
    )
    scheduled = _today_iso()
    material_req: Dict[str, Any] = {
        "material_req_id": str(uuid4()),
        "work_package_id": work_package_id,
        "work_unit_id": work_unit_id,
        "material_type": values.material_type,
        "cable_length_m": values.cable_length_m,
        "qty_required": values.qty_required,
        "total_meters_required": values.total_meters_required,
        "qty_delivered": values.qty_delivered,
        "meters_delivered": values.meters_delivered,
        "runner_id": None,
        "status": values.status,
        "created_at": f"{scheduled}T00:00:00.000Z",
        "last_updated_at": f"{scheduled}T00:00:00.000Z",
    }
    material_req.update(overrides)
    return material_req


def generate_dataset(
    work_package_count: int,
    units_per_package: int,
    work_types: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Generate a parameterised synthetic dataset (FR-023 AC-2, AC-3).

    Produces ``work_package_count`` WorkPackages cycling through ``work_types``, each with
    ``units_per_package`` WorkUnits and one MaterialRequirement (for Fiber/Copper types).

    Args:
        work_package_count: Number of WorkPackages to create (>= 0).
        units_per_package: Number of WorkUnits per WorkPackage (>= 0).
        work_types: Non-empty list of Work_Types to cycle through.

    Returns:
        A dict with keys ``work_packages``, ``work_units``, ``material_requirements``.

    Raises:
        RuntimeError: If ``ENVIRONMENT=prod`` (FR-023 AC-3, RISK-006).
        ValueError: If ``work_types`` is empty.
    """
    _guard_production()
    if not work_types:
        raise ValueError("work_types must be a non-empty list")

    work_packages: List[Dict[str, Any]] = []
    work_units: List[Dict[str, Any]] = []
    material_requirements: List[Dict[str, Any]] = []

    for i in range(work_package_count):
        work_type = work_types[i % len(work_types)]
        wp = make_work_package(work_type=work_type)
        work_packages.append(wp)

        for _ in range(units_per_package):
            work_units.append(
                make_work_unit(work_package_id=wp["work_package_id"], work_type=work_type)
            )

        # MaterialRequirements apply only to Fiber and Copper in V1 (AS-009).
        if work_type in (material_domain.FIBER, material_domain.COPPER):
            material_requirements.append(
                make_material_requirement(
                    work_package_id=wp["work_package_id"], material_type=work_type
                )
            )

    return {
        "work_packages": work_packages,
        "work_units": work_units,
        "material_requirements": material_requirements,
    }
