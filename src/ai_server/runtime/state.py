"""Runtime states and the explicit Phase 0 transition graph."""

from enum import StrEnum

from ai_server.runtime.errors import InvalidStateTransitionError


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
    RuntimeState.RECEIVED: frozenset({RuntimeState.CONTEXT_BUILDING}),
    RuntimeState.CONTEXT_BUILDING: frozenset({RuntimeState.PLANNING}),
    RuntimeState.PLANNING: frozenset({RuntimeState.POLICY_CHECK}),
    RuntimeState.POLICY_CHECK: frozenset(
        {RuntimeState.WAITING_FOR_APPROVAL, RuntimeState.EXECUTING}
    ),
    RuntimeState.WAITING_FOR_APPROVAL: frozenset({RuntimeState.EXECUTING}),
    RuntimeState.EXECUTING: frozenset({RuntimeState.VERIFYING}),
    RuntimeState.VERIFYING: frozenset({RuntimeState.COMPLETED}),
}


class RuntimeStateMachine:
    """Validate state movement without performing business operations."""

    @staticmethod
    def allowed_targets(current: RuntimeState) -> frozenset[RuntimeState]:
        """Return the states reachable from the current state in Phase 0."""
        return _ALLOWED_TRANSITIONS.get(current, frozenset())

    @classmethod
    def can_transition(cls, current: RuntimeState, target: RuntimeState) -> bool:
        """Return whether current may transition to target."""
        return target in cls.allowed_targets(current)

    @classmethod
    def transition(cls, current: RuntimeState, target: RuntimeState) -> RuntimeState:
        """Validate and return target or raise an explicit domain error."""
        if not cls.can_transition(current, target):
            message = f"Invalid Runtime transition: {current.value} -> {target.value}"
            raise InvalidStateTransitionError(message)
        return target


__all__ = ["RuntimeState", "RuntimeStateMachine"]
