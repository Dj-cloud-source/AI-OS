from ai_server.approval.errors import (
    ApprovalConfigurationError,
    ApprovalError,
    ApprovalReviewError,
    ApprovalStateError,
)
from ai_server.policy.errors import PolicyConfigurationError
from ai_server.runtime.errors import (
    AiServerError,
    ApprovalRequiredError,
    InvalidClockError,
    InvalidRuntimeOutcomeError,
    InvalidStateTransitionError,
    InvalidTaskError,
    PlanMismatchError,
    PolicyDeniedError,
    PolicyEvaluationError,
    ReservedStateTransitionError,
    TerminalStateMutationError,
    ToolExecutionError,
    UnsupportedTaskError,
    VerificationError,
)


def test_domain_error_codes_are_stable_and_unique() -> None:
    expected_codes = {
        AiServerError: "ai_server_error",
        ApprovalError: "approval_error",
        ApprovalConfigurationError: "approval_configuration",
        ApprovalReviewError: "approval_review",
        ApprovalStateError: "approval_state",
        ApprovalRequiredError: "approval_required",
        InvalidClockError: "invalid_clock",
        InvalidRuntimeOutcomeError: "invalid_runtime_outcome",
        InvalidTaskError: "invalid_task",
        InvalidStateTransitionError: "invalid_state_transition",
        TerminalStateMutationError: "terminal_state_mutation",
        ReservedStateTransitionError: "reserved_state_transition",
        UnsupportedTaskError: "unsupported_task",
        PlanMismatchError: "plan_mismatch",
        PolicyConfigurationError: "policy_configuration_error",
        PolicyDeniedError: "policy_denied",
        PolicyEvaluationError: "policy_evaluation_failed",
        ToolExecutionError: "tool_execution",
        VerificationError: "verification",
    }

    assert {error_type.code for error_type in expected_codes} == set(expected_codes.values())
    for error_type, expected_code in expected_codes.items():
        assert error_type.code == expected_code
