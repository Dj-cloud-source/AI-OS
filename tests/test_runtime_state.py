from itertools import product
from typing import cast

import pytest

from ai_server.runtime.errors import (
    InvalidStateTransitionError,
    ReservedStateTransitionError,
    TerminalStateMutationError,
)
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
    (RuntimeState.RECEIVED, RuntimeState.FAILED),
    (RuntimeState.CONTEXT_BUILDING, RuntimeState.PLANNING),
    (RuntimeState.CONTEXT_BUILDING, RuntimeState.FAILED),
    (RuntimeState.PLANNING, RuntimeState.POLICY_CHECK),
    (RuntimeState.PLANNING, RuntimeState.FAILED),
    (RuntimeState.POLICY_CHECK, RuntimeState.WAITING_FOR_APPROVAL),
    (RuntimeState.POLICY_CHECK, RuntimeState.FAILED),
    (RuntimeState.WAITING_FOR_APPROVAL, RuntimeState.EXECUTING),
    (RuntimeState.WAITING_FOR_APPROVAL, RuntimeState.FAILED),
    (RuntimeState.EXECUTING, RuntimeState.VERIFYING),
    (RuntimeState.EXECUTING, RuntimeState.FAILED),
    (RuntimeState.VERIFYING, RuntimeState.COMPLETED),
    (RuntimeState.VERIFYING, RuntimeState.FAILED),
}

TERMINAL_STATES = {RuntimeState.COMPLETED, RuntimeState.FAILED}
RESERVED_STATES = {
    RuntimeState.PARTIAL_SUCCESS,
    RuntimeState.ROLLBACK,
    RuntimeState.MANUAL_INTERVENTION_REQUIRED,
}


def test_runtime_state_has_exact_canonical_values_without_aliases() -> None:
    assert tuple(RuntimeState) == EXPECTED_STATES
    assert len(RuntimeState.__members__) == len(EXPECTED_STATES)
    assert "WAITING_APPROVAL" not in RuntimeState.__members__


@pytest.mark.parametrize(("current", "target"), list(product(EXPECTED_STATES, repeat=2)))
def test_phase_one_transition_matrix(
    current: RuntimeState,
    target: RuntimeState,
) -> None:
    expected = (current, target) in EXPECTED_EDGES
    assert RuntimeStateMachine.can_transition(current, target) is expected
    assert (target in RuntimeStateMachine.allowed_targets(current)) is expected

    if expected:
        assert RuntimeStateMachine.transition(current, target) is target
    elif current in TERMINAL_STATES:
        with pytest.raises(TerminalStateMutationError):
            RuntimeStateMachine.transition(current, target)
    elif current in RESERVED_STATES or target in RESERVED_STATES:
        with pytest.raises(ReservedStateTransitionError):
            RuntimeStateMachine.transition(current, target)
    else:
        with pytest.raises(InvalidStateTransitionError):
            RuntimeStateMachine.transition(current, target)


def test_waiting_is_the_only_execution_gate_and_reserved_states_have_no_edges() -> None:
    assert RuntimeStateMachine.allowed_targets(RuntimeState.WAITING_FOR_APPROVAL) == {
        RuntimeState.EXECUTING,
        RuntimeState.FAILED,
    }
    assert RuntimeStateMachine.can_transition(
        RuntimeState.WAITING_FOR_APPROVAL,
        RuntimeState.EXECUTING,
    )
    assert not RuntimeStateMachine.can_transition(
        RuntimeState.POLICY_CHECK,
        RuntimeState.EXECUTING,
    )
    for state in RESERVED_STATES:
        assert RuntimeStateMachine.allowed_targets(state) == frozenset()
        assert not any(
            RuntimeStateMachine.can_transition(source, state) for source in EXPECTED_STATES
        )


@pytest.mark.parametrize("invalid", ["RECEIVED", "BAD", object()])
def test_state_machine_rejects_non_enum_values_with_explicit_error(
    invalid: object,
) -> None:
    forged = cast(RuntimeState, invalid)

    assert RuntimeStateMachine.allowed_targets(forged) == frozenset()
    assert not RuntimeStateMachine.can_transition(
        forged,
        RuntimeState.CONTEXT_BUILDING,
    )
    assert not RuntimeStateMachine.can_transition(RuntimeState.RECEIVED, forged)
    with pytest.raises(InvalidStateTransitionError):
        RuntimeStateMachine.transition(forged, RuntimeState.CONTEXT_BUILDING)
    with pytest.raises(InvalidStateTransitionError):
        RuntimeStateMachine.transition(RuntimeState.RECEIVED, forged)
