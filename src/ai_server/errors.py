"""Shared base exception for explicit ai-server domain failures."""

from typing import ClassVar


class AiServerError(Exception):
    """Base exception for expected ai-server domain failures."""

    code: ClassVar[str] = "ai_server_error"


__all__ = ["AiServerError"]
