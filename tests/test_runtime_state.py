from itertools import product

import pytest

from ai_server.runtime.errors import InvalidStateTransitionError
from ai_server.runtime.state import RuntimeState, RuntimeStateMachine

EXPECTED_STATES = (
    RuntimeState.RECEIVED,
    RuntimeState.CONTEXT_BUILDING,
    RuntimeState.PLANNING,
    RuntimeState.POLICY_CHECK,
    RuntimeState.WAITING_FOR_APPROVAL,
    RuntimeState.EXECUTING,
    RuntimeState.VERIFYING,
    RuntimeState.COMPLETED,
    RuntimeState.FAILED,
    RuntimeState.PARTIAL_SUCCESS,
    RuntimeState.ROLLBACK,
    RuntimeState.MANUAL_INTERVENTION_REQUIRED,
)

EXPECTED_EDGES = {
    (RuntimeState.RECEIVED, RuntimeState.CONTEXT_BUILDING),
    (RuntimeState.CONTEXT_BUILDING, RuntimeState.PLANNING),
    (RuntimeState.PLANNING, RuntimeState.POLICY_CHECK),
    (RuntimeState.POLICY_CHECK, RuntimeState.WAITING_FOR_APPROVAL),
    (RuntimeState.POLICY_CHECK, RuntimeState.EXECUTING),
    (RuntimeState.WAITING_FOR_APPROVAL, RuntimeState.EXECUTING),
    (RuntimeState.EXECUTING, RuntimeState.VERIFYING),
    (RuntimeState.VERIFYING, RuntimeState.COMPLETED),
}


def test_runtime_state_has_exact_canonical_values_without_aliases() -> None:
    assert tuple(RuntimeState) == EXPECTED_STATES
    assert len(RuntimeState.__members__) == len(EXPECTED_STATES)
    assert "WAITING_APPROVAL" not in RuntimeState.__members__


@pytest.mark.parametrize(("current", "target"), list(product(EXPECTED_STATES, repeat=2)))
def test_phase_zero_transition_matrix(
    current: RuntimeState,
    target: RuntimeState,
) -> None:
    expected = (current, target) in EXPECTED_EDGES
    assert RuntimeStateMachine.can_transition(current, target) is expected
    assert (target in RuntimeStateMachine.allowed_targets(current)) is expected

    if expected:
        assert RuntimeStateMachine.transition(current, target) is target
    else:
        with pytest.raises(InvalidStateTransitionError):
            RuntimeStateMachine.transition(current, target)
