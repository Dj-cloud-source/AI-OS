from math import inf, nan

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from ai_server.tools.hashing import (
    CanonicalizationError,
    canonical_json_bytes,
    canonical_json_sha256,
)


class HashInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    z: int
    a: str


class UnsupportedHashInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, strict=True)

    value: object


def test_rfc8785_canonical_json_has_stable_key_order_and_utf8() -> None:
    first: JsonValue = {"message": "你好", "values": {"z": 1, "a": 2}}
    second: JsonValue = {"values": {"a": 2, "z": 1}, "message": "你好"}

    canonical = canonical_json_bytes(first)

    assert canonical == b'{"message":"\xe4\xbd\xa0\xe5\xa5\xbd","values":{"a":2,"z":1}}'
    assert canonical_json_bytes(second) == canonical
    assert canonical_json_sha256(second) == canonical_json_sha256(first)


def test_canonical_json_sha256_matches_golden_digest() -> None:
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert canonical_json_sha256({"b": 1, "a": 2}) == (
        "d3626ac30a87e6f7a6428233b3c68299976865fa5508e4267c5415c76af7a772"
    )


def test_pydantic_models_are_normalized_before_hashing() -> None:
    model = HashInput(z=1, a="safe")

    assert canonical_json_bytes(model) == b'{"a":"safe","z":1}'
    assert canonical_json_sha256(model) == canonical_json_sha256({"a": "safe", "z": 1})


def test_hash_changes_when_protocol_content_changes() -> None:
    original: JsonValue = {"target": "local-mock", "include_services": True}
    changed: JsonValue = {"target": "local-mock", "include_services": False}

    assert canonical_json_sha256(original) != canonical_json_sha256(changed)


def test_unserializable_models_fail_with_explicit_error() -> None:
    with pytest.raises(CanonicalizationError, match="RFC 8785"):
        canonical_json_bytes(UnsupportedHashInput(value=object()))


@pytest.mark.parametrize("unsupported", [nan, inf, -inf, 2**60])
def test_non_canonical_numbers_fail_with_explicit_error(unsupported: float | int) -> None:
    with pytest.raises(CanonicalizationError, match="RFC 8785"):
        canonical_json_bytes({"value": unsupported})
