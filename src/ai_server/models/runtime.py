"""Immutable Phase 1 Runtime lifecycle contracts."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_server.models.execution import ExecutionPlan
from ai_server.models.system_status import SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import TargetReference, ToolResult
from ai_server.runtime.state import RuntimeState, RuntimeStateMachine
from ai_server.tools.hashing import canonical_json_sha256


class RuntimeOutcomeStatus(StrEnum):
    """Public outcomes that Phase 1 Runtime calls may return."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"


class RuntimeComponent(StrEnum):
    """Runtime-owned components that may complete or fail."""

    RUNTIME = "RUNTIME"
    CONTEXT_BUILDER = "CONTEXT_BUILDER"
    PLANNER = "PLANNER"
    POLICY = "POLICY"
    EXECUTOR = "EXECUTOR"
    VERIFIER = "VERIFIER"


class LifecycleEventKind(StrEnum):
    """Kinds of immutable lifecycle evidence emitted by Runtime."""

    STATE_ENTERED = "STATE_ENTERED"
    COMPONENT_COMPLETED = "COMPONENT_COMPLETED"
    APPROVAL_DECISION_RECORDED = "APPROVAL_DECISION_RECORDED"
    PAUSED = "PAUSED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


_COMPONENT_STATES: dict[RuntimeComponent, RuntimeState] = {
    RuntimeComponent.CONTEXT_BUILDER: RuntimeState.CONTEXT_BUILDING,
    RuntimeComponent.PLANNER: RuntimeState.PLANNING,
    RuntimeComponent.POLICY: RuntimeState.POLICY_CHECK,
    RuntimeComponent.EXECUTOR: RuntimeState.EXECUTING,
    RuntimeComponent.VERIFIER: RuntimeState.VERIFYING,
}
_STATE_COMPONENTS = {state: component for component, state in _COMPONENT_STATES.items()}


class LifecycleEvent(BaseModel):
    """One ordered, timestamped fact from a Runtime lifecycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    sequence: int = Field(ge=0)
    occurred_at: datetime
    kind: LifecycleEventKind
    state: RuntimeState
    previous_state: RuntimeState | None = None
    component: RuntimeComponent | None = None
    reason_code: str | None = Field(default=None, min_length=1, pattern=r"^[a-z0-9_]+$")

    @field_validator("occurred_at")
    @classmethod
    def validate_utc_timestamp(cls, value: datetime) -> datetime:
        """Require UTC and copy untrusted datetime hooks into a built-in value."""
        try:
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError
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
        except BaseException:  # noqa: B036 - model data cannot control the process.
            raise ValueError("occurred_at must be timezone-aware UTC") from None

    @model_validator(mode="after")
    def validate_event_shape(self) -> Self:
        """Reject fields or state combinations that contradict the event kind."""
        if self.kind is LifecycleEventKind.STATE_ENTERED:
            self._validate_state_entry()
        elif self.kind is LifecycleEventKind.COMPONENT_COMPLETED:
            self._validate_component_completion()
        elif self.kind is LifecycleEventKind.APPROVAL_DECISION_RECORDED:
            self._validate_control_event(
                state=RuntimeState.WAITING_FOR_APPROVAL,
                component=None,
                reason_code="not_required",
            )
        elif self.kind is LifecycleEventKind.PAUSED:
            self._validate_control_event(
                state=RuntimeState.WAITING_FOR_APPROVAL,
                component=None,
                reason_code="approval_required",
            )
        elif self.kind is LifecycleEventKind.REJECTED:
            self._validate_control_event(
                state=RuntimeState.FAILED,
                component=RuntimeComponent.RUNTIME,
                reason_code="human_rejected",
            )
        else:
            if self.state is not RuntimeState.FAILED:
                raise ValueError("FAILED event must record FAILED state")
            if self.previous_state is not None:
                raise ValueError("FAILED event must not include previous_state")
            if self.component is None or self.reason_code is None:
                raise ValueError("FAILED event requires component and reason_code")
        return self

    def _validate_state_entry(self) -> None:
        if self.component is not None or self.reason_code is not None:
            raise ValueError("STATE_ENTERED event cannot include component or reason_code")
        if self.state is RuntimeState.RECEIVED:
            if self.previous_state is not None:
                raise ValueError("initial RECEIVED event cannot include previous_state")
            return
        if self.previous_state is None:
            raise ValueError("non-initial STATE_ENTERED event requires previous_state")
        if not RuntimeStateMachine.can_transition(self.previous_state, self.state):
            raise ValueError("STATE_ENTERED event records an invalid Runtime transition")

    def _validate_component_completion(self) -> None:
        if self.previous_state is not None or self.reason_code is not None:
            raise ValueError(
                "COMPONENT_COMPLETED event cannot include previous_state or reason_code"
            )
        expected_state = (
            _COMPONENT_STATES.get(self.component) if self.component is not None else None
        )
        if expected_state is None or self.state is not expected_state:
            raise ValueError("COMPONENT_COMPLETED event has an invalid component or state")

    def _validate_control_event(
        self,
        *,
        state: RuntimeState,
        component: RuntimeComponent | None,
        reason_code: str,
    ) -> None:
        if self.previous_state is not None:
            raise ValueError(f"{self.kind.value} event cannot include previous_state")
        if (
            self.state is not state
            or self.component is not component
            or self.reason_code != reason_code
        ):
            raise ValueError(f"{self.kind.value} event has contradictory fields")


class RuntimeFailure(BaseModel):
    """A stable, redacted Runtime failure safe to return to a caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    component: RuntimeComponent
    message: str = Field(min_length=1)


class RuntimeOutcome(BaseModel):
    """The immutable structured result of one Phase 1 Runtime invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RuntimeOutcomeStatus
    task: Task
    plan: ExecutionPlan | None = None
    results: tuple[ToolResult[SystemStatus], ...] = ()
    events: tuple[LifecycleEvent, ...] = Field(min_length=1)
    failure: RuntimeFailure | None = None

    @model_validator(mode="after")
    def validate_lifecycle_consistency(self) -> Self:
        """Require outcome, Task, plan, results, and events to tell one story."""
        self._validate_events()
        self._validate_plan_and_results()
        self._validate_terminal_shape()
        return self

    def _validate_events(self) -> None:
        expected_sequences = tuple(range(len(self.events)))
        if tuple(event.sequence for event in self.events) != expected_sequences:
            raise ValueError("lifecycle event sequences must be contiguous and start at zero")
        if any(event.task_id != self.task.task_id for event in self.events):
            raise ValueError("all lifecycle events must belong to the outcome Task")

        first = self.events[0]
        if (
            first.kind is not LifecycleEventKind.STATE_ENTERED
            or first.state is not RuntimeState.RECEIVED
        ):
            raise ValueError("lifecycle events must start with RECEIVED state entry")

        state_history: list[RuntimeState] = []
        current_state: RuntimeState | None = None
        completed_components: set[RuntimeComponent] = set()
        previous_timestamp: datetime | None = None
        not_required_count = 0
        pause_count = 0
        terminal_control_event_count = 0
        for event in self.events:
            if previous_timestamp is not None and event.occurred_at < previous_timestamp:
                raise ValueError("lifecycle event timestamps must not move backwards")
            previous_timestamp = event.occurred_at

            if event.kind is LifecycleEventKind.STATE_ENTERED:
                if current_state is None:
                    if event.previous_state is not None:
                        raise ValueError("initial state entry cannot name a previous state")
                elif event.previous_state is not current_state:
                    raise ValueError(
                        "state-entry previous_state must match the preceding current state"
                    )
                state_history.append(event.state)
                current_state = event.state
            elif current_state is None or event.state is not current_state:
                raise ValueError("non-state lifecycle event must use the current Runtime state")

            if event.kind is LifecycleEventKind.COMPONENT_COMPLETED:
                if event.component in completed_components:
                    raise ValueError("a Runtime component cannot complete more than once")
                if event.component is not None:
                    completed_components.add(event.component)
            elif event.kind is LifecycleEventKind.APPROVAL_DECISION_RECORDED:
                not_required_count += 1
            elif event.kind is LifecycleEventKind.PAUSED:
                pause_count += 1
            elif event.kind in {LifecycleEventKind.REJECTED, LifecycleEventKind.FAILED}:
                terminal_control_event_count += 1
                if event is not self.events[-1]:
                    raise ValueError("rejection or failure must be the final event")

        waiting_visited = RuntimeState.WAITING_FOR_APPROVAL in self.task.state_history
        approval_gate_evidence_count = not_required_count + pause_count
        if approval_gate_evidence_count != int(waiting_visited):
            raise ValueError(
                "WAITING_FOR_APPROVAL lifecycle must contain exactly one approval decision"
            )
        if terminal_control_event_count > 1:
            raise ValueError("a Runtime outcome can contain only one terminal control event")
        if tuple(state_history) != self.task.state_history:
            raise ValueError("state-entry events must exactly match Task state_history")
        if self.events[-1].state is not self.task.state:
            raise ValueError("final lifecycle event must match the Task state")
        visited_components = {
            component
            for state in self.task.state_history
            if (component := _STATE_COMPONENTS.get(state)) is not None
        }
        required_completed_components = {
            component
            for index, state in enumerate(self.task.state_history[:-1])
            if (component := _STATE_COMPONENTS.get(state)) is not None
            and self.task.state_history[index + 1] is not RuntimeState.FAILED
        }
        if not required_completed_components.issubset(
            completed_components
        ) or not completed_components.issubset(visited_components):
            raise ValueError("component-completion events must match successful lifecycle stages")
        if (
            self.failure is not None
            and self.failure.component is not RuntimeComponent.RUNTIME
            and self.failure.component in completed_components
        ):
            raise ValueError("a failed component cannot also be recorded as completed")

    def _validate_plan_and_results(self) -> None:
        completed_components = {
            event.component
            for event in self.events
            if event.kind is LifecycleEventKind.COMPONENT_COMPLETED
        }
        planner_completed = RuntimeComponent.PLANNER in completed_components
        executor_completed = RuntimeComponent.EXECUTOR in completed_components
        if self.plan is None:
            if planner_completed or self.results:
                raise ValueError("a completed Planner and Tool results require an execution plan")
            return
        if not planner_completed:
            raise ValueError("an execution plan requires a completed Planner stage")
        if self.plan.task_id != self.task.task_id or self.plan.target != self.task.target:
            raise ValueError("execution plan must belong to the outcome Task and target")
        if executor_completed and len(self.results) != len(self.plan.steps):
            raise ValueError("a completed Executor requires one result per planned step")
        if not executor_completed and self.results:
            executor_failed = (
                self.failure is not None
                and self.failure.component is RuntimeComponent.EXECUTOR
                and self.task.state is RuntimeState.FAILED
                and len(self.task.state_history) >= 2
                and self.task.state_history[-2] is RuntimeState.EXECUTING
            )
            if (
                not executor_failed
                or len(self.results) > len(self.plan.steps)
                or self.results[-1].success
                or any(not result.success for result in self.results[:-1])
            ):
                raise ValueError("Incomplete Executor results require one final structured failure")
        for step, result in zip(self.plan.steps, self.results, strict=False):
            expected_target = TargetReference(
                target_id=self.plan.target,
                resource_type="local_system",
                resource_id=step.arguments.target,
            )
            if (
                result.plan_step_id != step.step_id
                or result.tool_id != step.tool_id
                or result.tool_version != step.tool_version
                or result.contract_hash != step.contract_hash
                or result.arguments_hash != canonical_json_sha256(step.arguments)
                or result.target != expected_target
            ):
                raise ValueError("execution result identity must match its planned step")

    def _validate_terminal_shape(self) -> None:
        final_event = self.events[-1]
        if self.status is RuntimeOutcomeStatus.COMPLETED:
            if (
                self.task.state is not RuntimeState.COMPLETED
                or self.plan is None
                or len(self.results) != len(self.plan.steps)
                or any(not result.success for result in self.results)
                or self.failure is not None
                or final_event.kind is not LifecycleEventKind.STATE_ENTERED
            ):
                raise ValueError("COMPLETED outcome has contradictory fields")
            return

        if self.status is RuntimeOutcomeStatus.WAITING_FOR_APPROVAL:
            if (
                self.task.state is not RuntimeState.WAITING_FOR_APPROVAL
                or self.plan is None
                or self.results
                or self.failure is not None
                or final_event.kind is not LifecycleEventKind.PAUSED
            ):
                raise ValueError("WAITING_FOR_APPROVAL outcome has contradictory fields")
            return

        if (
            self.task.state is not RuntimeState.FAILED
            or self.failure is None
            or final_event.kind not in {LifecycleEventKind.FAILED, LifecycleEventKind.REJECTED}
        ):
            raise ValueError("FAILED outcome has contradictory fields")
        if final_event.reason_code != self.failure.code:
            raise ValueError("final failure event and Runtime error codes must match")
        if final_event.component is not self.failure.component:
            raise ValueError("final failure event and Runtime error components must match")
        if final_event.component is RuntimeComponent.RUNTIME:
            if final_event.kind is LifecycleEventKind.FAILED and (
                final_event.reason_code != "invalid_clock"
            ):
                raise ValueError("Runtime failure has an unsupported Phase 1 reason")
        else:
            previous_state = self.task.state_history[-2]
            expected_failure_component = _STATE_COMPONENTS.get(previous_state)
            if final_event.component is not expected_failure_component:
                raise ValueError("failure component must match the stage that entered FAILED")
        if final_event.kind is LifecycleEventKind.REJECTED and (
            len(self.task.state_history) < 2
            or self.task.state_history[-2] is not RuntimeState.WAITING_FOR_APPROVAL
            or not any(event.kind is LifecycleEventKind.PAUSED for event in self.events)
        ):
            raise ValueError("REJECTED outcome requires an earlier approval pause")


__all__ = [
    "LifecycleEvent",
    "LifecycleEventKind",
    "RuntimeComponent",
    "RuntimeFailure",
    "RuntimeOutcome",
    "RuntimeOutcomeStatus",
]
