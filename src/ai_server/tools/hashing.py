"""RFC 8785 canonical JSON and SHA-256 helpers for protocol integrity."""

from hashlib import sha256

import rfc8785
from pydantic import BaseModel, JsonValue
from pydantic_core import PydanticSerializationError

type Canonicalizable = JsonValue | BaseModel


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented as RFC 8785 canonical JSON."""


def canonical_json_bytes(value: Canonicalizable) -> bytes:
    """Return the RFC 8785 canonical JSON encoding of a JSON-compatible value."""
    try:
        normalized: JsonValue
        if isinstance(value, BaseModel):
            normalized = value.model_dump(mode="json", warnings="error")
        else:
            normalized = value
        return rfc8785.dumps(normalized)
    except (
        PydanticSerializationError,
        rfc8785.CanonicalizationError,
        TypeError,
        ValueError,
    ):
        raise CanonicalizationError(
            "Value cannot be represented as RFC 8785 canonical JSON"
        ) from None


def canonical_json_sha256(value: Canonicalizable) -> str:
    """Return a lowercase SHA-256 hex digest over RFC 8785 canonical JSON."""
    return sha256(canonical_json_bytes(value)).hexdigest()


__all__ = [
    "Canonicalizable",
    "CanonicalizationError",
    "canonical_json_bytes",
    "canonical_json_sha256",
]
