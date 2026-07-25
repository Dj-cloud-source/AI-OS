"""Fail-closed Phase 1 Runtime orchestration."""

import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Literal, cast
from uuid import UUID

from pydantic import ValidationError

from ai_server.context.builder import ContextBuilder
from ai_server.executor.service import Executor
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, ExecutionStep
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
from ai_server.models.tool import ToolMetadata, ToolResult
from ai_server.planner.service import GET_SYSTEM_STATUS_STEP, Planner
from ai_server.policy.engine import PolicyEngine, ToolKey
from ai_server.runtime.errors import (
    ApprovalRequiredError,
    ApprovalResumeUnavailableError,
    InvalidClockError,
    InvalidRuntimeOutcomeError,
    InvalidStateTransitionError,
    InvalidTaskError,
    PlanMismatchError,
    PolicyDeniedError,
    ReservedStateTransitionError,
    TerminalStateMutationError,
    ToolExecutionError,
    UnsupportedTaskError,
    VerificationError,
)
from ai_server.runtime.state import RuntimeState, RuntimeStateMachine
from ai_server.tools.get_system_status import GET_SYSTEM_STATUS_METADATA
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
    with suppress(Exception):
        logger.info(json.dumps(payload, sort_keys=True))


def _safe_log_lifecycle_event(event: LifecycleEvent) -> None:
    """Serialize and emit lifecycle evidence without affecting Runtime state."""
    with suppress(Exception):
        logger.info(
            json.dumps(
                {
                    "event": "runtime_lifecycle_event",
                    **event.model_dump(mode="json"),
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
    return (
        type(result) is ToolResult[SystemStatus]
        and type(result.data) is SystemStatus
        and type(result.data.services) is tuple
        and all(type(service) is ServiceStatus for service in result.data.services)
    )


def _outcome_has_trusted_identity_fields(outcome: RuntimeOutcome) -> bool:
    """Reject unsafe identity and timestamp objects before model serialization."""
    if not _task_has_trusted_nested_fields(outcome.task):
        return False
    if outcome.plan is not None and not _plan_has_trusted_nested_fields(outcome.plan):
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
        except Exception:
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
        policy: PolicyEngine | None = None,
        executor: Executor | None = None,
        verifier: Verifier | None = None,
        tool_metadata: ToolMetadata = GET_SYSTEM_STATUS_METADATA,
        clock: Clock = _utc_now,
    ) -> None:
        """Compose the concrete local-only Phase 1 Runtime."""
        self._context_builder = context_builder if context_builder is not None else ContextBuilder()
        self._planner = planner if planner is not None else Planner()
        self._policy = policy if policy is not None else PolicyEngine()
        self._executor = executor if executor is not None else Executor()
        self._verifier = verifier if verifier is not None else Verifier()
        self._clock = clock
        self._tool_metadata = self._validate_tool_metadata(tool_metadata)
        catalog: dict[ToolKey, ToolMetadata] = {
            (
                self._tool_metadata.name,
                self._tool_metadata.version,
            ): self._tool_metadata
        }
        self._catalog = MappingProxyType(catalog)

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
        except Exception as error:
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
        except Exception as error:
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
        try:
            policy_check = cast(Callable[..., object], self._policy.check)
            policy_result = policy_check(plan, self._catalog)
            if policy_result is not None:
                raise PolicyDeniedError("Policy returned an invalid decision signal")
        except ApprovalRequiredError:
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
                )
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
                )
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.WAITING_FOR_APPROVAL,
                task=task,
                plan=plan,
                events=recorder.events,
            )
        except Exception as error:
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.POLICY,
                error=error,
                plan=plan,
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
            )
        try:
            raw_results = self._executor.execute(plan)
            results = self._validate_results(raw_results, plan)
        except Exception as error:
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
                results=results,
            )
        try:
            verify = cast(Callable[..., object], self._verifier.verify)
            verification_result = verify(plan, results)
            if verification_result is not None:
                raise VerificationError("Verifier returned an invalid completion signal")
        except Exception as error:
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
                results=results,
            )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.COMPLETED,
            task=task,
            plan=plan,
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
                results=outcome.results,
            )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.FAILED,
            task=task,
            plan=outcome.plan,
            results=outcome.results,
            events=recorder.events,
            failure=failure,
        )

    @staticmethod
    def _validate_tool_metadata(tool_metadata: ToolMetadata) -> ToolMetadata:
        try:
            if type(tool_metadata) is not ToolMetadata:
                raise TypeError
            validated = ToolMetadata.model_validate(
                tool_metadata.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except Exception:
            raise PolicyDeniedError("Runtime rejected malformed Tool metadata") from None
        if validated != GET_SYSTEM_STATUS_METADATA:
            raise PolicyDeniedError("Runtime rejected noncanonical Tool metadata")
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
        except Exception:
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
        except (TypeError, ValidationError):
            raise InvalidTaskError("Context Builder returned invalid Runtime context") from None

    @staticmethod
    def _validate_plan(plan: ExecutionPlan) -> ExecutionPlan:
        try:
            if not _plan_has_trusted_nested_fields(plan):
                raise TypeError
            validated = ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="none"),
                strict=True,
            )
            if not _plan_has_trusted_nested_fields(validated) or validated.steps != (
                GET_SYSTEM_STATUS_STEP,
            ):
                raise TypeError
            return validated
        except (TypeError, ValidationError):
            raise PlanMismatchError("Planner returned a malformed execution plan") from None

    @staticmethod
    def _validate_results(
        results: tuple[ToolResult[SystemStatus], ...],
        plan: ExecutionPlan,
    ) -> tuple[ToolResult[SystemStatus], ...]:
        try:
            if type(results) is not tuple or len(results) != len(plan.steps):
                raise TypeError
            validated_results: list[ToolResult[SystemStatus]] = []
            for step, raw_result in zip(plan.steps, results, strict=True):
                if not _result_has_trusted_nested_fields(raw_result):
                    raise TypeError
                result = ToolResult[SystemStatus].model_validate(
                    raw_result.model_dump(mode="python", warnings="none"),
                    strict=True,
                )
                if (
                    not _result_has_trusted_nested_fields(result)
                    or result.tool_name != step.tool_name
                    or result.tool_version != step.tool_version
                ):
                    raise TypeError
                validated_results.append(result)
            return tuple(validated_results)
        except (TypeError, ValidationError):
            raise ToolExecutionError("Executor returned invalid structured evidence") from None

    @staticmethod
    def _validate_outcome(outcome: RuntimeOutcome) -> RuntimeOutcome:
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
                RuntimeEngine._validate_plan(validated.plan)
            return validated
        except Exception:
            raise InvalidRuntimeOutcomeError(
                "Runtime rejected malformed RuntimeOutcome input"
            ) from None

    def _failure_outcome(
        self,
        *,
        task: Task,
        recorder: _LifecycleRecorder,
        component: RuntimeComponent,
        error: Exception,
        plan: ExecutionPlan | None = None,
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
                results=results,
            )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.FAILED,
            task=task,
            plan=plan,
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
            results=results,
            events=recorder.events,
            failure=failure,
        )

    @staticmethod
    def _safe_failure(
        component: RuntimeComponent,
        error: Exception,
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
                result_status = "success"
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
                    "tool": step.tool_name,
                    "tool_version": step.tool_version,
                    "arguments": step.arguments.model_dump(mode="json"),
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
    """Create the concrete local-only Phase 1 Runtime."""
    return RuntimeEngine()


__all__ = ["Clock", "RuntimeEngine", "create_mock_runtime"]
