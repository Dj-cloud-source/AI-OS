from functools import wraps
from typing import cast

import pytest

import ai_server.tools.registry as registry_module
from ai_server.models.system_status import GetSystemStatusArguments, SystemStatus
from ai_server.models.tool import ToolMetadata, ToolRegistryRecord, ToolRegistryStatus
from ai_server.tools.artifact_loader import (
    ToolArtifactValidationError,
    ValidatedToolArtifacts,
    load_tool_artifacts,
)
from ai_server.tools.bootstrap import (
    GET_SYSTEM_STATUS_TOOL_ID,
    GET_SYSTEM_STATUS_TOOL_VERSION,
    build_default_registry,
)
from ai_server.tools.get_system_status import GetSystemStatusTool
from ai_server.tools.protocol import ToolHandler
from ai_server.tools.registry import (
    DuplicateToolRegistrationError,
    InvalidToolDefinitionError,
    ToolDefinition,
    ToolKey,
    ToolRegistry,
    ToolRegistryFrozenError,
    ToolRegistryNotFrozenError,
)

HANDLER_ENTRY_POINT = "ai_server.tools.get_system_status:GetSystemStatusTool.invoke"
INPUT_MODEL_ENTRY_POINT = "ai_server.models.system_status:GetSystemStatusArguments"
OUTPUT_MODEL_ENTRY_POINT = "ai_server.models.system_status:SystemStatus"


def make_definition() -> ToolDefinition[GetSystemStatusArguments, SystemStatus]:
    tool = GetSystemStatusTool()
    return ToolDefinition(
        tool_id=GET_SYSTEM_STATUS_TOOL_ID,
        version=GET_SYSTEM_STATUS_TOOL_VERSION,
        input_model=GetSystemStatusArguments,
        output_model=SystemStatus,
        handler=tool.invoke,
    )


def load_real_artifacts() -> ValidatedToolArtifacts:
    return load_tool_artifacts(
        GET_SYSTEM_STATUS_TOOL_ID,
        GET_SYSTEM_STATUS_TOOL_VERSION,
        handler_entry_point=HANDLER_ENTRY_POINT,
        input_model_entry_point=INPUT_MODEL_ENTRY_POINT,
        output_model_entry_point=OUTPUT_MODEL_ENTRY_POINT,
    )


def test_registry_requires_freeze_before_read_only_access() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolRegistryNotFrozenError):
        registry.metadata_snapshot()
    with pytest.raises(ToolRegistryNotFrozenError):
        registry.record_snapshot()

    registry.freeze()

    assert registry.is_frozen is True
    assert registry.metadata_snapshot() == {}
    assert registry.record_snapshot() == {}
    with pytest.raises(ToolRegistryFrozenError, match="after freeze"):
        registry.register(make_definition())


def test_real_artifacts_register_exact_identity_and_hashes_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[GetSystemStatusArguments] = []
    original = GetSystemStatusTool.invoke

    @wraps(original)
    def recording_invoke(
        self: GetSystemStatusTool,
        arguments: GetSystemStatusArguments,
    ) -> SystemStatus:
        calls.append(arguments)
        return original(self, arguments)

    monkeypatch.setattr(GetSystemStatusTool, "invoke", recording_invoke)
    expected = load_real_artifacts()

    registry = build_default_registry()
    key: ToolKey = (
        GET_SYSTEM_STATUS_TOOL_ID,
        GET_SYSTEM_STATUS_TOOL_VERSION,
    )
    metadata = registry.metadata_snapshot()[key]
    record = registry.record_snapshot()[key]

    assert calls == []
    assert metadata == expected.metadata
    assert record == expected.record
    assert metadata.tool_id == GET_SYSTEM_STATUS_TOOL_ID
    assert metadata.version == GET_SYSTEM_STATUS_TOOL_VERSION
    assert metadata.contract_hash == expected.contract_hash
    assert metadata.implementation_hash == expected.implementation_hash
    assert record.contract_hash == metadata.contract_hash
    assert record.implementation_hash == metadata.implementation_hash
    assert record.status is ToolRegistryStatus.REGISTERED


@pytest.mark.parametrize(
    ("tool_id", "version"),
    [
        ("unknown_tool", GET_SYSTEM_STATUS_TOOL_VERSION),
        (GET_SYSTEM_STATUS_TOOL_ID, "1.0.1"),
    ],
)
def test_registry_rejects_unknown_exact_artifact_identity(
    tool_id: str,
    version: str,
) -> None:
    definition = make_definition()
    changed = ToolDefinition(
        tool_id=tool_id,
        version=version,
        input_model=definition.input_model,
        output_model=definition.output_model,
        handler=definition.handler,
    )

    with pytest.raises(ToolArtifactValidationError):
        ToolRegistry().register(changed)


def test_registry_rejects_duplicate_exact_registration() -> None:
    registry = ToolRegistry()
    definition = make_definition()
    registry.register(definition)

    with pytest.raises(DuplicateToolRegistrationError, match="already registered"):
        registry.register(definition)


def test_registry_rejects_handler_without_reviewed_entry_point() -> None:
    def unreviewed_handler(arguments: GetSystemStatusArguments) -> SystemStatus:
        return GetSystemStatusTool().invoke(arguments)

    definition = ToolDefinition(
        tool_id=GET_SYSTEM_STATUS_TOOL_ID,
        version=GET_SYSTEM_STATUS_TOOL_VERSION,
        input_model=GetSystemStatusArguments,
        output_model=SystemStatus,
        handler=unreviewed_handler,
    )

    with pytest.raises(InvalidToolDefinitionError, match="entry points are malformed"):
        ToolRegistry().register(definition)


def test_registry_rejects_non_exact_handler_entry_point() -> None:
    def other_handler(arguments: GetSystemStatusArguments) -> SystemStatus:
        return GetSystemStatusTool().invoke(arguments)

    other_handler.__module__ = "ai_server.tools.get_system_status"
    other_handler.__qualname__ = "GetSystemStatusTool.other_handler"
    definition = ToolDefinition(
        tool_id=GET_SYSTEM_STATUS_TOOL_ID,
        version=GET_SYSTEM_STATUS_TOOL_VERSION,
        input_model=GetSystemStatusArguments,
        output_model=SystemStatus,
        handler=other_handler,
    )

    with pytest.raises(ToolArtifactValidationError, match="entry points do not match"):
        ToolRegistry().register(definition)


def test_registry_rejects_spoofed_exact_handler_entry_point() -> None:
    def spoofed_handler(arguments: GetSystemStatusArguments) -> SystemStatus:
        return GetSystemStatusTool().invoke(arguments).model_copy(update={"cpu_percent": 99.0})

    spoofed_handler.__module__ = "ai_server.tools.get_system_status"
    spoofed_handler.__qualname__ = "GetSystemStatusTool.invoke"
    definition = ToolDefinition(
        tool_id=GET_SYSTEM_STATUS_TOOL_ID,
        version=GET_SYSTEM_STATUS_TOOL_VERSION,
        input_model=GetSystemStatusArguments,
        output_model=SystemStatus,
        handler=spoofed_handler,
    )

    with pytest.raises(InvalidToolDefinitionError, match="reviewed entry points"):
        ToolRegistry().register(definition)


def test_registry_rejects_unbound_reviewed_instance_method() -> None:
    definition = ToolDefinition(
        tool_id=GET_SYSTEM_STATUS_TOOL_ID,
        version=GET_SYSTEM_STATUS_TOOL_VERSION,
        input_model=GetSystemStatusArguments,
        output_model=SystemStatus,
        handler=cast(
            ToolHandler[GetSystemStatusArguments, SystemStatus],
            GetSystemStatusTool.invoke,
        ),
    )

    with pytest.raises(InvalidToolDefinitionError, match="reviewed entry points"):
        ToolRegistry().register(definition)


def test_registry_rejects_reviewed_method_bound_to_subclass_instance() -> None:
    class SubclassedTool(GetSystemStatusTool):
        pass

    definition = ToolDefinition(
        tool_id=GET_SYSTEM_STATUS_TOOL_ID,
        version=GET_SYSTEM_STATUS_TOOL_VERSION,
        input_model=GetSystemStatusArguments,
        output_model=SystemStatus,
        handler=SubclassedTool().invoke,
    )

    with pytest.raises(InvalidToolDefinitionError, match="reviewed entry points"):
        ToolRegistry().register(definition)


def test_registry_rejects_model_with_spoofed_reviewed_entry_point() -> None:
    class SpoofedArguments(GetSystemStatusArguments):
        pass

    SpoofedArguments.__module__ = "ai_server.models.system_status"
    SpoofedArguments.__qualname__ = "GetSystemStatusArguments"
    definition = ToolDefinition(
        tool_id=GET_SYSTEM_STATUS_TOOL_ID,
        version=GET_SYSTEM_STATUS_TOOL_VERSION,
        input_model=SpoofedArguments,
        output_model=SystemStatus,
        handler=GetSystemStatusTool().invoke,
    )

    with pytest.raises(InvalidToolDefinitionError, match="reviewed entry points"):
        ToolRegistry().register(definition)


def test_registry_rejects_malformed_definition_explicitly() -> None:
    malformed = cast(
        ToolDefinition[GetSystemStatusArguments, SystemStatus],
        object(),
    )

    with pytest.raises(InvalidToolDefinitionError, match="malformed"):
        ToolRegistry().register(malformed)


def test_registry_sanitizes_unexpected_artifact_loader_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_ARTIFACT_LOADER_MARKER"

    def exploding_loader(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(registry_module, "load_tool_artifacts", exploding_loader)

    with pytest.raises(InvalidToolDefinitionError) as caught:
        ToolRegistry().register(make_definition())

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_registry_snapshots_are_immutable() -> None:
    registry = build_default_registry()
    key: ToolKey = (
        GET_SYSTEM_STATUS_TOOL_ID,
        GET_SYSTEM_STATUS_TOOL_VERSION,
    )
    metadata_snapshot = registry.metadata_snapshot()
    record_snapshot = registry.record_snapshot()

    with pytest.raises(TypeError):
        cast(dict[ToolKey, ToolMetadata], metadata_snapshot)[key] = metadata_snapshot[key]
    with pytest.raises(TypeError):
        cast(dict[ToolKey, ToolRegistryRecord], record_snapshot)[key] = record_snapshot[key]
