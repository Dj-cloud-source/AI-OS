"""Explicit failures raised while constructing deterministic Policy."""

from typing import ClassVar

from ai_server.errors import AiServerError


class PolicyConfigurationError(AiServerError, ValueError):
    """Raised when reviewed Policy configuration cannot be trusted at startup."""

    code: ClassVar[str] = "policy_configuration_error"


class PolicyEvaluationError(AiServerError):
    """Raised when Policy cannot produce a trustworthy structured decision."""

    code: ClassVar[str] = "policy_evaluation_failed"


__all__ = ["PolicyConfigurationError", "PolicyEvaluationError"]
