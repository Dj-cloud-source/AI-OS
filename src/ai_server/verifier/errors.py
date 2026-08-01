"""Explicit sanitized errors for the deterministic Verifier boundary."""


class VerificationInputError(ValueError):
    """Raised when no trustworthy Plan and Context binding can be established."""


__all__ = ["VerificationInputError"]
