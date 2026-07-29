import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import cast
from uuid import UUID, uuid4

import pytest

from ai_server.context.builder import ContextBuilder
from ai_server.executor.service import Executor
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan
from ai_server.models.runtime import (
    LifecycleEvent,
    LifecycleEventKind,
    RuntimeComponent,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
)
from ai_server.models.system_status import SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import (
    TargetReference,
    ToolError,
    ToolErrorCategory,
    ToolMetadata,
    ToolResult,
)
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine, ToolKey
from ai_server.runtime.engine import RuntimeEngine
from ai_server.runtime.errors import (
    ApprovalRequiredError,
    ApprovalResumeUnavailableError,
    InvalidClockError,
    InvalidRuntimeOutcomeError,
    InvalidStateTransitionError,
    InvalidTaskError,
    PlanMismatchError,
    PolicyDeniedError,
    TerminalStateMutationError,
    ToolExecutionError,
    UnsupportedTaskError,
    VerificationError,
)
from ai_server.runtime.state import RuntimeState
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.gateway import ToolGateway
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.tools.registry import ToolRegistry
from ai_server.verifier.service import Verifier

SENSITIVE_MARKER = "SENSITIVE_RUNTIME_MARKER"


@dataclass
class Trace:
    calls: list[str] = field(default_factory=list)
    fail_at: str | None = None
    error: Exception | None = None

    def record(self, stage: str) -> None:
        self.calls.append(stage)
        if self.fail_at == stage and self.error is not None:
            raise self.error


class RecordingContextBuilder(ContextBuilder):
    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    def build(self, task: Task) -> RuntimeContext:
        self._trace.record("context")
        return super().build(task)


class RecordingPlanner(Planner):
    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    def create_plan(
        self,
        context: RuntimeContext,
        metadata: ToolMetadata,
    ) -> ExecutionPlan:
        self._trace.record("planner")
        return super().create_plan(context, metadata)


class RecordingPolicy(PolicyEngine):
    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    def check(
        self,
        plan: ExecutionPlan,
        catalog: Mapping[ToolKey, ToolMetadata],
    ) -> None:
        self._trace.record("policy")
        return super().check(plan, catalog)


class RecordingExecutor(Executor):
    def __init__(self, trace: Trace, gateway: ToolGateway) -> None:
        self._trace = trace
        super().__init__(gateway)

    def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
        self._trace.record("executor")
        self._trace.record("tool")
        return super().execute(plan)


class RecordingVerifier(Verifier):
    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    def verify(
        self,
        plan: ExecutionPlan,
        results: tuple[ToolResult[SystemStatus], ...],
    ) -> None:
        self._trace.record("verifier")
        return super().verify(plan, results)


class SequenceClock:
    def __init__(self, values: list[datetime]) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class StatefulTimezone(tzinfo):
    def __init__(self, valid_reads: int, marker: str) -> None:
        self._valid_reads = valid_reads
        self._marker = marker
        self.calls = 0

    def utcoffset(self, value: datetime | None) -> timedelta:
        del value
        self.calls += 1
        if self.calls > self._valid_reads:
            raise RuntimeError(self._marker)
        return timedelta(0)

    def dst(self, value: datetime | None) -> timedelta:
        del value
        return timedelta(0)

    def tzname(self, value: datetime | None) -> str:
        del value
        return "stateful-test"


class ExplodingUUID(UUID):
    def __str__(self) -> str:
        raise RuntimeError(SENSITIVE_MARKER)


def clock_values(count: int) -> list[datetime]:
    base = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
    return [base + timedelta(seconds=index) for index in range(count)]


def make_runtime(
    trace: Trace,
    *,
    clock: SequenceClock | None = None,
    executor: Executor | None = None,
) -> RuntimeEngine:
    registry = build_default_registry()
    return RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=RecordingPlanner(trace),
        policy=RecordingPolicy(trace),
        executor=(
            executor
            if executor is not None
            else RecordingExecutor(
                trace,
                ToolGateway(registry, clock=lambda: 0),
            )
        ),
        verifier=RecordingVerifier(trace),
        registry=registry,
        clock=clock if clock is not None else lambda: datetime.now(UTC),
    )


def make_structured_result(
    plan: ExecutionPlan,
    *,
    success: bool,
) -> ToolResult[SystemStatus]:
    """Build exact success or failure evidence for Runtime boundary tests."""
    step = plan.steps[0]
    return ToolResult[SystemStatus](
        invocation_id=UUID("00000000-0000-4000-8000-000000000001"),
        plan_step_id=step.step_id,
        tool_id=step.tool_id,
        tool_version=step.tool_version,
        contract_hash=step.contract_hash,
        arguments_hash=canonical_json_sha256(step.arguments),
        target=TargetReference(
            target_id=plan.target,
            resource_type="local_system",
            resource_id=step.arguments.target,
        ),
        success=success,
        data=(
            SystemStatus(
                cpu_percent=12.5,
                memory_percent=34.0,
                disk_percent=45.5,
                services=(),
            )
            if success
            else None
        ),
        evidence={"source": "mock"} if success else {},
        error=(
            None
            if success
            else ToolError(
                code="tool_execution_failed",
                category=ToolErrorCategory.EXECUTION,
                message="Tool execution failed safely",
                retryable=False,
            )
        ),
        duration_ms=0,
    )


def test_runtime_completes_with_exact_state_and_event_history() -> None:
    base = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
    timestamps = [base + timedelta(seconds=index) for index in range(14)]
    trace = Trace()

    outcome = make_runtime(trace, clock=SequenceClock(timestamps)).run(
        Task(request=SUPPORTED_REQUEST)
    )

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert outcome.task.state is RuntimeState.COMPLETED
    assert outcome.task.state_history == (
        RuntimeState.RECEIVED,
        RuntimeState.CONTEXT_BUILDING,
        RuntimeState.PLANNING,
        RuntimeState.POLICY_CHECK,
        RuntimeState.WAITING_FOR_APPROVAL,
        RuntimeState.EXECUTING,
        RuntimeState.VERIFYING,
        RuntimeState.COMPLETED,
    )
    assert trace.calls == [
        "context",
        "planner",
        "policy",
        "executor",
        "tool",
        "verifier",
    ]
    assert [(event.kind, event.state, event.component) for event in outcome.events] == [
        (LifecycleEventKind.STATE_ENTERED, RuntimeState.RECEIVED, None),
        (LifecycleEventKind.STATE_ENTERED, RuntimeState.CONTEXT_BUILDING, None),
        (
            LifecycleEventKind.COMPONENT_COMPLETED,
            RuntimeState.CONTEXT_BUILDING,
            RuntimeComponent.CONTEXT_BUILDER,
        ),
        (LifecycleEventKind.STATE_ENTERED, RuntimeState.PLANNING, None),
        (
            LifecycleEventKind.COMPONENT_COMPLETED,
            RuntimeState.PLANNING,
            RuntimeComponent.PLANNER,
        ),
        (LifecycleEventKind.STATE_ENTERED, RuntimeState.POLICY_CHECK, None),
        (
            LifecycleEventKind.COMPONENT_COMPLETED,
            RuntimeState.POLICY_CHECK,
            RuntimeComponent.POLICY,
        ),
        (LifecycleEventKind.STATE_ENTERED, RuntimeState.WAITING_FOR_APPROVAL, None),
        (
            LifecycleEventKind.APPROVAL_DECISION_RECORDED,
            RuntimeState.WAITING_FOR_APPROVAL,
            None,
        ),
        (LifecycleEventKind.STATE_ENTERED, RuntimeState.EXECUTING, None),
        (
            LifecycleEventKind.COMPONENT_COMPLETED,
            RuntimeState.EXECUTING,
            RuntimeComponent.EXECUTOR,
        ),
        (LifecycleEventKind.STATE_ENTERED, RuntimeState.VERIFYING, None),
        (
            LifecycleEventKind.COMPONENT_COMPLETED,
            RuntimeState.VERIFYING,
            RuntimeComponent.VERIFIER,
        ),
        (LifecycleEventKind.STATE_ENTERED, RuntimeState.COMPLETED, None),
    ]
    assert outcome.events[8].reason_code == "not_required"
    assert not any(event.kind is LifecycleEventKind.PAUSED for event in outcome.events)
    assert [event.sequence for event in outcome.events] == list(range(14))
    assert [event.occurred_at for event in outcome.events] == timestamps
    assert len(outcome.results) == 1


@pytest.mark.parametrize(
    ("stage", "error", "component", "code", "expected_calls", "expected_history"),
    [
        (
            "context",
            InvalidTaskError(SENSITIVE_MARKER),
            RuntimeComponent.CONTEXT_BUILDER,
            "context_builder_failure",
            ["context"],
            (
                RuntimeState.RECEIVED,
                RuntimeState.CONTEXT_BUILDING,
                RuntimeState.FAILED,
            ),
        ),
        (
            "planner",
            UnsupportedTaskError(SENSITIVE_MARKER),
            RuntimeComponent.PLANNER,
            "unsupported_task",
            ["context", "planner"],
            (
                RuntimeState.RECEIVED,
                RuntimeState.CONTEXT_BUILDING,
                RuntimeState.PLANNING,
                RuntimeState.FAILED,
            ),
        ),
        (
            "policy",
            PolicyDeniedError(SENSITIVE_MARKER),
            RuntimeComponent.POLICY,
            "policy_denied",
            ["context", "planner", "policy"],
            (
                RuntimeState.RECEIVED,
                RuntimeState.CONTEXT_BUILDING,
                RuntimeState.PLANNING,
                RuntimeState.POLICY_CHECK,
                RuntimeState.FAILED,
            ),
        ),
        (
            "executor",
            ToolExecutionError(SENSITIVE_MARKER),
            RuntimeComponent.EXECUTOR,
            "tool_execution",
            ["context", "planner", "policy", "executor"],
            (
                RuntimeState.RECEIVED,
                RuntimeState.CONTEXT_BUILDING,
                RuntimeState.PLANNING,
                RuntimeState.POLICY_CHECK,
                RuntimeState.WAITING_FOR_APPROVAL,
                RuntimeState.EXECUTING,
                RuntimeState.FAILED,
            ),
        ),
        (
            "verifier",
            VerificationError(SENSITIVE_MARKER),
            RuntimeComponent.VERIFIER,
            "verification",
            ["context", "planner", "policy", "executor", "tool", "verifier"],
            (
                RuntimeState.RECEIVED,
                RuntimeState.CONTEXT_BUILDING,
                RuntimeState.PLANNING,
                RuntimeState.POLICY_CHECK,
                RuntimeState.WAITING_FOR_APPROVAL,
                RuntimeState.EXECUTING,
                RuntimeState.VERIFYING,
                RuntimeState.FAILED,
            ),
        ),
    ],
)
def test_known_component_failures_close_once_without_downstream_calls(
    stage: str,
    error: Exception,
    component: RuntimeComponent,
    code: str,
    expected_calls: list[str],
    expected_history: tuple[RuntimeState, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    trace = Trace(fail_at=stage, error=error)

    outcome = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.task.state_history == expected_history
    assert trace.calls == expected_calls
    assert outcome.failure is not None
    assert outcome.failure.component is component
    assert outcome.failure.code == code
    assert outcome.task.state_history.count(RuntimeState.FAILED) == 1
    failed_events = [event for event in outcome.events if event.kind is LifecycleEventKind.FAILED]
    assert len(failed_events) == 1
    assert failed_events[0] is outcome.events[-1]
    assert SENSITIVE_MARKER not in outcome.model_dump_json()
    assert SENSITIVE_MARKER not in caplog.text


@pytest.mark.parametrize(
    ("stage", "component", "code"),
    [
        ("context", RuntimeComponent.CONTEXT_BUILDER, "context_builder_failure"),
        ("planner", RuntimeComponent.PLANNER, "planner_failure"),
        ("policy", RuntimeComponent.POLICY, "policy_failure"),
        ("executor", RuntimeComponent.EXECUTOR, "executor_failure"),
        ("verifier", RuntimeComponent.VERIFIER, "verifier_failure"),
    ],
)
def test_unexpected_component_failures_are_redacted(
    stage: str,
    component: RuntimeComponent,
    code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    trace = Trace(fail_at=stage, error=RuntimeError(SENSITIVE_MARKER))

    outcome = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is component
    assert outcome.failure.code == code
    assert SENSITIVE_MARKER not in outcome.model_dump_json()
    assert SENSITIVE_MARKER not in caplog.text


@pytest.mark.parametrize(
    ("stage", "error", "expected_code"),
    [
        ("planner", UnsupportedTaskError("safe"), "unsupported_task"),
        ("planner", PlanMismatchError("safe"), "plan_mismatch"),
        ("policy", PolicyDeniedError("safe"), "policy_denied"),
        ("executor", ToolExecutionError("safe"), "tool_execution"),
        ("verifier", VerificationError("safe"), "verification"),
    ],
)
def test_exception_instance_cannot_override_stable_failure_code(
    stage: str,
    error: Exception,
    expected_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error.__dict__["code"] = SENSITIVE_MARKER.lower()
    caplog.set_level(logging.INFO)

    outcome = make_runtime(Trace(fail_at=stage, error=error)).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.failure is not None
    assert outcome.failure.code == expected_code
    assert SENSITIVE_MARKER.lower() not in outcome.model_dump_json()
    assert SENSITIVE_MARKER.lower() not in caplog.text


def test_non_none_policy_return_fails_closed_before_tool() -> None:
    class InvalidReturnPolicy(PolicyEngine):
        def check(
            self,
            plan: ExecutionPlan,
            catalog: Mapping[ToolKey, ToolMetadata],
        ) -> None:
            del plan, catalog
            return False  # type: ignore[return-value]

    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    executor = CountingExecutor()

    outcome = RuntimeEngine(
        policy=InvalidReturnPolicy(),
        executor=executor,
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "policy_denied"
    assert executor.calls == 0


def test_falsey_injected_policy_is_not_replaced_by_default() -> None:
    class FalseyDenyPolicy(PolicyEngine):
        def __init__(self) -> None:
            self.calls = 0

        def __bool__(self) -> bool:
            return False

        def check(
            self,
            plan: ExecutionPlan,
            catalog: Mapping[ToolKey, ToolMetadata],
        ) -> None:
            del plan, catalog
            self.calls += 1
            raise PolicyDeniedError("explicit deny")

    policy = FalseyDenyPolicy()

    outcome = RuntimeEngine(policy=policy).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "policy_denied"
    assert policy.calls == 1


def test_non_none_verifier_return_cannot_complete() -> None:
    class InvalidReturnVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
        ) -> None:
            del plan, results
            return False  # type: ignore[return-value]

    outcome = RuntimeEngine(verifier=InvalidReturnVerifier()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.task.state is RuntimeState.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "verification"


def test_executor_tuple_subclass_cannot_escape_through_magic_methods() -> None:
    class ExitingResults(tuple[object, ...]):
        def __len__(self) -> int:
            raise SystemExit(SENSITIVE_MARKER)

    class UntrustedExecutor(Executor):
        def __init__(self) -> None:
            pass

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            return cast(tuple[ToolResult[SystemStatus], ...], ExitingResults())

    outcome = RuntimeEngine(executor=UntrustedExecutor()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "tool_execution"
    assert outcome.failure.component is RuntimeComponent.EXECUTOR
    assert SENSITIVE_MARKER not in outcome.model_dump_json()


def test_structured_tool_failure_stops_before_verifier_and_is_preserved() -> None:
    trace = Trace()

    class StructuredFailureExecutor(Executor):
        def __init__(self) -> None:
            pass

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            trace.record("executor")
            trace.record("tool")
            return (make_structured_result(plan, success=False),)

    outcome = make_runtime(trace, executor=StructuredFailureExecutor()).run(
        Task(request=SUPPORTED_REQUEST)
    )

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.EXECUTOR
    assert outcome.failure.code == "tool_execution"
    assert len(outcome.results) == 1
    assert outcome.results[0].success is False
    assert outcome.results[0].error is not None
    assert outcome.results[0].error.code == "tool_execution_failed"
    assert trace.calls == ["context", "planner", "policy", "executor", "tool"]
    assert RuntimeState.VERIFYING not in outcome.task.state_history


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("arguments_hash", "d" * 64),
        (
            "target",
            TargetReference(
                target_id="local-mock",
                resource_type="other_resource",
                resource_id="local-mock",
            ),
        ),
    ],
)
def test_runtime_rejects_executor_evidence_not_bound_to_plan(
    field: str,
    value: object,
) -> None:
    trace = Trace()

    class MismatchedExecutor(Executor):
        def __init__(self) -> None:
            pass

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            trace.record("executor")
            trace.record("tool")
            result = make_structured_result(plan, success=True)
            return (result.model_copy(update={field: value}),)

    outcome = make_runtime(trace, executor=MismatchedExecutor()).run(
        Task(request=SUPPORTED_REQUEST)
    )

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.EXECUTOR
    assert outcome.failure.code == "tool_execution"
    assert outcome.results == ()
    assert trace.calls == ["context", "planner", "policy", "executor", "tool"]
    assert RuntimeState.VERIFYING not in outcome.task.state_history


@pytest.mark.parametrize("risk_label", ["L2", "L3"])
def test_approval_required_pauses_without_execution(risk_label: str) -> None:
    trace = Trace(
        fail_at="policy",
        error=ApprovalRequiredError(f"{risk_label} approval required"),
    )
    runtime = make_runtime(trace)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
    assert outcome.task.state is RuntimeState.WAITING_FOR_APPROVAL
    assert outcome.task.state_history[-2:] == (
        RuntimeState.POLICY_CHECK,
        RuntimeState.WAITING_FOR_APPROVAL,
    )
    assert trace.calls == ["context", "planner", "policy"]
    assert outcome.results == ()
    assert outcome.failure is None
    assert outcome.events[-1].kind is LifecycleEventKind.PAUSED
    assert outcome.events[-1].reason_code == "approval_required"
    assert not any(
        event.kind is LifecycleEventKind.APPROVAL_DECISION_RECORDED for event in outcome.events
    )

    with pytest.raises(ApprovalResumeUnavailableError):
        runtime.run(outcome.task)
    assert trace.calls == ["context", "planner", "policy"]


def test_human_rejection_closes_paused_outcome_without_tool_call() -> None:
    trace = Trace(
        fail_at="policy",
        error=ApprovalRequiredError("approval required"),
    )
    runtime = make_runtime(trace)
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))

    rejected = runtime.reject(paused)

    assert rejected.status is RuntimeOutcomeStatus.FAILED
    assert rejected.task.state_history[-2:] == (
        RuntimeState.WAITING_FOR_APPROVAL,
        RuntimeState.FAILED,
    )
    assert rejected.failure is not None
    assert rejected.failure.code == "human_rejected"
    assert rejected.failure.component is RuntimeComponent.RUNTIME
    assert rejected.events[:-2] == paused.events
    assert rejected.events[-2].kind is LifecycleEventKind.STATE_ENTERED
    assert rejected.events[-1].kind is LifecycleEventKind.REJECTED
    assert trace.calls == ["context", "planner", "policy"]


def test_reject_only_accepts_valid_waiting_outcome() -> None:
    completed = RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))
    trace = Trace(
        fail_at="policy",
        error=ApprovalRequiredError("approval required"),
    )
    paused = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))
    assert paused.plan is not None
    second = paused.plan.steps[0].model_copy(update={"step_id": "second-status"})
    multi_step_plan = paused.plan.model_copy(update={"steps": (*paused.plan.steps, second)})
    forged_multistep = paused.model_copy(update={"plan": multi_step_plan})

    with pytest.raises(TerminalStateMutationError):
        RuntimeEngine().reject(completed)
    with pytest.raises(InvalidRuntimeOutcomeError):
        RuntimeEngine().reject(cast(RuntimeOutcome, object()))
    with pytest.raises(InvalidRuntimeOutcomeError):
        RuntimeEngine().reject(forged_multistep)


@pytest.mark.parametrize(
    "identifier_location",
    ["task", "plan_id", "plan_task_id", "event"],
)
def test_reject_rejects_uuid_subclasses_without_logging_or_leaking(
    identifier_location: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trace = Trace(
        fail_at="policy",
        error=ApprovalRequiredError("approval required"),
    )
    paused = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))
    assert paused.plan is not None
    evil_task_id = ExplodingUUID(bytes=paused.task.task_id.bytes)
    evil_plan_id = ExplodingUUID(bytes=paused.plan.plan_id.bytes)
    caplog.clear()
    caplog.set_level(logging.INFO)

    if identifier_location == "task":
        forged = paused.model_copy(
            update={"task": paused.task.model_copy(update={"task_id": evil_task_id})}
        )
    elif identifier_location == "plan_id":
        forged = paused.model_copy(
            update={"plan": paused.plan.model_copy(update={"plan_id": evil_plan_id})}
        )
    elif identifier_location == "plan_task_id":
        forged = paused.model_copy(
            update={"plan": paused.plan.model_copy(update={"task_id": evil_task_id})}
        )
    else:
        forged_event = paused.events[0].model_copy(update={"task_id": evil_task_id})
        forged = paused.model_copy(update={"events": (forged_event, *paused.events[1:])})

    with pytest.raises(InvalidRuntimeOutcomeError):
        RuntimeEngine().reject(forged)

    assert SENSITIVE_MARKER not in caplog.text


def test_l1_policy_denial_returns_failed_without_execution() -> None:
    trace = Trace(
        fail_at="policy",
        error=PolicyDeniedError("L1 denied"),
    )

    outcome = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "policy_denied"
    assert trace.calls == ["context", "planner", "policy"]


def test_unsupported_task_and_malformed_plan_return_planner_failures() -> None:
    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    executor = CountingExecutor()
    unsupported = RuntimeEngine(executor=executor).run(Task(request="unsupported"))

    class EmptyPlanPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            return super().create_plan(context, metadata).model_copy(update={"steps": ()})

    malformed = RuntimeEngine(
        planner=EmptyPlanPlanner(),
        executor=executor,
    ).run(Task(request=SUPPORTED_REQUEST))

    assert unsupported.failure is not None
    assert unsupported.failure.code == "unsupported_task"
    assert malformed.failure is not None
    assert malformed.failure.code == "plan_mismatch"
    assert executor.calls == 0


def test_phase_one_rejects_multistep_plan_before_policy_or_tool() -> None:
    policy_calls = 0

    class MultiStepPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            second = plan.steps[0].model_copy(update={"step_id": "second-status"})
            return plan.model_copy(update={"steps": (*plan.steps, second)})

    class CountingPolicy(PolicyEngine):
        def check(
            self,
            plan: ExecutionPlan,
            catalog: Mapping[ToolKey, ToolMetadata],
        ) -> None:
            nonlocal policy_calls
            policy_calls += 1
            return super().check(plan, catalog)

    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    executor = CountingExecutor()
    outcome = RuntimeEngine(
        planner=MultiStepPlanner(),
        policy=CountingPolicy(),
        executor=executor,
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "plan_mismatch"
    assert outcome.plan is None
    assert policy_calls == 0
    assert executor.calls == 0


def test_uuid_subclass_plan_fails_before_policy_or_tool_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UntrustedIdentifierPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            return plan.model_copy(update={"plan_id": ExplodingUUID(bytes=plan.plan_id.bytes)})

    trace = Trace()
    caplog.set_level(logging.INFO)
    registry = build_default_registry()

    outcome = RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=UntrustedIdentifierPlanner(),
        policy=RecordingPolicy(trace),
        executor=RecordingExecutor(trace, ToolGateway(registry, clock=lambda: 0)),
        verifier=RecordingVerifier(trace),
        registry=registry,
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "plan_mismatch"
    assert outcome.plan is None
    assert trace.calls == ["context"]
    assert SENSITIVE_MARKER not in outcome.model_dump_json()
    assert SENSITIVE_MARKER not in caplog.text


@pytest.mark.parametrize(
    "changed_field",
    ["tool_id", "contract_hash", "implementation_hash"],
)
def test_untrusted_planned_identity_is_not_returned_or_logged(
    changed_field: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UntrustedPlanPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            step = plan.steps[0].model_copy(update={changed_field: SENSITIVE_MARKER})
            return plan.model_copy(update={"steps": (step,)})

    caplog.set_level(logging.INFO)

    outcome = RuntimeEngine(planner=UntrustedPlanPlanner()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "plan_mismatch"
    assert outcome.plan is None
    assert SENSITIVE_MARKER not in outcome.model_dump_json()
    assert SENSITIVE_MARKER not in caplog.text


def test_runtime_rejects_invalid_input_before_recording_or_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forged_task = Task(request=SUPPORTED_REQUEST).model_copy(update={"user": SENSITIVE_MARKER})
    unsealed_registry = ToolRegistry()
    empty_registry = ToolRegistry()
    empty_registry.freeze()
    caplog.set_level(logging.INFO)

    with pytest.raises(InvalidTaskError):
        RuntimeEngine().run(forged_task)
    with pytest.raises(PolicyDeniedError):
        RuntimeEngine(registry=unsealed_registry)
    with pytest.raises(PolicyDeniedError):
        RuntimeEngine(registry=empty_registry)

    assert SENSITIVE_MARKER not in caplog.text


def test_runtime_rejects_task_uuid_subclass_before_lifecycle_or_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = Task(request=SUPPORTED_REQUEST)
    forged = task.model_copy(update={"task_id": ExplodingUUID(bytes=uuid4().bytes)})
    caplog.set_level(logging.INFO)

    with pytest.raises(InvalidTaskError):
        RuntimeEngine().run(forged)

    assert caplog.records == []
    assert SENSITIVE_MARKER not in caplog.text


def test_runtime_rejects_task_model_subclass_before_baseexception_can_escape() -> None:
    class ExitingTask(Task):
        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            raise SystemExit(SENSITIVE_MARKER)

    with pytest.raises(InvalidTaskError) as caught:
        RuntimeEngine().run(ExitingTask(request=SUPPORTED_REQUEST))

    assert SENSITIVE_MARKER not in str(caught.value)
    assert caught.value.__cause__ is None


def test_reject_rejects_outcome_subclass_before_baseexception_can_escape() -> None:
    trace = Trace(
        fail_at="policy",
        error=ApprovalRequiredError("approval required"),
    )
    paused = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))

    class ExitingOutcome(RuntimeOutcome):
        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            raise SystemExit(SENSITIVE_MARKER)

    forged = ExitingOutcome.model_validate(
        paused.model_dump(mode="python"),
        strict=True,
    )

    with pytest.raises(InvalidRuntimeOutcomeError) as caught:
        RuntimeEngine().reject(forged)

    assert SENSITIVE_MARKER not in str(caught.value)
    assert caught.value.__cause__ is None


def test_reject_rejects_untrusted_event_timezone_without_calling_it() -> None:
    class ExitingTimezone(tzinfo):
        def __init__(self) -> None:
            self.calls = 0

        def utcoffset(self, value: datetime | None) -> timedelta:
            del value
            self.calls += 1
            raise SystemExit(SENSITIVE_MARKER)

        def dst(self, value: datetime | None) -> timedelta:
            del value
            return timedelta(0)

        def tzname(self, value: datetime | None) -> str:
            del value
            return "untrusted-test"

    trace = Trace(
        fail_at="policy",
        error=ApprovalRequiredError("approval required"),
    )
    paused = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))
    untrusted_timezone = ExitingTimezone()
    untrusted_timestamp = datetime(2026, 7, 25, 8, 0, tzinfo=untrusted_timezone)
    forged_event = paused.events[0].model_copy(update={"occurred_at": untrusted_timestamp})
    forged = paused.model_copy(update={"events": (forged_event, *paused.events[1:])})

    with pytest.raises(InvalidRuntimeOutcomeError) as caught:
        RuntimeEngine().reject(forged)

    assert untrusted_timezone.calls == 0
    assert SENSITIVE_MARKER not in str(caught.value)
    assert caught.value.__cause__ is None


def test_runtime_rejects_nonfresh_and_terminal_tasks() -> None:
    intermediate = Task(
        request=SUPPORTED_REQUEST,
        state=RuntimeState.PLANNING,
        state_history=(
            RuntimeState.RECEIVED,
            RuntimeState.CONTEXT_BUILDING,
            RuntimeState.PLANNING,
        ),
    )
    completed = RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))
    failed = RuntimeEngine().run(Task(request="unsupported"))

    with pytest.raises(InvalidStateTransitionError):
        RuntimeEngine().run(intermediate)
    with pytest.raises(TerminalStateMutationError):
        RuntimeEngine().run(completed.task)
    with pytest.raises(TerminalStateMutationError):
        RuntimeEngine().run(failed.task)


@pytest.mark.parametrize(
    "bad_clock",
    [
        SequenceClock([datetime(2026, 7, 25, 8, 0)]),
        SequenceClock(
            [
                datetime(
                    2026,
                    7,
                    25,
                    8,
                    0,
                    tzinfo=timezone(timedelta(hours=8)),
                )
            ]
        ),
    ],
)
def test_runtime_rejects_non_utc_clocks_before_components(
    bad_clock: SequenceClock,
) -> None:
    trace = Trace()

    with pytest.raises(InvalidClockError):
        make_runtime(trace, clock=bad_clock).run(Task(request=SUPPORTED_REQUEST))
    assert trace.calls == []


def test_clock_datetime_hooks_cannot_raise_baseexception_before_lifecycle() -> None:
    class ExitingTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta:
            del value
            raise SystemExit(SENSITIVE_MARKER)

        def dst(self, value: datetime | None) -> timedelta:
            del value
            return timedelta(0)

        def tzname(self, value: datetime | None) -> str:
            del value
            return "untrusted-test"

    timestamp = datetime(2026, 7, 25, 8, 0, tzinfo=ExitingTimezone())

    with pytest.raises(InvalidClockError) as caught:
        RuntimeEngine(clock=lambda: timestamp).run(Task(request=SUPPORTED_REQUEST))

    assert SENSITIVE_MARKER not in str(caught.value)
    assert caught.value.__cause__ is None


def test_clock_datetime_hooks_cannot_raise_baseexception_after_tool() -> None:
    class ExitingTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta:
            del value
            raise SystemExit(SENSITIVE_MARKER)

        def dst(self, value: datetime | None) -> timedelta:
            del value
            return timedelta(0)

        def tzname(self, value: datetime | None) -> str:
            del value
            return "untrusted-test"

    untrusted_timestamp = datetime(
        2026,
        7,
        25,
        8,
        0,
        8,
        tzinfo=ExitingTimezone(),
    )
    trace = Trace()

    outcome = make_runtime(
        trace,
        clock=SequenceClock([*clock_values(10), untrusted_timestamp]),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "invalid_clock"
    assert trace.calls == ["context", "planner", "policy", "executor", "tool"]
    assert len(outcome.results) == 1
    assert SENSITIVE_MARKER not in outcome.model_dump_json()


def test_runtime_rejects_failing_and_backward_clocks() -> None:
    trace = Trace()

    def failing_clock() -> datetime:
        raise RuntimeError(SENSITIVE_MARKER)

    with pytest.raises(InvalidClockError) as caught:
        RuntimeEngine(clock=failing_clock).run(Task(request=SUPPORTED_REQUEST))
    assert SENSITIVE_MARKER not in str(caught.value)

    later = datetime(2026, 7, 25, 8, 0, 1, tzinfo=UTC)
    earlier = later - timedelta(seconds=1)
    outcome = make_runtime(
        trace,
        clock=SequenceClock([later, earlier]),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "invalid_clock"
    assert outcome.failure.component is RuntimeComponent.RUNTIME
    assert outcome.task.state_history == (
        RuntimeState.RECEIVED,
        RuntimeState.FAILED,
    )
    assert trace.calls == []


def test_internal_event_shape_error_is_not_misclassified_as_clock_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def contradictory_event(**payload: object) -> LifecycleEvent:
        payload["component"] = RuntimeComponent.RUNTIME
        return LifecycleEvent.model_validate(payload)

    monkeypatch.setattr(
        "ai_server.runtime.engine.LifecycleEvent",
        contradictory_event,
    )

    with pytest.raises(InvalidRuntimeOutcomeError):
        RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))


def test_clock_timestamp_is_normalized_before_event_validation_and_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_TZ_MARKER"
    stateful_timezone = StatefulTimezone(valid_reads=1, marker=marker)
    first = datetime(2026, 7, 25, 8, 0, tzinfo=stateful_timezone)
    remaining = clock_values(13)
    caplog.set_level(logging.INFO)

    outcome = RuntimeEngine(clock=SequenceClock([first, *remaining])).run(
        Task(request=SUPPORTED_REQUEST)
    )

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert stateful_timezone.calls == 1
    assert all(event.occurred_at.tzinfo is UTC for event in outcome.events)
    assert marker not in caplog.text


def test_stateful_timezone_failure_after_tool_returns_safe_failed_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_LATE_TZ_MARKER"
    stateful_timezone = StatefulTimezone(valid_reads=10, marker=marker)
    timestamps = [
        datetime(2026, 7, 25, 8, 0, index, tzinfo=stateful_timezone) for index in range(14)
    ]
    trace = Trace()
    caplog.set_level(logging.INFO)

    outcome = make_runtime(
        trace,
        clock=SequenceClock(timestamps),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "invalid_clock"
    assert trace.calls == ["context", "planner", "policy", "executor", "tool"]
    assert len(outcome.results) == 1
    assert all(event.occurred_at.tzinfo is UTC for event in outcome.events)
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


@pytest.mark.parametrize(
    ("valid_clock_reads", "expected_calls", "failed_from", "result_count"),
    [
        (2, ["context"], RuntimeState.CONTEXT_BUILDING, 0),
        (4, ["context", "planner"], RuntimeState.PLANNING, 0),
        (6, ["context", "planner", "policy"], RuntimeState.POLICY_CHECK, 0),
        (7, ["context", "planner", "policy"], RuntimeState.POLICY_CHECK, 0),
        (8, ["context", "planner", "policy"], RuntimeState.WAITING_FOR_APPROVAL, 0),
        (9, ["context", "planner", "policy"], RuntimeState.WAITING_FOR_APPROVAL, 0),
        (
            10,
            ["context", "planner", "policy", "executor", "tool"],
            RuntimeState.EXECUTING,
            1,
        ),
        (
            12,
            ["context", "planner", "policy", "executor", "tool", "verifier"],
            RuntimeState.VERIFYING,
            1,
        ),
        (
            13,
            ["context", "planner", "policy", "executor", "tool", "verifier"],
            RuntimeState.VERIFYING,
            1,
        ),
    ],
)
def test_late_clock_failure_returns_structured_failed_outcome(
    valid_clock_reads: int,
    expected_calls: list[str],
    failed_from: RuntimeState,
    result_count: int,
) -> None:
    trace = Trace()

    outcome = make_runtime(
        trace,
        clock=SequenceClock(clock_values(valid_clock_reads)),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "invalid_clock"
    assert outcome.failure.component is RuntimeComponent.RUNTIME
    assert outcome.task.state_history[-2:] == (failed_from, RuntimeState.FAILED)
    assert outcome.events[-1].kind is LifecycleEventKind.FAILED
    assert outcome.events[-1].reason_code == "invalid_clock"
    assert len(outcome.results) == result_count
    assert trace.calls == expected_calls
    if failed_from is RuntimeState.WAITING_FOR_APPROVAL:
        assert (
            sum(
                event.kind is LifecycleEventKind.APPROVAL_DECISION_RECORDED
                for event in outcome.events
            )
            == 1
        )
    assert RuntimeOutcome.model_validate_json(outcome.model_dump_json()) == outcome


def test_clock_failure_after_completed_stage_preserves_completion_evidence() -> None:
    trace = Trace()

    outcome = make_runtime(
        trace,
        clock=SequenceClock(clock_values(3)),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.RUNTIME
    assert outcome.task.state_history[-2:] == (
        RuntimeState.CONTEXT_BUILDING,
        RuntimeState.FAILED,
    )
    assert any(
        event.kind is LifecycleEventKind.COMPONENT_COMPLETED
        and event.component is RuntimeComponent.CONTEXT_BUILDER
        for event in outcome.events
    )


@pytest.mark.parametrize("valid_clock_reads", [2, 3])
def test_clock_failure_while_recording_component_failure_still_closes(
    valid_clock_reads: int,
) -> None:
    trace = Trace(fail_at="context", error=RuntimeError(SENSITIVE_MARKER))

    outcome = make_runtime(
        trace,
        clock=SequenceClock(clock_values(valid_clock_reads)),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "invalid_clock"
    assert outcome.failure.component is RuntimeComponent.RUNTIME
    assert outcome.task.state_history.count(RuntimeState.FAILED) == 1
    assert SENSITIVE_MARKER not in outcome.model_dump_json()


def test_clock_failure_while_recording_pause_still_closes_without_tool() -> None:
    trace = Trace(
        fail_at="policy",
        error=ApprovalRequiredError("approval required"),
    )

    outcome = make_runtime(
        trace,
        clock=SequenceClock(clock_values(8)),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "invalid_clock"
    assert outcome.task.state_history[-2:] == (
        RuntimeState.WAITING_FOR_APPROVAL,
        RuntimeState.FAILED,
    )
    assert any(event.kind is LifecycleEventKind.PAUSED for event in outcome.events)
    assert trace.calls == ["context", "planner", "policy"]


@pytest.mark.parametrize("valid_clock_reads", [9, 10])
def test_clock_failure_during_rejection_returns_structured_failed_outcome(
    valid_clock_reads: int,
) -> None:
    trace = Trace(
        fail_at="policy",
        error=ApprovalRequiredError("approval required"),
    )
    runtime = make_runtime(
        trace,
        clock=SequenceClock(clock_values(valid_clock_reads)),
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))

    rejected = runtime.reject(paused)

    assert rejected.status is RuntimeOutcomeStatus.FAILED
    assert rejected.failure is not None
    assert rejected.failure.code == "invalid_clock"
    assert rejected.failure.component is RuntimeComponent.RUNTIME
    assert rejected.task.state is RuntimeState.FAILED
    assert rejected.events[-1].kind is LifecycleEventKind.FAILED
    assert trace.calls == ["context", "planner", "policy"]


def test_equal_utc_timestamps_are_allowed_because_sequence_is_authoritative() -> None:
    timestamp = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)

    outcome = RuntimeEngine(clock=lambda: timestamp).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert {event.occurred_at for event in outcome.events} == {timestamp}


def test_logging_failure_does_not_corrupt_authoritative_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_logger(message: object) -> None:
        del message
        raise RuntimeError(SENSITIVE_MARKER)

    monkeypatch.setattr("ai_server.runtime.engine.logger.info", broken_logger)

    outcome = RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert outcome.task.state is RuntimeState.COMPLETED


def test_runtime_runs_do_not_share_events_or_state() -> None:
    runtime = RuntimeEngine()
    first = runtime.run(Task(request=SUPPORTED_REQUEST))
    second = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert first.task.task_id != second.task.task_id
    assert first.events is not second.events
    assert first.task.state_history == second.task.state_history
    assert all(event.task_id == first.task.task_id for event in first.events)
    assert all(event.task_id == second.task.task_id for event in second.events)


def test_runtime_emits_structured_transition_and_execution_audit_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    outcome = make_runtime(Trace()).run(Task(request=SUPPORTED_REQUEST))
    messages = [json.loads(record.message) for record in caplog.records]
    transitions = [
        message for message in messages if message["event"] == "runtime_state_transition"
    ]
    audits = [message for message in messages if message["event"] == "execution_audit"]

    assert len(transitions) == 7
    assert all(message["task_id"] == str(outcome.task.task_id) for message in transitions)
    assert transitions[-1]["to_state"] == "COMPLETED"
    assert audits == [
        {
            "approval_id": None,
            "arguments": {"redacted": True},
            "duration_ms": 0,
            "event": "execution_audit",
            "operator": "local-user",
            "plan_id": audits[0]["plan_id"],
            "result": "success",
            "target": "local-mock",
            "task_id": str(outcome.task.task_id),
            "tool": "get_system_status",
            "tool_version": "1.0.0",
            "user": "local-user",
            "verification": "passed",
        }
    ]


def test_failed_executor_and_verifier_emit_safe_audits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    executor_failure = make_runtime(
        Trace(fail_at="executor", error=ToolExecutionError(SENSITIVE_MARKER))
    ).run(Task(request=SUPPORTED_REQUEST))
    verifier_failure = make_runtime(
        Trace(fail_at="verifier", error=VerificationError(SENSITIVE_MARKER))
    ).run(Task(request=SUPPORTED_REQUEST))

    messages = [json.loads(record.message) for record in caplog.records]
    audits = [message for message in messages if message["event"] == "execution_audit"]

    assert executor_failure.task.state is RuntimeState.FAILED
    assert verifier_failure.task.state is RuntimeState.FAILED
    assert [audit["verification"] for audit in audits] == ["not_run", "failed"]
    assert [audit["result"] for audit in audits] == ["execution_failed", "success"]
    assert SENSITIVE_MARKER not in caplog.text
