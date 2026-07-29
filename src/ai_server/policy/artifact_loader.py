"""Load and verify immutable package-resident Policy artifacts."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any, Never, cast

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import JsonValue, ValidationError

from ai_server.models.policy import PolicyProfile, PolicyReviewRecord
from ai_server.models.tool import ToolMetadata
from ai_server.policy.errors import PolicyConfigurationError
from ai_server.tools.hashing import canonical_json_sha256

DEFAULT_POLICY_ID = "local-default"
DEFAULT_POLICY_VERSION = "1.0.0"
POLICY_PROFILE_SCHEMA_ID = "urn:ai-server:policy:profile:1"
POLICY_REVIEW_RECORD_SCHEMA_ID = "urn:ai-server:policy:review-record:1"

_POLICY_SCHEMA_FILES = (
    "policy-profile-v1.json",
    "policy-review-record-v1.json",
)
_POLICY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True, slots=True)
class ValidatedPolicyArtifacts:
    """Immutable verified evidence for one active Policy Profile."""

    profile: PolicyProfile
    review_record: PolicyReviewRecord
    profile_hash: str


def load_policy_artifacts(
    authoritative_metadata: Mapping[tuple[str, str], ToolMetadata],
    *,
    policy_id: str = DEFAULT_POLICY_ID,
    version: str = DEFAULT_POLICY_VERSION,
) -> ValidatedPolicyArtifacts:
    """Load and fail-closed validate one exact reviewed Policy Profile."""
    if (
        type(policy_id) is not str
        or _POLICY_ID_PATTERN.fullmatch(policy_id) is None
        or type(version) is not str
        or _VERSION_PATTERN.fullmatch(version) is None
        or not isinstance(authoritative_metadata, Mapping)
    ):
        raise PolicyConfigurationError("Policy configuration identity is invalid")
    try:
        artifact_root = files("ai_server.policy_artifacts").joinpath(policy_id, version)
        schemas = _load_normative_schemas()
        profile_raw, profile_text = _load_json_document(artifact_root.joinpath("profile.json"))
        review_raw, review_text = _load_json_document(artifact_root.joinpath("review-record.json"))
        _validate_schema_document(
            schemas[POLICY_PROFILE_SCHEMA_ID],
            profile_raw,
        )
        _validate_schema_document(
            schemas[POLICY_REVIEW_RECORD_SCHEMA_ID],
            review_raw,
        )
        profile = PolicyProfile.model_validate_json(profile_text, strict=True)
        review_record = PolicyReviewRecord.model_validate_json(review_text, strict=True)
        profile_hash = canonical_json_sha256(cast(JsonValue, profile_raw))
        _validate_review_binding(
            profile,
            review_record,
            policy_id=policy_id,
            version=version,
            profile_hash=profile_hash,
        )
        _validate_registry_references(profile, authoritative_metadata)
        return ValidatedPolicyArtifacts(
            profile=profile,
            review_record=review_record,
            profile_hash=profile_hash,
        )
    except PolicyConfigurationError:
        raise
    except (KeyError, TypeError, ValueError, ValidationError):
        raise PolicyConfigurationError("Policy configuration failed strict validation") from None
    except BaseException:
        raise PolicyConfigurationError("Policy configuration could not be loaded safely") from None


def _load_normative_schemas() -> dict[str, dict[str, Any]]:
    schema_package = files("ai_server.schemas.policy")
    schemas: dict[str, dict[str, Any]] = {}
    for filename in _POLICY_SCHEMA_FILES:
        raw, _ = _load_json_document(schema_package.joinpath(filename))
        Draft202012Validator.check_schema(raw)
        schema_id = raw.get("$id")
        if type(schema_id) is not str or schema_id in schemas:
            raise PolicyConfigurationError("Policy Schema identity is invalid")
        schemas[schema_id] = raw
    expected_ids = {
        POLICY_PROFILE_SCHEMA_ID,
        POLICY_REVIEW_RECORD_SCHEMA_ID,
    }
    if set(schemas) != expected_ids:
        raise PolicyConfigurationError("Normative Policy Schema set is incomplete")
    return schemas


def _load_json_document(resource: Traversable) -> tuple[dict[str, Any], str]:
    if not resource.is_file():
        raise PolicyConfigurationError("Required Policy artifact is missing")
    text = resource.read_text(encoding="utf-8")
    raw = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_object_keys,
        parse_constant=_reject_nonstandard_json_constant,
    )
    if type(raw) is not dict:
        raise PolicyConfigurationError("Policy artifact must be a JSON object")
    return cast(dict[str, Any], raw), text


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise PolicyConfigurationError("Policy JSON object keys must be unique")
        document[key] = value
    return document


def _reject_nonstandard_json_constant(value: str) -> Never:
    del value
    raise PolicyConfigurationError("Policy artifacts must use standard JSON")


def _validate_schema_document(
    schema: dict[str, Any],
    value: dict[str, Any],
) -> None:
    validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    )
    if next(validator.iter_errors(value), None) is not None:
        raise PolicyConfigurationError("Policy artifact does not match its Schema")


def _validate_review_binding(
    profile: PolicyProfile,
    review_record: PolicyReviewRecord,
    *,
    policy_id: str,
    version: str,
    profile_hash: str,
) -> None:
    if (
        profile.policy_id != policy_id
        or profile.version != version
        or review_record.policy_id != policy_id
        or review_record.version != version
        or review_record.content_hash != profile_hash
        or review_record.status != "active"
        or review_record.reviewer != "local-owner"
    ):
        raise PolicyConfigurationError("Policy Profile does not match its active review record")


def _validate_registry_references(
    profile: PolicyProfile,
    authoritative_metadata: Mapping[tuple[str, str], ToolMetadata],
) -> None:
    for rule in profile.rules:
        try:
            metadata = authoritative_metadata[(rule.tool_id, rule.tool_version)]
        except (KeyError, TypeError):
            raise PolicyConfigurationError(
                "Policy capability references an unavailable Tool"
            ) from None
        if type(metadata) is not ToolMetadata:
            raise PolicyConfigurationError("Policy capability references malformed Tool metadata")
        try:
            validated_metadata = ToolMetadata.model_validate(
                metadata.model_dump(mode="python", warnings="error"),
                strict=True,
            )
        except BaseException:
            raise PolicyConfigurationError(
                "Policy capability references malformed Tool metadata"
            ) from None
        if (
            validated_metadata.tool_id != rule.tool_id
            or validated_metadata.version != rule.tool_version
            or validated_metadata.contract_hash != rule.contract_hash
            or validated_metadata.implementation_hash != rule.implementation_hash
            or validated_metadata.target_scope.resource_type != rule.target.resource_type
            or validated_metadata.target_scope.maximum_targets != 1
            or validated_metadata.target_scope.allow_dynamic_expansion is not False
            or rule.target.target_id != rule.target.resource_id
        ):
            raise PolicyConfigurationError(
                "Policy capability does not match authoritative Tool metadata"
            )


__all__ = [
    "DEFAULT_POLICY_ID",
    "DEFAULT_POLICY_VERSION",
    "POLICY_PROFILE_SCHEMA_ID",
    "POLICY_REVIEW_RECORD_SCHEMA_ID",
    "ValidatedPolicyArtifacts",
    "load_policy_artifacts",
]
