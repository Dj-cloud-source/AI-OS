"""Explicit domain exceptions for the Phase 1 Runtime."""

from typing import ClassVar


class AiServerError(Exception):
    """Base exception for expected Runtime failures."""

    code: ClassVar[str] = "ai_server_error"


class InvalidTaskError(AiServerError):
    """Raised when Runtime input is not a valid local Task."""

    code: ClassVar[str] = "invalid_task"


class InvalidStateTransitionError(AiServerError):
    """Raised when a Runtime state transition is not allowed."""

    code: ClassVar[str] = "invalid_state_transition"


class TerminalStateMutationError(InvalidStateTransitionError):
    """Raised when a caller attempts to mutate a terminal Task."""

    code: ClassVar[str] = "terminal_state_mutation"


class ReservedStateTransitionError(InvalidStateTransitionError):
    """Raised when a transition enters or leaves a state reserved for a later phase."""

    code: ClassVar[str] = "reserved_state_transition"


class ApprovalRequiredError(AiServerError):
    """Signal that Policy requires approval before any execution may occur."""

    code: ClassVar[str] = "approval_required"


class ApprovalResumeUnavailableError(InvalidStateTransitionError):
    """Raised when Phase 1 is asked to resume approval-paused work."""

    code: ClassVar[str] = "approval_resume_unavailable"


class InvalidClockError(AiServerError):
    """Raised when the lifecycle clock fails or returns an invalid timestamp."""

    code: ClassVar[str] = "invalid_clock"


class InvalidRuntimeOutcomeError(AiServerError):
    """Raised when a supplied RuntimeOutcome fails strict boundary validation."""

    code: ClassVar[str] = "invalid_runtime_outcome"


class UnsupportedTaskError(AiServerError):
    """Raised when the planner cannot handle a task request."""

    code: ClassVar[str] = "unsupported_task"


class PlanMismatchError(AiServerError):
    """Raised when a plan does not match its Task or registered Tool."""

    code: ClassVar[str] = "plan_mismatch"


class PolicyDeniedError(AiServerError):
    """Raised when deterministic Policy does not allow execution."""

    code: ClassVar[str] = "policy_denied"


class ToolExecutionError(AiServerError):
    """Raised when Executor cannot safely invoke the planned Tool."""

    code: ClassVar[str] = "tool_execution"


class VerificationError(AiServerError):
    """Raised when structured execution evidence does not verify the goal."""

    code: ClassVar[str] = "verification"


__all__ = [
    "AiServerError",
    "ApprovalRequiredError",
    "ApprovalResumeUnavailableError",
    "InvalidClockError",
    "InvalidRuntimeOutcomeError",
    "InvalidTaskError",
    "InvalidStateTransitionError",
    "PlanMismatchError",
    "PolicyDeniedError",
    "ReservedStateTransitionError",
    "TerminalStateMutationError",
    "ToolExecutionError",
    "UnsupportedTaskError",
    "VerificationError",
]
