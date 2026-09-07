"""WorkUnit state machine — pure transition logic (no I/O).

Implements the complete, exhaustive set of valid WorkUnit state transitions from the
design's "WorkUnit State Machine" section. Any transition not in the valid-transition
table is rejected with ``INVALID_STATE_TRANSITION`` (FR-021 AC-1, FR-021 AC-2, BR-009).

This module is a pure function library: it performs no I/O, no AWS/DynamoDB calls, no
network access, and no environment reads. It operates only on plain strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# WorkUnit states (design: WorkUnit State Machine; requirements Glossary)
# ---------------------------------------------------------------------------
AVAILABLE = "AVAILABLE"
CLAIMED = "CLAIMED"
IN_PROGRESS = "IN_PROGRESS"
PREP_COMPLETE = "PREP_COMPLETE"
LABELED = "LABELED"
TOTE_ASSIGNED = "TOTE_ASSIGNED"
READY_TO_VERIFY = "READY_TO_VERIFY"
VERIFIED = "VERIFIED"
COMPLETE = "COMPLETE"
REWORK_REQUIRED = "REWORK_REQUIRED"

#: The complete, ordered set of valid WorkUnit states.
ALL_STATES: Tuple[str, ...] = (
    AVAILABLE,
    CLAIMED,
    IN_PROGRESS,
    PREP_COMPLETE,
    LABELED,
    TOTE_ASSIGNED,
    READY_TO_VERIFY,
    VERIFIED,
    COMPLETE,
    REWORK_REQUIRED,
)

# ---------------------------------------------------------------------------
# Transition triggers (design: Valid Transitions table "Trigger" column)
# ---------------------------------------------------------------------------
CLAIM = "claim"
START = "start"
PREP_COMPLETE_TRANSITION = "prep-complete"
LABEL = "label"
TOTE_ASSIGN = "tote-assign"
READY_TO_VERIFY_TRANSITION = "ready-to-verify"
VERIFY = "verify"
FAIL_VERIFY = "fail-verify"
COMPLETE_TRANSITION = "complete"

#: The complete set of transition names accepted by the state machine.
ALL_TRANSITIONS: Tuple[str, ...] = (
    CLAIM,
    START,
    PREP_COMPLETE_TRANSITION,
    LABEL,
    TOTE_ASSIGN,
    READY_TO_VERIFY_TRANSITION,
    VERIFY,
    FAIL_VERIFY,
    COMPLETE_TRANSITION,
)

INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"

# ---------------------------------------------------------------------------
# Valid-transition table (design: WorkUnit State Machine → Valid Transitions)
#
# Keyed by (from_state, transition) → to_state. Exhaustive: every entry here is
# a valid transition; every (state, transition) pair NOT present is invalid.
#
# Note the ``claim`` trigger is valid from two source states:
#   - AVAILABLE       → CLAIMED       (standard claim / pull model)
#   - REWORK_REQUIRED → IN_PROGRESS   (rework re-claim; completed_qty preserved)
# ---------------------------------------------------------------------------
_VALID_TRANSITIONS: Dict[Tuple[str, str], str] = {
    (AVAILABLE, CLAIM): CLAIMED,
    (CLAIMED, START): IN_PROGRESS,
    (IN_PROGRESS, PREP_COMPLETE_TRANSITION): PREP_COMPLETE,
    (PREP_COMPLETE, LABEL): LABELED,
    (LABELED, TOTE_ASSIGN): TOTE_ASSIGNED,
    (TOTE_ASSIGNED, READY_TO_VERIFY_TRANSITION): READY_TO_VERIFY,
    (READY_TO_VERIFY, VERIFY): VERIFIED,
    (READY_TO_VERIFY, FAIL_VERIFY): REWORK_REQUIRED,
    (REWORK_REQUIRED, CLAIM): IN_PROGRESS,
    (VERIFIED, COMPLETE_TRANSITION): COMPLETE,
}


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of an attempted state transition.

    Attributes:
        is_valid: True if the (current_state, transition) pair is a valid transition.
        new_state: The resulting state when valid; the unchanged current_state when
            invalid (the entity never leaves its current state on an invalid request).
        error_code: ``INVALID_STATE_TRANSITION`` when invalid; None when valid.
    """

    is_valid: bool
    new_state: str
    error_code: Optional[str] = None


def apply_transition(current_state: str, transition: str) -> TransitionResult:
    """Apply a WorkUnit state transition.

    Pure function with no side effects. Accepts the current state and a requested
    transition trigger and returns a :class:`TransitionResult`.

    The transition is accepted if and only if ``(current_state, transition)`` appears
    in the valid-transition table (FR-021 AC-1). For all other pairs the transition is
    rejected with ``INVALID_STATE_TRANSITION`` and the state is left unchanged
    (FR-021 AC-2, BR-009).

    Args:
        current_state: The WorkUnit's current state.
        transition: The requested transition trigger (e.g. ``"claim"``).

    Returns:
        A :class:`TransitionResult` describing the outcome.
    """
    new_state = _VALID_TRANSITIONS.get((current_state, transition))
    if new_state is None:
        return TransitionResult(
            is_valid=False,
            new_state=current_state,
            error_code=INVALID_STATE_TRANSITION,
        )
    return TransitionResult(is_valid=True, new_state=new_state, error_code=None)
