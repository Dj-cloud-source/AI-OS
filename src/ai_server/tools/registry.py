"""Explicit, immutable-after-startup registry for local Tool definitions."""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from types import MappingProxyType
from typing import ClassVar

from pydantic import BaseModel

from ai_server.models.tool import (
    ToolContract,
    ToolMetadata,
    ToolRegistryRecord,
)
from ai_server.tools.artifact_loader import (
    ToolArtifactValidationError,
    load_tool_artifacts,
)
from ai_server.tools.protocol import ToolHandler

ToolKey = tuple[str, str]


class ToolRegistryError(Exception):
    """Base class for deterministic Tool Registry failures."""

    code: ClassVar[str] = "tool_registry_error"


class InvalidToolDefinitionError(ToolRegistryError):
    """Raised when a registration does not satisfy its declared contract."""

    code: ClassVar[str] = "invalid_tool_definition"


class DuplicateToolRegistrationError(ToolRegistryError):
    """Raised when the same immutable Tool identity is registered twice."""

    code: ClassVar[str] = "duplicate_tool_registration"


class ToolRegistryFrozenError(ToolRegistryError):
    """Raised when registration is attempted after the Registry is frozen."""

    code: ClassVar[str] = "tool_registry_frozen"


class ToolRegistryNotFrozenError(ToolRegistryError):
    """Raised when resolution is attempted before registration is complete."""

    code: ClassVar[str] = "tool_registry_not_frozen"


class UnknownToolError(ToolRegistryError):
    """Raised when an exact registered Tool identity cannot be resolved."""

    code: ClassVar[str] = "unknown_tool"


@dataclass(frozen=True, slots=True)
class ToolDefinition[ArgumentsT: BaseModel, PayloadT: BaseModel]:
    """Identify one packaged Tool and bind its typed payload implementation."""

    tool_id: str
    version: str
    input_model: type[ArgumentsT]
    output_model: type[PayloadT]
    handler: ToolHandler[ArgumentsT, PayloadT]


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    """Private validated capability available only to Tool Gateway."""

    metadata: ToolMetadata
    record: ToolRegistryRecord
    contract: ToolContract
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    _handler: ToolHandler[BaseModel, BaseModel]

    def _invoke_payload(self, arguments: BaseModel) -> BaseModel:
        return self._handler(arguments)


class ToolRegistry:
    """Register exact local Tool versions explicitly, then freeze for use."""

    def __init__(self) -> None:
        """Create an empty mutable startup Registry."""
        self._entries: dict[ToolKey, _RegisteredTool] = {}
        self._frozen = False

    @property
    def is_frozen(self) -> bool:
        """Return whether startup registration has been sealed."""
        return self._frozen

    def register[ArgumentsT: BaseModel, PayloadT: BaseModel](
        self,
        definition: ToolDefinition[ArgumentsT, PayloadT],
    ) -> None:
        """Verify packaged evidence and explicitly register one exact Tool identity."""
        if self._frozen:
            raise ToolRegistryFrozenError("Tool Registry does not accept registration after freeze")
        if type(definition) is not ToolDefinition:
            raise InvalidToolDefinitionError("Tool definition is malformed")

        _validate_implementation_binding(
            definition.input_model,
            definition.output_model,
            definition.handler,
        )
        handler_entry_point = _qualified_entry_point(definition.handler)
        input_model_entry_point = _qualified_entry_point(definition.input_model)
        output_model_entry_point = _qualified_entry_point(definition.output_model)
        if (
            handler_entry_point is None
            or input_model_entry_point is None
            or output_model_entry_point is None
        ):
            raise InvalidToolDefinitionError("Tool implementation entry points are malformed")
        try:
            artifacts = load_tool_artifacts(
                definition.tool_id,
                definition.version,
                handler_entry_point=handler_entry_point,
                input_model_entry_point=input_model_entry_point,
                output_model_entry_point=output_model_entry_point,
            )
        except ToolArtifactValidationError:
            raise
        except BaseException:
            raise InvalidToolDefinitionError(
                "Tool registration artifacts failed validation"
            ) from None
        metadata = _validate_metadata(artifacts.metadata)
        validated_record = _validate_record(artifacts.record)
        _validate_registration_binding(
            metadata,
            validated_record,
            artifacts.contract,
            definition.input_model,
            definition.output_model,
            definition.handler,
            reviewed_handler_entry_point=artifacts.handler_entry_point,
            reviewed_input_model_entry_point=artifacts.input_model_entry_point,
            reviewed_output_model_entry_point=artifacts.output_model_entry_point,
            implementation_files=artifacts.implementation_files,
        )

        key = (metadata.tool_id, metadata.version)
        if key in self._entries:
            raise DuplicateToolRegistrationError("Tool identity is already registered")

        def erased_handler(arguments: BaseModel) -> BaseModel:
            if type(arguments) is not definition.input_model:
                raise TypeError("Registered Tool received the wrong argument model")
            return definition.handler(arguments)

        self._entries[key] = _RegisteredTool(
            metadata=metadata,
            record=validated_record,
            contract=artifacts.contract,
            input_model=definition.input_model,
            output_model=definition.output_model,
            _handler=erased_handler,
        )

    def freeze(self) -> None:
        """Seal startup registration so exact resolution becomes available."""
        self._frozen = True

    def _resolve(self, tool_id: str, version: str) -> _RegisteredTool:
        if not self._frozen:
            raise ToolRegistryNotFrozenError("Tool Registry must be frozen before resolution")
        try:
            return self._entries[(tool_id, version)]
        except (KeyError, TypeError):
            raise UnknownToolError("Requested Tool identity is not registered") from None

    def metadata_snapshot(self) -> Mapping[ToolKey, ToolMetadata]:
        """Return an immutable authoritative metadata view for Policy."""
        if not self._frozen:
            raise ToolRegistryNotFrozenError("Tool Registry must be frozen before metadata access")
        return MappingProxyType({key: entry.metadata for key, entry in self._entries.items()})

    def record_snapshot(self) -> Mapping[ToolKey, ToolRegistryRecord]:
        """Return an immutable Registry Record view for audit and diagnostics."""
        if not self._frozen:
            raise ToolRegistryNotFrozenError("Tool Registry must be frozen before record access")
        return MappingProxyType({key: entry.record for key, entry in self._entries.items()})


def _validate_metadata(metadata: ToolMetadata) -> ToolMetadata:
    if type(metadata) is not ToolMetadata:
        raise InvalidToolDefinitionError("Tool metadata is malformed")
    try:
        return ToolMetadata.model_validate(
            metadata.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise InvalidToolDefinitionError("Tool metadata is malformed") from None


def _validate_record(record: ToolRegistryRecord) -> ToolRegistryRecord:
    if type(record) is not ToolRegistryRecord:
        raise InvalidToolDefinitionError("Tool Registry Record is malformed")
    try:
        return ToolRegistryRecord.model_validate(
            record.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise InvalidToolDefinitionError("Tool Registry Record is malformed") from None


def _validate_implementation_binding[ArgumentsT: BaseModel, PayloadT: BaseModel](
    input_model: type[ArgumentsT],
    output_model: type[PayloadT],
    handler: ToolHandler[ArgumentsT, PayloadT],
) -> None:
    try:
        models_are_valid = (
            isinstance(input_model, type)
            and issubclass(input_model, BaseModel)
            and isinstance(output_model, type)
            and issubclass(output_model, BaseModel)
        )
    except TypeError:
        models_are_valid = False

    if not models_are_valid or not callable(handler):
        raise InvalidToolDefinitionError("Tool implementation binding is malformed")


def _validate_registration_binding[ArgumentsT: BaseModel, PayloadT: BaseModel](
    metadata: ToolMetadata,
    record: ToolRegistryRecord,
    contract: ToolContract,
    input_model: type[ArgumentsT],
    output_model: type[PayloadT],
    handler: ToolHandler[ArgumentsT, PayloadT],
    *,
    reviewed_handler_entry_point: str,
    reviewed_input_model_entry_point: str,
    reviewed_output_model_entry_point: str,
    implementation_files: tuple[str, ...],
) -> None:
    if (
        _resolve_reviewed_entry_point(reviewed_input_model_entry_point) is not input_model
        or _resolve_reviewed_entry_point(reviewed_output_model_entry_point) is not output_model
        or not _handler_matches_reviewed_entry_point(
            handler,
            reviewed_handler_entry_point,
        )
    ):
        raise InvalidToolDefinitionError(
            "Tool implementation objects do not match reviewed entry points"
        )
    if metadata.input_model != _qualified_entry_point(
        input_model
    ) or metadata.output_model != _qualified_entry_point(output_model):
        raise InvalidToolDefinitionError("Tool model binding does not match immutable metadata")
    if (
        record.tool_id != metadata.tool_id
        or record.version != metadata.version
        or record.contract_hash != metadata.contract_hash
        or record.implementation_hash != metadata.implementation_hash
        or contract.tool_id != metadata.tool_id
        or contract.version != metadata.version
        or contract.implementation_hash != metadata.implementation_hash
        or contract.risk_level is not metadata.risk_level
        or contract.side_effects != metadata.side_effects
        or contract.target_scope != metadata.target_scope
        or contract.timeout_ms != metadata.timeout_ms
        or contract.idempotent is not metadata.idempotent
    ):
        raise InvalidToolDefinitionError(
            "Tool Contract or Registry Record does not match immutable metadata"
        )
    bound_entry_points = (
        _qualified_entry_point(input_model),
        _qualified_entry_point(output_model),
        _qualified_entry_point(handler),
    )
    bound_modules = tuple(
        _entry_point_source_path(entry_point) if entry_point is not None else None
        for entry_point in bound_entry_points
    )
    if any(path is None or path not in implementation_files for path in bound_modules):
        raise InvalidToolDefinitionError(
            "Tool implementation binding is outside the reviewed manifest"
        )


def _qualified_entry_point(value: object) -> str | None:
    function = getattr(value, "__func__", value)
    module = getattr(function, "__module__", None)
    qualified_name = getattr(function, "__qualname__", None)
    if (
        type(module) is not str
        or not module.startswith("ai_server.")
        or type(qualified_name) is not str
        or "<locals>" in qualified_name
    ):
        return None
    return f"{module}:{qualified_name}"


def _resolve_reviewed_entry_point(entry_point: str) -> object | None:
    module_name, separator, qualified_name = entry_point.partition(":")
    components = qualified_name.split(".")
    if (
        separator != ":"
        or not module_name.startswith("ai_server.")
        or not components
        or any(not component.isidentifier() for component in components)
    ):
        return None
    try:
        resolved: object = import_module(module_name)
        for component in components:
            resolved = getattr(resolved, component)
        return resolved
    except BaseException:
        return None


def _handler_matches_reviewed_entry_point(
    handler: object,
    entry_point: str,
) -> bool:
    reviewed_handler = _resolve_reviewed_entry_point(entry_point)
    concrete_handler = getattr(handler, "__func__", handler)
    if reviewed_handler is None or concrete_handler is not reviewed_handler:
        return False

    bound_instance = getattr(handler, "__self__", None)
    module_name, _, qualified_name = entry_point.partition(":")
    owner_name, owner_separator, _ = qualified_name.rpartition(".")
    if owner_separator != ".":
        return bound_instance is None
    reviewed_owner = _resolve_reviewed_entry_point(f"{module_name}:{owner_name}")
    if isinstance(reviewed_owner, type):
        return bound_instance is not None and type(bound_instance) is reviewed_owner
    return bound_instance is None


def _entry_point_source_path(entry_point: str) -> str | None:
    module, separator, _ = entry_point.partition(":")
    if separator != ":" or not module.startswith("ai_server."):
        return None
    return f"{module.replace('.', '/')}.py"


__all__ = [
    "DuplicateToolRegistrationError",
    "InvalidToolDefinitionError",
    "ToolDefinition",
    "ToolKey",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolRegistryFrozenError",
    "ToolRegistryNotFrozenError",
    "UnknownToolError",
]
