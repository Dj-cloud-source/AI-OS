"""Runtime states and the explicit fail-closed Phase 1 transition graph."""

from enum import StrEnum

from ai_server.runtime.errors import (
    InvalidStateTransitionError,
    ReservedStateTransitionError,
    TerminalStateMutationError,
)


class RuntimeState(StrEnum):
    """Canonical task lifecycle states."""

    RECEIVED = "RECEIVED"
    CONTEXT_BUILDING = "CONTEXT_BUILDING"
    PLANNING = "PLANNING"
    POLICY_CHECK = "POLICY_CHECK"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    ROLLBACK = "ROLLBACK"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"


_ALLOWED_TRANSITIONS: dict[RuntimeState, frozenset[RuntimeState]] = {
    RuntimeState.RECEIVED: frozenset({RuntimeState.CONTEXT_BUILDING, RuntimeState.FAILED}),
    RuntimeState.CONTEXT_BUILDING: frozenset({RuntimeState.PLANNING, RuntimeState.FAILED}),
    RuntimeState.PLANNING: frozenset({RuntimeState.POLICY_CHECK, RuntimeState.FAILED}),
    RuntimeState.POLICY_CHECK: frozenset(
        {
            RuntimeState.WAITING_FOR_APPROVAL,
            RuntimeState.EXECUTING,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.WAITING_FOR_APPROVAL: frozenset({RuntimeState.FAILED}),
    RuntimeState.EXECUTING: frozenset({RuntimeState.VERIFYING, RuntimeState.FAILED}),
    RuntimeState.VERIFYING: frozenset({RuntimeState.COMPLETED, RuntimeState.FAILED}),
}

_TERMINAL_STATES = frozenset({RuntimeState.COMPLETED, RuntimeState.FAILED})
_RESERVED_STATES = frozenset(
    {
        RuntimeState.PARTIAL_SUCCESS,
        RuntimeState.ROLLBACK,
        RuntimeState.MANUAL_INTERVENTION_REQUIRED,
    }
)


class RuntimeStateMachine:
    """Validate state movement without performing business operations."""

    @staticmethod
    def allowed_targets(current: object) -> frozenset[RuntimeState]:
        """Return the states reachable from the current state in Phase 1."""
        if not isinstance(current, RuntimeState):
            return frozenset()
        return _ALLOWED_TRANSITIONS.get(current, frozenset())

    @classmethod
    def can_transition(cls, current: object, target: object) -> bool:
        """Return whether current may transition to target."""
        if not isinstance(current, RuntimeState) or not isinstance(target, RuntimeState):
            return False
        return target in cls.allowed_targets(current)

    @classmethod
    def transition(cls, current: object, target: object) -> RuntimeState:
        """Validate and return target or raise an explicit domain error."""
        if not isinstance(current, RuntimeState) or not isinstance(target, RuntimeState):
            raise InvalidStateTransitionError(
                "Runtime transitions require canonical RuntimeState values"
            )
        if cls.can_transition(current, target):
            return target

        message = f"Invalid Runtime transition: {current.value} -> {target.value}"
        if current in _TERMINAL_STATES:
            raise TerminalStateMutationError(message)
        if current in _RESERVED_STATES or target in _RESERVED_STATES:
            raise ReservedStateTransitionError(message)
        raise InvalidStateTransitionError(message)


__all__ = ["RuntimeState", "RuntimeStateMachine"]
