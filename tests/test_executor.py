from typing import cast
from uuid import UUID

import pytest
from pydantic import BaseModel

from ai_server.context.builder import ContextBuilder
from ai_server.executor.service import Executor
from ai_server.models.execution import ExecutionPlan
from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.task import Task
from ai_server.models.tool import (
    RedactionRequirement,
    RiskLevel,
    RollbackRequirement,
    RollbackStrategy,
    SideEffectKind,
    ToolCall,
    ToolError,
    ToolErrorCategory,
    ToolMetadata,
    ToolResult,
    ToolSideEffects,
    ToolTargetScope,
    VerificationRequirement,
)
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.runtime.errors import ToolExecutionError
from ai_server.tools.gateway import ToolGateway, ToolGatewayError
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.tools.registry import ToolRegistry

CONTRACT_HASH = "a" * 64
IMPLEMENTATION_HASH = "b" * 64
INVOCATION_ID = UUID("00000000-0000-4000-8000-000000000001")
SECOND_INVOCATION_ID = UUID("00000000-0000-4000-8000-000000000002")


def make_plan() -> ExecutionPlan:
    """Build a hash-bound plan from a read-only metadata projection."""
    metadata = ToolMetadata(
        tool_id="get_system_status",
        version="1.0.0",
        contract_hash=CONTRACT_HASH,
        implementation_hash=IMPLEMENTATION_HASH,
        description="Return deterministic simulated system status.",
        risk_level=RiskLevel.L0,
        side_effects=ToolSideEffects(
            mutates_remote_state=False,
            kind=SideEffectKind.NONE,
        ),
        target_scope=ToolTargetScope(
            resource_type="local_system",
            maximum_targets=1,
            selector_field="target",
            allow_dynamic_expansion=False,
        ),
        redaction=RedactionRequirement(
            profile_id="local-default",
            profile_version="1.0.0",
            safe_evidence_fields=("source",),
            max_retained_payload_bytes=4096,
        ),
        verification=VerificationRequirement(
            required=True,
            evidence_fields=("source",),
        ),
        rollback=RollbackRequirement(
            required=False,
            available=False,
            strategy=RollbackStrategy.NOT_REQUIRED,
        ),
        timeout_ms=1000,
        idempotent=True,
        input_schema_id="urn:ai-server:tool:get-system-status:input-v1",
        output_schema_id="urn:ai-server:tool:get-system-status:output-v1",
        input_model="GetSystemStatusArguments",
        output_model="SystemStatus",
    )
    task = Task(request=SUPPORTED_REQUEST)
    context = ContextBuilder().build(task)
    return Planner().create_plan(context, metadata)


def make_executor() -> tuple[Executor, ToolGateway]:
    """Build an Executor with deterministic invocation identities."""
    invocation_ids = iter((INVOCATION_ID, SECOND_INVOCATION_ID))
    registry = ToolRegistry()
    registry.freeze()
    gateway = ToolGateway(registry)
    return (
        Executor(gateway, invocation_id_factory=lambda: next(invocation_ids)),
        gateway,
    )


def success_result(
    call: ToolCall[GetSystemStatusArguments],
) -> ToolResult[SystemStatus]:
    """Return structured success evidence bound to one exact ToolCall."""
    return ToolResult[SystemStatus](
        invocation_id=call.invocation_id,
        plan_step_id=call.plan_step_id,
        tool_id=call.tool_id,
        tool_version=call.tool_version,
        contract_hash=call.contract_hash,
        arguments_hash=call.arguments_hash,
        target=call.target,
        success=True,
        data=SystemStatus(
            cpu_percent=12.5,
            memory_percent=34.0,
            disk_percent=45.5,
            services=(ServiceStatus(name="mock-api", state="running"),),
        ),
        evidence={"source": "mock"},
        error=None,
        duration_ms=0,
    )


def failure_result(
    call: ToolCall[GetSystemStatusArguments],
) -> ToolResult[SystemStatus]:
    """Return a structured failure bound to one exact ToolCall."""
    return ToolResult[SystemStatus](
        invocation_id=call.invocation_id,
        plan_step_id=call.plan_step_id,
        tool_id=call.tool_id,
        tool_version=call.tool_version,
        contract_hash=call.contract_hash,
        arguments_hash=call.arguments_hash,
        target=call.target,
        success=False,
        data=None,
        evidence={},
        error=ToolError(
            code="tool_execution_failed",
            category=ToolErrorCategory.EXECUTION,
            message="Tool execution failed safely",
            retryable=False,
        ),
        duration_ms=0,
    )


def test_executor_builds_exact_hash_bound_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    executor, gateway = make_executor()
    calls: list[ToolCall[GetSystemStatusArguments]] = []

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> ToolResult[SystemStatus]:
        calls.append(call)
        return success_result(call)

    monkeypatch.setattr(gateway, "invoke", recording_invoke)

    results = executor.execute(plan)

    assert len(calls) == 1
    call = calls[0]
    step = plan.steps[0]
    assert call.invocation_id == INVOCATION_ID
    assert call.plan_step_id == step.step_id
    assert call.tool_id == step.tool_id
    assert call.tool_version == step.tool_version
    assert call.contract_hash == step.contract_hash
    assert call.implementation_hash == step.implementation_hash
    assert call.arguments_hash == canonical_json_sha256(step.arguments)
    assert call.target.target_id == plan.target
    assert call.target.resource_type == "local_system"
    assert call.target.resource_id == step.arguments.target
    assert call.arguments == step.arguments
    assert len(results) == 1
    assert results[0].success is True
    assert results[0].invocation_id == call.invocation_id
    assert results[0].plan_step_id == call.plan_step_id
    assert results[0].tool_id == call.tool_id
    assert results[0].tool_version == call.tool_version
    assert results[0].contract_hash == call.contract_hash
    assert results[0].arguments_hash == call.arguments_hash
    assert results[0].target == call.target


def test_executor_stops_after_first_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    second = plan.steps[0].model_copy(update={"step_id": "second-status"})
    multi_step_plan = plan.model_copy(update={"steps": (plan.steps[0], second)})
    executor, gateway = make_executor()
    calls: list[ToolCall[GetSystemStatusArguments]] = []

    def failing_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> ToolResult[SystemStatus]:
        calls.append(call)
        return failure_result(call)

    monkeypatch.setattr(gateway, "invoke", failing_invoke)

    results = executor.execute(multi_step_plan)

    assert len(calls) == 1
    assert len(results) == 1
    assert results[0].success is False
    assert results[0].error is not None
    assert results[0].error.code == "tool_execution_failed"


def test_executor_rejects_malformed_gateway_result_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    executor, gateway = make_executor()
    calls = 0

    def malformed_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> ToolResult[BaseModel]:
        nonlocal calls
        calls += 1
        del call
        return cast(ToolResult[BaseModel], object())

    monkeypatch.setattr(gateway, "invoke", malformed_invoke)

    with pytest.raises(ToolExecutionError, match="malformed evidence"):
        executor.execute(plan)

    assert calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invocation_id", SECOND_INVOCATION_ID),
        ("plan_step_id", "other-step"),
        ("tool_id", "other_tool"),
        ("tool_version", "1.0.1"),
        ("contract_hash", "c" * 64),
        ("arguments_hash", "d" * 64),
    ],
)
def test_executor_rejects_result_identity_mismatch(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    executor, gateway = make_executor()

    def mismatched_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> ToolResult[SystemStatus]:
        result = success_result(call)
        return result.model_copy(update={field: value})

    monkeypatch.setattr(gateway, "invoke", mismatched_invoke)

    with pytest.raises(ToolExecutionError, match="identity"):
        executor.execute(plan)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_id", "other-target"),
        ("resource_type", "other_resource"),
        ("resource_id", "other-resource"),
    ],
)
def test_executor_rejects_any_result_target_mismatch(
    field: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    executor, gateway = make_executor()

    def mismatched_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> ToolResult[SystemStatus]:
        result = success_result(call)
        target = result.target.model_copy(update={field: value})
        return result.model_copy(update={"target": target})

    monkeypatch.setattr(gateway, "invoke", mismatched_invoke)

    with pytest.raises(ToolExecutionError, match="identity"):
        executor.execute(plan)


def test_executor_wraps_gateway_failure_without_retry_or_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    executor, gateway = make_executor()
    marker = "SENSITIVE_GATEWAY_MARKER"
    calls = 0

    def failing_invoke(call: ToolCall[GetSystemStatusArguments]) -> ToolResult[BaseModel]:
        nonlocal calls
        calls += 1
        del call
        raise ToolGatewayError(marker)

    monkeypatch.setattr(gateway, "invoke", failing_invoke)

    with pytest.raises(ToolExecutionError, match="rejected") as caught:
        executor.execute(plan)

    assert calls == 1
    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_executor_rejects_empty_plan_before_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    executor, gateway = make_executor()
    calls = 0

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> ToolResult[BaseModel]:
        nonlocal calls
        calls += 1
        raise AssertionError(call)

    monkeypatch.setattr(gateway, "invoke", recording_invoke)
    empty_plan = plan.model_copy(update={"steps": ()})

    with pytest.raises(ToolExecutionError, match="malformed plan"):
        executor.execute(empty_plan)

    assert calls == 0
