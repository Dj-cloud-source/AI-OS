from typing import cast

import pytest

from ai_server.context.builder import ContextBuilder
from ai_server.executor.service import Executor
from ai_server.models.execution import ExecutionPlan
from ai_server.models.system_status import GetSystemStatusArguments, SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import ToolResult
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.runtime.errors import ToolExecutionError, VerificationError
from ai_server.tools.get_system_status import GET_SYSTEM_STATUS_METADATA, get_system_status
from ai_server.verifier.service import Verifier


def make_plan() -> ExecutionPlan:
    task = Task(request=SUPPORTED_REQUEST)
    return Planner().create_plan(ContextBuilder().build(task), GET_SYSTEM_STATUS_METADATA)


def test_executor_invokes_bound_tool_exactly_once() -> None:
    calls: list[GetSystemStatusArguments] = []

    def counting_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        calls.append(arguments)
        return get_system_status(arguments)

    plan = make_plan()
    results = Executor(system_status_tool=counting_tool).execute(plan)

    assert calls == [plan.steps[0].arguments]
    assert len(results) == 1


def test_executor_does_not_retry_explicit_tool_failure() -> None:
    calls = 0

    def failing_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal calls
        calls += 1
        del arguments
        raise ToolExecutionError("simulated Tool failure")

    with pytest.raises(ToolExecutionError):
        Executor(system_status_tool=failing_tool).execute(make_plan())
    assert calls == 1


def test_executor_wraps_unexpected_tool_failure_without_retrying() -> None:
    calls = 0

    def failing_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal calls
        calls += 1
        del arguments
        raise RuntimeError("sensitive implementation detail")

    with pytest.raises(ToolExecutionError, match="Tool invocation failed safely") as caught:
        Executor(system_status_tool=failing_tool).execute(make_plan())

    assert "sensitive implementation detail" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert calls == 1


def test_executor_rejects_malformed_tool_result_without_retrying() -> None:
    calls = 0

    def malformed_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal calls
        calls += 1
        del arguments
        return cast(ToolResult[SystemStatus], object())

    with pytest.raises(ToolExecutionError, match="invalid structured result"):
        Executor(system_status_tool=malformed_tool).execute(make_plan())

    assert calls == 1


def test_executor_rejects_unknown_identity_without_calling_tool() -> None:
    calls = 0

    def counting_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal calls
        calls += 1
        return get_system_status(arguments)

    plan = make_plan()
    changed_step = plan.steps[0].model_copy(update={"tool_version": "9.9.9"})
    changed_plan = plan.model_copy(update={"steps": (changed_step,)})

    with pytest.raises(ToolExecutionError):
        Executor(system_status_tool=counting_tool).execute(changed_plan)
    assert calls == 0


def test_executor_rejects_forged_empty_plan_without_calling_tool() -> None:
    calls = 0

    def counting_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal calls
        calls += 1
        return get_system_status(arguments)

    empty_plan = make_plan().model_copy(update={"steps": ()})

    with pytest.raises(ToolExecutionError, match="malformed plan"):
        Executor(system_status_tool=counting_tool).execute(empty_plan)
    assert calls == 0


def test_verifier_accepts_existing_structured_evidence_without_new_tool_call() -> None:
    calls = 0

    def counting_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal calls
        calls += 1
        return get_system_status(arguments)

    plan = make_plan()
    results = Executor(system_status_tool=counting_tool).execute(plan)
    assert calls == 1

    Verifier().verify(plan, results)
    assert calls == 1


def test_verifier_rejects_mismatched_identity() -> None:
    plan = make_plan()
    result = get_system_status(plan.steps[0].arguments)
    wrong_identity = result.model_copy(update={"tool_name": "wrong_tool"})

    with pytest.raises(VerificationError):
        Verifier().verify(plan, (wrong_identity,))


def test_verifier_accepts_stopped_service_as_valid_observed_status() -> None:
    plan = make_plan()
    result = get_system_status(plan.steps[0].arguments)
    stopped_data = result.data.model_copy(
        update={"services": (result.data.services[0].model_copy(update={"state": "stopped"}),)}
    )
    stopped_result = result.model_copy(update={"data": stopped_data})

    Verifier().verify(plan, (stopped_result,))


def test_verifier_rejects_empty_plan_and_missing_evidence() -> None:
    plan = make_plan()

    with pytest.raises(VerificationError, match="count"):
        Verifier().verify(plan, ())
    with pytest.raises(VerificationError, match="malformed plan"):
        Verifier().verify(plan.model_copy(update={"steps": ()}), ())


def test_verifier_rejects_malformed_evidence_with_explicit_error() -> None:
    malformed = cast(tuple[ToolResult[SystemStatus], ...], (object(),))

    with pytest.raises(VerificationError, match="evidence is malformed") as caught:
        Verifier().verify(make_plan(), malformed)

    assert caught.value.__cause__ is None
