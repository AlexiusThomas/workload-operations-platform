"""WorkPackage aggregate state derivation — pure, deterministic logic (no I/O).

WorkPackage state is never stored as an independently mutable attribute. It is computed
at query time from the aggregate of its child WorkUnit states and MaterialRequirement
statuses using the priority-ordered rules from the design's "WorkPackage Aggregate State
Derivation Rules" section (FR-021 AC-3).

Priority order (earlier priorities override later ones):
    1.  Any WorkUnit in REWORK_REQUIRED                          -> REWORK_REQUIRED
    2.  Any WorkUnit in READY_TO_VERIFY                          -> PENDING_VERIFICATION
    3.  Any WorkUnit in IN_PROGRESS or CLAIMED                   -> IN_PROGRESS
    4.  Any MaterialRequirement in MATERIAL_REQUIRED/CLAIMED     -> AWAITING_MATERIAL
    5.  Any WorkUnit in LABELED / TOTE_ASSIGNED / PREP_COMPLETE  -> PREP_IN_PROGRESS
    5a. Some WorkUnits COMPLETE/VERIFIED, remainder AVAILABLE    -> PARTIALLY_COMPLETE
    6.  All WorkUnits COMPLETE                                   -> COMPLETE
    7.  All WorkUnits VERIFIED                                   -> VERIFIED
    8.  All WorkUnits AVAILABLE                                  -> AVAILABLE
    9.  WorkPackage has no WorkUnits                             -> IMPORTED

Note: Priority 4 (AWAITING_MATERIAL) is evaluated BEFORE Priority 5 (PREP_IN_PROGRESS):
a WorkPackage with any open MaterialRequirement is AWAITING_MATERIAL even if some
WorkUnits are in PREP_COMPLETE. Priority 3 (IN_PROGRESS/CLAIMED) still beats Priority 4.

This module is pure: no I/O, no AWS calls, no environment reads. It accepts plain
sequences of state/status strings and returns a single derived-state string.
"""

from __future__ import annotations

from typing import Sequence

from backend.domain import state_machine as sm

# ---------------------------------------------------------------------------
# Derived WorkPackage states (design: valid derived WorkPackage states)
# ---------------------------------------------------------------------------
WP_AVAILABLE = "AVAILABLE"
WP_IN_PROGRESS = "IN_PROGRESS"
WP_PREP_IN_PROGRESS = "PREP_IN_PROGRESS"
WP_AWAITING_MATERIAL = "AWAITING_MATERIAL"
WP_PENDING_VERIFICATION = "PENDING_VERIFICATION"
WP_REWORK_REQUIRED = "REWORK_REQUIRED"
WP_VERIFIED = "VERIFIED"
WP_COMPLETE = "COMPLETE"
WP_PARTIALLY_COMPLETE = "PARTIALLY_COMPLETE"
WP_IMPORTED = "IMPORTED"

#: The complete set of valid derived WorkPackage states.
ALL_DERIVED_STATES = frozenset(
    {
        WP_AVAILABLE,
        WP_IN_PROGRESS,
        WP_PREP_IN_PROGRESS,
        WP_AWAITING_MATERIAL,
        WP_PENDING_VERIFICATION,
        WP_REWORK_REQUIRED,
        WP_VERIFIED,
        WP_COMPLETE,
        WP_PARTIALLY_COMPLETE,
        WP_IMPORTED,
    }
)

# ---------------------------------------------------------------------------
# MaterialRequirement statuses (design: MaterialRequirement State Machine)
# ---------------------------------------------------------------------------
MATERIAL_REQUIRED = "MATERIAL_REQUIRED"
MATERIAL_CLAIMED = "MATERIAL_CLAIMED"
MATERIAL_DELIVERED = "MATERIAL_DELIVERED"
READY_FOR_PREP = "READY_FOR_PREP"

#: The complete, ordered set of valid MaterialRequirement statuses.
ALL_MATERIAL_STATUSES = (
    MATERIAL_REQUIRED,
    MATERIAL_CLAIMED,
    MATERIAL_DELIVERED,
    READY_FOR_PREP,
)

#: MaterialRequirement statuses considered "open" (not yet delivered) for Priority 4.
_OPEN_MATERIAL_STATUSES = frozenset({MATERIAL_REQUIRED, MATERIAL_CLAIMED})

#: WorkUnit states that count as "prep-in-progress" for Priority 5.
_PREP_STATES = frozenset({sm.LABELED, sm.TOTE_ASSIGNED, sm.PREP_COMPLETE})

#: WorkUnit states that count as "terminal-successful" for Priority 5a / 6 / 7.
_TERMINAL_STATES = frozenset({sm.COMPLETE, sm.VERIFIED})


def compute_work_package_state(
    work_units: Sequence[str],
    material_requirements: Sequence[str],
) -> str:
    """Derive a WorkPackage's aggregate state from its children.

    Pure and deterministic: given the same inputs it always returns the same output,
    with no randomness and no side effects (FR-021 AC-3).

    Args:
        work_units: A sequence of child WorkUnit state strings (e.g. ``"AVAILABLE"``).
        material_requirements: A sequence of child MaterialRequirement status strings
            (e.g. ``"MATERIAL_REQUIRED"``). May be empty.

    Returns:
        Exactly one of the valid derived WorkPackage states in :data:`ALL_DERIVED_STATES`.
    """
    # Priority 9: no WorkUnits -> IMPORTED.
    if not work_units:
        return WP_IMPORTED

    unit_states = list(work_units)
    unit_state_set = set(unit_states)

    # Priority 1: any WorkUnit in REWORK_REQUIRED.
    if sm.REWORK_REQUIRED in unit_state_set:
        return WP_REWORK_REQUIRED

    # Priority 2: any WorkUnit in READY_TO_VERIFY.
    if sm.READY_TO_VERIFY in unit_state_set:
        return WP_PENDING_VERIFICATION

    # Priority 3: any WorkUnit in IN_PROGRESS or CLAIMED (beats open material).
    if sm.IN_PROGRESS in unit_state_set or sm.CLAIMED in unit_state_set:
        return WP_IN_PROGRESS

    # Priority 4: any open MaterialRequirement (beats PREP_IN_PROGRESS).
    if any(status in _OPEN_MATERIAL_STATUSES for status in material_requirements):
        return WP_AWAITING_MATERIAL

    # Priority 5: any WorkUnit in LABELED / TOTE_ASSIGNED / PREP_COMPLETE.
    if any(state in _PREP_STATES for state in unit_state_set):
        return WP_PREP_IN_PROGRESS

    # Priority 5a: some WorkUnits COMPLETE/VERIFIED, the remainder AVAILABLE.
    has_terminal = any(state in _TERMINAL_STATES for state in unit_state_set)
    if has_terminal and sm.AVAILABLE in unit_state_set:
        return WP_PARTIALLY_COMPLETE

    # Priority 6: all WorkUnits COMPLETE.
    if unit_state_set == {sm.COMPLETE}:
        return WP_COMPLETE

    # Priority 7: all WorkUnits VERIFIED.
    if unit_state_set == {sm.VERIFIED}:
        return WP_VERIFIED

    # Priority 8: all WorkUnits AVAILABLE.
    if unit_state_set == {sm.AVAILABLE}:
        return WP_AVAILABLE

    # After Priorities 1-5 have filtered out every active, prep, rework, and
    # verification-pending state, the only states that can remain are AVAILABLE,
    # VERIFIED, and COMPLETE. Priority 5a handled any set mixing AVAILABLE with a
    # terminal state, and Priorities 6/7/8 handled the pure single-state sets. The one
    # remaining case is a mix of only COMPLETE and VERIFIED (no AVAILABLE): a
    # fully-terminal package where not every WorkUnit has reached COMPLETE. Because all
    # units are at least VERIFIED but the package is not fully COMPLETE, the derived
    # state is VERIFIED (design: Aggregate State Derivation Examples, mixed COMPLETE +
    # VERIFIED row; FR-021 AC-3).
    return WP_VERIFIED
