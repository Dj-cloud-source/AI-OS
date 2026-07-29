"""Fail-closed local Runtime orchestration."""

import json
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from pydantic import ValidationError

from ai_server.context.builder import ContextBuilder
from ai_server.executor.service import Executor
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.policy import (
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEffect,
    PolicyEvaluationContext,
    PolicyReasonCode,
    StepPolicyDecision,
)
from ai_server.models.runtime import (
    LifecycleEvent,
    LifecycleEventKind,
    RuntimeComponent,
    RuntimeFailure,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
)
from ai_server.models.system_status import GetSystemStatusArguments, ServiceStatus, SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import RiskLevel, TargetReference, ToolError, ToolMetadata, ToolResult
from ai_server.planner.service import Planner
from ai_server.policy.engine import PolicyEngine
from ai_server.runtime.errors import (
    ApprovalRequiredError,
    ApprovalResumeUnavailableError,
    InvalidClockError,
    InvalidRuntimeOutcomeError,
    InvalidStateTransitionError,
    InvalidTaskError,
    PlanMismatchError,
    PolicyDeniedError,
    PolicyEvaluationError,
    ReservedStateTransitionError,
    TerminalStateMutationError,
    ToolExecutionError,
    UnsupportedTaskError,
    VerificationError,
)
from ai_server.runtime.state import RuntimeState, RuntimeStateMachine
from ai_server.tools.bootstrap import (
    GET_SYSTEM_STATUS_TOOL_ID,
    GET_SYSTEM_STATUS_TOOL_VERSION,
    build_default_registry,
)
from ai_server.tools.gateway import ToolGateway
from ai_server.tools.hashing import CanonicalizationError, canonical_json_sha256
from ai_server.tools.registry import ToolKey, ToolRegistry
from ai_server.verifier.service import Verifier

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

_RESERVED_STATES = frozenset(
    {
        RuntimeState.PARTIAL_SUCCESS,
        RuntimeState.ROLLBACK,
        RuntimeState.MANUAL_INTERVENTION_REQUIRED,
    }
)
_TERMINAL_STATES = frozenset({RuntimeState.COMPLETED, RuntimeState.FAILED})


def _utc_now() -> datetime:
    """Return the current UTC time for lifecycle evidence."""
    return datetime.now(UTC)


def _safe_log(payload: dict[str, object]) -> None:
    """Emit a structured record without letting logging break Runtime state."""
    with suppress(BaseException):  # noqa: B036 - logging cannot control Runtime.
        logger.info(json.dumps(payload, sort_keys=True))


def _safe_log_lifecycle_event(event: LifecycleEvent) -> None:
    """Serialize and emit lifecycle evidence without affecting Runtime state."""
    with suppress(BaseException):  # noqa: B036 - logging cannot control Runtime.
        logger.info(
            json.dumps(
                {
                    "event": "runtime_lifecycle_event",
                    **event.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )


def _safe_log_policy_decision(decision: PolicyDecision) -> None:
    """Emit a redacted structured Policy audit without raw arguments."""
    with suppress(BaseException):  # noqa: B036 - logging cannot control Runtime.
        logger.info(
            json.dumps(
                {
                    "event": "policy_decision",
                    "task_id": str(decision.task_id),
                    "plan_id": str(decision.plan_id),
                    "policy_id": decision.policy_id,
                    "policy_version": decision.policy_version,
                    "policy_hash": decision.policy_hash,
                    "operator_id": decision.operator_id,
                    "target": decision.target.model_dump(mode="json"),
                    "effect": decision.effect.value,
                    "reason_code": decision.reason_code.value,
                    "effective_risk": (
                        decision.effective_risk.value
                        if decision.effective_risk is not None
                        else None
                    ),
                    "approval_requirement": (
                        decision.approval_requirement.value
                        if decision.approval_requirement is not None
                        else None
                    ),
                    "manual_confirmation_requirement": (
                        decision.manual_confirmation_requirement.value
                        if decision.manual_confirmation_requirement is not None
                        else None
                    ),
                    "steps": [
                        {
                            "step_id": step.step_id,
                            "tool_id": step.tool_id,
                            "tool_version": step.tool_version,
                            "contract_hash": step.contract_hash,
                            "implementation_hash": step.implementation_hash,
                            "arguments_hash": step.arguments_hash,
                            "resolved_risk": (
                                step.resolved_risk.value if step.resolved_risk is not None else None
                            ),
                            "effect": step.effect.value,
                            "reason_code": step.reason_code.value,
                        }
                        for step in decision.step_decisions
                    ],
                },
                sort_keys=True,
            )
        )


def _normalize_utc_timestamp(value: object) -> datetime | None:
    """Copy an untrusted clock value into a built-in UTC datetime."""
    try:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            return None
        return datetime(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            tzinfo=UTC,
            fold=value.fold,
        )
    except BaseException:  # noqa: B036 - untrusted datetime hooks cannot control Runtime.
        return None


def _is_builtin_uuid(value: object) -> bool:
    """Return whether an identifier is an exact built-in UUID instance."""
    return type(value) is UUID


def _task_has_trusted_nested_fields(task: Task) -> bool:
    """Return whether Task containers contain only canonical Runtime states."""
    return (
        type(task) is Task
        and _is_builtin_uuid(task.task_id)
        and type(task.state_history) is tuple
        and all(type(state) is RuntimeState for state in task.state_history)
    )


def _plan_has_trusted_nested_fields(plan: ExecutionPlan) -> bool:
    """Return whether a plan contains only exact Phase 1 step models."""
    return (
        type(plan) is ExecutionPlan
        and _is_builtin_uuid(plan.plan_id)
        and _is_builtin_uuid(plan.task_id)
        and type(plan.steps) is tuple
        and all(
            type(step) is ExecutionStep and type(step.arguments) is GetSystemStatusArguments
            for step in plan.steps
        )
    )


def _result_has_trusted_nested_fields(result: ToolResult[SystemStatus]) -> bool:
    """Return whether Tool evidence contains only exact structured result models."""
    if type(result) is not ToolResult[SystemStatus]:
        return False
    if result.success:
        return (
            type(result.data) is SystemStatus
            and type(result.data.services) is tuple
            and all(type(service) is ServiceStatus for service in result.data.services)
            and result.error is None
        )
    return result.data is None and type(result.error) is ToolError


def _policy_decision_has_trusted_nested_fields(decision: PolicyDecision) -> bool:
    """Reject unsafe Policy identity, enum, target, and Step objects."""
    if (
        type(decision) is not PolicyDecision
        or not _is_builtin_uuid(decision.task_id)
        or not _is_builtin_uuid(decision.plan_id)
        or type(decision.target) is not TargetReference
        or type(decision.effect) is not PolicyEffect
        or type(decision.reason_code) is not PolicyReasonCode
        or (decision.effective_risk is not None and type(decision.effective_risk) is not RiskLevel)
        or (
            decision.approval_requirement is not None
            and type(decision.approval_requirement) is not PolicyApprovalRequirement
        )
        or (
            decision.manual_confirmation_requirement is not None
            and type(decision.manual_confirmation_requirement) is not ManualConfirmationRequirement
        )
        or type(decision.step_decisions) is not tuple
    ):
        return False
    return all(
        type(step) is StepPolicyDecision
        and type(step.effect) is PolicyEffect
        and type(step.reason_code) is PolicyReasonCode
        and (step.resolved_risk is None or type(step.resolved_risk) is RiskLevel)
        and (
            step.approval_requirement is None
            or type(step.approval_requirement) is PolicyApprovalRequirement
        )
        and (
            step.manual_confirmation_requirement is None
            or type(step.manual_confirmation_requirement) is ManualConfirmationRequirement
        )
        for step in decision.step_decisions
    )


def _outcome_has_trusted_identity_fields(outcome: RuntimeOutcome) -> bool:
    """Reject unsafe identity and timestamp objects before model serialization."""
    if not _task_has_trusted_nested_fields(outcome.task):
        return False
    if outcome.plan is not None and not _plan_has_trusted_nested_fields(outcome.plan):
        return False
    if outcome.policy_decision is not None and not _policy_decision_has_trusted_nested_fields(
        outcome.policy_decision
    ):
        return False
    if type(outcome.events) is not tuple:
        return False
    for event in outcome.events:
        if (
            type(event) is not LifecycleEvent
            or not _is_builtin_uuid(event.task_id)
            or type(event.sequence) is not int
            or type(event.occurred_at) is not datetime
            or event.occurred_at.tzinfo is not UTC
            or type(event.kind) is not LifecycleEventKind
            or type(event.state) is not RuntimeState
            or (event.previous_state is not None and type(event.previous_state) is not RuntimeState)
            or (event.component is not None and type(event.component) is not RuntimeComponent)
            or (event.reason_code is not None and type(event.reason_code) is not str)
        ):
            return False
    if type(outcome.results) is not tuple or any(
        not _result_has_trusted_nested_fields(result) for result in outcome.results
    ):
        return False
    return outcome.failure is None or type(outcome.failure) is RuntimeFailure


class _LifecycleRecorder:
    """Build one immutable ordered lifecycle from a trusted clock."""

    def __init__(
        self,
        *,
        task_id: UUID,
        clock: Clock,
        existing: tuple[LifecycleEvent, ...] = (),
    ) -> None:
        self._task_id = task_id
        self._clock = clock
        self._events = list(existing)
        self._last_timestamp = (
            _normalize_utc_timestamp(existing[-1].occurred_at) if existing else None
        )
        if existing and self._last_timestamp is None:
            raise InvalidRuntimeOutcomeError(
                "RuntimeOutcome contains an untrusted lifecycle timestamp"
            )
        self._clock_failed = False

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        """Return an immutable snapshot of recorded events."""
        return tuple(self._events)

    def record(
        self,
        *,
        kind: LifecycleEventKind,
        state: RuntimeState,
        previous_state: RuntimeState | None = None,
        component: RuntimeComponent | None = None,
        reason_code: str | None = None,
    ) -> bool:
        """Append one validated lifecycle fact and report whether clock data was valid."""
        timestamp = self._read_clock()
        if timestamp is None:
            return False
        self._append(
            timestamp=timestamp,
            kind=kind,
            state=state,
            previous_state=previous_state,
            component=component,
            reason_code=reason_code,
        )
        return True

    def record_with_last_timestamp(
        self,
        *,
        kind: LifecycleEventKind,
        state: RuntimeState,
        previous_state: RuntimeState | None = None,
        component: RuntimeComponent | None = None,
        reason_code: str | None = None,
    ) -> None:
        """Append emergency evidence using the most recent trusted timestamp."""
        if self._last_timestamp is None:
            raise InvalidClockError("Runtime has no trusted timestamp for failure evidence")
        self._append(
            timestamp=self._last_timestamp,
            kind=kind,
            state=state,
            previous_state=previous_state,
            component=component,
            reason_code=reason_code,
        )

    def _append(
        self,
        *,
        timestamp: datetime,
        kind: LifecycleEventKind,
        state: RuntimeState,
        previous_state: RuntimeState | None,
        component: RuntimeComponent | None,
        reason_code: str | None,
    ) -> None:
        try:
            event = LifecycleEvent(
                task_id=self._task_id,
                sequence=len(self._events),
                occurred_at=timestamp,
                kind=kind,
                state=state,
                previous_state=previous_state,
                component=component,
                reason_code=reason_code,
            )
        except ValidationError:
            raise InvalidRuntimeOutcomeError(
                "Runtime produced contradictory lifecycle evidence"
            ) from None
        self._events.append(event)
        self._last_timestamp = timestamp
        _safe_log_lifecycle_event(event)

    def _read_clock(self) -> datetime | None:
        if self._clock_failed:
            return None
        try:
            timestamp = _normalize_utc_timestamp(self._clock())
            if timestamp is None or (
                self._last_timestamp is not None and timestamp < self._last_timestamp
            ):
                raise ValueError
        except BaseException:  # noqa: B036 - untrusted clock failures must fail closed.
            self._clock_failed = True
            return None
        return timestamp


class RuntimeEngine:
    """Own Task state and orchestrate the five local-only Runtime components."""

    def __init__(
        self,
        *,
        context_builder: ContextBuilder | None = None,
        planner: Planner | None = None,
        executor: Executor | None = None,
        verifier: Verifier | None = None,
        registry: ToolRegistry | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        """Compose the concrete local-only Runtime from a verified Tool Registry."""
        self._context_builder = context_builder if context_builder is not None else ContextBuilder()
        self._planner = planner if planner is not None else Planner()
        self._verifier = verifier if verifier is not None else Verifier()
        self._clock = clock
        self._registry = self._validate_registry(
            registry if registry is not None else self._build_default_registry()
        )
        self._catalog = self._registry.metadata_snapshot()
        self._tool_metadata = self._resolve_runtime_metadata(self._catalog)
        self._policy = PolicyEngine(self._registry)
        self._executor = executor if executor is not None else Executor(ToolGateway(self._registry))

    def run(self, task: Task) -> RuntimeOutcome:
        """Run a fresh Task until completion, failure, or an approval pause."""
        task = self._validate_task(task)
        self._ensure_fresh_task(task)
        recorder = _LifecycleRecorder(task_id=task.task_id, clock=self._clock)
        if not recorder.record(kind=LifecycleEventKind.STATE_ENTERED, state=task.state):
            raise InvalidClockError("Runtime clock failed before lifecycle recording began")

        task, transition_recorded = self._transition(
            task,
            RuntimeState.CONTEXT_BUILDING,
            recorder,
        )
        if not transition_recorded:
            return self._clock_failure_outcome(task=task, recorder=recorder)
        try:
            raw_context = self._context_builder.build(task)
            context = self._validate_context(raw_context, task)
        except BaseException as error:
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.CONTEXT_BUILDER,
                error=error,
            )
        if not recorder.record(
            kind=LifecycleEventKind.COMPONENT_COMPLETED,
            state=task.state,
            component=RuntimeComponent.CONTEXT_BUILDER,
        ):
            recorder.record_with_last_timestamp(
                kind=LifecycleEventKind.COMPONENT_COMPLETED,
                state=task.state,
                component=RuntimeComponent.CONTEXT_BUILDER,
            )
            return self._clock_failure_outcome(task=task, recorder=recorder)

        task, transition_recorded = self._transition(
            task,
            RuntimeState.PLANNING,
            recorder,
        )
        if not transition_recorded:
            return self._clock_failure_outcome(task=task, recorder=recorder)
        try:
            raw_plan = self._planner.create_plan(context, self._tool_metadata)
            plan = self._validate_plan(raw_plan)
            if plan.task_id != task.task_id or plan.target != task.target:
                raise PlanMismatchError("Planner returned a plan for a different Task or target")
        except BaseException as error:
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.PLANNER,
                error=error,
            )
        if not recorder.record(
            kind=LifecycleEventKind.COMPONENT_COMPLETED,
            state=task.state,
            component=RuntimeComponent.PLANNER,
        ):
            recorder.record_with_last_timestamp(
                kind=LifecycleEventKind.COMPONENT_COMPLETED,
                state=task.state,
                component=RuntimeComponent.PLANNER,
            )
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
            )

        task, transition_recorded = self._transition(
            task,
            RuntimeState.POLICY_CHECK,
            recorder,
        )
        if not transition_recorded:
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
            )
        policy_context = PolicyEvaluationContext(
            operator_id=task.user,
            target=TargetReference(
                target_id=task.target,
                resource_type="local_system",
                resource_id=task.target,
            ),
        )
        try:
            policy_evaluate = cast(Callable[..., object], self._policy.evaluate)
            raw_policy_decision = policy_evaluate(plan, policy_context)
            policy_decision = self._validate_policy_decision(
                raw_policy_decision,
                plan=plan,
                context=policy_context,
            )
            _safe_log_policy_decision(policy_decision)
        except BaseException:
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.POLICY,
                error=PolicyEvaluationError(
                    "Policy did not return a trustworthy structured decision"
                ),
                plan=plan,
            )
        if policy_decision.effect is PolicyEffect.DENY:
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.POLICY,
                error=PolicyDeniedError("Policy denied the immutable execution plan"),
                plan=plan,
                policy_decision=policy_decision,
            )
        if not recorder.record(
            kind=LifecycleEventKind.COMPONENT_COMPLETED,
            state=task.state,
            component=RuntimeComponent.POLICY,
        ):
            recorder.record_with_last_timestamp(
                kind=LifecycleEventKind.COMPONENT_COMPLETED,
                state=task.state,
                component=RuntimeComponent.POLICY,
            )
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
            )

        task, transition_recorded = self._transition(
            task,
            RuntimeState.WAITING_FOR_APPROVAL,
            recorder,
        )
        if not transition_recorded:
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
            )
        if policy_decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL:
            if not recorder.record(
                kind=LifecycleEventKind.PAUSED,
                state=task.state,
                reason_code=ApprovalRequiredError.code,
            ):
                recorder.record_with_last_timestamp(
                    kind=LifecycleEventKind.PAUSED,
                    state=task.state,
                    reason_code=ApprovalRequiredError.code,
                )
                return self._clock_failure_outcome(
                    task=task,
                    recorder=recorder,
                    plan=plan,
                    policy_decision=policy_decision,
                )
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.WAITING_FOR_APPROVAL,
                task=task,
                plan=plan,
                policy_decision=policy_decision,
                events=recorder.events,
            )
        if not recorder.record(
            kind=LifecycleEventKind.APPROVAL_DECISION_RECORDED,
            state=task.state,
            reason_code="not_required",
        ):
            recorder.record_with_last_timestamp(
                kind=LifecycleEventKind.APPROVAL_DECISION_RECORDED,
                state=task.state,
                reason_code="not_required",
            )
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
            )

        task, transition_recorded = self._transition(
            task,
            RuntimeState.EXECUTING,
            recorder,
        )
        if not transition_recorded:
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
            )
        try:
            raw_results = self._executor.execute(plan)
            results = self._validate_results(raw_results, plan)
        except BaseException as error:
            self._log_execution_audit(
                task,
                plan,
                (),
                result_override="execution_failed",
                verification="not_run",
            )
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.EXECUTOR,
                error=error,
                plan=plan,
                policy_decision=policy_decision,
            )
        if not results[-1].success:
            self._log_execution_audit(
                task,
                plan,
                results,
                verification="not_run",
            )
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.EXECUTOR,
                error=ToolExecutionError("Tool returned a structured failure"),
                plan=plan,
                policy_decision=policy_decision,
                results=results,
            )
        if not recorder.record(
            kind=LifecycleEventKind.COMPONENT_COMPLETED,
            state=task.state,
            component=RuntimeComponent.EXECUTOR,
        ):
            recorder.record_with_last_timestamp(
                kind=LifecycleEventKind.COMPONENT_COMPLETED,
                state=task.state,
                component=RuntimeComponent.EXECUTOR,
            )
            self._log_execution_audit(
                task,
                plan,
                results,
                verification="not_run",
            )
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
            )

        task, transition_recorded = self._transition(
            task,
            RuntimeState.VERIFYING,
            recorder,
        )
        if not transition_recorded:
            self._log_execution_audit(
                task,
                plan,
                results,
                verification="not_run",
            )
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
            )
        try:
            verify = cast(Callable[..., object], self._verifier.verify)
            verification_result = verify(plan, results)
            if verification_result is not None:
                raise VerificationError("Verifier returned an invalid completion signal")
        except BaseException as error:
            self._log_execution_audit(
                task,
                plan,
                results,
                verification="failed",
            )
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.VERIFIER,
                error=error,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
            )
        if not recorder.record(
            kind=LifecycleEventKind.COMPONENT_COMPLETED,
            state=task.state,
            component=RuntimeComponent.VERIFIER,
        ):
            recorder.record_with_last_timestamp(
                kind=LifecycleEventKind.COMPONENT_COMPLETED,
                state=task.state,
                component=RuntimeComponent.VERIFIER,
            )
            self._log_execution_audit(
                task,
                plan,
                results,
                verification="passed",
            )
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
            )
        self._log_execution_audit(
            task,
            plan,
            results,
            verification="passed",
        )
        task, transition_recorded = self._transition(
            task,
            RuntimeState.COMPLETED,
            recorder,
        )
        if not transition_recorded:
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
            )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.COMPLETED,
            task=task,
            plan=plan,
            policy_decision=policy_decision,
            results=results,
            events=recorder.events,
        )

    def reject(self, outcome: RuntimeOutcome) -> RuntimeOutcome:
        """Record human rejection of an approval-paused Phase 1 outcome."""
        outcome = self._validate_outcome(outcome)
        if outcome.task.state in _TERMINAL_STATES:
            raise TerminalStateMutationError(
                f"Cannot reject terminal Runtime state: {outcome.task.state.value}"
            )
        if (
            outcome.status is not RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
            or outcome.task.state is not RuntimeState.WAITING_FOR_APPROVAL
        ):
            raise InvalidStateTransitionError(
                "Human rejection requires a WAITING_FOR_APPROVAL outcome"
            )

        recorder = _LifecycleRecorder(
            task_id=outcome.task.task_id,
            clock=self._clock,
            existing=outcome.events,
        )
        task, transition_recorded = self._transition(
            outcome.task,
            RuntimeState.FAILED,
            recorder,
        )
        if not transition_recorded:
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=outcome.plan,
                policy_decision=outcome.policy_decision,
                results=outcome.results,
            )
        failure = RuntimeFailure(
            code="human_rejected",
            component=RuntimeComponent.RUNTIME,
            message="Human approval was rejected.",
        )
        if not recorder.record(
            kind=LifecycleEventKind.REJECTED,
            state=task.state,
            component=failure.component,
            reason_code=failure.code,
        ):
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=outcome.plan,
                policy_decision=outcome.policy_decision,
                results=outcome.results,
            )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.FAILED,
            task=task,
            plan=outcome.plan,
            policy_decision=outcome.policy_decision,
            results=outcome.results,
            events=recorder.events,
            failure=failure,
        )

    @staticmethod
    def _build_default_registry() -> ToolRegistry:
        try:
            return build_default_registry()
        except BaseException:
            raise PolicyDeniedError(
                "Runtime could not bootstrap the reviewed Tool Registry"
            ) from None

    @staticmethod
    def _validate_registry(registry: ToolRegistry) -> ToolRegistry:
        if type(registry) is not ToolRegistry or not registry.is_frozen:
            raise PolicyDeniedError("Runtime rejected an unsealed Tool Registry")
        return registry

    @staticmethod
    def _resolve_runtime_metadata(
        catalog: Mapping[ToolKey, ToolMetadata],
    ) -> ToolMetadata:
        try:
            metadata = catalog.get(
                (
                    GET_SYSTEM_STATUS_TOOL_ID,
                    GET_SYSTEM_STATUS_TOOL_VERSION,
                )
            )
            if type(metadata) is not ToolMetadata:
                raise TypeError
            validated = ToolMetadata.model_validate(
                metadata.model_dump(mode="python", warnings="error"),
                strict=True,
            )
        except BaseException:
            raise PolicyDeniedError("Runtime could not resolve canonical Tool metadata") from None
        return validated

    @staticmethod
    def _validate_task(task: Task) -> Task:
        try:
            if not _task_has_trusted_nested_fields(task):
                raise TypeError
            validated = Task.model_validate(
                task.model_dump(mode="python", warnings="none"),
                strict=True,
            )
            if not _task_has_trusted_nested_fields(validated):
                raise TypeError
            return validated
        except BaseException:
            raise InvalidTaskError("Runtime rejected malformed Task input") from None

    @staticmethod
    def _ensure_fresh_task(task: Task) -> None:
        if task.state in _TERMINAL_STATES:
            raise TerminalStateMutationError(
                f"Runtime cannot rerun terminal state: {task.state.value}"
            )
        if task.state in _RESERVED_STATES:
            raise ReservedStateTransitionError(
                f"Runtime cannot run reserved state: {task.state.value}"
            )
        if task.state is RuntimeState.WAITING_FOR_APPROVAL:
            raise ApprovalResumeUnavailableError(
                "Approval-paused work cannot resume before Phase 4"
            )
        if task.state is not RuntimeState.RECEIVED or task.state_history != (
            RuntimeState.RECEIVED,
        ):
            raise InvalidStateTransitionError("Runtime.run accepts only a fresh RECEIVED Task")

    @staticmethod
    def _validate_context(context: RuntimeContext, task: Task) -> RuntimeContext:
        try:
            if type(context) is not RuntimeContext or not _is_builtin_uuid(context.task_id):
                raise TypeError
            validated = RuntimeContext.model_validate(
                context.model_dump(mode="python", warnings="none"),
                strict=True,
            )
            if (
                not _is_builtin_uuid(validated.task_id)
                or validated.task_id != task.task_id
                or validated.request != task.request
                or validated.user != task.user
                or validated.target != task.target
            ):
                raise TypeError
            return validated
        except BaseException:
            raise InvalidTaskError("Context Builder returned invalid Runtime context") from None

    def _validate_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        try:
            if not _plan_has_trusted_nested_fields(plan):
                raise TypeError
            validated = ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="none"),
                strict=True,
            )
            if not _plan_has_trusted_nested_fields(validated):
                raise TypeError
            if len(validated.steps) > 64:
                raise TypeError
            if any(
                step.role is not StepRole.OBSERVE or step.arguments.target != validated.target
                for step in validated.steps
            ):
                raise TypeError
            return validated
        except BaseException:
            raise PlanMismatchError("Planner returned a malformed execution plan") from None

    def _validate_policy_decision(
        self,
        decision: object,
        *,
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        try:
            if type(
                decision
            ) is not PolicyDecision or not _policy_decision_has_trusted_nested_fields(decision):
                raise TypeError
            validated = PolicyDecision.model_validate(
                decision.model_dump(mode="python", warnings="error"),
                strict=True,
            )
            if not _policy_decision_has_trusted_nested_fields(validated):
                raise TypeError
            if (
                validated.policy_id != self._policy.policy_id
                or validated.policy_version != self._policy.policy_version
                or validated.policy_hash != self._policy.policy_hash
                or validated.task_id != plan.task_id
                or validated.plan_id != plan.plan_id
                or validated.operator_id != context.operator_id
                or validated.target != context.target
                or len(validated.step_decisions) != len(plan.steps)
            ):
                raise TypeError
            first_denied: StepPolicyDecision | None = None
            for step, step_decision in zip(
                plan.steps,
                validated.step_decisions,
                strict=True,
            ):
                if (
                    step_decision.step_id != step.step_id
                    or step_decision.tool_id != step.tool_id
                    or step_decision.tool_version != step.tool_version
                    or step_decision.contract_hash != step.contract_hash
                    or step_decision.implementation_hash != step.implementation_hash
                    or step_decision.arguments_hash != canonical_json_sha256(step.arguments)
                ):
                    raise TypeError
                metadata = self._catalog.get((step.tool_id, step.tool_version))
                if metadata is None:
                    if (
                        step_decision.resolved_risk is not None
                        or step_decision.approval_requirement is not None
                        or step_decision.manual_confirmation_requirement is not None
                        or step_decision.effect is not PolicyEffect.DENY
                        or step_decision.reason_code is not PolicyReasonCode.UNKNOWN_TOOL
                    ):
                        raise TypeError
                elif (
                    type(metadata) is not ToolMetadata
                    or step_decision.resolved_risk is not metadata.risk_level
                    or step_decision.approval_requirement is None
                    or step_decision.manual_confirmation_requirement is None
                    or (
                        metadata.risk_level in {RiskLevel.L2, RiskLevel.L3}
                        and step_decision.approval_requirement
                        is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
                    )
                    or (
                        metadata.risk_level is RiskLevel.L3
                        and (
                            step_decision.effect is PolicyEffect.ALLOW
                            or step_decision.manual_confirmation_requirement
                            is not ManualConfirmationRequirement.PER_INVOCATION
                        )
                    )
                    or (
                        metadata.risk_level is not RiskLevel.L3
                        and step_decision.manual_confirmation_requirement
                        is not ManualConfirmationRequirement.NOT_REQUIRED
                    )
                    or (
                        step_decision.effect is PolicyEffect.ALLOW
                        and (
                            step.contract_hash != metadata.contract_hash
                            or step.implementation_hash != metadata.implementation_hash
                        )
                    )
                ):
                    raise TypeError
                if first_denied is None and step_decision.effect is PolicyEffect.DENY:
                    first_denied = step_decision
            if validated.effect is PolicyEffect.DENY:
                if first_denied is None or validated.reason_code is not first_denied.reason_code:
                    raise TypeError
            elif first_denied is not None:
                raise TypeError
            return validated
        except (
            CanonicalizationError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            raise PolicyEvaluationError("Runtime rejected an invalid PolicyDecision") from None
        except BaseException:
            raise PolicyEvaluationError(
                "Runtime could not validate the PolicyDecision safely"
            ) from None

    def _validate_results(
        self,
        results: tuple[ToolResult[SystemStatus], ...],
        plan: ExecutionPlan,
    ) -> tuple[ToolResult[SystemStatus], ...]:
        try:
            if type(results) is not tuple or not results or len(results) > len(plan.steps):
                raise TypeError
            validated_results: list[ToolResult[SystemStatus]] = []
            for step, raw_result in zip(plan.steps, results, strict=False):
                if not _result_has_trusted_nested_fields(raw_result):
                    raise TypeError
                result = ToolResult[SystemStatus].model_validate(
                    raw_result.model_dump(mode="python", warnings="error"),
                    strict=True,
                )
                expected_target = TargetReference(
                    target_id=plan.target,
                    resource_type="local_system",
                    resource_id=step.arguments.target,
                )
                if (
                    not _result_has_trusted_nested_fields(result)
                    or result.plan_step_id != step.step_id
                    or result.tool_id != step.tool_id
                    or result.tool_version != step.tool_version
                    or result.contract_hash != step.contract_hash
                    or result.arguments_hash != canonical_json_sha256(step.arguments)
                    or result.target != expected_target
                    or result.duration_ms > self._tool_metadata.timeout_ms
                ):
                    raise TypeError
                validated_results.append(result)
            if len(validated_results) != len(plan.steps) and validated_results[-1].success:
                raise TypeError
            if any(not result.success for result in validated_results[:-1]):
                raise TypeError
            return tuple(validated_results)
        except BaseException:
            raise ToolExecutionError("Executor returned invalid structured evidence") from None

    def _validate_outcome(self, outcome: RuntimeOutcome) -> RuntimeOutcome:
        try:
            if type(outcome) is not RuntimeOutcome or not _outcome_has_trusted_identity_fields(
                outcome
            ):
                raise TypeError
            validated = RuntimeOutcome.model_validate(
                outcome.model_dump(mode="python", warnings="none"),
                strict=True,
            )
            if not _outcome_has_trusted_identity_fields(validated):
                raise TypeError
            normalized_events: list[LifecycleEvent] = []
            for event in validated.events:
                timestamp = _normalize_utc_timestamp(event.occurred_at)
                if timestamp is None:
                    raise TypeError
                event_payload = event.model_dump(mode="python", warnings="none")
                event_payload["occurred_at"] = timestamp
                normalized_events.append(LifecycleEvent.model_validate(event_payload, strict=True))
            outcome_payload = validated.model_dump(mode="python", warnings="none")
            outcome_payload["events"] = tuple(normalized_events)
            validated = RuntimeOutcome.model_validate(outcome_payload, strict=True)
            if not _outcome_has_trusted_identity_fields(validated):
                raise TypeError
            if validated.plan is not None:
                self._validate_plan(validated.plan)
                if validated.policy_decision is not None:
                    self._validate_policy_decision(
                        validated.policy_decision,
                        plan=validated.plan,
                        context=PolicyEvaluationContext(
                            operator_id=validated.task.user,
                            target=TargetReference(
                                target_id=validated.task.target,
                                resource_type="local_system",
                                resource_id=validated.task.target,
                            ),
                        ),
                    )
            return validated
        except BaseException:
            raise InvalidRuntimeOutcomeError(
                "Runtime rejected malformed RuntimeOutcome input"
            ) from None

    def _failure_outcome(
        self,
        *,
        task: Task,
        recorder: _LifecycleRecorder,
        component: RuntimeComponent,
        error: BaseException,
        plan: ExecutionPlan | None = None,
        policy_decision: PolicyDecision | None = None,
        results: tuple[ToolResult[SystemStatus], ...] = (),
    ) -> RuntimeOutcome:
        code, message = self._safe_failure(component, error)
        task, transition_recorded = self._transition(
            task,
            RuntimeState.FAILED,
            recorder,
        )
        if not transition_recorded:
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
            )
        failure = RuntimeFailure(code=code, component=component, message=message)
        if not recorder.record(
            kind=LifecycleEventKind.FAILED,
            state=task.state,
            component=component,
            reason_code=code,
        ):
            return self._clock_failure_outcome(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
            )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.FAILED,
            task=task,
            plan=plan,
            policy_decision=policy_decision,
            results=results,
            events=recorder.events,
            failure=failure,
        )

    @staticmethod
    def _clock_failure_outcome(
        *,
        task: Task,
        recorder: _LifecycleRecorder,
        plan: ExecutionPlan | None = None,
        policy_decision: PolicyDecision | None = None,
        results: tuple[ToolResult[SystemStatus], ...] = (),
    ) -> RuntimeOutcome:
        if task.state is not RuntimeState.FAILED:
            current = task.state
            next_state = RuntimeStateMachine.transition(current, RuntimeState.FAILED)
            recorder.record_with_last_timestamp(
                kind=LifecycleEventKind.STATE_ENTERED,
                state=next_state,
                previous_state=current,
            )
            task = task.model_copy(
                update={
                    "state": next_state,
                    "state_history": (*task.state_history, next_state),
                }
            )
            _safe_log(
                {
                    "event": "runtime_state_transition",
                    "task_id": str(task.task_id),
                    "user": task.user,
                    "target": task.target,
                    "from_state": current.value,
                    "to_state": next_state.value,
                }
            )
        failure = RuntimeFailure(
            code=InvalidClockError.code,
            component=RuntimeComponent.RUNTIME,
            message="Runtime lifecycle clock failed safely.",
        )
        recorder.record_with_last_timestamp(
            kind=LifecycleEventKind.FAILED,
            state=task.state,
            component=failure.component,
            reason_code=failure.code,
        )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.FAILED,
            task=task,
            plan=plan,
            policy_decision=policy_decision,
            results=results,
            events=recorder.events,
            failure=failure,
        )

    @staticmethod
    def _safe_failure(
        component: RuntimeComponent,
        error: BaseException,
    ) -> tuple[str, str]:
        if component is RuntimeComponent.CONTEXT_BUILDER:
            return "context_builder_failure", "Context Builder failed safely."
        if component is RuntimeComponent.PLANNER:
            if isinstance(error, UnsupportedTaskError):
                return UnsupportedTaskError.code, "Planner does not support this Task."
            if isinstance(error, PlanMismatchError):
                return PlanMismatchError.code, "Planner returned an invalid execution plan."
            return "planner_failure", "Planner failed safely."
        if component is RuntimeComponent.POLICY:
            if isinstance(error, PolicyDeniedError):
                return PolicyDeniedError.code, "Policy denied execution."
            if isinstance(error, PolicyEvaluationError):
                return PolicyEvaluationError.code, "Policy evaluation failed safely."
            return "policy_failure", "Policy evaluation failed safely."
        if component is RuntimeComponent.EXECUTOR:
            if isinstance(error, ToolExecutionError):
                return ToolExecutionError.code, "Executor failed safely."
            return "executor_failure", "Executor failed safely."
        if component is RuntimeComponent.VERIFIER:
            if isinstance(error, VerificationError):
                return VerificationError.code, "Verification failed safely."
            return "verifier_failure", "Verifier failed safely."
        return "runtime_failure", "Runtime failed safely."

    @staticmethod
    def _log_execution_audit(
        task: Task,
        plan: ExecutionPlan,
        results: tuple[ToolResult[SystemStatus], ...],
        *,
        verification: Literal["passed", "failed", "not_run"],
        result_override: Literal["execution_failed"] | None = None,
    ) -> None:
        for index, step in enumerate(plan.steps):
            result = results[index] if index < len(results) else None
            result_status: Literal["success", "invalid", "execution_failed"]
            duration_ms: int | None
            if result_override is not None:
                result_status = result_override
                duration_ms = None
            elif isinstance(result, ToolResult):
                result_status = "success" if result.success else "execution_failed"
                duration_ms = result.duration_ms
            else:
                result_status = "invalid"
                duration_ms = None
            _safe_log(
                {
                    "event": "execution_audit",
                    "task_id": str(task.task_id),
                    "plan_id": str(plan.plan_id),
                    "approval_id": None,
                    "operator": task.user,
                    "user": task.user,
                    "target": task.target,
                    "tool": step.tool_id,
                    "tool_version": step.tool_version,
                    "arguments": {"redacted": True},
                    "result": result_status,
                    "duration_ms": duration_ms,
                    "verification": verification,
                }
            )

    @staticmethod
    def _transition(
        task: Task,
        target: RuntimeState,
        recorder: _LifecycleRecorder,
    ) -> tuple[Task, bool]:
        current = task.state
        next_state = RuntimeStateMachine.transition(current, target)
        if not recorder.record(
            kind=LifecycleEventKind.STATE_ENTERED,
            state=next_state,
            previous_state=current,
        ):
            return task, False
        updated = task.model_copy(
            update={
                "state": next_state,
                "state_history": (*task.state_history, next_state),
            }
        )
        _safe_log(
            {
                "event": "runtime_state_transition",
                "task_id": str(task.task_id),
                "user": task.user,
                "target": task.target,
                "from_state": current.value,
                "to_state": next_state.value,
            }
        )
        return updated, True


def create_mock_runtime() -> RuntimeEngine:
    """Create the concrete local-only Mock Runtime."""
    return RuntimeEngine()


__all__ = ["Clock", "RuntimeEngine", "create_mock_runtime"]
