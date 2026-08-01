"""Explicit fail-closed errors for governed execution."""

import re
from typing import ClassVar

from ai_server.errors import AiServerError
from ai_server.models.approval import ApprovalValidationReason

_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_EXECUTION_AUTHORIZATION_REASONS = frozenset(
    {
        "approval_missing",
        "approval_record_unavailable",
        "arguments_hash_failed",
        "attempt_id_invalid",
        "authorization_evidence_failed",
        "authorization_evidence_invalid",
        "authorization_hash_failed",
        "executor_clock_failed",
        "invocation_id_invalid",
        "l3_challenge_invalid",
        "l3_role_invalid",
        "mutation_role_invalid",
        "plan_malformed",
        "policy_decision_malformed",
        "policy_decision_mismatch",
        "policy_revalidation_failed",
        "policy_step_mismatch",
        "target_mismatch",
        "tool_call_build_failed",
        "unexpected_approval",
    }
    | {reason.value for reason in ApprovalValidationReason}
)


class ExecutorError(AiServerError):
    """Base class for safe Executor failures."""

    code: ClassVar[str] = "executor_error"


class ExecutorConfigurationError(ExecutorError):
    """Reject malformed authorities, clocks, ID factories, or Gateway bindings."""

    code: ClassVar[str] = "executor_configuration"


class ExecutionAuthorizationError(ExecutorError):
    """Reject work that cannot establish one governed execution attempt."""

    code: ClassVar[str] = "execution_authorization_failed"

    def __init__(self, message: str, *, reason_code: str) -> None:
        """Create a sanitized authorization failure with one stable reason."""
        if (
            type(reason_code) is not str
            or _REASON_PATTERN.fullmatch(reason_code) is None
            or reason_code not in _EXECUTION_AUTHORIZATION_REASONS
        ):
            reason_code = self.code
        self.reason_code = reason_code
        super().__init__(message)


def safe_execution_authorization_reason(error: BaseException) -> str:
    """Return only a trusted stable reason from an exact authorization error."""
    if type(error) is not ExecutionAuthorizationError:
        return ExecutionAuthorizationError.code
    reason = error.__dict__.get("reason_code")
    if type(reason) is not str or reason not in _EXECUTION_AUTHORIZATION_REASONS:
        return ExecutionAuthorizationError.code
    return reason


class ExecutionAttemptError(ExecutorError):
    """Reject a malformed, forged, closed, replayed, or out-of-order attempt."""

    code: ClassVar[str] = "execution_attempt_invalid"


__all__ = [
    "ExecutionAttemptError",
    "ExecutionAuthorizationError",
    "ExecutorConfigurationError",
    "ExecutorError",
    "safe_execution_authorization_reason",
]
