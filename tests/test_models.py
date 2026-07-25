from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.task import Task
from ai_server.models.tool import RiskLevel, ToolMetadata, ToolResult
from ai_server.runtime.state import RuntimeState


def assign_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


def make_step(*, step_id: str = "status") -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        role=StepRole.OBSERVE,
        tool_name="get_system_status",
        tool_version="1.0.0",
        arguments=GetSystemStatusArguments(),
        reason="Collect simulated status.",
        impact="No external impact.",
        verification="Require simulated structured evidence.",
        recovery="No rollback required.",
    )


def make_status() -> SystemStatus:
    return SystemStatus(
        cpu_percent=12.5,
        memory_percent=34.0,
        disk_percent=45.5,
        services=(ServiceStatus(name="mock-api", state="running"),),
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


def test_execution_step_rejects_untyped_arguments_and_forged_risk() -> None:
    payload = make_step().model_dump(mode="json")
    payload["arguments"] = {"target": "not-allowed"}
    with pytest.raises(ValidationError):
        ExecutionStep.model_validate(payload)

    payload = make_step().model_dump(mode="json")
    payload["risk_level"] = "L3"
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
    metadata = ToolMetadata(
        name="get_system_status",
        version="1.0.0",
        description="Mock status.",
        risk_level=RiskLevel.L0,
        timeout_seconds=1.0,
        idempotent=True,
        input_model="GetSystemStatusArguments",
        output_model="SystemStatus",
    )
    plan = ExecutionPlan(task_id=task.task_id, target=task.target, steps=(step,))
    status = make_status()
    result = ToolResult[SystemStatus](
        tool_name="get_system_status",
        tool_version="1.0.0",
        data=status,
        duration_ms=0,
    )

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
    plan = ExecutionPlan(task_id=task_id, target="local-mock", steps=(step,))

    assert isinstance(plan.steps, tuple)
    assert ExecutionPlan.model_validate_json(plan.model_dump_json()) == plan

    with pytest.raises(ValidationError):
        ExecutionPlan(task_id=task_id, target="local-mock", steps=())
    with pytest.raises(ValidationError):
        ExecutionPlan(task_id=task_id, target="local-mock", steps=(step, step))


def test_tool_result_requires_typed_data_and_non_negative_duration() -> None:
    status = make_status()
    result = ToolResult[SystemStatus](
        tool_name="get_system_status",
        tool_version="1.0.0",
        data=status,
        duration_ms=0,
    )

    assert ToolResult[SystemStatus].model_validate_json(result.model_dump_json()) == result

    invalid_data = {
        "tool_name": "get_system_status",
        "tool_version": "1.0.0",
        "success": True,
        "data": "plain string",
        "duration_ms": 0,
    }
    with pytest.raises(ValidationError):
        ToolResult[SystemStatus].model_validate(invalid_data)
    with pytest.raises(ValidationError):
        ToolResult[SystemStatus](
            tool_name="get_system_status",
            tool_version="1.0.0",
            data=status,
            duration_ms=-1,
        )


def test_valid_task_history_accepts_only_declared_edges() -> None:
    history = (
        RuntimeState.RECEIVED,
        RuntimeState.CONTEXT_BUILDING,
        RuntimeState.PLANNING,
        RuntimeState.POLICY_CHECK,
        RuntimeState.EXECUTING,
        RuntimeState.VERIFYING,
        RuntimeState.COMPLETED,
    )
    task = Task(request="get_system_status", state=RuntimeState.COMPLETED, state_history=history)
    assert task.state_history == history
