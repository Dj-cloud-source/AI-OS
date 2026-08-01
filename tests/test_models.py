from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_server.context.builder import ContextBuilder
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
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
    TargetReference,
    ToolMetadata,
    ToolResult,
    ToolSideEffects,
    ToolTargetScope,
    VerificationRequirement,
)
from ai_server.models.verification import EqualityCriterion
from ai_server.planner.service import Planner
from ai_server.runtime.errors import InvalidTaskError, UnsupportedTaskError
from ai_server.runtime.state import RuntimeState
from ai_server.tools.hashing import canonical_json_sha256

CONTRACT_HASH = "a" * 64
IMPLEMENTATION_HASH = "b" * 64
INVOCATION_ID = UUID("00000000-0000-4000-8000-000000000001")


def assign_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


def make_step(*, step_id: str = "status") -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        role=StepRole.OBSERVE,
        tool_id="get_system_status",
        tool_version="1.0.0",
        contract_hash=CONTRACT_HASH,
        implementation_hash=IMPLEMENTATION_HASH,
        arguments=GetSystemStatusArguments(),
        reason="Collect simulated status.",
        impact="No external impact.",
        verification="Require simulated structured evidence.",
        recovery="No rollback required.",
    )


def make_criterion(*, step_id: str = "status") -> EqualityCriterion:
    """Build one mandatory machine-readable Mock verification criterion."""
    return EqualityCriterion(
        criterion_id="mock-source",
        evidence_step_id=step_id,
        source="evidence",
        field="source",
        expected="mock",
    )


def make_status() -> SystemStatus:
    return SystemStatus(
        cpu_percent=12.5,
        memory_percent=34.0,
        disk_percent=45.5,
        services=(ServiceStatus(name="mock-api", state="running"),),
    )


def make_tool_metadata() -> ToolMetadata:
    return ToolMetadata(
        tool_id="get_system_status",
        version="1.0.0",
        contract_hash=CONTRACT_HASH,
        implementation_hash=IMPLEMENTATION_HASH,
        description="Mock status.",
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


def make_result(
    status: SystemStatus | None = None,
    *,
    duration_ms: int = 0,
) -> ToolResult[SystemStatus]:
    arguments = GetSystemStatusArguments()
    return ToolResult[SystemStatus](
        invocation_id=INVOCATION_ID,
        plan_step_id="status",
        tool_id="get_system_status",
        tool_version="1.0.0",
        contract_hash=CONTRACT_HASH,
        arguments_hash=canonical_json_sha256(arguments),
        target=TargetReference(
            target_id=arguments.target,
            resource_type="local_system",
            resource_id=arguments.target,
        ),
        success=True,
        data=status if status is not None else make_status(),
        evidence={"source": "mock"},
        error=None,
        duration_ms=duration_ms,
    )


def test_task_and_context_round_trip_with_consistent_ids() -> None:
    task = Task(request="get_system_status")
    context = RuntimeContext(
        task_id=task.task_id,
        request=task.request,
        user=task.user,
        target=task.target,
    )

    assert context.task_id == task.task_id
    assert RuntimeContext.model_validate_json(context.model_dump_json()) == context


@pytest.mark.parametrize(
    "payload",
    [
        {
            "request": "get_system_status",
            "state": "COMPLETED",
            "state_history": ["RECEIVED", "COMPLETED"],
        },
        {
            "request": "get_system_status",
            "state": "PLANNING",
            "state_history": ["RECEIVED", "CONTEXT_BUILDING"],
        },
        {
            "request": "get_system_status",
            "state_history": [],
        },
        {
            "request": "get_system_status",
            "unexpected": True,
        },
        {
            "request": "get_system_status",
            "user": "remote-user",
        },
        {
            "request": "get_system_status",
            "target": "remote-server",
        },
    ],
)
def test_task_rejects_invalid_or_extra_state_data(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(payload)


def test_execution_step_rejects_untyped_arguments_legacy_identity_and_forged_risk() -> None:
    payload = make_step().model_dump(mode="json")
    payload["arguments"] = {"target": "not-allowed"}
    with pytest.raises(ValidationError):
        ExecutionStep.model_validate(payload)

    payload = make_step().model_dump(mode="json")
    payload["tool_name"] = "get_system_status"
    with pytest.raises(ValidationError):
        ExecutionStep.model_validate(payload)

    payload = make_step().model_dump(mode="json")
    payload["risk_level"] = "L3"
    with pytest.raises(ValidationError):
        ExecutionStep.model_validate(payload)

    payload = make_step().model_dump(mode="json")
    payload["contract_hash"] = "not-a-hash"
    with pytest.raises(ValidationError):
        ExecutionStep.model_validate(payload)


def test_phase_zero_models_are_frozen() -> None:
    task = Task(request="get_system_status")
    context = RuntimeContext(
        task_id=task.task_id,
        request=task.request,
        user=task.user,
        target=task.target,
    )
    step = make_step()
    arguments = GetSystemStatusArguments()
    metadata = make_tool_metadata()
    plan = ExecutionPlan(
        task_id=task.task_id,
        target=task.target,
        steps=(step,),
        verification_criteria=(make_criterion(),),
    )
    status = make_status()
    result = make_result(status)

    assignments = (
        (task, "request", "changed"),
        (context, "request", "changed"),
        (step, "reason", "changed"),
        (arguments, "target", "local-mock"),
        (metadata, "risk_level", RiskLevel.L3),
        (plan, "target", "local-mock"),
        (status, "cpu_percent", 99.0),
        (result, "duration_ms", 10),
    )
    for instance, name, value in assignments:
        with pytest.raises(ValidationError):
            assign_attribute(instance, name, value)


def test_execution_plan_requires_ordered_unique_steps() -> None:
    task_id = uuid4()
    step = make_step()
    plan = ExecutionPlan(
        task_id=task_id,
        target="local-mock",
        steps=(step,),
        verification_criteria=(make_criterion(),),
    )

    assert isinstance(plan.steps, tuple)
    assert ExecutionPlan.model_validate_json(plan.model_dump_json()) == plan

    with pytest.raises(ValidationError):
        ExecutionPlan(
            task_id=task_id,
            target="local-mock",
            steps=(),
            verification_criteria=(make_criterion(),),
        )
    with pytest.raises(ValidationError):
        ExecutionPlan(
            task_id=task_id,
            target="local-mock",
            steps=(step, step),
            verification_criteria=(make_criterion(),),
        )


def test_execution_plan_v2_requires_unique_step_bound_verification_criteria() -> None:
    task_id = uuid4()
    step = make_step()
    criterion = make_criterion()

    with pytest.raises(ValidationError):
        ExecutionPlan(
            task_id=task_id,
            target="local-mock",
            steps=(step,),
            verification_criteria=(),
        )
    with pytest.raises(ValidationError, match="unique"):
        ExecutionPlan(
            task_id=task_id,
            target="local-mock",
            steps=(step,),
            verification_criteria=(criterion, criterion),
        )
    with pytest.raises(ValidationError, match="planned evidence"):
        ExecutionPlan(
            task_id=task_id,
            target="local-mock",
            steps=(step,),
            verification_criteria=(
                criterion.model_copy(update={"evidence_step_id": "missing-step"}),
            ),
        )
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(
            {
                "schema_version": "1",
                "task_id": task_id,
                "target": "local-mock",
                "steps": (step,),
                "verification_criteria": (criterion,),
            },
            strict=True,
        )


def test_tool_result_requires_typed_data_and_non_negative_duration() -> None:
    status = make_status()
    result = make_result(status)

    assert ToolResult[SystemStatus].model_validate_json(result.model_dump_json()) == result

    invalid_data = result.model_dump(mode="python")
    invalid_data["data"] = "plain string"
    with pytest.raises(ValidationError):
        ToolResult[SystemStatus].model_validate(invalid_data)

    negative_duration = result.model_dump(mode="python")
    negative_duration["duration_ms"] = -1
    with pytest.raises(ValidationError):
        ToolResult[SystemStatus].model_validate(negative_duration)


def test_valid_task_history_accepts_only_declared_edges() -> None:
    history = (
        RuntimeState.RECEIVED,
        RuntimeState.CONTEXT_BUILDING,
        RuntimeState.PLANNING,
        RuntimeState.POLICY_CHECK,
        RuntimeState.WAITING_FOR_APPROVAL,
        RuntimeState.EXECUTING,
        RuntimeState.VERIFYING,
        RuntimeState.COMPLETED,
    )
    task = Task(request="get_system_status", state=RuntimeState.COMPLETED, state_history=history)
    assert task.state_history == history


def test_task_history_rejects_policy_to_execution_bypass() -> None:
    history = (
        RuntimeState.RECEIVED,
        RuntimeState.CONTEXT_BUILDING,
        RuntimeState.PLANNING,
        RuntimeState.POLICY_CHECK,
        RuntimeState.EXECUTING,
    )

    with pytest.raises(ValidationError):
        Task(
            request="get_system_status",
            state=RuntimeState.EXECUTING,
            state_history=history,
        )


def test_context_builder_rejects_untrusted_task_inputs_with_explicit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_CONTEXT_TASK_MARKER"
    task = Task(request="get_system_status")

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(Task, "model_dump", exploding_model_dump)

    with pytest.raises(InvalidTaskError) as caught:
        ContextBuilder().build(task)
    with pytest.raises(InvalidTaskError):
        ContextBuilder().build(cast(Task, object()))

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_context_builder_rejects_uuid_subclass_without_propagating_it() -> None:
    class UntrustedUUID(UUID):
        pass

    task = Task(request="get_system_status")
    forged = task.model_copy(update={"task_id": UntrustedUUID(bytes=task.task_id.bytes)})

    with pytest.raises(InvalidTaskError):
        ContextBuilder().build(forged)


def test_planner_wraps_untrusted_context_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_PLANNER_CONTEXT_MARKER"
    task = Task(request="get_system_status")
    context = ContextBuilder().build(task)

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(RuntimeContext, "model_dump", exploding_model_dump)

    with pytest.raises(InvalidTaskError) as caught:
        Planner().create_plan(context, make_tool_metadata())

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_planner_wraps_untrusted_metadata_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_PLANNER_METADATA_MARKER"
    task = Task(request="get_system_status")
    context = ContextBuilder().build(task)
    metadata = make_tool_metadata()

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(ToolMetadata, "model_dump", exploding_model_dump)

    with pytest.raises(UnsupportedTaskError) as caught:
        Planner().create_plan(context, metadata)

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
