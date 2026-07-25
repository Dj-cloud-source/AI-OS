import json
import logging
from collections.abc import Mapping

import pytest

from ai_server.context.builder import ContextBuilder
from ai_server.executor.service import Executor
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan
from ai_server.models.system_status import GetSystemStatusArguments, SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import RiskLevel, ToolMetadata, ToolResult
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine, ToolKey
from ai_server.runtime.engine import RuntimeEngine
from ai_server.runtime.errors import (
    InvalidTaskError,
    PlanMismatchError,
    PolicyDeniedError,
    ToolExecutionError,
    UnsupportedTaskError,
    VerificationError,
)
from ai_server.runtime.state import RuntimeState
from ai_server.tools.get_system_status import GET_SYSTEM_STATUS_METADATA, get_system_status
from ai_server.verifier.service import Verifier


def test_runtime_completes_exact_l0_state_history() -> None:
    completed = RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))

    assert completed.state is RuntimeState.COMPLETED
    assert completed.state_history == (
        RuntimeState.RECEIVED,
        RuntimeState.CONTEXT_BUILDING,
        RuntimeState.PLANNING,
        RuntimeState.POLICY_CHECK,
        RuntimeState.EXECUTING,
        RuntimeState.VERIFYING,
        RuntimeState.COMPLETED,
    )
    assert RuntimeState.WAITING_FOR_APPROVAL not in completed.state_history


def test_runtime_calls_components_and_tool_in_exact_order() -> None:
    events: list[str] = []

    class RecordingContextBuilder(ContextBuilder):
        def build(self, task: Task) -> RuntimeContext:
            events.append("context")
            return super().build(task)

    class RecordingPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            events.append("planner")
            return super().create_plan(context, metadata)

    class RecordingPolicy(PolicyEngine):
        def check(
            self,
            plan: ExecutionPlan,
            catalog: Mapping[ToolKey, ToolMetadata],
        ) -> None:
            events.append("policy")
            return super().check(plan, catalog)

    def recording_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        events.append("tool")
        return get_system_status(arguments)

    class RecordingExecutor(Executor):
        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            events.append("executor")
            return super().execute(plan)

    class RecordingVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
        ) -> None:
            events.append("verifier")
            return super().verify(plan, results)

    runtime = RuntimeEngine(
        context_builder=RecordingContextBuilder(),
        planner=RecordingPlanner(),
        policy=RecordingPolicy(),
        executor=RecordingExecutor(system_status_tool=recording_tool),
        verifier=RecordingVerifier(),
    )
    runtime.run(Task(request=SUPPORTED_REQUEST))

    assert events == ["context", "planner", "policy", "executor", "tool", "verifier"]


def test_policy_denial_stops_before_executor_tool_and_verifier() -> None:
    tool_calls = 0
    verifier_calls = 0

    def counting_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal tool_calls
        tool_calls += 1
        return get_system_status(arguments)

    class CountingVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
        ) -> None:
            nonlocal verifier_calls
            verifier_calls += 1
            return super().verify(plan, results)

    l1_metadata = GET_SYSTEM_STATUS_METADATA.model_copy(update={"risk_level": RiskLevel.L1})
    runtime = RuntimeEngine(
        executor=Executor(system_status_tool=counting_tool),
        verifier=CountingVerifier(),
        tool_metadata=l1_metadata,
    )

    with pytest.raises(PolicyDeniedError):
        runtime.run(Task(request=SUPPORTED_REQUEST))
    assert tool_calls == 0
    assert verifier_calls == 0


def test_unsupported_task_stops_before_tool() -> None:
    tool_calls = 0

    def counting_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal tool_calls
        tool_calls += 1
        return get_system_status(arguments)

    runtime = RuntimeEngine(executor=Executor(system_status_tool=counting_tool))
    with pytest.raises(UnsupportedTaskError):
        runtime.run(Task(request="unsupported"))
    assert tool_calls == 0


def test_runtime_rejects_forged_empty_plan_before_policy_or_tool() -> None:
    policy_calls = 0
    tool_calls = 0

    class EmptyPlanPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            valid_plan = super().create_plan(context, metadata)
            return valid_plan.model_copy(update={"steps": ()})

    class CountingPolicy(PolicyEngine):
        def check(
            self,
            plan: ExecutionPlan,
            catalog: Mapping[ToolKey, ToolMetadata],
        ) -> None:
            nonlocal policy_calls
            policy_calls += 1
            return super().check(plan, catalog)

    def counting_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal tool_calls
        tool_calls += 1
        return get_system_status(arguments)

    runtime = RuntimeEngine(
        planner=EmptyPlanPlanner(),
        policy=CountingPolicy(),
        executor=Executor(system_status_tool=counting_tool),
    )

    with pytest.raises(PlanMismatchError, match="malformed execution plan"):
        runtime.run(Task(request=SUPPORTED_REQUEST))

    assert policy_calls == 0
    assert tool_calls == 0


def test_runtime_rejects_forged_local_identity_before_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_USER_MARKER"
    task = Task(request=SUPPORTED_REQUEST).model_copy(update={"user": marker})
    caplog.set_level(logging.INFO)

    with pytest.raises(InvalidTaskError):
        RuntimeEngine().run(task)

    assert marker not in caplog.text


def test_runtime_runs_do_not_share_state_history() -> None:
    runtime = RuntimeEngine()
    first = runtime.run(Task(request=SUPPORTED_REQUEST))
    second = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert first.task_id != second.task_id
    assert first.state_history == second.state_history


def test_runtime_emits_structured_transition_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    completed = RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))
    messages = [json.loads(record.message) for record in caplog.records]
    transitions = [
        message for message in messages if message["event"] == "runtime_state_transition"
    ]
    audits = [message for message in messages if message["event"] == "execution_audit"]

    assert len(transitions) == 6
    assert all(message["task_id"] == str(completed.task_id) for message in transitions)
    assert transitions[-1]["to_state"] == "COMPLETED"
    assert len(audits) == 1
    assert audits == [
        {
            "approval_id": None,
            "arguments": {"target": "local-mock"},
            "duration_ms": 0,
            "event": "execution_audit",
            "operator": "local-user",
            "plan_id": audits[0]["plan_id"],
            "result": "success",
            "target": "local-mock",
            "task_id": str(completed.task_id),
            "tool": "get_system_status",
            "tool_version": "1.0.0",
            "user": "local-user",
            "verification": "passed",
        }
    ]


def test_runtime_audits_failed_verification_without_completing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class RejectingVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
        ) -> None:
            del plan, results
            raise VerificationError("simulated verification failure")

    caplog.set_level(logging.INFO)
    with pytest.raises(VerificationError):
        RuntimeEngine(verifier=RejectingVerifier()).run(Task(request=SUPPORTED_REQUEST))

    messages = [json.loads(record.message) for record in caplog.records]
    transitions = [
        message for message in messages if message["event"] == "runtime_state_transition"
    ]
    audits = [message for message in messages if message["event"] == "execution_audit"]

    assert transitions[-1]["to_state"] == "VERIFYING"
    assert all(message["to_state"] != "COMPLETED" for message in transitions)
    assert len(audits) == 1
    assert audits[0]["verification"] == "failed"


def test_runtime_audits_tool_failure_without_verifying_or_completing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool_calls = 0

    def failing_tool(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
        nonlocal tool_calls
        tool_calls += 1
        del arguments
        raise ToolExecutionError("simulated redacted failure")

    caplog.set_level(logging.INFO)
    with pytest.raises(ToolExecutionError):
        RuntimeEngine(executor=Executor(system_status_tool=failing_tool)).run(
            Task(request=SUPPORTED_REQUEST)
        )

    messages = [json.loads(record.message) for record in caplog.records]
    transitions = [
        message for message in messages if message["event"] == "runtime_state_transition"
    ]
    audits = [message for message in messages if message["event"] == "execution_audit"]

    assert tool_calls == 1
    assert transitions[-1]["to_state"] == "EXECUTING"
    assert all(message["to_state"] not in {"VERIFYING", "COMPLETED"} for message in transitions)
    assert len(audits) == 1
    assert audits[0]["result"] == "execution_failed"
    assert audits[0]["duration_ms"] is None
    assert audits[0]["verification"] == "not_run"
