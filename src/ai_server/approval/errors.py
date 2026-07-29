"""Explicit failures for the process-local Approval boundary."""

from typing import ClassVar

from ai_server.errors import AiServerError


class ApprovalError(AiServerError):
    """Base class for fail-closed Approval failures."""

    code: ClassVar[str] = "approval_error"


class ApprovalConfigurationError(ApprovalError):
    """Reject malformed clocks, metadata, constraints, or ID factories."""

    code: ClassVar[str] = "approval_configuration"


class ApprovalReviewError(ApprovalError):
    """Reject an untrusted or ineligible Plan review request."""

    code: ClassVar[str] = "approval_review"


class ApprovalStateError(ApprovalError):
    """Reject an invalid, unknown, terminal, or replayed Approval action."""

    code: ClassVar[str] = "approval_state"


__all__ = [
    "ApprovalConfigurationError",
    "ApprovalError",
    "ApprovalReviewError",
    "ApprovalStateError",
]
