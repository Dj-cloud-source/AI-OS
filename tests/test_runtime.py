import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from inspect import signature
from types import MappingProxyType
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_server.approval.errors import ApprovalStateError
from ai_server.context.builder import ContextBuilder
from ai_server.executor.errors import ExecutionAuthorizationError
from ai_server.executor.service import Executor as GovernedExecutor
from ai_server.models.approval import ApprovalAuditEventKind
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.executor import (
    DispatchStatus,
    EffectDisposition,
    ExecutionAttemptAuthorization,
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionNextState,
    ExecutionReport,
    ExecutionReportStatus,
    ManualConfirmationChallenge,
    StepExecutionRecord,
)
from ai_server.models.policy import (
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEffect,
    PolicyEvaluationContext,
    PolicyReasonCode,
)
from ai_server.models.runtime import (
    LifecycleEvent,
    LifecycleEventKind,
    RuntimeComponent,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
)
from ai_server.models.system_status import GetSystemStatusArguments, SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import (
    ApprovalBinding,
    ApprovalImplication,
    RiskLevel,
    SideEffectKind,
    TargetReference,
    ToolCall,
    ToolContract,
    ToolError,
    ToolErrorCategory,
    ToolMetadata,
    ToolReference,
    ToolResult,
    ToolSideEffects,
)
from ai_server.models.verification import (
    EqualityCriterion,
    ExpectedStateCriterion,
    VerificationCheckStatus,
    VerificationContext,
    VerificationEffectDisposition,
    VerificationFailureReason,
    VerificationResult,
    VerificationStatus,
)
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine
from ai_server.runtime.engine import RuntimeEngine
from ai_server.runtime.errors import (
    InvalidClockError,
    InvalidRuntimeOutcomeError,
    InvalidStateTransitionError,
    InvalidTaskError,
    PlanMismatchError,
    PolicyDeniedError,
    PolicyEvaluationError,
    TerminalStateMutationError,
    ToolExecutionError,
    UnsupportedTaskError,
    VerificationError,
)
from ai_server.runtime.state import RuntimeState
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.gateway import GatewayDispatchReceipt
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.tools.registry import ToolRegistry
from ai_server.verifier.service import Verifier

SENSITIVE_MARKER = "SENSITIVE_RUNTIME_MARKER"


@dataclass
class Trace:
    calls: list[str] = field(default_factory=list)
    fail_at: str | None = None
    error: BaseException | None = None

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


class MismatchedCriterionPlanner(Planner):
    """Create a valid Plan whose first mandatory criterion cannot match the Mock result."""

    def create_plan(
        self,
        context: RuntimeContext,
        metadata: ToolMetadata,
    ) -> ExecutionPlan:
        plan = super().create_plan(context, metadata)
        first = plan.verification_criteria[0]
        assert type(first) is EqualityCriterion
        mismatch = first.model_copy(update={"expected": "not-mock"})
        return plan.model_copy(
            update={
                "verification_criteria": (
                    mismatch,
                    *plan.verification_criteria[1:],
                )
            }
        )


class SyntheticMutationPlanner(Planner):
    """Build a test-only L2 Mock Action with independent read-only evidence."""

    def __init__(self, expected_state: Literal["running", "stopped"]) -> None:
        self._expected_state = expected_state
        self.action_metadata: ToolMetadata | None = None

    def create_plan(
        self,
        context: RuntimeContext,
        metadata: ToolMetadata,
    ) -> ExecutionPlan:
        action_metadata = self.action_metadata
        assert action_metadata is not None
        action = ExecutionStep(
            step_id="synthetic-mutation",
            role=StepRole.ACTION,
            tool_id=action_metadata.tool_id,
            tool_version=action_metadata.version,
            contract_hash=action_metadata.contract_hash,
            implementation_hash=action_metadata.implementation_hash,
            arguments=GetSystemStatusArguments(),
            reason="Exercise mutation effect closure using only the local Mock payload.",
            impact="Synthetic L2 effect metadata; no real state is changed.",
            verification="Require an independent read-only service-state observation.",
            recovery="No automatic recovery is permitted in this synthetic test.",
        )
        verify = ExecutionStep(
            step_id="verify-synthetic-mutation",
            role=StepRole.VERIFY,
            tool_id=metadata.tool_id,
            tool_version=metadata.version,
            contract_hash=metadata.contract_hash,
            implementation_hash=metadata.implementation_hash,
            arguments=GetSystemStatusArguments(),
            reason="Collect independent local Mock verification evidence.",
            impact="Read-only deterministic Mock observation.",
            verification="Confirm the expected service state.",
            recovery="No rollback is required for the read-only evidence Step.",
        )
        return ExecutionPlan(
            task_id=context.task_id,
            target=context.target,
            steps=(action, verify),
            verification_criteria=(
                ExpectedStateCriterion(
                    criterion_id="mock-api-state",
                    evidence_step_id=verify.step_id,
                    service_name="mock-api",
                    expected_state=self._expected_state,
                ),
            ),
        )


type PolicyEvaluate = Callable[
    [ExecutionPlan, PolicyEvaluationContext],
    PolicyDecision,
]


def replace_policy_evaluate(
    runtime: RuntimeEngine,
    evaluate: PolicyEvaluate,
) -> None:
    """Replace only the private Policy call in a fully constructed test Runtime."""
    runtime._policy.evaluate = evaluate  # type: ignore[assignment]


def install_recording_policy(
    runtime: RuntimeEngine,
    trace: Trace,
    *,
    approval_requirement: PolicyApprovalRequirement | None = None,
) -> None:
    """Wrap the Runtime-owned Policy without reopening constructor injection."""
    trusted_evaluate = runtime._policy.evaluate
    initial_evaluation_pending = True

    def evaluate(
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        nonlocal initial_evaluation_pending
        if initial_evaluation_pending:
            initial_evaluation_pending = False
            trace.record("policy")
        decision = trusted_evaluate(plan, context)
        if approval_requirement is not None:
            steps = tuple(
                step.model_copy(update={"approval_requirement": approval_requirement})
                for step in decision.step_decisions
            )
            decision = decision.model_copy(
                update={
                    "approval_requirement": approval_requirement,
                    "step_decisions": steps,
                }
            )
        return decision

    replace_policy_evaluate(runtime, evaluate)


class RecordingExecutor:
    """Record Runtime-to-Executor phase calls while preserving real authority."""

    def __init__(self, trace: Trace, delegate: GovernedExecutor) -> None:
        self._trace = trace
        self._delegate = delegate

    def begin_attempt(
        self,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        approval_id: UUID | None,
    ) -> ExecutionAttemptAuthorization:
        return self._delegate.begin_attempt(plan, policy_decision, approval_id)

    def execute_actions(
        self,
        authorization: ExecutionAttemptAuthorization,
        confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
    ) -> ExecutionReport:
        self._trace.record("executor")
        self._trace.record("tool")
        return self._delegate.execute_actions(authorization, confirmation_reader)

    def execute_verification(
        self,
        authorization: ExecutionAttemptAuthorization,
    ) -> ExecutionReport:
        self._trace.record("verification_tool")
        return self._delegate.execute_verification(authorization)

    def abort_attempt(
        self,
        authorization: ExecutionAttemptAuthorization,
        *,
        reason_code: str = "attempt_aborted",
    ) -> ExecutionReport:
        return self._delegate.abort_attempt(authorization, reason_code=reason_code)


class Executor:
    """Adapt legacy hostile-Executor fixtures to the Phase 5 report boundary."""

    def __init__(self) -> None:
        self._plans: dict[UUID, ExecutionPlan] = {}
        self._authorizations: dict[UUID, ExecutionAttemptAuthorization] = {}

    def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
        del plan
        return ()

    def begin_attempt(
        self,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        approval_id: UUID | None,
    ) -> ExecutionAttemptAuthorization:
        if not hasattr(self, "_plans"):
            self._plans = {}
            self._authorizations = {}
        attempt_id = uuid4()
        requirement = policy_decision.approval_requirement
        assert requirement is not None
        human = requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
        draft = ExecutionAttemptAuthorization.model_construct(
            execution_attempt_id=attempt_id,
            task_id=plan.task_id,
            plan_id=plan.plan_id,
            plan_digest=canonical_json_sha256(plan),
            policy_decision_hash=canonical_json_sha256(policy_decision),
            approval_requirement=requirement,
            approval_id=approval_id if human else None,
            approval_plan_hash="a" * 64 if human else None,
            approval_record_hash="b" * 64 if human else None,
            approval_expires_at=(datetime(2099, 1, 1, tzinfo=UTC) if human else None),
            content_hash="0" * 64,
        )
        content_hash = canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"content_hash"}, warnings="error")
        )
        authorization = ExecutionAttemptAuthorization.model_validate(
            {
                **draft.model_dump(mode="python", exclude={"content_hash"}),
                "content_hash": content_hash,
            },
            strict=True,
        )
        self._plans[attempt_id] = plan
        self._authorizations[attempt_id] = authorization
        return authorization

    def execute_actions(
        self,
        authorization: ExecutionAttemptAuthorization,
        confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
    ) -> ExecutionReport:
        del confirmation_reader
        plan = self._plans[authorization.execution_attempt_id]
        raw_results = self.execute(plan)
        if type(raw_results) is not tuple:
            return cast(ExecutionReport, raw_results)
        records: list[StepExecutionRecord] = []
        for index, result in enumerate(raw_results):
            step = plan.steps[index]
            records.append(
                StepExecutionRecord(
                    step_index=index,
                    step_id=result.plan_step_id,
                    role=step.role,
                    tool_id=result.tool_id,
                    tool_version=result.tool_version,
                    contract_hash=result.contract_hash,
                    implementation_hash=step.implementation_hash,
                    arguments_hash=result.arguments_hash,
                    target=result.target,
                    invocation_id=result.invocation_id,
                    dispatch_status=DispatchStatus.HANDLER_DISPATCHED,
                    effect_disposition=EffectDisposition.NONE,
                    result=result,
                    failure_code=(
                        result.error.code
                        if not result.success and result.error is not None
                        else None
                    ),
                )
            )
        failed_record = next(
            (record for record in records if record.failure_code is not None),
            None,
        )
        return self._make_report(
            authorization,
            tuple(records),
            failure_code=(failed_record.failure_code if failed_record is not None else None),
            failed_step_index=(failed_record.step_index if failed_record is not None else None),
        )

    def execute_verification(
        self,
        authorization: ExecutionAttemptAuthorization,
    ) -> ExecutionReport:
        return self.execute_actions(authorization)

    def abort_attempt(
        self,
        authorization: ExecutionAttemptAuthorization,
        *,
        reason_code: str = "attempt_aborted",
    ) -> ExecutionReport:
        return self._make_report(
            authorization,
            (),
            failure_code=reason_code,
            failed_step_index=None,
        )

    @staticmethod
    def _make_report(
        authorization: ExecutionAttemptAuthorization,
        records: tuple[StepExecutionRecord, ...],
        *,
        failure_code: str | None,
        failed_step_index: int | None,
    ) -> ExecutionReport:
        events: list[ExecutionEvent] = [
            ExecutionEvent(
                sequence=0,
                kind=ExecutionEventKind.ATTEMPT_AUTHORIZED,
                execution_attempt_id=authorization.execution_attempt_id,
            )
        ]
        for record in records:
            events.append(
                ExecutionEvent(
                    sequence=len(events),
                    kind=ExecutionEventKind.STEP_FINISHED,
                    execution_attempt_id=authorization.execution_attempt_id,
                    step_index=record.step_index,
                    step_id=record.step_id,
                    invocation_id=record.invocation_id,
                    dispatch_status=record.dispatch_status,
                    effect_disposition=record.effect_disposition,
                )
            )
        if failure_code is None:
            events.append(
                ExecutionEvent(
                    sequence=len(events),
                    kind=ExecutionEventKind.ATTEMPT_CLOSED,
                    execution_attempt_id=authorization.execution_attempt_id,
                )
            )
            status = ExecutionReportStatus.READY_FOR_VERIFIER
            next_state = ExecutionNextState.VERIFYING
        else:
            events.append(
                ExecutionEvent(
                    sequence=len(events),
                    kind=ExecutionEventKind.ATTEMPT_FAILED,
                    execution_attempt_id=authorization.execution_attempt_id,
                    reason_code=failure_code,
                )
            )
            events.append(
                ExecutionEvent(
                    sequence=len(events),
                    kind=ExecutionEventKind.ATTEMPT_CLOSED,
                    execution_attempt_id=authorization.execution_attempt_id,
                )
            )
            status = ExecutionReportStatus.FAILED
            next_state = ExecutionNextState.FAILED
        draft = ExecutionReport.model_construct(
            execution_attempt_id=authorization.execution_attempt_id,
            authorization_hash=authorization.content_hash,
            task_id=authorization.task_id,
            plan_id=authorization.plan_id,
            plan_digest=authorization.plan_digest,
            policy_decision_hash=authorization.policy_decision_hash,
            approval_id=authorization.approval_id,
            status=status,
            next_state=next_state,
            records=records,
            events=tuple(events),
            total_duration_ms=0,
            failed_step_index=failed_step_index,
            failure_code=failure_code,
            human_intervention_required=False,
            content_hash="0" * 64,
        )
        content_hash = canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"content_hash"}, warnings="error")
        )
        return ExecutionReport.model_validate(
            {
                **draft.model_dump(mode="python", exclude={"content_hash"}),
                "content_hash": content_hash,
            },
            strict=True,
        )


class RecordingVerifier(Verifier):
    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    def verify(
        self,
        plan: ExecutionPlan,
        results: tuple[ToolResult[SystemStatus], ...],
        context: VerificationContext,
    ) -> VerificationResult:
        self._trace.record("verifier")
        return super().verify(plan, results, context)


class SequenceClock:
    def __init__(self, values: list[datetime]) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class AdjustableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


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
    clock: Callable[[], datetime] | None = None,
    executor: GovernedExecutor | Executor | None = None,
    approval_requirement: PolicyApprovalRequirement | None = None,
) -> RuntimeEngine:
    registry = build_default_registry()
    runtime = RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=RecordingPlanner(trace),
        executor=cast(GovernedExecutor | None, executor),
        verifier=RecordingVerifier(trace),
        registry=registry,
        clock=clock if clock is not None else lambda: datetime.now(UTC),
    )
    if executor is None:
        runtime._executor = cast(
            GovernedExecutor,
            RecordingExecutor(trace, runtime._executor),
        )
    install_recording_policy(
        runtime,
        trace,
        approval_requirement=approval_requirement,
    )
    return runtime


def declare_mock_verification_tool(runtime: RuntimeEngine) -> None:
    """Install a synthetic reviewed self-reference for explicit VERIFY tests."""
    key = ("get_system_status", "1.0.0")
    metadata = runtime._catalog[key]
    verification = metadata.verification.model_copy(
        update={"tools": (ToolReference(tool_id=metadata.tool_id, version=metadata.version),)}
    )
    declared = metadata.model_copy(update={"verification": verification})
    snapshot = MappingProxyType({key: declared})
    runtime._catalog = snapshot
    runtime._policy._metadata = snapshot


def install_synthetic_mutating_mock(runtime: RuntimeEngine) -> ToolMetadata:
    """Add a test-only mutating alias whose handler still returns local Mock data."""
    source_key = ("get_system_status", "1.0.0")
    action_key = ("synthetic_mutating_status", "1.0.0")
    source_entry = runtime._registry._entries[source_key]
    side_effects = ToolSideEffects(
        mutates_remote_state=True,
        kind=SideEffectKind.SERVICE_STATE_CHANGE,
    )
    verification = source_entry.metadata.verification.model_copy(
        update={
            "tools": (
                ToolReference(
                    tool_id=source_entry.metadata.tool_id,
                    version=source_entry.metadata.version,
                ),
            )
        }
    )
    approval = source_entry.contract.approval.model_copy(
        update={
            "implication": ApprovalImplication.EXPLICIT_HUMAN_APPROVAL,
            "binds": (
                ApprovalBinding.PLAN_HASH,
                ApprovalBinding.ARGUMENTS,
                ApprovalBinding.EXPIRATION,
            ),
        }
    )
    output_schema = cast(
        dict[str, object],
        json.loads(json.dumps(source_entry.contract.output_schema)),
    )
    output_properties = cast(dict[str, object], output_schema["properties"])
    tool_id_schema = cast(dict[str, object], output_properties["tool_id"])
    tool_id_schema["const"] = action_key[0]
    contract_document = source_entry.contract.model_dump(mode="python", warnings="error")
    contract_document.update(
        {
            "tool_id": action_key[0],
            "risk_level": RiskLevel.L2,
            "approval": approval,
            "side_effects": side_effects,
            "verification": verification,
            "output_schema": output_schema,
        }
    )
    action_contract = ToolContract.model_validate(contract_document, strict=True)
    action_contract_hash = canonical_json_sha256(action_contract)
    metadata_document = source_entry.metadata.model_dump(mode="python", warnings="error")
    metadata_document.update(
        {
            "tool_id": action_key[0],
            "contract_hash": action_contract_hash,
            "risk_level": RiskLevel.L2,
            "side_effects": side_effects,
            "verification": verification,
        }
    )
    action_metadata = ToolMetadata.model_validate(metadata_document, strict=True)
    action_record = source_entry.record.model_copy(
        update={
            "tool_id": action_key[0],
            "contract_hash": action_contract_hash,
        }
    )
    runtime._registry._entries[action_key] = replace(
        source_entry,
        metadata=action_metadata,
        contract=action_contract,
        record=action_record,
    )
    snapshot = MappingProxyType(
        {
            source_key: source_entry.metadata,
            action_key: action_metadata,
        }
    )
    runtime._catalog = snapshot
    runtime._policy._metadata = snapshot
    runtime._approval._metadata = snapshot
    base_rule = runtime._policy._profile.rules[0]
    action_rule = base_rule.model_copy(
        update={
            "rule_id": "synthetic-mutation-rule",
            "tool_id": action_metadata.tool_id,
            "tool_version": action_metadata.version,
            "contract_hash": action_metadata.contract_hash,
            "implementation_hash": action_metadata.implementation_hash,
            "minimum_approval": PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
        }
    )
    runtime._policy._profile = runtime._policy._profile.model_copy(
        update={"rules": (*runtime._policy._profile.rules, action_rule)}
    )
    runtime._policy._policy_hash = canonical_json_sha256(runtime._policy._profile)
    return action_metadata


def run_synthetic_mutation(
    expected_state: Literal["running", "stopped"],
    *,
    verifier: Verifier | None = None,
) -> tuple[RuntimeEngine, RuntimeOutcome]:
    """Approve and run one test-only mutation against deterministic Mock handlers."""
    planner = SyntheticMutationPlanner(expected_state)
    runtime = RuntimeEngine(planner=planner, verifier=verifier)
    planner.action_metadata = install_synthetic_mutating_mock(runtime)
    waiting = runtime.run(Task(request=SUPPORTED_REQUEST))
    assert waiting.status is RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
    review = runtime.prepare_approval_review(waiting)
    approval = runtime.commit_approval(waiting, review.review_id)
    return runtime, runtime.resume_approved(waiting, approval.approval_id)


def forge_execution_report(
    report: ExecutionReport,
    **updates: object,
) -> ExecutionReport:
    """Rehash an intentionally unvalidated exact report for hostile-boundary tests."""
    draft = report.model_copy(update={**updates, "content_hash": "0" * 64})
    content_hash = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    return report.model_copy(update={**updates, "content_hash": content_hash})


def forge_verification_result(
    result: VerificationResult,
    **updates: object,
) -> VerificationResult:
    """Rehash one structurally valid hostile Verifier result for boundary tests."""
    draft = result.model_copy(update={**updates, "content_hash": "0" * 64})
    document = draft.model_dump(mode="python", warnings="error")
    document["content_hash"] = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    return VerificationResult.model_validate(document, strict=True)


def rehash_execution_authorization(
    authorization: ExecutionAttemptAuthorization,
    **updates: object,
) -> ExecutionAttemptAuthorization:
    """Rebuild one structurally valid authorization with changed bound fields."""
    draft = authorization.model_copy(update={**updates, "content_hash": "0" * 64})
    document = draft.model_dump(mode="python", warnings="error")
    document["content_hash"] = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    return ExecutionAttemptAuthorization.model_validate(document, strict=True)


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
        evidence=(
            {
                "source": "mock",
                "simulated": True,
                "target": "local-mock",
                "hostname": "mock-server",
            }
            if success
            else {}
        ),
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
    clock_reads = [base + timedelta(seconds=index) for index in range(15)]
    trace = Trace()

    outcome = make_runtime(trace, clock=SequenceClock(clock_reads)).run(
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
    assert [event.occurred_at for event in outcome.events] == [
        *clock_reads[:12],
        *clock_reads[13:],
    ]
    assert len(outcome.results) == 1
    assert outcome.plan is not None
    assert outcome.policy_decision is not None
    assert outcome.execution_authorization is not None
    assert outcome.execution_report is not None
    assert outcome.execution_authorization.approval_id is None
    assert (
        outcome.execution_report.execution_attempt_id
        == outcome.execution_authorization.execution_attempt_id
    )
    assert (
        outcome.execution_report.authorization_hash == outcome.execution_authorization.content_hash
    )
    assert outcome.execution_report.results == outcome.results
    assert outcome.policy_decision.task_id == outcome.task.task_id
    assert outcome.policy_decision.plan_id == outcome.plan.plan_id
    assert outcome.policy_decision.operator_id == outcome.task.user
    assert outcome.policy_decision.target == TargetReference(
        target_id=outcome.task.target,
        resource_type="local_system",
        resource_id=outcome.task.target,
    )
    assert outcome.policy_decision.effective_risk is RiskLevel.L0
    assert outcome.verification_result is not None
    assert outcome.verification_result.status is VerificationStatus.PASSED
    assert outcome.verification_result.plan_digest == canonical_json_sha256(outcome.plan)
    assert (
        outcome.verification_result.execution_attempt_id
        == outcome.execution_authorization.execution_attempt_id
    )
    assert (
        outcome.verification_result.execution_report_hash == outcome.execution_report.content_hash
    )
    assert outcome.verification_result.content_hash == canonical_json_sha256(
        outcome.verification_result.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    assert outcome.final_effect_disposition is VerificationEffectDisposition.NONE
    assert outcome.human_intervention_required is False


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
            PolicyEvaluationError(SENSITIVE_MARKER),
            RuntimeComponent.POLICY,
            "policy_evaluation_failed",
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
        ("policy", RuntimeComponent.POLICY, "policy_evaluation_failed"),
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
    ("stage", "component", "code", "expected_calls"),
    [
        (
            "context",
            RuntimeComponent.CONTEXT_BUILDER,
            "context_builder_failure",
            ["context"],
        ),
        (
            "planner",
            RuntimeComponent.PLANNER,
            "planner_failure",
            ["context", "planner"],
        ),
        (
            "executor",
            RuntimeComponent.EXECUTOR,
            "executor_failure",
            ["context", "planner", "policy", "executor"],
        ),
        (
            "verifier",
            RuntimeComponent.VERIFIER,
            "verifier_failure",
            ["context", "planner", "policy", "executor", "tool", "verifier"],
        ),
    ],
)
def test_component_system_exit_is_sanitized_without_downstream_calls(
    stage: str,
    component: RuntimeComponent,
    code: str,
    expected_calls: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = f"SENSITIVE_{stage.upper()}_SYSTEM_EXIT"
    trace = Trace(fail_at=stage, error=SystemExit(marker))
    caplog.set_level(logging.INFO)

    outcome = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is component
    assert outcome.failure.code == code
    assert trace.calls == expected_calls
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


@pytest.mark.parametrize(
    ("stage", "error", "expected_code"),
    [
        ("planner", UnsupportedTaskError("safe"), "unsupported_task"),
        ("planner", PlanMismatchError("safe"), "plan_mismatch"),
        ("policy", PolicyEvaluationError("safe"), "policy_evaluation_failed"),
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
    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    executor = CountingExecutor()
    registry = build_default_registry()
    runtime = RuntimeEngine(
        executor=cast(GovernedExecutor, executor),
        registry=registry,
    )

    def invalid_evaluate(
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        del plan, context
        return cast(PolicyDecision, False)

    replace_policy_evaluate(runtime, invalid_evaluate)
    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "policy_evaluation_failed"
    assert outcome.policy_decision is None
    assert executor.calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool_id", "unknown_tool"),
        ("tool_version", "9.9.9"),
    ],
)
def test_unknown_planned_tool_is_structurally_denied_without_executor(
    field: str,
    value: str,
) -> None:
    class UnknownToolPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            step = plan.steps[0].model_copy(update={field: value})
            return plan.model_copy(update={"steps": (step,)})

    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    executor = CountingExecutor()
    outcome = RuntimeEngine(
        planner=UnknownToolPlanner(),
        executor=cast(GovernedExecutor, executor),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.POLICY
    assert outcome.failure.code == "policy_denied"
    assert outcome.policy_decision is not None
    assert outcome.policy_decision.effect is PolicyEffect.DENY
    assert outcome.policy_decision.reason_code is PolicyReasonCode.UNKNOWN_TOOL
    assert outcome.policy_decision.effective_risk is None
    assert executor.calls == 0
    assert RuntimeState.EXECUTING not in outcome.task.state_history


@pytest.mark.parametrize(
    "field",
    ["contract_hash", "implementation_hash"],
)
def test_plan_integrity_drift_is_structurally_denied_without_executor(
    field: str,
) -> None:
    class IntegrityDriftPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            step = plan.steps[0].model_copy(update={field: "d" * 64})
            return plan.model_copy(update={"steps": (step,)})

    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    executor = CountingExecutor()
    outcome = RuntimeEngine(
        planner=IntegrityDriftPlanner(),
        executor=cast(GovernedExecutor, executor),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.POLICY
    assert outcome.failure.code == "policy_denied"
    assert outcome.policy_decision is not None
    assert outcome.policy_decision.effect is PolicyEffect.DENY
    assert outcome.policy_decision.reason_code is PolicyReasonCode.TOOL_INTEGRITY_MISMATCH
    assert executor.calls == 0
    assert RuntimeState.EXECUTING not in outcome.task.state_history


def test_runtime_constructor_does_not_expose_policy_injection() -> None:
    class ForgedPolicy(PolicyEngine):
        pass

    registry = build_default_registry()
    forged = ForgedPolicy(registry)
    construct = cast(Callable[..., RuntimeEngine], RuntimeEngine)

    assert "policy" not in signature(RuntimeEngine).parameters
    with pytest.raises(TypeError, match="policy"):
        construct(policy=forged, registry=registry)

    runtime = RuntimeEngine(registry=registry)
    assert type(runtime._policy) is PolicyEngine


@pytest.mark.parametrize(
    "mutation",
    [
        "policy_id",
        "policy_version",
        "policy_hash",
        "task_id",
        "plan_id",
        "operator_id",
        "target",
        "step_contract_hash",
        "step_implementation_hash",
        "arguments_hash",
        "risk",
        "approval_mismatch",
        "l1_reason_on_l0",
        "l3_reason_on_l0",
    ],
)
def test_tampered_policy_decision_fails_closed_without_dispatch(
    mutation: str,
) -> None:
    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    registry = build_default_registry()
    executor = CountingExecutor()
    runtime = RuntimeEngine(
        executor=cast(GovernedExecutor, executor),
        registry=registry,
    )
    trusted_evaluate = runtime._policy.evaluate

    def tampering_evaluate(
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        decision = trusted_evaluate(plan, context)
        step = decision.step_decisions[0]
        if mutation == "policy_id":
            return decision.model_copy(update={"policy_id": "other-policy"})
        if mutation == "policy_version":
            return decision.model_copy(update={"policy_version": "9.9.9"})
        if mutation == "policy_hash":
            return decision.model_copy(update={"policy_hash": "d" * 64})
        if mutation == "task_id":
            return decision.model_copy(update={"task_id": uuid4()})
        if mutation == "plan_id":
            return decision.model_copy(update={"plan_id": uuid4()})
        if mutation == "operator_id":
            return decision.model_copy(update={"operator_id": "other-user"})
        if mutation == "target":
            return decision.model_copy(
                update={
                    "target": TargetReference(
                        target_id="other-target",
                        resource_type="local_system",
                        resource_id="other-target",
                    )
                }
            )
        if mutation == "step_contract_hash":
            changed_step = step.model_copy(update={"contract_hash": "d" * 64})
            return decision.model_copy(update={"step_decisions": (changed_step,)})
        if mutation == "step_implementation_hash":
            changed_step = step.model_copy(update={"implementation_hash": "d" * 64})
            return decision.model_copy(update={"step_decisions": (changed_step,)})
        if mutation == "arguments_hash":
            changed_step = step.model_copy(update={"arguments_hash": "d" * 64})
            return decision.model_copy(update={"step_decisions": (changed_step,)})
        if mutation == "risk":
            changed_step = step.model_copy(update={"resolved_risk": RiskLevel.L1})
            return decision.model_copy(
                update={
                    "effective_risk": RiskLevel.L1,
                    "step_decisions": (changed_step,),
                }
            )
        if mutation in {"l1_reason_on_l0", "l3_reason_on_l0"}:
            reason = (
                PolicyReasonCode.L1_RULE_MISSING
                if mutation == "l1_reason_on_l0"
                else PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE
            )
            changed_step = step.model_copy(
                update={
                    "effect": PolicyEffect.DENY,
                    "reason_code": reason,
                }
            )
            return decision.model_copy(
                update={
                    "effect": PolicyEffect.DENY,
                    "reason_code": reason,
                    "step_decisions": (changed_step,),
                }
            )
        changed_step = step.model_copy(
            update={"approval_requirement": (PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL)}
        )
        return decision.model_copy(update={"step_decisions": (changed_step,)})

    replace_policy_evaluate(runtime, tampering_evaluate)
    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "policy_evaluation_failed"
    assert outcome.policy_decision is None
    assert RuntimeState.WAITING_FOR_APPROVAL not in outcome.task.state_history
    assert executor.calls == 0


@pytest.mark.parametrize("hash_field", ["contract_hash", "implementation_hash"])
def test_allow_decision_cannot_authorize_plan_hash_not_in_registry(
    hash_field: str,
) -> None:
    class TamperedHashPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            step = plan.steps[0].model_copy(update={hash_field: "d" * 64})
            return plan.model_copy(update={"steps": (step,)})

    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    executor = CountingExecutor()
    runtime = RuntimeEngine(
        planner=TamperedHashPlanner(),
        executor=cast(GovernedExecutor, executor),
    )
    trusted_evaluate = runtime._policy.evaluate

    def forged_allow_evaluate(
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        denied = trusted_evaluate(plan, context)
        step = denied.step_decisions[0].model_copy(
            update={
                "effect": PolicyEffect.ALLOW,
                "reason_code": PolicyReasonCode.ALLOWED,
            }
        )
        return denied.model_copy(
            update={
                "effect": PolicyEffect.ALLOW,
                "reason_code": PolicyReasonCode.ALLOWED,
                "step_decisions": (step,),
            }
        )

    replace_policy_evaluate(runtime, forged_allow_evaluate)
    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "policy_evaluation_failed"
    assert outcome.policy_decision is None
    assert RuntimeState.WAITING_FOR_APPROVAL not in outcome.task.state_history
    assert executor.calls == 0


def test_reordered_multistep_policy_decision_fails_closed_without_dispatch() -> None:
    class MultiStepPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            second = plan.steps[0].model_copy(update={"step_id": "second-status"})
            return plan.model_copy(update={"steps": (*plan.steps, second)})

    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    registry = build_default_registry()
    executor = CountingExecutor()
    runtime = RuntimeEngine(
        planner=MultiStepPlanner(),
        executor=cast(GovernedExecutor, executor),
        registry=registry,
    )
    trusted_evaluate = runtime._policy.evaluate

    def reordering_evaluate(
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        decision = trusted_evaluate(plan, context)
        return decision.model_copy(
            update={"step_decisions": tuple(reversed(decision.step_decisions))}
        )

    replace_policy_evaluate(runtime, reordering_evaluate)
    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "policy_evaluation_failed"
    assert outcome.policy_decision is None
    assert executor.calls == 0


def test_non_none_verifier_return_cannot_complete() -> None:
    class InvalidReturnVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
            context: VerificationContext,
        ) -> VerificationResult:
            del plan, results, context
            return False  # type: ignore[return-value]

    outcome = RuntimeEngine(verifier=InvalidReturnVerifier()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.task.state is RuntimeState.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "verification"
    assert outcome.verification_result is not None
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.VERIFIER_RESULT_INVALID,
    )


def test_verifier_hash_drift_is_result_invalid_not_component_failure() -> None:
    class HashDriftVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
            context: VerificationContext,
        ) -> VerificationResult:
            result = super().verify(plan, results, context)
            return result.model_copy(update={"content_hash": "f" * 64})

    outcome = RuntimeEngine(verifier=HashDriftVerifier()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "verification"
    assert outcome.verification_result is not None
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.VERIFIER_RESULT_INVALID,
    )


@pytest.mark.parametrize(
    "mutation",
    ["missing_check", "reordered_checks", "attempt_binding", "report_binding"],
)
def test_runtime_rejects_hash_valid_verifier_shape_or_binding_drift(
    mutation: str,
) -> None:
    class BindingDriftVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
            context: VerificationContext,
        ) -> VerificationResult:
            result = super().verify(plan, results, context)
            if mutation == "missing_check":
                updates: dict[str, object] = {"checks": result.checks[:-1]}
            elif mutation == "reordered_checks":
                updates = {"checks": tuple(reversed(result.checks))}
            elif mutation == "attempt_binding":
                updates = {"execution_attempt_id": uuid4()}
            else:
                updates = {"execution_report_hash": "f" * 64}
            return forge_verification_result(result, **updates)

    outcome = RuntimeEngine(verifier=BindingDriftVerifier()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.verification_result is not None
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.VERIFIER_RESULT_INVALID,
    )


def test_runtime_rejects_hash_valid_verifier_effect_closure_drift() -> None:
    class ClosureDriftVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
            context: VerificationContext,
        ) -> VerificationResult:
            result = super().verify(plan, results, context)
            draft = result.model_copy(
                update={
                    "effect_disposition": VerificationEffectDisposition.UNKNOWN,
                    "human_intervention_required": True,
                    "content_hash": "0" * 64,
                }
            )
            return draft.model_copy(
                update={
                    "content_hash": canonical_json_sha256(
                        draft.model_dump(
                            mode="json",
                            exclude={"content_hash"},
                            warnings="error",
                        )
                    )
                }
            )

    outcome = RuntimeEngine(verifier=ClosureDriftVerifier()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.verification_result is not None
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.VERIFIER_RESULT_INVALID,
    )


def test_mandatory_criterion_mismatch_fails_with_hash_bound_result() -> None:
    outcome = RuntimeEngine(planner=MismatchedCriterionPlanner()).run(
        Task(request=SUPPORTED_REQUEST)
    )

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.task.state is RuntimeState.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.VERIFIER
    assert outcome.failure.code == "verification"
    assert outcome.plan is not None
    assert outcome.execution_authorization is not None
    assert outcome.execution_report is not None
    assert outcome.verification_result is not None
    result = outcome.verification_result
    assert result.status is VerificationStatus.FAILED
    assert result.failure_reasons == (VerificationFailureReason.CRITERION_MISMATCH,)
    assert result.checks[0].status is VerificationCheckStatus.FAILED
    assert result.checks[0].failure_reason is VerificationFailureReason.CRITERION_MISMATCH
    assert result.plan_digest == canonical_json_sha256(outcome.plan)
    assert result.execution_attempt_id == outcome.execution_authorization.execution_attempt_id
    assert result.execution_report_hash == outcome.execution_report.content_hash
    assert result.content_hash == canonical_json_sha256(
        result.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    assert outcome.final_effect_disposition is VerificationEffectDisposition.NONE
    assert outcome.human_intervention_required is False


def test_runtime_recomputes_and_rejects_hash_valid_forged_pass() -> None:
    class ForgedPassedVerifier(Verifier):
        forged: VerificationResult | None = None

        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
            context: VerificationContext,
        ) -> VerificationResult:
            trusted_failure = super().verify(plan, results, context)
            assert trusted_failure.status is VerificationStatus.FAILED
            passed_checks = tuple(
                check.model_copy(
                    update={
                        "status": VerificationCheckStatus.PASSED,
                        "failure_reason": None,
                    }
                )
                for check in trusted_failure.checks
            )
            self.forged = forge_verification_result(
                trusted_failure,
                status=VerificationStatus.PASSED,
                checks=passed_checks,
                failure_reasons=(),
                effect_disposition=VerificationEffectDisposition.NONE,
                human_intervention_required=False,
            )
            return self.forged

    verifier = ForgedPassedVerifier()
    outcome = RuntimeEngine(
        planner=MismatchedCriterionPlanner(),
        verifier=verifier,
    ).run(Task(request=SUPPORTED_REQUEST))

    assert verifier.forged is not None
    assert verifier.forged.status is VerificationStatus.PASSED
    assert verifier.forged.content_hash == canonical_json_sha256(
        verifier.forged.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.VERIFIER
    assert outcome.failure.code == "verification"
    assert outcome.verification_result is not None
    assert outcome.verification_result.status is VerificationStatus.FAILED
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.VERIFIER_RESULT_INVALID,
    )
    assert outcome.verification_result != verifier.forged
    assert outcome.task.state_history[-2:] == (
        RuntimeState.VERIFYING,
        RuntimeState.FAILED,
    )


def test_verifier_exception_produces_redacted_structured_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_VERIFIER_EXCEPTION_SECRET"
    trace = Trace(fail_at="verifier", error=RuntimeError(marker))
    caplog.set_level(logging.INFO)

    outcome = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.VERIFIER
    assert outcome.failure.code == "verifier_failure"
    assert outcome.verification_result is not None
    assert outcome.verification_result.status is VerificationStatus.FAILED
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.VERIFIER_FAILED,
    )
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


def test_verifier_cannot_mutate_runtime_authoritative_inputs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_MUTATING_VERIFIER_SECRET"

    class MutatingVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
            context: VerificationContext,
        ) -> VerificationResult:
            object.__setattr__(plan, "target", "forged-target")
            object.__setattr__(results[0], "success", False)
            object.__setattr__(context, "plan_digest", "f" * 64)
            raise RuntimeError(marker)

    caplog.set_level(logging.INFO)
    outcome = RuntimeEngine(verifier=MutatingVerifier()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.plan is not None
    assert outcome.plan.target == "local-mock"
    assert outcome.results[0].success is True
    assert outcome.verification_result is not None
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.VERIFIER_FAILED,
    )
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


def test_runtime_outcome_rejects_rehashed_pass_for_failed_verifier() -> None:
    failed = make_runtime(Trace(fail_at="verifier", error=RuntimeError(SENSITIVE_MARKER))).run(
        Task(request=SUPPORTED_REQUEST)
    )
    assert failed.verification_result is not None
    passed_checks = tuple(
        check.model_copy(
            update={
                "status": VerificationCheckStatus.PASSED,
                "failure_reason": None,
            }
        )
        for check in failed.verification_result.checks
    )
    forged = forge_verification_result(
        failed.verification_result,
        status=VerificationStatus.PASSED,
        checks=passed_checks,
        failure_reasons=(),
        effect_disposition=VerificationEffectDisposition.NONE,
        human_intervention_required=False,
    )
    payload = failed.model_dump(mode="python")
    payload["verification_result"] = forged

    with pytest.raises(ValidationError, match="Verifier"):
        RuntimeOutcome.model_validate(payload, strict=True)


def test_verification_clock_capture_failure_returns_clock_unavailable_result() -> None:
    trace = Trace()

    outcome = make_runtime(
        trace,
        clock=SequenceClock(clock_values(12)),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.RUNTIME
    assert outcome.failure.code == "invalid_clock"
    assert outcome.execution_report is not None
    assert outcome.verification_result is not None
    result = outcome.verification_result
    assert result.status is VerificationStatus.FAILED
    assert result.failure_reasons == (VerificationFailureReason.CLOCK_UNAVAILABLE,)
    assert result.execution_report_hash == outcome.execution_report.content_hash
    assert result.effect_disposition is VerificationEffectDisposition.NONE
    assert result.human_intervention_required is False
    assert trace.calls == ["context", "planner", "policy", "executor", "tool"]


def test_runtime_elapsed_time_conservatively_bounds_evidence_freshness() -> None:
    class TightFreshnessPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            criteria = tuple(
                criterion.model_copy(update={"maximum_age_ms": 1_000})
                for criterion in plan.verification_criteria
            )
            return plan.model_copy(update={"verification_criteria": criteria})

    outcome = RuntimeEngine(
        planner=TightFreshnessPlanner(),
        clock=SequenceClock(clock_values(15)),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.verification_result is not None
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.STALE_EVIDENCE,
    )


def test_mutating_effect_becomes_verified_only_after_independent_pass() -> None:
    _, outcome = run_synthetic_mutation("running")

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert outcome.execution_report is not None
    assert tuple(record.effect_disposition for record in outcome.execution_report.records) == (
        EffectDisposition.PENDING_VERIFICATION,
        EffectDisposition.NONE,
    )
    assert outcome.verification_result is not None
    assert outcome.verification_result.status is VerificationStatus.PASSED
    assert outcome.verification_result.effect_disposition is (
        VerificationEffectDisposition.VERIFIED
    )
    assert outcome.final_effect_disposition is VerificationEffectDisposition.VERIFIED
    assert outcome.human_intervention_required is False


def test_mutating_criterion_failure_closes_effect_unknown_for_human() -> None:
    _, outcome = run_synthetic_mutation("stopped")

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.verification_result is not None
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.CRITERION_MISMATCH,
    )
    assert outcome.verification_result.effect_disposition is (VerificationEffectDisposition.UNKNOWN)
    assert outcome.final_effect_disposition is VerificationEffectDisposition.UNKNOWN
    assert outcome.human_intervention_required is True


def test_mutating_verifier_exception_closes_effect_unknown_for_human() -> None:
    class RaisingVerifier(Verifier):
        def verify(
            self,
            plan: ExecutionPlan,
            results: tuple[ToolResult[SystemStatus], ...],
            context: VerificationContext,
        ) -> VerificationResult:
            del plan, results, context
            raise RuntimeError(SENSITIVE_MARKER)

    _, outcome = run_synthetic_mutation("running", verifier=RaisingVerifier())

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.verification_result is not None
    assert outcome.verification_result.failure_reasons == (
        VerificationFailureReason.VERIFIER_FAILED,
    )
    assert outcome.final_effect_disposition is VerificationEffectDisposition.UNKNOWN
    assert outcome.human_intervention_required is True


def test_mutating_verification_acquisition_failure_closes_effect_unknown() -> None:
    class VerificationAcquisitionFailure:
        def __init__(self, delegate: GovernedExecutor) -> None:
            self._delegate = delegate

        def begin_attempt(
            self,
            plan: ExecutionPlan,
            policy_decision: PolicyDecision,
            approval_id: UUID | None,
        ) -> ExecutionAttemptAuthorization:
            return self._delegate.begin_attempt(plan, policy_decision, approval_id)

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            return self._delegate.execute_actions(authorization, confirmation_reader)

        def execute_verification(
            self,
            authorization: ExecutionAttemptAuthorization,
        ) -> ExecutionReport:
            return self._delegate.abort_attempt(
                authorization,
                reason_code="verification_acquisition_failed",
            )

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            return self._delegate.abort_attempt(
                authorization,
                reason_code=reason_code,
            )

    planner = SyntheticMutationPlanner("running")
    runtime = RuntimeEngine(planner=planner)
    planner.action_metadata = install_synthetic_mutating_mock(runtime)
    runtime._executor = cast(
        GovernedExecutor,
        VerificationAcquisitionFailure(runtime._executor),
    )
    waiting = runtime.run(Task(request=SUPPORTED_REQUEST))
    review = runtime.prepare_approval_review(waiting)
    approval = runtime.commit_approval(waiting, review.review_id)

    outcome = runtime.resume_approved(waiting, approval.approval_id)

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.execution_report is not None
    assert outcome.execution_report.status is ExecutionReportStatus.FAILED
    assert outcome.verification_result is None
    assert outcome.final_effect_disposition is VerificationEffectDisposition.UNKNOWN
    assert outcome.human_intervention_required is True


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

    outcome = RuntimeEngine(executor=cast(GovernedExecutor, UntrustedExecutor())).run(
        Task(request=SUPPORTED_REQUEST)
    )

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
    assert outcome.policy_decision is not None
    assert outcome.policy_decision.effect is PolicyEffect.ALLOW
    assert len(outcome.results) == 1
    assert outcome.results[0].success is False
    assert outcome.results[0].error is not None
    assert outcome.results[0].error.code == "tool_execution_failed"
    assert trace.calls == ["context", "planner", "policy", "executor", "tool"]
    assert RuntimeState.VERIFYING not in outcome.task.state_history


@pytest.mark.parametrize("hostile_variant", ["secret_evidence", "secret_error"])
def test_runtime_revalidates_results_from_hostile_executor_reports(
    hostile_variant: str,
) -> None:
    trace = Trace()
    secret_marker = "bash -c RUNTIME_SECRET_MARKER"

    class HostileExecutor(Executor):
        def __init__(self) -> None:
            pass

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            trace.record("executor")
            trace.record("tool")
            if hostile_variant == "secret_evidence":
                base = make_structured_result(plan, success=True)
                document = base.model_dump(mode="python", warnings="error")
                document["evidence"] = {"password": secret_marker}
                result = ToolResult[SystemStatus].model_validate(document, strict=True)
            else:
                base = make_structured_result(plan, success=False)
                assert base.error is not None
                result = base.model_copy(
                    update={"error": base.error.model_copy(update={"message": secret_marker})}
                )
            return (result,)

    outcome = make_runtime(trace, executor=HostileExecutor()).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.EXECUTOR
    assert outcome.failure.code == "tool_execution"
    assert outcome.results == ()
    assert secret_marker not in outcome.model_dump_json()
    assert trace.calls == ["context", "planner", "policy", "executor", "tool"]


def test_runtime_result_tuple_validation_uses_exact_registered_timeout() -> None:
    registry = build_default_registry()
    task = Task(request=SUPPORTED_REQUEST)
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    plan = Planner().create_plan(ContextBuilder().build(task), metadata)
    result = make_structured_result(plan, success=True).model_copy(
        update={"duration_ms": metadata.timeout_ms + 1}
    )
    runtime = RuntimeEngine(registry=registry)

    with pytest.raises(ToolExecutionError, match="invalid structured evidence"):
        runtime._validate_results((result,), plan)


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


def test_policy_raised_human_approval_pauses_without_execution() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )

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
    assert outcome.policy_decision is not None
    assert outcome.policy_decision.effect is PolicyEffect.ALLOW
    assert (
        outcome.policy_decision.approval_requirement
        is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
    )
    assert outcome.events[-1].kind is LifecycleEventKind.PAUSED
    assert outcome.events[-1].reason_code == "approval_required"
    assert not any(
        event.kind is LifecycleEventKind.APPROVAL_DECISION_RECORDED for event in outcome.events
    )

    with pytest.raises(InvalidStateTransitionError, match="resume_approved"):
        runtime.run(outcome.task)
    assert trace.calls == ["context", "planner", "policy"]


def test_human_rejection_closes_paused_outcome_without_tool_call() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
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
    assert rejected.policy_decision == paused.policy_decision
    assert rejected.events[:-2] == paused.events
    assert rejected.events[-2].kind is LifecycleEventKind.STATE_ENTERED
    assert rejected.events[-1].kind is LifecycleEventKind.REJECTED
    assert trace.calls == ["context", "planner", "policy"]


def test_review_and_commit_record_authorization_without_dispatch() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))

    review = runtime.prepare_approval_review(paused)
    record = runtime.commit_approval(paused, review.review_id)

    assert paused.status is RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
    assert paused.task.state is RuntimeState.WAITING_FOR_APPROVAL
    assert review.plan_hash == record.plan_hash
    assert record.approver == "local-owner"
    assert trace.calls == ["context", "planner", "policy"]
    assert not any(call in {"executor", "tool", "verifier"} for call in trace.calls)
    with pytest.raises(InvalidStateTransitionError, match="resume_approved"):
        runtime.run(paused.task)


def test_committed_human_approval_resumes_exact_plan_in_same_process() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))
    review = runtime.prepare_approval_review(paused)
    record = runtime.commit_approval(paused, review.review_id)

    assert trace.calls == ["context", "planner", "policy"]

    completed = runtime.resume_approved(paused, record.approval_id)

    assert completed.status is RuntimeOutcomeStatus.COMPLETED
    assert completed.execution_authorization is not None
    assert completed.execution_report is not None
    assert completed.execution_authorization.approval_id == record.approval_id
    assert completed.execution_authorization.approval_plan_hash == record.plan_hash
    assert completed.execution_authorization.approval_record_hash == record.content_hash
    consumed = tuple(
        event
        for event in completed.events
        if event.kind is LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED
    )
    assert len(consumed) == 1
    assert consumed[0].approval_id == record.approval_id
    assert (
        consumed[0].execution_attempt_id == completed.execution_authorization.execution_attempt_id
    )
    assert trace.calls == [
        "context",
        "planner",
        "policy",
        "executor",
        "tool",
        "verifier",
    ]


@pytest.mark.parametrize("approval_case", ["unknown", "expired"])
def test_invalid_human_approval_stays_paused_with_structured_rejection(
    approval_case: str,
) -> None:
    clock = AdjustableClock(datetime(2026, 7, 25, 8, 0, tzinfo=UTC))
    trace = Trace()
    runtime = make_runtime(
        trace,
        clock=clock,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))

    if approval_case == "unknown":
        approval_id = uuid4()
        expected_reason = "unknown_approval"
    else:
        review = runtime.prepare_approval_review(paused)
        record = runtime.commit_approval(paused, review.review_id)
        approval_id = record.approval_id
        expected_reason = "approval_expired"
        clock.value += timedelta(hours=1)

    rejected = runtime.resume_approved(paused, approval_id)

    assert rejected.status is RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
    assert rejected.task.state is RuntimeState.WAITING_FOR_APPROVAL
    assert rejected.events[:-1] == paused.events
    assert rejected.events[-1].kind is LifecycleEventKind.AUTHORIZATION_REJECTED
    assert rejected.events[-1].reason_code == expected_reason
    assert rejected.execution_authorization is None
    assert rejected.execution_report is None
    assert rejected.results == ()
    assert trace.calls == ["context", "planner", "policy"]


@pytest.mark.parametrize("reason_code", ["approval_missing", "approval_rejected"])
def test_retryable_approval_gate_errors_remain_waiting(reason_code: str) -> None:
    class GateRejectingExecutor(Executor):
        def begin_attempt(
            self,
            plan: ExecutionPlan,
            policy_decision: PolicyDecision,
            approval_id: UUID | None,
        ) -> ExecutionAttemptAuthorization:
            del plan, policy_decision, approval_id
            raise ExecutionAuthorizationError(
                "Approval gate rejected safely",
                reason_code=reason_code,
            )

    trace = Trace()
    runtime = make_runtime(
        trace,
        executor=GateRejectingExecutor(),
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))

    rejected = runtime.resume_approved(paused, uuid4())

    assert rejected.status is RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
    assert rejected.events[-1].kind is LifecycleEventKind.AUTHORIZATION_REJECTED
    assert rejected.events[-1].reason_code == reason_code
    assert RuntimeState.EXECUTING not in rejected.task.state_history


@pytest.mark.parametrize("forgery", ["mutated_field", "subclass"])
def test_runtime_normalizes_untrusted_authorization_reason_without_leaking(
    caplog: pytest.LogCaptureFixture,
    forgery: str,
) -> None:
    marker = "sensitive_authorization_token"

    class ForgedAuthorizationError(ExecutionAuthorizationError):
        pass

    if forgery == "mutated_field":
        forged_error = ExecutionAuthorizationError(
            "SENSITIVE AUTHORIZATION MESSAGE",
            reason_code="approval_missing",
        )
        forged_error.__dict__["reason_code"] = marker
    else:
        forged_error = ForgedAuthorizationError(
            "SENSITIVE AUTHORIZATION MESSAGE",
            reason_code="approval_missing",
        )

    class AuthorizationFailureExecutor(Executor):
        def begin_attempt(
            self,
            plan: ExecutionPlan,
            policy_decision: PolicyDecision,
            approval_id: UUID | None,
        ) -> ExecutionAttemptAuthorization:
            del plan, policy_decision, approval_id
            raise forged_error

    caplog.set_level(logging.INFO)
    outcome = make_runtime(
        Trace(),
        executor=AuthorizationFailureExecutor(),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "execution_authorization_failed"
    assert outcome.failure.component is RuntimeComponent.APPROVAL
    assert RuntimeState.EXECUTING not in outcome.task.state_history
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text
    assert "SENSITIVE AUTHORIZATION MESSAGE" not in caplog.text


def test_consumed_approval_cannot_resume_twice_or_dispatch_twice() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))
    review = runtime.prepare_approval_review(paused)
    record = runtime.commit_approval(paused, review.review_id)

    first = runtime.resume_approved(paused, record.approval_id)
    replay = runtime.resume_approved(paused, record.approval_id)

    assert first.status is RuntimeOutcomeStatus.COMPLETED
    assert replay.status is not RuntimeOutcomeStatus.COMPLETED
    assert trace.calls.count("executor") == 1
    assert trace.calls.count("tool") == 1


def test_sibling_approvals_for_same_paused_plan_dispatch_at_most_once() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))
    first_review = runtime.prepare_approval_review(paused)
    second_review = runtime.prepare_approval_review(paused)
    first_record = runtime.commit_approval(paused, first_review.review_id)
    second_record = runtime.commit_approval(paused, second_review.review_id)

    first = runtime.resume_approved(paused, first_record.approval_id)
    sibling = runtime.resume_approved(paused, second_record.approval_id)

    assert first.status is RuntimeOutcomeStatus.COMPLETED
    assert sibling.status is RuntimeOutcomeStatus.FAILED
    assert sibling.failure is not None
    assert sibling.failure.code == "approval_already_consumed"
    assert sibling.failure.component is RuntimeComponent.APPROVAL
    assert sibling.execution_authorization is None
    assert trace.calls.count("executor") == 1
    assert trace.calls.count("tool") == 1


def test_concurrent_double_resume_dispatches_at_most_once() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))
    review = runtime.prepare_approval_review(paused)
    record = runtime.commit_approval(paused, review.review_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(
            pool.submit(runtime.resume_approved, paused, record.approval_id) for _ in range(2)
        )
        outcomes = tuple(future.result() for future in futures)

    assert sum(outcome.status is RuntimeOutcomeStatus.COMPLETED for outcome in outcomes) == 1
    assert trace.calls.count("executor") == 1
    assert trace.calls.count("tool") == 1


@pytest.mark.parametrize("authorization_kind", ["non_model", "invalid_content_hash"])
def test_runtime_rejects_malformed_attempt_authorization_before_execution(
    authorization_kind: str,
) -> None:
    class MalformedAuthorizationExecutor(RecordingExecutor):
        def __init__(self, trace: Trace, delegate: GovernedExecutor) -> None:
            super().__init__(trace, delegate)
            self.execution_calls = 0

        def begin_attempt(
            self,
            plan: ExecutionPlan,
            policy_decision: PolicyDecision,
            approval_id: UUID | None,
        ) -> ExecutionAttemptAuthorization:
            authorization = super().begin_attempt(plan, policy_decision, approval_id)
            if authorization_kind == "non_model":
                return cast(ExecutionAttemptAuthorization, object())
            return ExecutionAttemptAuthorization.model_construct(
                **authorization.model_dump(
                    mode="python",
                    exclude={"content_hash"},
                    warnings="error",
                ),
                content_hash="f" * 64,
            )

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            del authorization, confirmation_reader
            self.execution_calls += 1
            raise AssertionError("malformed authorization reached execution")

    trace = Trace()
    runtime = RuntimeEngine()
    hostile = MalformedAuthorizationExecutor(trace, runtime._executor)
    runtime._executor = cast(GovernedExecutor, hostile)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "authorization_evidence_invalid"
    assert outcome.failure.component is RuntimeComponent.APPROVAL
    assert RuntimeState.EXECUTING not in outcome.task.state_history
    assert outcome.execution_authorization is None
    assert outcome.execution_report is None
    assert hostile.execution_calls == 0


def test_runtime_rejects_authorization_not_bound_to_consumed_approval_record() -> None:
    class ApprovalEvidenceForgingExecutor(RecordingExecutor):
        def __init__(self, trace: Trace, delegate: GovernedExecutor) -> None:
            super().__init__(trace, delegate)
            self.execution_calls = 0

        def begin_attempt(
            self,
            plan: ExecutionPlan,
            policy_decision: PolicyDecision,
            approval_id: UUID | None,
        ) -> ExecutionAttemptAuthorization:
            authorization = super().begin_attempt(plan, policy_decision, approval_id)
            return rehash_execution_authorization(
                authorization,
                approval_record_hash="f" * 64,
            )

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            del authorization, confirmation_reader
            self.execution_calls += 1
            raise AssertionError("forged Approval evidence reached execution")

    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))
    review = runtime.prepare_approval_review(paused)
    record = runtime.commit_approval(paused, review.review_id)
    hostile = ApprovalEvidenceForgingExecutor(trace, runtime._executor)
    runtime._executor = cast(GovernedExecutor, hostile)

    outcome = runtime.resume_approved(paused, record.approval_id)

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "authorization_evidence_invalid"
    assert outcome.failure.component is RuntimeComponent.APPROVAL
    assert RuntimeState.EXECUTING not in outcome.task.state_history
    assert outcome.execution_authorization is None
    assert hostile.execution_calls == 0


def test_forged_execution_authorization_and_report_are_rejected() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))
    review = runtime.prepare_approval_review(paused)
    record = runtime.commit_approval(paused, review.review_id)
    completed = runtime.resume_approved(paused, record.approval_id)
    assert completed.execution_authorization is not None
    assert completed.execution_report is not None

    forged_authorization = completed.execution_authorization.model_copy(
        update={"plan_digest": "d" * 64}
    )
    forged_report = completed.execution_report.model_copy(update={"authorization_hash": "e" * 64})
    outcomes = (
        paused.model_copy(
            update={
                "execution_authorization": forged_authorization,
                "execution_report": completed.execution_report,
                "results": completed.results,
            }
        ),
        paused.model_copy(
            update={
                "execution_authorization": completed.execution_authorization,
                "execution_report": forged_report,
                "results": completed.results,
            }
        ),
    )

    for forged in outcomes:
        with pytest.raises(InvalidRuntimeOutcomeError):
            runtime.prepare_approval_review(forged)


def test_phase4_reject_approval_audits_review_and_closes_runtime() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    paused = runtime.run(Task(request=SUPPORTED_REQUEST))
    review = runtime.prepare_approval_review(paused)

    rejected = runtime.reject_approval(paused, review.review_id)

    assert rejected.status is RuntimeOutcomeStatus.FAILED
    assert rejected.failure is not None
    assert rejected.failure.code == "human_rejected"
    assert trace.calls == ["context", "planner", "policy"]


def test_phase4_commit_rebinds_review_to_the_exact_waiting_outcome() -> None:
    first_trace = Trace()
    first_runtime = make_runtime(
        first_trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    first = first_runtime.run(Task(request=SUPPORTED_REQUEST))
    review = first_runtime.prepare_approval_review(first)

    second_trace = Trace()
    second_runtime = make_runtime(
        second_trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    second = second_runtime.run(Task(request=SUPPORTED_REQUEST))

    with pytest.raises(ApprovalStateError):
        first_runtime.commit_approval(second, review.review_id)
    assert first_trace.calls == ["context", "planner", "policy"]
    assert second_trace.calls == ["context", "planner", "policy"]


def test_phase4_reject_rebinds_review_to_the_exact_waiting_outcome() -> None:
    trace = Trace()
    runtime = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )
    first = runtime.run(Task(request=SUPPORTED_REQUEST))
    second = runtime.run(Task(request=SUPPORTED_REQUEST))
    first_review = runtime.prepare_approval_review(first)
    second_review = runtime.prepare_approval_review(second)

    with pytest.raises(ApprovalStateError):
        runtime.reject_approval(first, second_review.review_id)

    assert all(
        event.kind is not ApprovalAuditEventKind.PLAN_APPROVAL_REJECTED
        for event in runtime._approval.events
    )
    rejected = runtime.reject_approval(first, first_review.review_id)
    assert rejected.failure is not None
    assert rejected.failure.code == "human_rejected"


def test_phase4_approval_methods_reject_nonwaiting_outcomes() -> None:
    completed = RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))

    with pytest.raises(InvalidRuntimeOutcomeError):
        RuntimeEngine().prepare_approval_review(completed)


def test_reject_only_accepts_valid_waiting_outcome() -> None:
    completed = RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))
    trace = Trace()
    paused = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    ).run(Task(request=SUPPORTED_REQUEST))
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
    trace = Trace()
    paused = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    ).run(Task(request=SUPPORTED_REQUEST))
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


def test_unsupported_task_and_malformed_plan_return_planner_failures() -> None:
    class CountingExecutor(Executor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
            del plan
            self.calls += 1
            return ()

    executor = CountingExecutor()
    unsupported = RuntimeEngine(executor=cast(GovernedExecutor, executor)).run(
        Task(request="unsupported")
    )

    class EmptyPlanPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            return super().create_plan(context, metadata).model_copy(update={"steps": ()})

    malformed = RuntimeEngine(
        planner=EmptyPlanPlanner(),
        executor=cast(GovernedExecutor, executor),
    ).run(Task(request=SUPPORTED_REQUEST))

    assert unsupported.failure is not None
    assert unsupported.failure.code == "unsupported_task"
    assert malformed.failure is not None
    assert malformed.failure.code == "plan_mismatch"
    assert executor.calls == 0


def test_runtime_accepts_read_only_action_role() -> None:
    trace = Trace()

    class NonObservePlanner(RecordingPlanner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            step = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
            return plan.model_copy(update={"steps": (step,)})

    registry = build_default_registry()
    runtime = RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=NonObservePlanner(trace),
        verifier=RecordingVerifier(trace),
        registry=registry,
    )
    runtime._executor = cast(
        GovernedExecutor,
        RecordingExecutor(trace, runtime._executor),
    )
    install_recording_policy(runtime, trace)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert outcome.failure is None
    assert outcome.plan is not None
    assert outcome.execution_report is not None
    assert outcome.plan.steps[0].role is StepRole.ACTION
    assert outcome.execution_report.records[0].role is StepRole.ACTION
    assert "verification_tool" not in trace.calls


def test_runtime_rejects_mutating_observe_before_gateway_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MutatingObservePlanner(SyntheticMutationPlanner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            mutating_observe = plan.steps[0].model_copy(update={"role": StepRole.OBSERVE})
            return plan.model_copy(update={"steps": (mutating_observe, *plan.steps[1:])})

    planner = MutatingObservePlanner("running")
    runtime = RuntimeEngine(planner=planner)
    planner.action_metadata = install_synthetic_mutating_mock(runtime)
    gateway_calls = 0
    original_invoke = runtime._executor._gateway._invoke_with_receipt

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal gateway_calls
        gateway_calls += 1
        return original_invoke(call)

    monkeypatch.setattr(runtime._executor._gateway, "_invoke_with_receipt", recording_invoke)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.PLANNER
    assert outcome.failure.code == "plan_mismatch"
    assert gateway_calls == 0
    assert RuntimeState.EXECUTING not in outcome.task.state_history


def test_runtime_rejects_contract_undeclared_verify_step_before_dispatch() -> None:
    trace = Trace()

    class UndeclaredVerifyPlanner(RecordingPlanner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            verify = plan.steps[0].model_copy(update={"role": StepRole.VERIFY})
            return plan.model_copy(update={"steps": (verify,)})

    runtime = RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=UndeclaredVerifyPlanner(trace),
        verifier=RecordingVerifier(trace),
    )
    runtime._executor = cast(
        GovernedExecutor,
        RecordingExecutor(trace, runtime._executor),
    )

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "plan_mismatch"
    assert trace.calls == ["context", "planner"]


def test_action_prefix_runs_before_verify_suffix_runtime_phase() -> None:
    class ActionVerifyPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            action = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
            verify = plan.steps[0].model_copy(
                update={"step_id": "verify-system-status", "role": StepRole.VERIFY}
            )
            criterion = plan.verification_criteria[0].model_copy(
                update={"evidence_step_id": verify.step_id}
            )
            return plan.model_copy(
                update={
                    "steps": (action, verify),
                    "verification_criteria": (criterion,),
                }
            )

    trace = Trace()
    registry = build_default_registry()
    runtime = RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=ActionVerifyPlanner(),
        verifier=RecordingVerifier(trace),
        registry=registry,
    )
    declare_mock_verification_tool(runtime)
    runtime._executor = cast(
        GovernedExecutor,
        RecordingExecutor(trace, runtime._executor),
    )
    install_recording_policy(runtime, trace)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert outcome.execution_report is not None
    assert tuple(record.role for record in outcome.execution_report.records) == (
        StepRole.ACTION,
        StepRole.VERIFY,
    )
    assert trace.calls.index("executor") < trace.calls.index("verification_tool")
    executor_completed_index = next(
        index
        for index, event in enumerate(outcome.events)
        if event.kind is LifecycleEventKind.COMPONENT_COMPLETED
        and event.component is RuntimeComponent.EXECUTOR
    )
    verifying_entered_index = next(
        index
        for index, event in enumerate(outcome.events)
        if event.kind is LifecycleEventKind.STATE_ENTERED and event.state is RuntimeState.VERIFYING
    )
    assert executor_completed_index < verifying_entered_index


def test_executor_failure_during_verifying_produces_consistent_failed_outcome() -> None:
    class ActionVerifyPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            action = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
            verify = plan.steps[0].model_copy(
                update={"step_id": "verify-system-status", "role": StepRole.VERIFY}
            )
            criterion = plan.verification_criteria[0].model_copy(
                update={"evidence_step_id": verify.step_id}
            )
            return plan.model_copy(
                update={
                    "steps": (action, verify),
                    "verification_criteria": (criterion,),
                }
            )

    class VerificationAbortExecutor:
        def __init__(self, delegate: GovernedExecutor) -> None:
            self._delegate = delegate

        def begin_attempt(
            self,
            plan: ExecutionPlan,
            policy_decision: PolicyDecision,
            approval_id: UUID | None,
        ) -> ExecutionAttemptAuthorization:
            return self._delegate.begin_attempt(plan, policy_decision, approval_id)

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            return self._delegate.execute_actions(authorization, confirmation_reader)

        def execute_verification(
            self,
            authorization: ExecutionAttemptAuthorization,
        ) -> ExecutionReport:
            return self._delegate.abort_attempt(
                authorization,
                reason_code="verification_dispatch_failed",
            )

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            return self._delegate.abort_attempt(
                authorization,
                reason_code=reason_code,
            )

    runtime = RuntimeEngine(planner=ActionVerifyPlanner())
    declare_mock_verification_tool(runtime)
    runtime._executor = cast(
        GovernedExecutor,
        VerificationAbortExecutor(runtime._executor),
    )

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.task.state_history[-2:] == (
        RuntimeState.VERIFYING,
        RuntimeState.FAILED,
    )
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.EXECUTOR
    assert outcome.execution_authorization is not None
    assert outcome.execution_report is not None
    assert outcome.execution_report.status is ExecutionReportStatus.FAILED
    assert outcome.execution_report.failure_code == "verification_dispatch_failed"
    assert tuple(record.role for record in outcome.execution_report.records) == (StepRole.ACTION,)
    assert outcome.results == outcome.execution_report.results
    assert any(
        event.kind is LifecycleEventKind.COMPONENT_COMPLETED
        and event.component is RuntimeComponent.EXECUTOR
        and event.state is RuntimeState.EXECUTING
        for event in outcome.events
    )
    assert outcome.events[-1].kind is LifecycleEventKind.FAILED
    assert outcome.events[-1].state is RuntimeState.FAILED
    assert outcome.events[-1].component is RuntimeComponent.EXECUTOR


@pytest.mark.parametrize("abort_returns_cumulative", [True, False])
def test_verification_reports_cannot_erase_trusted_action_prefix(
    abort_returns_cumulative: bool,
) -> None:
    class ActionVerifyPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            action = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
            verify = plan.steps[0].model_copy(
                update={"step_id": "verify-system-status", "role": StepRole.VERIFY}
            )
            criterion = plan.verification_criteria[0].model_copy(
                update={"evidence_step_id": verify.step_id}
            )
            return plan.model_copy(
                update={
                    "steps": (action, verify),
                    "verification_criteria": (criterion,),
                }
            )

    class TruncatingVerificationExecutor(RecordingExecutor):
        def __init__(self, trace: Trace, delegate: GovernedExecutor) -> None:
            super().__init__(trace, delegate)
            self.prior_report: ExecutionReport | None = None
            self.abort_calls = 0

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            report = super().execute_actions(authorization, confirmation_reader)
            self.prior_report = report
            return report

        def execute_verification(
            self,
            authorization: ExecutionAttemptAuthorization,
        ) -> ExecutionReport:
            return Executor._make_report(
                authorization,
                (),
                failure_code="verification_dispatch_failed",
                failed_step_index=None,
            )

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            self.abort_calls += 1
            if abort_returns_cumulative:
                return self._delegate.abort_attempt(
                    authorization,
                    reason_code=reason_code,
                )
            return Executor._make_report(
                authorization,
                (),
                failure_code=reason_code,
                failed_step_index=None,
            )

    trace = Trace()
    runtime = RuntimeEngine(planner=ActionVerifyPlanner())
    declare_mock_verification_tool(runtime)
    hostile = TruncatingVerificationExecutor(trace, runtime._executor)
    runtime._executor = cast(GovernedExecutor, hostile)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    prior = hostile.prior_report
    assert prior is not None
    assert prior.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
    assert len(prior.records) == 1
    assert hostile.abort_calls == 1
    assert outcome.status is RuntimeOutcomeStatus.FAILED
    if abort_returns_cumulative:
        assert outcome.execution_report is not None
        assert outcome.execution_report.status is ExecutionReportStatus.FAILED
        assert outcome.execution_report.records[:1] == prior.records
        assert outcome.execution_uncertainty is None
    else:
        assert outcome.execution_report == prior
        assert outcome.results == prior.results
        assert outcome.execution_uncertainty is not None
        assert outcome.execution_uncertainty.dispatch_status is DispatchStatus.UNKNOWN
        assert outcome.execution_uncertainty.prior_report_hash == prior.content_hash


def test_pre_dispatch_clock_and_abort_failure_records_known_no_dispatch_uncertainty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_PRE_DISPATCH_ABORT_FAILURE"

    class ArmableClock:
        def __init__(self) -> None:
            self.failed = False
            self.reads = 0

        def __call__(self) -> datetime:
            if self.failed:
                raise RuntimeError(marker)
            value = datetime(2026, 7, 25, 8, 0, tzinfo=UTC) + timedelta(seconds=self.reads)
            self.reads += 1
            return value

    class AbortFailureBeforeDispatch(RecordingExecutor):
        def __init__(
            self,
            trace: Trace,
            delegate: GovernedExecutor,
            clock: ArmableClock,
        ) -> None:
            super().__init__(trace, delegate)
            self._clock = clock
            self.abort_calls = 0

        def begin_attempt(
            self,
            plan: ExecutionPlan,
            policy_decision: PolicyDecision,
            approval_id: UUID | None,
        ) -> ExecutionAttemptAuthorization:
            authorization = super().begin_attempt(plan, policy_decision, approval_id)
            self._clock.failed = True
            return authorization

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            del authorization, confirmation_reader
            raise AssertionError("Runtime dispatched after its lifecycle clock failed")

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            del authorization, reason_code
            self.abort_calls += 1
            raise RuntimeError(marker)

    caplog.set_level(logging.INFO)
    clock = ArmableClock()
    trace = Trace()
    runtime = RuntimeEngine(clock=clock)
    hostile = AbortFailureBeforeDispatch(trace, runtime._executor, clock)
    runtime._executor = cast(GovernedExecutor, hostile)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "execution_abort_uncertain"
    assert RuntimeState.EXECUTING not in outcome.task.state_history
    assert outcome.execution_report is None
    assert outcome.results == ()
    assert hostile.abort_calls == 1
    uncertainty = outcome.execution_uncertainty
    assert uncertainty is not None
    assert uncertainty.dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert uncertainty.effect_disposition is EffectDisposition.NONE
    assert not uncertainty.human_intervention_required
    assert uncertainty.prior_report_hash is None
    audits = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "execution_uncertainty_audit"
    ]
    assert len(audits) == 1
    assert audits[0]["dispatch_status"] == "NOT_DISPATCHED"
    assert RuntimeOutcome.model_validate_json(outcome.model_dump_json()) == outcome
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


def test_closed_action_with_forged_report_records_unknown_uncertainty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_CLOSED_ACTION_ABORT_FAILURE"

    class ActionPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            action = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
            return plan.model_copy(update={"steps": (action,)})

    class CloseThenForgeExecutor(RecordingExecutor):
        def __init__(self, trace: Trace, delegate: GovernedExecutor) -> None:
            super().__init__(trace, delegate)
            self.trusted_closed_report: ExecutionReport | None = None
            self.abort_calls = 0

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            report = super().execute_actions(authorization, confirmation_reader)
            self.trusted_closed_report = report
            return forge_execution_report(report, total_duration_ms=None)

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            del authorization, reason_code
            self.abort_calls += 1
            raise RuntimeError(marker)

    caplog.set_level(logging.INFO)
    trace = Trace()
    runtime = RuntimeEngine(planner=ActionPlanner())
    hostile = CloseThenForgeExecutor(trace, runtime._executor)
    runtime._executor = cast(GovernedExecutor, hostile)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    trusted = hostile.trusted_closed_report
    assert trusted is not None
    assert trusted.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert trusted.records[0].dispatch_status is DispatchStatus.HANDLER_DISPATCHED
    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "execution_abort_uncertain"
    assert outcome.execution_report is None
    assert outcome.results == ()
    assert hostile.abort_calls == 1
    uncertainty = outcome.execution_uncertainty
    assert uncertainty is not None
    assert uncertainty.dispatch_status is DispatchStatus.UNKNOWN
    assert uncertainty.effect_disposition is EffectDisposition.UNKNOWN
    assert uncertainty.human_intervention_required
    assert uncertainty.prior_report_hash is None
    audits = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "execution_uncertainty_audit"
    ]
    assert len(audits) == 1
    assert set(audits[0]) == {
        "event",
        "task_id",
        "plan_id",
        "approval_id",
        "execution_attempt_id",
        "authorization_hash",
        "prior_report_hash",
        "uncertainty_hash",
        "uncertainty_kind",
        "dispatch_status",
        "effect_disposition",
        "human_intervention_required",
        "reason_code",
        "verification",
    }
    assert RuntimeOutcome.model_validate_json(outcome.model_dump_json()) == outcome
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


def test_action_report_with_unconsumed_confirmation_is_rejected() -> None:
    class ActionPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            action = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
            return plan.model_copy(update={"steps": (action,)})

    class ConfirmationForgingExecutor(RecordingExecutor):
        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            report = super().execute_actions(authorization, confirmation_reader)
            forged_record = report.records[0].model_copy(
                update={
                    "confirmation_id": uuid4(),
                    "confirmation_record_hash": "f" * 64,
                }
            )
            return forge_execution_report(report, records=(forged_record,))

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            del authorization, reason_code
            raise RuntimeError(SENSITIVE_MARKER)

    trace = Trace()
    runtime = RuntimeEngine(planner=ActionPlanner())
    hostile = ConfirmationForgingExecutor(trace, runtime._executor)
    runtime._executor = cast(GovernedExecutor, hostile)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "execution_abort_uncertain"
    assert outcome.execution_report is None
    assert outcome.results == ()
    assert outcome.execution_uncertainty is not None
    assert outcome.execution_uncertainty.dispatch_status is DispatchStatus.UNKNOWN
    assert RuntimeOutcome.model_validate_json(outcome.model_dump_json()) == outcome


def test_clock_and_abort_failure_after_action_preserves_unknown_prior_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_POST_ACTION_CLOCK_ABORT_FAILURE"

    class ArmableClock:
        def __init__(self) -> None:
            self.failed = False
            self.reads = 0

        def __call__(self) -> datetime:
            if self.failed:
                raise RuntimeError(marker)
            value = datetime(2026, 7, 25, 8, 0, tzinfo=UTC) + timedelta(seconds=self.reads)
            self.reads += 1
            return value

    class ActionVerifyPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            action = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
            verify = plan.steps[0].model_copy(
                update={"step_id": "verify-system-status", "role": StepRole.VERIFY}
            )
            criterion = plan.verification_criteria[0].model_copy(
                update={"evidence_step_id": verify.step_id}
            )
            return plan.model_copy(
                update={
                    "steps": (action, verify),
                    "verification_criteria": (criterion,),
                }
            )

    class ArmClockAfterAction(RecordingExecutor):
        def __init__(
            self,
            trace: Trace,
            delegate: GovernedExecutor,
            clock: ArmableClock,
        ) -> None:
            super().__init__(trace, delegate)
            self._clock = clock
            self.prior_report: ExecutionReport | None = None

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            report = super().execute_actions(authorization, confirmation_reader)
            self.prior_report = report
            self._clock.failed = True
            return report

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            del authorization, reason_code
            raise RuntimeError(marker)

    caplog.set_level(logging.INFO)
    clock = ArmableClock()
    trace = Trace()
    runtime = RuntimeEngine(planner=ActionVerifyPlanner(), clock=clock)
    declare_mock_verification_tool(runtime)
    hostile = ArmClockAfterAction(trace, runtime._executor, clock)
    runtime._executor = cast(GovernedExecutor, hostile)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    prior = hostile.prior_report
    assert prior is not None
    assert prior.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.execution_report == prior
    assert outcome.results == prior.results
    uncertainty = outcome.execution_uncertainty
    assert uncertainty is not None
    assert uncertainty.dispatch_status is DispatchStatus.UNKNOWN
    assert uncertainty.effect_disposition is EffectDisposition.UNKNOWN
    assert uncertainty.human_intervention_required
    assert uncertainty.prior_report_hash == prior.content_hash
    audits = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "execution_uncertainty_audit"
    ]
    assert len(audits) == 1
    assert audits[0]["prior_report_hash"] == prior.content_hash
    assert RuntimeOutcome.model_validate_json(outcome.model_dump_json()) == outcome
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


def test_closed_verification_with_forged_report_preserves_hash_bound_prior_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_CLOSED_VERIFY_ABORT_FAILURE"

    class ActionVerifyPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            action = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
            verify = plan.steps[0].model_copy(
                update={"step_id": "verify-system-status", "role": StepRole.VERIFY}
            )
            criterion = plan.verification_criteria[0].model_copy(
                update={"evidence_step_id": verify.step_id}
            )
            return plan.model_copy(
                update={
                    "steps": (action, verify),
                    "verification_criteria": (criterion,),
                }
            )

    class CloseVerifyThenForgeExecutor(RecordingExecutor):
        def __init__(self, trace: Trace, delegate: GovernedExecutor) -> None:
            super().__init__(trace, delegate)
            self.prior_report: ExecutionReport | None = None
            self.closed_report: ExecutionReport | None = None
            self.abort_calls = 0

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            report = super().execute_actions(authorization, confirmation_reader)
            self.prior_report = report
            return report

        def execute_verification(
            self,
            authorization: ExecutionAttemptAuthorization,
        ) -> ExecutionReport:
            report = super().execute_verification(authorization)
            self.closed_report = report
            return forge_execution_report(report, total_duration_ms=None)

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            del authorization, reason_code
            self.abort_calls += 1
            raise RuntimeError(marker)

    caplog.set_level(logging.INFO)
    trace = Trace()
    runtime = RuntimeEngine(planner=ActionVerifyPlanner())
    declare_mock_verification_tool(runtime)
    hostile = CloseVerifyThenForgeExecutor(trace, runtime._executor)
    runtime._executor = cast(GovernedExecutor, hostile)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    prior = hostile.prior_report
    closed = hostile.closed_report
    assert prior is not None
    assert prior.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
    assert closed is not None
    assert closed.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert hostile.abort_calls == 1
    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.execution_report == prior
    assert outcome.results == prior.results
    uncertainty = outcome.execution_uncertainty
    assert uncertainty is not None
    assert uncertainty.dispatch_status is DispatchStatus.UNKNOWN
    assert uncertainty.effect_disposition is EffectDisposition.UNKNOWN
    assert uncertainty.human_intervention_required
    assert uncertainty.prior_report_hash == prior.content_hash
    audits = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "execution_uncertainty_audit"
    ]
    assert len(audits) == 1
    assert audits[0]["prior_report_hash"] == prior.content_hash
    assert audits[0]["verification"] == "not_run"
    assert RuntimeOutcome.model_validate_json(outcome.model_dump_json()) == outcome
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


def test_valid_multistep_l0_plan_runs_in_order_through_policy_and_execution() -> None:
    class MultiStepPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            second = plan.steps[0].model_copy(update={"step_id": "second-status"})
            return plan.model_copy(update={"steps": (*plan.steps, second)})

    trace = Trace()
    registry = build_default_registry()
    runtime = RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=MultiStepPlanner(),
        verifier=RecordingVerifier(trace),
        registry=registry,
    )
    runtime._executor = cast(
        GovernedExecutor,
        RecordingExecutor(trace, runtime._executor),
    )
    install_recording_policy(runtime, trace)
    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert outcome.failure is None
    assert outcome.plan is not None
    assert outcome.policy_decision is not None
    assert [step.step_id for step in outcome.plan.steps] == [
        "get-system-status",
        "second-status",
    ]
    assert [step.step_id for step in outcome.policy_decision.step_decisions] == [
        "get-system-status",
        "second-status",
    ]
    assert [result.plan_step_id for result in outcome.results] == [
        "get-system-status",
        "second-status",
    ]
    assert trace.calls == ["context", "policy", "executor", "tool", "verifier"]


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

    runtime = RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=UntrustedIdentifierPlanner(),
        verifier=RecordingVerifier(trace),
        registry=registry,
    )
    runtime._executor = cast(
        GovernedExecutor,
        RecordingExecutor(trace, runtime._executor),
    )
    install_recording_policy(runtime, trace)
    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "plan_mismatch"
    assert outcome.plan is None
    assert trace.calls == ["context"]
    assert SENSITIVE_MARKER not in outcome.model_dump_json()
    assert SENSITIVE_MARKER not in caplog.text


def test_poisoned_exact_plan_model_dump_system_exit_fails_before_policy_or_tool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_PLAN_MODEL_DUMP_SYSTEM_EXIT"
    trace = Trace()

    class PoisonedPlanPlanner(RecordingPlanner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)

            def exiting_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
                del args, kwargs
                raise SystemExit(marker)

            object.__setattr__(plan, "model_dump", exiting_model_dump)
            return plan

    registry = build_default_registry()
    runtime = RuntimeEngine(
        context_builder=RecordingContextBuilder(trace),
        planner=PoisonedPlanPlanner(trace),
        verifier=RecordingVerifier(trace),
        registry=registry,
    )
    runtime._executor = cast(
        GovernedExecutor,
        RecordingExecutor(trace, runtime._executor),
    )
    install_recording_policy(runtime, trace)
    caplog.set_level(logging.INFO)

    outcome = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.component is RuntimeComponent.PLANNER
    assert outcome.failure.code == "plan_mismatch"
    assert outcome.plan is None
    assert outcome.policy_decision is None
    assert trace.calls == ["context", "planner"]
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


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
    trace = Trace()
    paused = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    ).run(Task(request=SUPPORTED_REQUEST))

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

    trace = Trace()
    paused = make_runtime(
        trace,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    ).run(Task(request=SUPPORTED_REQUEST))
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


def test_clock_system_exit_on_first_read_is_sanitized_and_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_FIRST_CLOCK_SYSTEM_EXIT"
    trace = Trace()
    caplog.set_level(logging.INFO)

    def exiting_clock() -> datetime:
        raise SystemExit(marker)

    with pytest.raises(InvalidClockError) as caught:
        make_runtime(trace, clock=exiting_clock).run(Task(request=SUPPORTED_REQUEST))

    assert trace.calls == []
    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
    assert marker not in caplog.text


def test_clock_system_exit_after_execution_returns_sanitized_failed_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_LATE_CLOCK_SYSTEM_EXIT"
    values = iter(clock_values(10))
    trace = Trace()
    caplog.set_level(logging.INFO)

    def exiting_clock() -> datetime:
        try:
            return next(values)
        except StopIteration:
            raise SystemExit(marker) from None

    outcome = make_runtime(
        trace,
        clock=exiting_clock,
    ).run(Task(request=SUPPORTED_REQUEST))

    assert outcome.status is RuntimeOutcomeStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code == "invalid_clock"
    assert outcome.failure.component is RuntimeComponent.RUNTIME
    assert outcome.task.state_history[-2:] == (
        RuntimeState.EXECUTING,
        RuntimeState.FAILED,
    )
    assert len(outcome.results) == 1
    assert trace.calls == ["context", "planner", "policy", "executor", "tool"]
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


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
    remaining = clock_values(14)
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
            ["context", "planner", "policy", "executor", "tool"],
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
    trace = Trace()

    outcome = make_runtime(
        trace,
        clock=SequenceClock(clock_values(8)),
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
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
    trace = Trace()
    runtime = make_runtime(
        trace,
        clock=SequenceClock(clock_values(valid_clock_reads)),
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
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


def test_logging_handler_system_exit_cannot_interrupt_allow_lifecycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SENSITIVE_LOG_HANDLER_SYSTEM_EXIT"

    class ExitingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def emit(self, record: logging.LogRecord) -> None:
            del record
            self.calls += 1
            raise SystemExit(marker)

    handler = ExitingHandler()
    runtime_logger = logging.getLogger("ai_server.runtime.engine")
    trace = Trace()
    caplog.set_level(logging.INFO)
    runtime_logger.addHandler(handler)
    try:
        outcome = make_runtime(trace).run(Task(request=SUPPORTED_REQUEST))
    finally:
        runtime_logger.removeHandler(handler)

    assert outcome.status is RuntimeOutcomeStatus.COMPLETED
    assert outcome.task.state is RuntimeState.COMPLETED
    assert trace.calls.count("tool") == 1
    assert handler.calls > 0
    assert marker not in outcome.model_dump_json()
    assert marker not in caplog.text


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
    verification_audits = [
        message for message in messages if message["event"] == "verification_audit"
    ]

    assert len(transitions) == 7
    assert all(message["task_id"] == str(outcome.task.task_id) for message in transitions)
    assert transitions[-1]["to_state"] == "COMPLETED"
    assert len(audits) == 1
    assert outcome.execution_authorization is not None
    assert outcome.execution_report is not None
    duration_ms = audits[0].pop("duration_ms")
    assert type(duration_ms) is int and duration_ms >= 0
    assert audits == [
        {
            "approval_id": None,
            "arguments": {"redacted": True},
            "dispatch_status": "HANDLER_DISPATCHED",
            "effect_disposition": "NONE",
            "event": "execution_audit",
            "execution_attempt_id": str(outcome.execution_authorization.execution_attempt_id),
            "failure_code": None,
            "invocation_id": str(outcome.execution_report.records[0].invocation_id),
            "operator": "local-user",
            "plan_id": audits[0]["plan_id"],
            "report_hash": outcome.execution_report.content_hash,
            "result": "success",
            "role": "OBSERVE",
            "step_index": 0,
            "target": "local-mock",
            "task_id": str(outcome.task.task_id),
            "tool": "get_system_status",
            "tool_version": "1.0.0",
            "user": "local-user",
            "verification": "passed",
        }
    ]
    assert outcome.verification_result is not None
    assert len(verification_audits) == 1
    verification_audit = verification_audits[0]
    assert verification_audit["status"] == "PASSED"
    assert verification_audit["verification_result_hash"] == (
        outcome.verification_result.content_hash
    )
    assert verification_audit["execution_report_hash"] == (outcome.execution_report.content_hash)
    assert verification_audit["effect_disposition"] == "NONE"
    assert verification_audit["human_intervention_required"] is False
    assert "expected" not in verification_audit
    assert "raw_evidence" not in verification_audit


def test_runtime_emits_exact_redacted_policy_decision_audit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    outcome = RuntimeEngine().run(Task(request=SUPPORTED_REQUEST))
    messages = [json.loads(record.message) for record in caplog.records]
    policy_audits = [message for message in messages if message["event"] == "policy_decision"]

    assert outcome.policy_decision is not None
    assert outcome.plan is not None
    assert len(policy_audits) == 1
    audit = policy_audits[0]
    step_audit = audit["steps"][0]
    step = outcome.plan.steps[0]
    assert audit["task_id"] == str(outcome.task.task_id)
    assert audit["plan_id"] == str(outcome.plan.plan_id)
    assert audit["policy_id"] == outcome.policy_decision.policy_id
    assert audit["policy_version"] == outcome.policy_decision.policy_version
    assert audit["policy_hash"] == outcome.policy_decision.policy_hash
    assert audit["effect"] == "ALLOW"
    assert audit["reason_code"] == "allowed"
    assert step_audit["tool_id"] == step.tool_id
    assert step_audit["tool_version"] == step.tool_version
    assert step_audit["contract_hash"] == step.contract_hash
    assert step_audit["implementation_hash"] == step.implementation_hash
    assert step_audit["arguments_hash"] == canonical_json_sha256(step.arguments)
    assert "arguments" not in step_audit
    assert "request" not in audit


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
    verification_audits = [
        message for message in messages if message["event"] == "verification_audit"
    ]

    assert executor_failure.task.state is RuntimeState.FAILED
    assert verifier_failure.task.state is RuntimeState.FAILED
    assert [audit["verification"] for audit in audits] == ["not_run", "failed"]
    assert [audit["result"] for audit in audits] == ["invalid", "success"]
    assert all(audit["report_hash"] for audit in audits)
    assert len(verification_audits) == 1
    assert verification_audits[0]["status"] == "FAILED"
    assert verification_audits[0]["failure_reasons"] == ["VERIFIER_FAILED"]
    assert SENSITIVE_MARKER not in caplog.text
