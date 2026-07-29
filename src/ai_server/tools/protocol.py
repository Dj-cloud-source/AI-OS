"""Shared constants and callable boundary for the Tool Protocol."""

from typing import Literal, Protocol

from pydantic import BaseModel

TOOL_CONTRACT_SCHEMA_VERSION: Literal["1"] = "1"
TOOL_SCHEMA_DIALECT: Literal["https://json-schema.org/draft/2020-12/schema"] = (
    "https://json-schema.org/draft/2020-12/schema"
)

TOOL_CONTRACT_SCHEMA_ID = "urn:ai-server:schema:tool-contract-v1"
TOOL_RESULT_SCHEMA_ID = "urn:ai-server:schema:tool-result-v1"
TOOL_REPLAY_FIXTURE_SCHEMA_ID = "urn:ai-server:schema:tool-replay-fixture-v1"
TOOL_REGISTRY_RECORD_SCHEMA_ID = "urn:ai-server:schema:tool-registry-record-v1"
TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID = "urn:ai-server:schema:tool-implementation-bundle-v1"

NORMATIVE_TOOL_SCHEMA_FILES = (
    "tool-contract-v1.json",
    "tool-result-v1.json",
    "tool-replay-fixture-v1.json",
    "tool-registry-record-v1.json",
    "tool-implementation-bundle-v1.json",
)

GATEWAY_FAILURE_CONTRACTS = (
    ("arguments_hash_mismatch", "integrity", False),
    ("gateway_clock_failed", "internal", False),
    ("invalid_arguments", "validation", False),
    ("malformed_tool_output", "output", False),
    ("result_redaction_failed", "safety", False),
    ("target_not_allowed", "target", False),
    ("tool_execution_failed", "execution", False),
    ("tool_timeout", "timeout", False),
)

FORBIDDEN_DATA_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "token",
    }
)

FORBIDDEN_VALUE_MARKERS = (
    "-----begin private key-----",
    "-----begin openssh private key-----",
    "#!/bin/",
    "bash -c",
    "curl | sh",
    "powershell -command",
    "sh -c",
    "wget | sh",
)


class ToolHandler[ArgumentsT: BaseModel, PayloadT: BaseModel](Protocol):
    """Return only typed Tool payload data; the Gateway owns the result envelope."""

    def __call__(self, arguments: ArgumentsT, /) -> PayloadT:
        """Invoke one bounded Tool operation with already validated arguments."""
        ...


__all__ = [
    "FORBIDDEN_DATA_KEYS",
    "FORBIDDEN_VALUE_MARKERS",
    "GATEWAY_FAILURE_CONTRACTS",
    "NORMATIVE_TOOL_SCHEMA_FILES",
    "TOOL_CONTRACT_SCHEMA_ID",
    "TOOL_CONTRACT_SCHEMA_VERSION",
    "TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID",
    "TOOL_REGISTRY_RECORD_SCHEMA_ID",
    "TOOL_REPLAY_FIXTURE_SCHEMA_ID",
    "TOOL_RESULT_SCHEMA_ID",
    "TOOL_SCHEMA_DIALECT",
    "ToolHandler",
]
