"""Property-based tests for the WorkUnit state machine (Correctness Property 2)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.domain import state_machine as sm

# The set of valid (from_state, transition) pairs, mirrored from the design's
# Valid Transitions table, used as the independent oracle for the property.
VALID_TRANSITION_PAIRS = {
    (sm.AVAILABLE, sm.CLAIM),
    (sm.CLAIMED, sm.START),
    (sm.IN_PROGRESS, sm.PREP_COMPLETE_TRANSITION),
    (sm.PREP_COMPLETE, sm.LABEL),
    (sm.LABELED, sm.TOTE_ASSIGN),
    (sm.TOTE_ASSIGNED, sm.READY_TO_VERIFY_TRANSITION),
    (sm.READY_TO_VERIFY, sm.VERIFY),
    (sm.READY_TO_VERIFY, sm.FAIL_VERIFY),
    (sm.REWORK_REQUIRED, sm.CLAIM),
    (sm.VERIFIED, sm.COMPLETE_TRANSITION),
}

# Expected resulting state for each valid pair.
EXPECTED_NEW_STATE = {
    (sm.AVAILABLE, sm.CLAIM): sm.CLAIMED,
    (sm.CLAIMED, sm.START): sm.IN_PROGRESS,
    (sm.IN_PROGRESS, sm.PREP_COMPLETE_TRANSITION): sm.PREP_COMPLETE,
    (sm.PREP_COMPLETE, sm.LABEL): sm.LABELED,
    (sm.LABELED, sm.TOTE_ASSIGN): sm.TOTE_ASSIGNED,
    (sm.TOTE_ASSIGNED, sm.READY_TO_VERIFY_TRANSITION): sm.READY_TO_VERIFY,
    (sm.READY_TO_VERIFY, sm.VERIFY): sm.VERIFIED,
    (sm.READY_TO_VERIFY, sm.FAIL_VERIFY): sm.REWORK_REQUIRED,
    (sm.REWORK_REQUIRED, sm.CLAIM): sm.IN_PROGRESS,
    (sm.VERIFIED, sm.COMPLETE_TRANSITION): sm.COMPLETE,
}


@settings(max_examples=200)
@given(
    state=st.sampled_from(sm.ALL_STATES),
    transition=st.sampled_from(sm.ALL_TRANSITIONS),
)
def test_state_machine_validity(state: str, transition: str) -> None:
    # Feature: workload-operations-platform, Property 2: State Machine Transition Validity
    # Validates: Requirements FR-021 AC-1, FR-021 AC-2, BR-009
    result = sm.apply_transition(current_state=state, transition=transition)
    expected_valid = (state, transition) in VALID_TRANSITION_PAIRS

    assert result.is_valid == expected_valid
    if expected_valid:
        assert result.error_code is None
        assert result.new_state == EXPECTED_NEW_STATE[(state, transition)]
    else:
        # Invalid pairs return INVALID_STATE_TRANSITION and leave the state unchanged.
        assert result.error_code == sm.INVALID_STATE_TRANSITION
        assert result.new_state == state
