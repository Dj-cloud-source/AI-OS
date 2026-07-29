"""Load and verify immutable package-resident Tool registration artifacts."""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any, ClassVar, cast

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import JsonValue, ValidationError
from referencing import Registry, Resource

from ai_server.models.tool import (
    ToolContract,
    ToolMetadata,
    ToolRegistryRecord,
    ToolRegistryStatus,
)
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.tools.protocol import (
    FORBIDDEN_DATA_KEYS,
    FORBIDDEN_VALUE_MARKERS,
    GATEWAY_FAILURE_CONTRACTS,
    NORMATIVE_TOOL_SCHEMA_FILES,
    TOOL_CONTRACT_SCHEMA_ID,
    TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID,
    TOOL_REGISTRY_RECORD_SCHEMA_ID,
    TOOL_REPLAY_FIXTURE_SCHEMA_ID,
    TOOL_RESULT_SCHEMA_ID,
)

RUNTIME_ABI = "python-source-v1.requires-python-ge-3.12"
DEPENDENCY_LOCK_FORMAT = "uv-tool-lock-v1"

_TOOL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PREFIXED_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRUSTED_PACKAGE_REGISTRY = "https://pypi.org/simple"
_TRUSTED_ARTIFACT_PREFIX = "https://files.pythonhosted.org/packages/"


class ToolArtifactValidationError(ValueError):
    """Raised when reviewed Tool artifacts cannot be trusted for registration."""

    code: ClassVar[str] = "tool_artifact_validation"


@dataclass(frozen=True, slots=True)
class ValidatedToolArtifacts:
    """Immutable verified evidence required for one explicit Tool registration."""

    contract: ToolContract
    record: ToolRegistryRecord
    metadata: ToolMetadata
    contract_hash: str
    implementation_hash: str
    implementation_files: tuple[str, ...]
    handler_entry_point: str
    input_model_entry_point: str
    output_model_entry_point: str
    fixture_ids: tuple[str, ...]


def load_tool_artifacts(
    tool_id: str,
    version: str,
    *,
    handler_entry_point: str,
    input_model_entry_point: str,
    output_model_entry_point: str,
) -> ValidatedToolArtifacts:
    """Load, validate, and integrity-check one exact packaged Tool artifact set."""
    if (
        type(tool_id) is not str
        or _TOOL_ID_PATTERN.fullmatch(tool_id) is None
        or type(version) is not str
        or _VERSION_PATTERN.fullmatch(version) is None
        or any(
            type(entry_point) is not str or not entry_point
            for entry_point in (
                handler_entry_point,
                input_model_entry_point,
                output_model_entry_point,
            )
        )
    ):
        raise ToolArtifactValidationError("Tool artifact identity is malformed")
    try:
        artifact_root = files("ai_server.tool_artifacts").joinpath(tool_id, version)
        schemas, schema_registry = _load_normative_schemas()
        contract_raw, contract_text = _load_json_document(artifact_root.joinpath("contract.json"))
        record_raw, record_text = _load_json_document(
            artifact_root.joinpath("registry-record.json")
        )
        manifest_raw, _ = _load_json_document(artifact_root.joinpath("implementation-bundle.json"))

        _validate_schema_document(
            schemas[TOOL_CONTRACT_SCHEMA_ID],
            contract_raw,
            schema_registry,
        )
        _validate_schema_document(
            schemas[TOOL_REGISTRY_RECORD_SCHEMA_ID],
            record_raw,
            schema_registry,
        )
        _validate_schema_document(
            schemas[TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID],
            manifest_raw,
            schema_registry,
        )
        contract = ToolContract.model_validate_json(contract_text, strict=True)
        record = ToolRegistryRecord.model_validate_json(record_text, strict=True)
        _validate_gateway_failure_contracts(contract)

        _validate_json_schema(contract.input_schema)
        _validate_json_schema(contract.output_schema)
        (
            implementation_hash,
            implementation_files,
            manifest_handler,
            manifest_input_model,
            manifest_output_model,
        ) = _verify_implementation_bundle(
            manifest_raw,
            artifact_root=artifact_root,
            tool_id=tool_id,
            version=version,
        )
        if (
            manifest_handler != handler_entry_point
            or manifest_input_model != input_model_entry_point
            or manifest_output_model != output_model_entry_point
        ):
            raise ToolArtifactValidationError(
                "Tool implementation entry points do not match reviewed artifacts"
            )
        contract_hash = canonical_json_sha256(cast(JsonValue, contract_raw))
        _validate_contract_record_binding(
            contract,
            record,
            tool_id=tool_id,
            version=version,
            contract_hash=contract_hash,
            implementation_hash=implementation_hash,
        )
        fixture_ids = _verify_replay_fixtures(
            contract,
            artifact_root=artifact_root,
            schemas=schemas,
            schema_registry=schema_registry,
            contract_hash=contract_hash,
        )
        metadata = _metadata_from_contract(
            contract,
            contract_hash=contract_hash,
            implementation_hash=implementation_hash,
            input_model=input_model_entry_point,
            output_model=output_model_entry_point,
        )
        return ValidatedToolArtifacts(
            contract=contract,
            record=record,
            metadata=metadata,
            contract_hash=contract_hash,
            implementation_hash=implementation_hash,
            implementation_files=implementation_files,
            handler_entry_point=manifest_handler,
            input_model_entry_point=manifest_input_model,
            output_model_entry_point=manifest_output_model,
            fixture_ids=fixture_ids,
        )
    except ToolArtifactValidationError:
        raise
    except (KeyError, TypeError, ValueError, ValidationError):
        raise ToolArtifactValidationError("Tool artifacts failed strict validation") from None
    except BaseException:
        raise ToolArtifactValidationError("Tool artifacts could not be loaded safely") from None


def result_matches_contract(
    document: dict[str, JsonValue],
    contract: ToolContract,
) -> bool:
    """Return whether a ToolResult matches global and exact Contract schemas."""
    try:
        schemas, registry = _load_normative_schemas()
        _validate_schema_document(
            schemas[TOOL_RESULT_SCHEMA_ID],
            cast(dict[str, Any], document),
            registry,
        )
        validator = Draft202012Validator(
            contract.output_schema,
            format_checker=FormatChecker(),
        )
        return next(validator.iter_errors(document), None) is None
    except BaseException:
        return False


def _load_normative_schemas() -> tuple[dict[str, dict[str, Any]], Registry[Any]]:
    schema_package = files("ai_server.schemas.tool")
    schemas: dict[str, dict[str, Any]] = {}
    for filename in NORMATIVE_TOOL_SCHEMA_FILES:
        raw, _ = _load_json_document(schema_package.joinpath(filename))
        Draft202012Validator.check_schema(raw)
        schema_id = raw.get("$id")
        if type(schema_id) is not str or schema_id in schemas:
            raise ToolArtifactValidationError("Normative Tool Schema identity is invalid")
        schemas[schema_id] = raw
    expected_ids = {
        TOOL_CONTRACT_SCHEMA_ID,
        TOOL_RESULT_SCHEMA_ID,
        TOOL_REPLAY_FIXTURE_SCHEMA_ID,
        TOOL_REGISTRY_RECORD_SCHEMA_ID,
        TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID,
    }
    if set(schemas) != expected_ids:
        raise ToolArtifactValidationError("Normative Tool Schema set is incomplete")
    resources = (
        (schema_id, Resource.from_contents(schema)) for schema_id, schema in schemas.items()
    )
    return schemas, Registry().with_resources(resources)


def _load_json_document(resource: Traversable) -> tuple[dict[str, Any], str]:
    if not resource.is_file():
        raise ToolArtifactValidationError("Required Tool artifact is missing")
    text = resource.read_text(encoding="utf-8")
    raw = json.loads(text)
    if type(raw) is not dict:
        raise ToolArtifactValidationError("Tool artifact must be a JSON object")
    return cast(dict[str, Any], raw), text


def _validate_schema_document(
    schema: dict[str, Any],
    value: dict[str, Any],
    registry: Registry[Any],
) -> None:
    validator = Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    )
    if next(validator.iter_errors(value), None) is not None:
        raise ToolArtifactValidationError("Tool artifact does not match its Schema")


def _validate_json_schema(schema: dict[str, JsonValue]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ToolArtifactValidationError("Tool input or output Schema is invalid") from None


def _verify_implementation_bundle(
    manifest: dict[str, Any],
    *,
    artifact_root: Traversable,
    tool_id: str,
    version: str,
) -> tuple[str, tuple[str, ...], str, str, str]:
    if (
        manifest.get("tool_id") != tool_id
        or manifest.get("version") != version
        or manifest.get("runtime_abi") != RUNTIME_ABI
    ):
        raise ToolArtifactValidationError("Tool implementation identity is invalid")

    dependency_lock = artifact_root.joinpath("dependency-lock.json")
    if not dependency_lock.is_file():
        raise ToolArtifactValidationError("Reviewed Tool dependency lock is missing")
    dependency_bytes = dependency_lock.read_bytes()
    expected_dependency_hash = f"sha256:{sha256(dependency_bytes).hexdigest()}"
    if manifest.get("dependency_lock_sha256") != expected_dependency_hash:
        raise ToolArtifactValidationError("Tool dependency lock integrity check failed")
    _validate_dependency_lock(dependency_bytes)

    file_entries = manifest.get("files")
    if type(file_entries) is not list:
        raise ToolArtifactValidationError("Tool implementation file manifest is malformed")
    raw_paths = tuple(entry.get("path") if type(entry) is dict else None for entry in file_entries)
    if any(type(path) is not str for path in raw_paths):
        raise ToolArtifactValidationError("Tool implementation path is malformed")
    implementation_files = cast(tuple[str, ...], raw_paths)
    if len(implementation_files) != len(set(implementation_files)) or implementation_files != tuple(
        sorted(implementation_files)
    ):
        raise ToolArtifactValidationError("Tool implementation paths must be unique and sorted")
    for entry in file_entries:
        _verify_package_file(cast(dict[str, Any], entry))
    handler_entry_point = cast(str, manifest["handler_entry_point"])
    input_model_entry_point = cast(str, manifest["input_model_entry_point"])
    output_model_entry_point = cast(str, manifest["output_model_entry_point"])
    entry_point_paths = (
        _entry_point_source_path(handler_entry_point),
        _entry_point_source_path(input_model_entry_point),
        _entry_point_source_path(output_model_entry_point),
    )
    if any(path is None or path not in implementation_files for path in entry_point_paths):
        raise ToolArtifactValidationError(
            "Tool implementation entry point is outside the reviewed manifest"
        )
    return (
        canonical_json_sha256(cast(JsonValue, manifest)),
        implementation_files,
        handler_entry_point,
        input_model_entry_point,
        output_model_entry_point,
    )


def _validate_dependency_lock(raw_bytes: bytes) -> None:
    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ToolArtifactValidationError("Reviewed Tool dependency lock is invalid") from None
    if (
        type(raw) is not dict
        or set(raw)
        != {
            "format",
            "source_lock",
            "requires_python",
            "roots",
            "packages",
        }
        or raw.get("format") != DEPENDENCY_LOCK_FORMAT
        or raw.get("requires_python") != ">=3.12"
        or type(raw.get("roots")) is not list
        or type(raw.get("packages")) is not list
    ):
        raise ToolArtifactValidationError("Reviewed Tool dependency lock is invalid")
    source_lock = raw.get("source_lock")
    if (
        type(source_lock) is not dict
        or set(source_lock) != {"format_version", "revision"}
        or type(source_lock.get("format_version")) is not int
        or source_lock.get("format_version", 0) < 1
        or type(source_lock.get("revision")) is not int
        or source_lock.get("revision", 0) < 1
    ):
        raise ToolArtifactValidationError("Reviewed Tool dependency lock is invalid")
    roots = raw["roots"]
    packages = raw["packages"]
    if (
        not roots
        or any(
            type(root) is not str or _PACKAGE_NAME_PATTERN.fullmatch(root) is None for root in roots
        )
        or roots != sorted(set(roots))
        or any(type(package) is not dict for package in packages)
    ):
        raise ToolArtifactValidationError("Reviewed Tool dependency lock is invalid")
    for package in packages:
        _validate_locked_package(cast(dict[str, object], package))
    identities = tuple((package.get("name"), package.get("version")) for package in packages)
    if (
        not identities
        or any(
            type(name) is not str
            or not name
            or type(package_version) is not str
            or not package_version
            for name, package_version in identities
        )
        or identities != tuple(sorted(set(identities)))
    ):
        raise ToolArtifactValidationError("Reviewed Tool dependency lock is invalid")
    package_names = {cast(str, name) for name, _ in identities}
    if not set(roots).issubset(package_names):
        raise ToolArtifactValidationError("Reviewed Tool dependency roots are incomplete")
    for package in packages:
        dependencies = cast(list[str], package["dependencies"])
        if not set(dependencies).issubset(package_names):
            raise ToolArtifactValidationError("Reviewed Tool dependency closure is incomplete")


def _validate_locked_package(package: dict[str, object]) -> None:
    if set(package) != {
        "name",
        "version",
        "source",
        "dependencies",
        "artifacts",
    }:
        raise ToolArtifactValidationError("Reviewed Tool dependency package is invalid")
    name = package.get("name")
    version = package.get("version")
    source = package.get("source")
    dependencies = package.get("dependencies")
    artifacts = package.get("artifacts")
    if (
        type(name) is not str
        or _PACKAGE_NAME_PATTERN.fullmatch(name) is None
        or type(version) is not str
        or not version
        or len(version) > 128
        or type(source) is not dict
        or source != {"registry": _TRUSTED_PACKAGE_REGISTRY}
        or type(dependencies) is not list
        or any(
            type(dependency) is not str or _PACKAGE_NAME_PATTERN.fullmatch(dependency) is None
            for dependency in dependencies
        )
        or dependencies != sorted(set(dependencies))
        or type(artifacts) is not list
        or not artifacts
        or any(type(artifact) is not dict for artifact in artifacts)
    ):
        raise ToolArtifactValidationError("Reviewed Tool dependency package is invalid")
    for artifact in artifacts:
        _validate_locked_artifact(cast(dict[str, object], artifact))
    filenames = tuple(cast(str, cast(dict[str, object], item)["filename"]) for item in artifacts)
    if filenames != tuple(sorted(set(filenames))):
        raise ToolArtifactValidationError("Reviewed Tool artifacts must be unique and sorted")


def _validate_locked_artifact(artifact: dict[str, object]) -> None:
    if set(artifact) != {"filename", "url", "sha256", "size_bytes"}:
        raise ToolArtifactValidationError("Reviewed Tool dependency artifact is invalid")
    filename = artifact.get("filename")
    url = artifact.get("url")
    digest = artifact.get("sha256")
    size_bytes = artifact.get("size_bytes")
    if (
        type(filename) is not str
        or not filename
        or "/" in filename
        or filename in {".", ".."}
        or type(url) is not str
        or not url.startswith(_TRUSTED_ARTIFACT_PREFIX)
        or url.rsplit("/", maxsplit=1)[-1] != filename
        or type(digest) is not str
        or _PREFIXED_HASH_PATTERN.fullmatch(digest) is None
        or type(size_bytes) is not int
        or size_bytes < 1
    ):
        raise ToolArtifactValidationError("Reviewed Tool dependency artifact is invalid")


def _verify_package_file(entry: dict[str, Any]) -> None:
    path = entry.get("path")
    if type(path) is not str:
        raise ToolArtifactValidationError("Tool implementation path is invalid")
    components = path.split("/")
    if (
        len(components) < 2
        or components[0] != "ai_server"
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ToolArtifactValidationError("Tool implementation path is invalid")
    resource = files("ai_server").joinpath(*components[1:])
    is_symlink = getattr(resource, "is_symlink", None)
    if callable(is_symlink) and is_symlink():
        raise ToolArtifactValidationError("Tool implementation symlinks are forbidden")
    if not resource.is_file():
        raise ToolArtifactValidationError("Tool implementation file is missing")
    raw_bytes = resource.read_bytes()
    if (
        entry.get("size_bytes") != len(raw_bytes)
        or entry.get("sha256") != f"sha256:{sha256(raw_bytes).hexdigest()}"
    ):
        raise ToolArtifactValidationError("Tool implementation file integrity failed")


def _entry_point_source_path(entry_point: str) -> str | None:
    module, separator, _ = entry_point.partition(":")
    if separator != ":" or not module.startswith("ai_server."):
        return None
    return f"{module.replace('.', '/')}.py"


def _validate_contract_record_binding(
    contract: ToolContract,
    record: ToolRegistryRecord,
    *,
    tool_id: str,
    version: str,
    contract_hash: str,
    implementation_hash: str,
) -> None:
    if (
        contract.tool_id != tool_id
        or contract.version != version
        or contract.implementation_hash != implementation_hash
        or record.tool_id != tool_id
        or record.version != version
        or record.contract_hash != contract_hash
        or record.implementation_hash != implementation_hash
        or record.status is not ToolRegistryStatus.REGISTERED
        or record.reviewer != "local-owner"
    ):
        raise ToolArtifactValidationError("Tool review bindings do not match artifacts")


def _validate_gateway_failure_contracts(contract: ToolContract) -> None:
    declared = {
        (definition.code, definition.category.value, definition.retryable)
        for definition in contract.errors
    }
    if not set(GATEWAY_FAILURE_CONTRACTS).issubset(declared):
        raise ToolArtifactValidationError("Tool Contract omits a required Gateway failure")


def _verify_replay_fixtures(
    contract: ToolContract,
    *,
    artifact_root: Traversable,
    schemas: dict[str, dict[str, Any]],
    schema_registry: Registry[Any],
    contract_hash: str,
) -> tuple[str, ...]:
    fixture_ids: list[str] = []
    sequence_positions: list[int] = []
    error_contracts_validated = False
    for reference in contract.replay_fixtures:
        raw, _ = _load_json_document(artifact_root.joinpath(reference.path))
        _validate_schema_document(
            schemas[TOOL_REPLAY_FIXTURE_SCHEMA_ID],
            raw,
            schema_registry,
        )
        fixture_id = raw.get("fixture_id")
        if fixture_id != reference.fixture_id or fixture_id in fixture_ids:
            raise ToolArtifactValidationError("Replay fixture identity is invalid")
        declared_content_hash = raw.get("content_hash")
        hash_input = dict(raw)
        hash_input.pop("content_hash", None)
        if declared_content_hash != canonical_json_sha256(cast(JsonValue, hash_input)):
            raise ToolArtifactValidationError("Replay fixture content integrity failed")
        _validate_replay_binding(
            raw,
            contract=contract,
            contract_hash=contract_hash,
            schemas=schemas,
            schema_registry=schema_registry,
        )
        if not error_contracts_validated:
            _validate_error_results_against_contract(
                raw,
                contract=contract,
                schemas=schemas,
                schema_registry=schema_registry,
            )
            error_contracts_validated = True
        if _contains_forbidden_content(raw):
            raise ToolArtifactValidationError("Replay fixture contains forbidden content")
        fixture_ids.append(cast(str, fixture_id))
        sequence_positions.append(cast(int, raw["sequence_position"]))
    if len(sequence_positions) != len(set(sequence_positions)) or sequence_positions != sorted(
        sequence_positions
    ):
        raise ToolArtifactValidationError(
            "Replay fixture sequence positions must be unique and ordered"
        )
    return tuple(fixture_ids)


def _validate_error_results_against_contract(
    fixture: dict[str, Any],
    *,
    contract: ToolContract,
    schemas: dict[str, dict[str, Any]],
    schema_registry: Registry[Any],
) -> None:
    result = fixture.get("result")
    if type(result) is not dict:
        raise ToolArtifactValidationError("Replay fixture result is malformed")
    output_validator = Draft202012Validator(
        contract.output_schema,
        format_checker=FormatChecker(),
    )
    for definition in contract.errors:
        failure_result = {
            **result,
            "success": False,
            "data": None,
            "evidence": {},
            "error": definition.model_dump(mode="json", warnings="error"),
            "duration_ms": 0,
        }
        _validate_schema_document(
            schemas[TOOL_RESULT_SCHEMA_ID],
            failure_result,
            schema_registry,
        )
        if next(output_validator.iter_errors(failure_result), None) is not None:
            raise ToolArtifactValidationError("Tool output Schema rejects a declared error")


def _validate_replay_binding(
    fixture: dict[str, Any],
    *,
    contract: ToolContract,
    contract_hash: str,
    schemas: dict[str, dict[str, Any]],
    schema_registry: Registry[Any],
) -> None:
    arguments = fixture.get("input")
    result = fixture.get("result")
    if type(arguments) is not dict or type(result) is not dict:
        raise ToolArtifactValidationError("Replay fixture invocation is malformed")
    if (
        fixture.get("tool_id") != contract.tool_id
        or fixture.get("version") != contract.version
        or fixture.get("arguments_hash") != canonical_json_sha256(cast(JsonValue, arguments))
        or result.get("tool_id") != contract.tool_id
        or result.get("tool_version") != contract.version
        or result.get("contract_hash") != contract_hash
        or result.get("arguments_hash") != fixture.get("arguments_hash")
        or result.get("duration_ms", contract.timeout_ms + 1) > contract.timeout_ms
    ):
        raise ToolArtifactValidationError("Replay fixture bindings do not match Tool")
    target = result.get("target")
    selector = arguments.get(contract.target_scope.selector_field)
    if (
        type(target) is not dict
        or target.get("resource_type") != contract.target_scope.resource_type
        or target.get("target_id") != selector
        or target.get("resource_id") != selector
    ):
        raise ToolArtifactValidationError("Replay fixture target exceeds Tool scope")
    success = result.get("success")
    error = result.get("error")
    if fixture.get("expected_outcome") != (
        "success" if success is True else "failure"
    ) or fixture.get("expected_error_code") != (
        None if success is True else error.get("code") if type(error) is dict else None
    ):
        raise ToolArtifactValidationError("Replay fixture expected outcome is inconsistent")
    if success is False:
        if type(error) is not dict:
            raise ToolArtifactValidationError("Replay fixture error is not declared")
        declared_errors = {
            (definition.code, definition.category.value, definition.retryable)
            for definition in contract.errors
        }
        error_binding = (
            error.get("code"),
            error.get("category"),
            error.get("retryable"),
        )
        if error_binding not in declared_errors:
            raise ToolArtifactValidationError("Replay fixture error is not declared")
    redaction = fixture.get("redaction")
    if (
        type(redaction) is not dict
        or redaction.get("profile_version") != contract.redaction.profile_version
        or redaction.get("sanitized") is not True
    ):
        raise ToolArtifactValidationError("Replay fixture redaction evidence is invalid")
    verification = fixture.get("verification_result")
    if contract.verification.required and (
        type(verification) is not dict or verification.get("success") is not True
    ):
        raise ToolArtifactValidationError("Replay fixture verification evidence is incomplete")

    _validate_schema_document(
        schemas[TOOL_RESULT_SCHEMA_ID],
        result,
        schema_registry,
    )
    input_validator = Draft202012Validator(
        contract.input_schema,
        format_checker=FormatChecker(),
    )
    if next(input_validator.iter_errors(arguments), None) is not None:
        raise ToolArtifactValidationError("Replay fixture input is invalid")
    output_validator = Draft202012Validator(
        contract.output_schema,
        format_checker=FormatChecker(),
    )
    if next(output_validator.iter_errors(result), None) is not None:
        raise ToolArtifactValidationError("Replay fixture output is invalid")


def _metadata_from_contract(
    contract: ToolContract,
    *,
    contract_hash: str,
    implementation_hash: str,
    input_model: str,
    output_model: str,
) -> ToolMetadata:
    input_schema_id = contract.input_schema.get("$id")
    output_schema_id = contract.output_schema.get("$id")
    if type(input_schema_id) is not str or type(output_schema_id) is not str:
        raise ToolArtifactValidationError("Tool input or output Schema ID is missing")
    return ToolMetadata(
        tool_id=contract.tool_id,
        version=contract.version,
        contract_hash=contract_hash,
        implementation_hash=implementation_hash,
        description=contract.description,
        risk_level=contract.risk_level,
        side_effects=contract.side_effects,
        target_scope=contract.target_scope,
        redaction=contract.redaction,
        verification=contract.verification,
        rollback=contract.rollback,
        timeout_ms=contract.timeout_ms,
        idempotent=contract.idempotent,
        input_schema_id=input_schema_id,
        output_schema_id=output_schema_id,
        input_model=input_model,
        output_model=output_model,
    )


def _contains_forbidden_content(value: object) -> bool:
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        return any(
            (type(key) is str and key.lower() in FORBIDDEN_DATA_KEYS)
            or _contains_forbidden_content(item)
            for key, item in mapping.items()
        )
    if type(value) is list:
        return any(_contains_forbidden_content(item) for item in cast(list[object], value))
    if type(value) is str:
        normalized = value.lower()
        return any(marker in normalized for marker in FORBIDDEN_VALUE_MARKERS)
    return False


__all__ = [
    "DEPENDENCY_LOCK_FORMAT",
    "RUNTIME_ABI",
    "ToolArtifactValidationError",
    "ValidatedToolArtifacts",
    "load_tool_artifacts",
    "result_matches_contract",
]
