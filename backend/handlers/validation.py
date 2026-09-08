"""JSON-schema request validation for WOP mutating endpoints (SEC-006 AC-1, AC-2, AC-3).

Every mutating request body is validated against a ``jsonschema`` Draft-2020-12 schema
*before* it reaches business logic. A validation failure raises
:class:`~backend.observability.errors.ValidationError` (HTTP 400) with a human-readable
field detail and never exposes an internal stack trace (SEC-006 AC-2).

Injection safety (SEC-006 AC-3): validated string inputs are, by project convention,
passed to DynamoDB exclusively as *expression attribute values* (never interpolated into
expression strings). The repositories in :mod:`backend.data.repositories` build all
expressions with ``ExpressionAttributeValues``/``ExpressionAttributeNames``; this module's
role is boundary validation of shape and type. See ``EXPRESSION_VALUE_CONVENTION`` below.

This module is pure and import-safe (no AWS, no environment reads).
"""

from __future__ import annotations

from typing import Any, Dict, List

from jsonschema import Draft202012Validator

from backend.observability.errors import ValidationError

#: Documented convention (SEC-006 AC-3): all validated strings are used as DynamoDB
#: expression attribute VALUES, never string-interpolated into an expression. Repositories
#: MUST bind user input through ExpressionAttributeValues / ExpressionAttributeNames.
EXPRESSION_VALUE_CONVENTION = (
    "All string inputs validated here are passed to DynamoDB as expression attribute "
    "values (ExpressionAttributeValues) only; they are never interpolated into a "
    "DynamoDB expression string."
)

#: Supported Work_Type enumeration (requirements Glossary; design WorkPackage.work_type).
WORK_TYPES: List[str] = [
    "Fiber",
    "Copper",
    "Metronome",
    "GB200LC",
    "GB300R",
    "GB300AC",
    "Prep Support",
    "Labeling",
]

#: Material types supported in V1 (AS-009).
MATERIAL_TYPES: List[str] = ["Fiber", "Copper"]

#: Mandatory WorkPackage attributes (FR-002 AC-4; Property 10).
WORK_PACKAGE_MANDATORY_ATTRIBUTES: List[str] = [
    "site",
    "rack_position",
    "work_type",
    "scheduled_date",
]

_NON_EMPTY_STRING = {"type": "string", "minLength": 1}
_POSITIVE_INT = {"type": "integer", "minimum": 1}
_NON_NEGATIVE_INT = {"type": "integer", "minimum": 0}
_POSITIVE_NUMBER = {"type": "number", "exclusiveMinimum": 0}
#: ISO calendar date YYYY-MM-DD with bounded month (01-12) and day (01-31).
_ISO_DATE = {
    "type": "string",
    "pattern": r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
}

#: Max WorkUnits creatable atomically with a WorkPackage. DynamoDB TransactWriteItems
#: caps at 100 actions; create emits 2 (package canonical+audit) + 3 per unit
#: (canonical+projection+audit): 2 + 3N <= 100 => N <= 32.
MAX_WORK_UNITS_PER_PACKAGE = 32


# ---------------------------------------------------------------------------
# Schemas (design: API Design request bodies)
# ---------------------------------------------------------------------------
WORK_UNIT_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "required_qty": _POSITIVE_INT,
        "work_type": {"type": "string", "enum": WORK_TYPES},
    },
    "required": ["required_qty", "work_type"],
    "additionalProperties": False,
}

WORK_PACKAGE_CREATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "site": _NON_EMPTY_STRING,
        "rack_position": _NON_EMPTY_STRING,
        "work_type": {"type": "string", "enum": WORK_TYPES},
        "scheduled_date": _ISO_DATE,
        "work_units": {
            "type": "array",
            "items": WORK_UNIT_ITEM_SCHEMA,
            "maxItems": MAX_WORK_UNITS_PER_PACKAGE,
        },
    },
    "required": WORK_PACKAGE_MANDATORY_ATTRIBUTES,
    "additionalProperties": False,
}

QUANTITY_UPDATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"completed_qty": _NON_NEGATIVE_INT},
    "required": ["completed_qty"],
    "additionalProperties": False,
}

TOTE_ASSIGN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"tote_id": _NON_EMPTY_STRING},
    "required": ["tote_id"],
    "additionalProperties": False,
}

FAIL_VERIFY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"failure_reason": _NON_EMPTY_STRING},
    "required": ["failure_reason"],
    "additionalProperties": False,
}

MATERIAL_CREATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "material_type": {"type": "string", "enum": MATERIAL_TYPES},
        "cable_length_m": _POSITIVE_NUMBER,
        "qty_required": _POSITIVE_INT,
        "work_unit_id": _NON_EMPTY_STRING,
    },
    "required": ["material_type", "cable_length_m", "qty_required"],
    "additionalProperties": False,
}

MATERIAL_DELIVER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "qty_delivered": _POSITIVE_INT,
        "meters_delivered": _POSITIVE_NUMBER,
    },
    "required": ["qty_delivered", "meters_delivered"],
    "additionalProperties": False,
}

ASSIGNMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"technician_id": _NON_EMPTY_STRING},
    "required": ["technician_id"],
    "additionalProperties": False,
}

REPORT_REQUEST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    # ISO week key such as 2024-W03.
    "properties": {"week_key": {"type": "string", "pattern": r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$"}},
    "required": ["week_key"],
    "additionalProperties": False,
}

#: Registry mapping a logical operation name to its request schema.
SCHEMAS: Dict[str, Dict[str, Any]] = {
    "work_package_create": WORK_PACKAGE_CREATE_SCHEMA,
    "quantity_update": QUANTITY_UPDATE_SCHEMA,
    "tote_assign": TOTE_ASSIGN_SCHEMA,
    "fail_verify": FAIL_VERIFY_SCHEMA,
    "material_create": MATERIAL_CREATE_SCHEMA,
    "material_deliver": MATERIAL_DELIVER_SCHEMA,
    "assignment": ASSIGNMENT_SCHEMA,
    "report_request": REPORT_REQUEST_SCHEMA,
}


def _format_error_detail(field_path: str, message: str) -> str:
    """Build a concise, non-leaking validation message for a single failing field."""
    if field_path:
        return f"Field '{field_path}': {message}"
    return message


def validate(schema: Dict[str, Any], body: Any) -> None:
    """Validate ``body`` against ``schema`` (SEC-006 AC-1).

    Args:
        schema: A JSON schema dict.
        body: The parsed request body to validate.

    Raises:
        ValidationError: If the body fails validation. The message lists each failing
            field. No stack trace is included (SEC-006 AC-2).
    """
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(body), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    details = [
        _format_error_detail(".".join(str(p) for p in err.absolute_path), err.message)
        for err in errors
    ]
    raise ValidationError("; ".join(details))


def validate_operation(operation: str, body: Any) -> None:
    """Validate a request body for a named operation using the schema registry.

    Args:
        operation: A key in :data:`SCHEMAS` (e.g. ``"work_package_create"``).
        body: The parsed request body.

    Raises:
        ValidationError: If the operation is unknown or the body fails validation.
    """
    schema = SCHEMAS.get(operation)
    if schema is None:
        raise ValidationError(f"Unknown operation for validation: {operation}")
    validate(schema, body)
