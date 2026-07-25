"""Explicit domain exceptions for the Phase 0 Runtime."""

from typing import ClassVar


class AiServerError(Exception):
    """Base exception for expected Runtime failures."""

    code: ClassVar[str] = "ai_server_error"


class InvalidTaskError(AiServerError):
    """Raised when Runtime input is not a valid local Phase 0 Task."""

    code: ClassVar[str] = "invalid_task"


class InvalidStateTransitionError(AiServerError):
    """Raised when a Runtime state transition is not allowed."""

    code: ClassVar[str] = "invalid_state_transition"


class UnsupportedTaskError(AiServerError):
    """Raised when the Phase 0 planner cannot handle a task request."""

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
    "InvalidTaskError",
    "InvalidStateTransitionError",
    "PlanMismatchError",
    "PolicyDeniedError",
    "ToolExecutionError",
    "UnsupportedTaskError",
    "VerificationError",
]
