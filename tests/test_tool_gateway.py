from collections.abc import Callable, Iterator
from functools import wraps
from typing import Any, Literal, cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

import ai_server.tools.gateway as gateway_module
from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.tool import (
    TargetReference,
    ToolCall,
    ToolErrorCategory,
    ToolMetadata,
    ToolResult,
)
from ai_server.tools.artifact_loader import (
    ValidatedToolArtifacts,
    load_tool_artifacts,
    result_matches_contract,
)
from ai_server.tools.bootstrap import (
    GET_SYSTEM_STATUS_TOOL_ID,
    GET_SYSTEM_STATUS_TOOL_VERSION,
    build_default_registry,
)
from ai_server.tools.gateway import (
    InvalidGatewayConfigurationError,
    InvalidToolCallError,
    ToolGateway,
    ToolIntegrityError,
    ToolResolutionError,
)
from ai_server.tools.get_system_status import GetSystemStatusTool
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.tools.registry import ToolKey, ToolRegistry

INVOCATION_ID = UUID("00000000-0000-4000-8000-000000000001")
TOOL_KEY: ToolKey = (
    GET_SYSTEM_STATUS_TOOL_ID,
    GET_SYSTEM_STATUS_TOOL_VERSION,
)
TARGET = TargetReference(
    target_id="local-mock",
    resource_type="local_system",
    resource_id="local-mock",
)
PayloadBehavior = Callable[[GetSystemStatusArguments], SystemStatus]
HANDLER_ENTRY_POINT = "ai_server.tools.get_system_status:GetSystemStatusTool.invoke"
INPUT_MODEL_ENTRY_POINT = "ai_server.models.system_status:GetSystemStatusArguments"
OUTPUT_MODEL_ENTRY_POINT = "ai_server.models.system_status:SystemStatus"


class WrongArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target: Literal["local-mock"] = "local-mock"


def sequence_clock(*values: int) -> Callable[[], int]:
    iterator = iter(values)
    return lambda: next(iterator)


def make_system_status(
    arguments: GetSystemStatusArguments,
    *,
    services: tuple[ServiceStatus, ...] = (ServiceStatus(name="mock-api", state="running"),),
) -> SystemStatus:
    return SystemStatus(
        target=arguments.target,
        cpu_percent=12.5,
        memory_percent=34.0,
        disk_percent=45.5,
        services=services,
    )


def install_recording_tool(
    monkeypatch: pytest.MonkeyPatch,
    *,
    behavior: PayloadBehavior | None = None,
) -> list[GetSystemStatusArguments]:
    calls: list[GetSystemStatusArguments] = []
    original = GetSystemStatusTool.invoke

    @wraps(original)
    def recording_invoke(
        self: GetSystemStatusTool,
        arguments: GetSystemStatusArguments,
    ) -> SystemStatus:
        calls.append(arguments)
        if behavior is not None:
            return behavior(arguments)
        return original(self, arguments)

    monkeypatch.setattr(GetSystemStatusTool, "invoke", recording_invoke)
    return calls


def metadata_from(registry: ToolRegistry) -> ToolMetadata:
    return registry.metadata_snapshot()[TOOL_KEY]


def load_real_artifacts() -> ValidatedToolArtifacts:
    return load_tool_artifacts(
        GET_SYSTEM_STATUS_TOOL_ID,
        GET_SYSTEM_STATUS_TOOL_VERSION,
        handler_entry_point=HANDLER_ENTRY_POINT,
        input_model_entry_point=INPUT_MODEL_ENTRY_POINT,
        output_model_entry_point=OUTPUT_MODEL_ENTRY_POINT,
    )


def make_call(
    metadata: ToolMetadata,
    *,
    arguments: GetSystemStatusArguments | None = None,
    target: TargetReference = TARGET,
) -> ToolCall[GetSystemStatusArguments]:
    trusted_arguments = arguments or GetSystemStatusArguments()
    return ToolCall[GetSystemStatusArguments](
        invocation_id=INVOCATION_ID,
        plan_step_id="observe-status",
        tool_id=metadata.tool_id,
        tool_version=metadata.version,
        contract_hash=metadata.contract_hash,
        implementation_hash=metadata.implementation_hash,
        arguments_hash=canonical_json_sha256(trusted_arguments),
        target=target,
        arguments=trusted_arguments,
    )


def assert_failure(
    result: ToolResult[BaseModel],
    *,
    code: str,
    category: ToolErrorCategory,
) -> None:
    assert result.success is False
    assert result.data is None
    assert result.error is not None
    assert result.error.code == code
    assert result.error.category is category
    assert result.error.retryable is False
    artifacts = load_real_artifacts()
    result_document = cast(
        dict[str, JsonValue],
        result.model_dump(mode="json", warnings="error"),
    )
    assert result_matches_contract(result_document, artifacts.contract)


def test_gateway_requires_an_exact_frozen_registry() -> None:
    with pytest.raises(InvalidGatewayConfigurationError, match="frozen"):
        ToolGateway(ToolRegistry())


def test_real_tool_success_binds_exact_identity_hashes_and_result_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    metadata = metadata_from(registry)
    call = make_call(metadata)
    artifacts = load_real_artifacts()

    result = ToolGateway(
        registry,
        clock=sequence_clock(1_000_000, 2_000_000),
    ).invoke(call)

    assert calls == [GetSystemStatusArguments()]
    assert result.invocation_id == call.invocation_id
    assert result.plan_step_id == call.plan_step_id
    assert result.tool_id == metadata.tool_id
    assert result.tool_version == metadata.version
    assert result.contract_hash == metadata.contract_hash
    assert result.arguments_hash == call.arguments_hash
    assert result.target == TARGET
    assert result.success is True
    assert result.error is None
    assert type(result.data) is SystemStatus
    assert result.data.source == "mock"
    assert result.data.simulated is True
    assert result.duration_ms == 1
    payload_document = result.data.model_dump(mode="json", warnings="error")
    expected_evidence = {
        field: payload_document[field]
        for field in artifacts.contract.redaction.safe_evidence_fields
        if field in payload_document
    }
    assert result.evidence == expected_evidence
    assert set(result.evidence).isdisjoint(artifacts.contract.redaction.output_fields)
    result_document = cast(
        dict[str, JsonValue],
        result.model_dump(mode="json", warnings="error"),
    )
    assert result_matches_contract(result_document, artifacts.contract)


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("tool_id", "unknown_tool"),
        ("tool_version", "1.0.1"),
    ],
)
def test_gateway_rejects_unknown_exact_identity_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
    changed_value: str,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    call = make_call(metadata_from(registry)).model_copy(update={changed_field: changed_value})

    with pytest.raises(ToolResolutionError, match="not registered"):
        ToolGateway(registry).invoke(call)

    assert calls == []


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("contract_hash", "c" * 64),
        ("implementation_hash", "d" * 64),
    ],
)
def test_gateway_rejects_contract_or_implementation_hash_drift_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
    changed_value: str,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    call = make_call(metadata_from(registry)).model_copy(update={changed_field: changed_value})

    with pytest.raises(ToolIntegrityError, match="integrity"):
        ToolGateway(registry).invoke(call)

    assert calls == []


def test_gateway_returns_structured_arguments_hash_failure_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    call = make_call(metadata_from(registry)).model_copy(update={"arguments_hash": "e" * 64})

    result = ToolGateway(registry).invoke(call)

    assert calls == []
    assert_failure(
        result,
        code="arguments_hash_mismatch",
        category=ToolErrorCategory.INTEGRITY,
    )
    artifacts = load_real_artifacts()
    result_document = cast(
        dict[str, JsonValue],
        result.model_dump(mode="json", warnings="error"),
    )
    assert result_matches_contract(result_document, artifacts.contract)


def test_real_registered_input_schema_is_strict_and_target_bounded() -> None:
    artifacts = load_real_artifacts()
    gateway_exports = vars(gateway_module)
    validator_type = cast(
        Any,
        gateway_exports["Draft202012Validator"],
    )
    format_checker_type = cast(
        Any,
        gateway_exports["FormatChecker"],
    )
    validator = validator_type(
        artifacts.contract.input_schema,
        format_checker=format_checker_type(),
    )

    assert list(validator.iter_errors({"target": "local-mock"})) == []
    assert list(validator.iter_errors({"target": "other-mock"}))
    assert list(
        validator.iter_errors(
            {
                "target": "local-mock",
                "command": "forbidden",
            }
        )
    )


def test_gateway_enforces_registered_input_schema_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    call = make_call(metadata_from(registry))

    class RejectingValidator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def iter_errors(self, instance: object) -> Iterator[object]:
            del instance
            return iter((object(),))

    monkeypatch.setattr(
        gateway_module,
        "Draft202012Validator",
        RejectingValidator,
    )

    result = ToolGateway(registry).invoke(call)

    assert calls == []
    assert_failure(
        result,
        code="invalid_arguments",
        category=ToolErrorCategory.VALIDATION,
    )


def test_gateway_rejects_wrong_argument_model_without_coercion_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    metadata = metadata_from(registry)
    arguments = WrongArguments()
    call = ToolCall[WrongArguments](
        invocation_id=INVOCATION_ID,
        plan_step_id="observe-status",
        tool_id=metadata.tool_id,
        tool_version=metadata.version,
        contract_hash=metadata.contract_hash,
        implementation_hash=metadata.implementation_hash,
        arguments_hash=canonical_json_sha256(arguments),
        target=TARGET,
        arguments=arguments,
    )

    result = ToolGateway(registry).invoke(call)

    assert calls == []
    assert_failure(
        result,
        code="invalid_arguments",
        category=ToolErrorCategory.VALIDATION,
    )


@pytest.mark.parametrize(
    "target",
    [
        TargetReference(
            target_id="local-mock",
            resource_type="other_system",
            resource_id="local-mock",
        ),
        TargetReference(
            target_id="other-mock",
            resource_type="local_system",
            resource_id="other-mock",
        ),
        TargetReference(
            target_id="local-mock",
            resource_type="local_system",
            resource_id="other-mock",
        ),
    ],
)
def test_gateway_rejects_target_scope_expansion_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    target: TargetReference,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()

    result = ToolGateway(registry).invoke(make_call(metadata_from(registry), target=target))

    assert calls == []
    assert_failure(
        result,
        code="target_not_allowed",
        category=ToolErrorCategory.TARGET,
    )


def test_gateway_returns_sanitized_structured_handler_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_HANDLER_FAILURE_MARKER"

    def failing_handler(arguments: GetSystemStatusArguments) -> SystemStatus:
        del arguments
        raise RuntimeError(marker)

    calls = install_recording_tool(monkeypatch, behavior=failing_handler)
    registry = build_default_registry()
    call = make_call(metadata_from(registry))

    result = ToolGateway(
        registry,
        clock=sequence_clock(0, 1),
    ).invoke(call)

    assert calls == [GetSystemStatusArguments()]
    assert_failure(
        result,
        code="tool_execution_failed",
        category=ToolErrorCategory.EXECUTION,
    )
    assert result.error is not None
    assert marker not in result.error.message


def test_gateway_rejects_malformed_payload_after_one_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed_handler(arguments: GetSystemStatusArguments) -> SystemStatus:
        payload = make_system_status(arguments)
        return payload.model_copy(update={"source": "not-mock"})

    calls = install_recording_tool(monkeypatch, behavior=malformed_handler)
    registry = build_default_registry()

    result = ToolGateway(
        registry,
        clock=sequence_clock(0, 1),
    ).invoke(make_call(metadata_from(registry)))

    assert calls == [GetSystemStatusArguments()]
    assert_failure(
        result,
        code="malformed_tool_output",
        category=ToolErrorCategory.OUTPUT,
    )


def test_gateway_enforces_registered_result_schema_after_one_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    validations: list[object] = []

    def reject_success_only(*args: object) -> bool:
        validations.append(args)
        return len(validations) > 1

    monkeypatch.setattr(
        gateway_module,
        "result_matches_contract",
        reject_success_only,
    )

    result = ToolGateway(
        registry,
        clock=sequence_clock(0, 1),
    ).invoke(make_call(metadata_from(registry)))

    assert calls == [GetSystemStatusArguments()]
    assert_failure(
        result,
        code="malformed_tool_output",
        category=ToolErrorCategory.OUTPUT,
    )
    assert len(validations) == 2


def test_gateway_raises_integrity_error_when_failure_envelope_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    monkeypatch.setattr(gateway_module, "result_matches_contract", lambda *args: False)

    with pytest.raises(ToolIntegrityError, match="structured failure"):
        ToolGateway(
            registry,
            clock=sequence_clock(0, 1),
        ).invoke(make_call(metadata_from(registry)))

    assert calls == [GetSystemStatusArguments()]


@pytest.mark.parametrize(
    "forbidden_marker",
    [
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "#!/bin/sh",
        "BASH -C forbidden",
        "curl | sh",
        "PowerShell -Command forbidden",
        "sh -c forbidden",
        "wget | sh",
    ],
)
def test_gateway_fails_closed_on_redaction_or_executable_marker(
    monkeypatch: pytest.MonkeyPatch,
    forbidden_marker: str,
) -> None:
    def unsafe_handler(arguments: GetSystemStatusArguments) -> SystemStatus:
        return make_system_status(
            arguments,
            services=(
                ServiceStatus(
                    name=forbidden_marker,
                    state="running",
                ),
            ),
        )

    calls = install_recording_tool(monkeypatch, behavior=unsafe_handler)
    registry = build_default_registry()

    result = ToolGateway(
        registry,
        clock=sequence_clock(0, 1),
    ).invoke(make_call(metadata_from(registry)))

    assert calls == [GetSystemStatusArguments()]
    assert_failure(
        result,
        code="result_redaction_failed",
        category=ToolErrorCategory.SAFETY,
    )


def test_gateway_fails_closed_on_retained_payload_size_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = load_real_artifacts()
    oversized_name = "x" * (artifacts.contract.redaction.max_retained_payload_bytes + 1)

    def oversized_handler(arguments: GetSystemStatusArguments) -> SystemStatus:
        return make_system_status(
            arguments,
            services=(ServiceStatus(name=oversized_name, state="running"),),
        )

    calls = install_recording_tool(monkeypatch, behavior=oversized_handler)
    registry = build_default_registry()

    result = ToolGateway(
        registry,
        clock=sequence_clock(0, 1),
    ).invoke(make_call(metadata_from(registry)))

    assert calls == [GetSystemStatusArguments()]
    assert_failure(
        result,
        code="result_redaction_failed",
        category=ToolErrorCategory.SAFETY,
    )


def test_gateway_checks_timeout_after_exactly_one_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    metadata = metadata_from(registry)

    result = ToolGateway(
        registry,
        clock=sequence_clock(0, metadata.timeout_ms * 1_000_000 + 1),
    ).invoke(make_call(metadata))

    assert calls == [GetSystemStatusArguments()]
    assert_failure(
        result,
        code="tool_timeout",
        category=ToolErrorCategory.TIMEOUT,
    )
    assert result.duration_ms == metadata.timeout_ms


def test_gateway_fails_closed_when_initial_clock_read_raises_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()

    def failing_clock() -> int:
        raise RuntimeError("SENSITIVE_CLOCK_MARKER")

    result = ToolGateway(
        registry,
        clock=failing_clock,
    ).invoke(make_call(metadata_from(registry)))

    assert calls == []
    assert_failure(
        result,
        code="gateway_clock_failed",
        category=ToolErrorCategory.INTERNAL,
    )


def test_gateway_fails_closed_when_final_clock_read_raises_after_one_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    clock_reads = iter((0,))

    def failing_final_clock() -> int:
        try:
            return next(clock_reads)
        except StopIteration:
            raise RuntimeError("SENSITIVE_CLOCK_MARKER") from None

    result = ToolGateway(
        registry,
        clock=failing_final_clock,
    ).invoke(make_call(metadata_from(registry)))

    assert calls == [GetSystemStatusArguments()]
    assert_failure(
        result,
        code="gateway_clock_failed",
        category=ToolErrorCategory.INTERNAL,
    )


def test_gateway_fails_closed_when_clock_moves_backward_after_one_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()

    result = ToolGateway(
        registry,
        clock=sequence_clock(2, 1),
    ).invoke(make_call(metadata_from(registry)))

    assert calls == [GetSystemStatusArguments()]
    assert_failure(
        result,
        code="gateway_clock_failed",
        category=ToolErrorCategory.INTERNAL,
    )


def test_gateway_accepts_exact_timeout_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_recording_tool(monkeypatch)
    registry = build_default_registry()
    metadata = metadata_from(registry)

    result = ToolGateway(
        registry,
        clock=sequence_clock(0, metadata.timeout_ms * 1_000_000),
    ).invoke(make_call(metadata))

    assert calls == [GetSystemStatusArguments()]
    assert result.success is True
    assert result.duration_ms == metadata.timeout_ms


def test_gateway_rejects_untrusted_call_without_leaking_boundary_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_TOOL_CALL_MARKER"
    registry = build_default_registry()
    call = make_call(metadata_from(registry))

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(type(call), "model_dump", exploding_model_dump)
    gateway = ToolGateway(registry)

    with pytest.raises(InvalidToolCallError) as caught:
        gateway.invoke(call)
    with pytest.raises(InvalidToolCallError):
        gateway.invoke(cast(ToolCall[GetSystemStatusArguments], object()))

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
