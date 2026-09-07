"""WorkUnit quantity validation — pure logic (no I/O).

Enforces the WorkUnit quantity invariant: ``required_qty`` must be a positive integer
and ``completed_qty`` must satisfy ``0 <= completed_qty <= required_qty``
(FR-002 AC-3, FR-009 AC-2, FR-009 AC-3, BR-002).

Pure function library: no I/O, no AWS calls, no environment reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

QUANTITY_EXCEEDED = "QUANTITY_EXCEEDED"
INVALID_QUANTITY = "INVALID_QUANTITY"


@dataclass(frozen=True)
class QuantityValidationResult:
    """Outcome of a quantity-update validation.

    Attributes:
        is_error: True when the proposed quantity update is rejected.
        error_code: The specific error code when rejected; None on success.
            - ``QUANTITY_EXCEEDED`` when new_completed_qty > required_qty.
            - ``INVALID_QUANTITY`` when required_qty <= 0 or new_completed_qty < 0.
    """

    is_error: bool
    error_code: Optional[str] = None


def validate_quantity_update(required_qty: int, new_completed_qty: int) -> QuantityValidationResult:
    """Validate a proposed completed-quantity update against the required quantity.

    The invariant enforced is: ``required_qty > 0`` AND
    ``0 <= new_completed_qty <= required_qty`` (FR-002 AC-3, FR-009 AC-2, BR-002).

    A proposed completed quantity strictly greater than the required quantity is
    rejected with ``QUANTITY_EXCEEDED`` (FR-009 AC-3). A non-positive required
    quantity or a negative completed quantity is rejected with ``INVALID_QUANTITY``.

    Args:
        required_qty: The WorkUnit's required quantity (must be a positive integer).
        new_completed_qty: The proposed completed quantity (must be non-negative).

    Returns:
        A :class:`QuantityValidationResult`. ``is_error`` is False only when the
        update satisfies the invariant.
    """
    if required_qty <= 0:
        return QuantityValidationResult(is_error=True, error_code=INVALID_QUANTITY)
    if new_completed_qty < 0:
        return QuantityValidationResult(is_error=True, error_code=INVALID_QUANTITY)
    if new_completed_qty > required_qty:
        return QuantityValidationResult(is_error=True, error_code=QUANTITY_EXCEEDED)
    return QuantityValidationResult(is_error=False, error_code=None)
