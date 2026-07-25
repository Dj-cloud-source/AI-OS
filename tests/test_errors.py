from ai_server.runtime.errors import (
    AiServerError,
    InvalidStateTransitionError,
    InvalidTaskError,
    PlanMismatchError,
    PolicyDeniedError,
    ToolExecutionError,
    UnsupportedTaskError,
    VerificationError,
)


def test_domain_error_codes_are_stable_and_unique() -> None:
    expected_codes = {
        AiServerError: "ai_server_error",
        InvalidTaskError: "invalid_task",
        InvalidStateTransitionError: "invalid_state_transition",
        UnsupportedTaskError: "unsupported_task",
        PlanMismatchError: "plan_mismatch",
        PolicyDeniedError: "policy_denied",
        ToolExecutionError: "tool_execution",
        VerificationError: "verification",
    }

    assert {error_type.code for error_type in expected_codes} == set(expected_codes.values())
    for error_type, expected_code in expected_codes.items():
        assert error_type.code == expected_code
