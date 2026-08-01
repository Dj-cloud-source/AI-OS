"""Immutable Runtime lifecycle contracts."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_server.models.execution import ExecutionPlan, StepRole
from ai_server.models.executor import (
    DispatchStatus,
    EffectDisposition,
    ExecutionAttemptAuthorization,
    ExecutionNextState,
    ExecutionReport,
    ExecutionReportStatus,
    ExecutionUncertainty,
)
from ai_server.models.policy import (
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEffect,
)
from ai_server.models.system_status import SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import TargetReference, ToolResult
from ai_server.models.verification import (
    VerificationEffectDisposition,
    VerificationResult,
    VerificationStatus,
)
from ai_server.runtime.state import RuntimeState, RuntimeStateMachine
from ai_server.tools.hashing import canonical_json_sha256

_STRICT_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


class RuntimeOutcomeStatus(StrEnum):
    """Public outcomes that current Runtime calls may return."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"


class RuntimeComponent(StrEnum):
    """Runtime-owned components that may complete or fail."""

    RUNTIME = "RUNTIME"
    CONTEXT_BUILDER = "CONTEXT_BUILDER"
    PLANNER = "PLANNER"
    POLICY = "POLICY"
    APPROVAL = "APPROVAL"
    EXECUTOR = "EXECUTOR"
    VERIFIER = "VERIFIER"


class LifecycleEventKind(StrEnum):
    """Kinds of immutable lifecycle evidence emitted by Runtime."""

    STATE_ENTERED = "STATE_ENTERED"
    COMPONENT_COMPLETED = "COMPONENT_COMPLETED"
    APPROVAL_DECISION_RECORDED = "APPROVAL_DECISION_RECORDED"
    APPROVAL_AUTHORIZATION_CONSUMED = "APPROVAL_AUTHORIZATION_CONSUMED"
    AUTHORIZATION_REJECTED = "AUTHORIZATION_REJECTED"
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
_FAILURE_STATE_COMPONENTS: dict[RuntimeState, frozenset[RuntimeComponent]] = {
    RuntimeState.CONTEXT_BUILDING: frozenset({RuntimeComponent.CONTEXT_BUILDER}),
    RuntimeState.PLANNING: frozenset({RuntimeComponent.PLANNER}),
    RuntimeState.POLICY_CHECK: frozenset({RuntimeComponent.POLICY}),
    RuntimeState.WAITING_FOR_APPROVAL: frozenset({RuntimeComponent.APPROVAL}),
    RuntimeState.EXECUTING: frozenset({RuntimeComponent.EXECUTOR}),
    RuntimeState.VERIFYING: frozenset(
        {
            RuntimeComponent.EXECUTOR,
            RuntimeComponent.VERIFIER,
        }
    ),
}


class LifecycleEvent(BaseModel):
    """One ordered, timestamped fact from a Runtime lifecycle."""

    model_config = _STRICT_FROZEN_CONFIG

    task_id: UUID
    sequence: int = Field(ge=0)
    occurred_at: datetime
    kind: LifecycleEventKind
    state: RuntimeState
    previous_state: RuntimeState | None = None
    component: RuntimeComponent | None = None
    reason_code: str | None = Field(default=None, min_length=1, pattern=r"^[a-z0-9_]+$")
    approval_id: UUID | None = None
    execution_attempt_id: UUID | None = None

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
        authorization_fields = (self.approval_id, self.execution_attempt_id)
        if self.kind is LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED:
            if any(value is None for value in authorization_fields):
                raise ValueError("APPROVAL_AUTHORIZATION_CONSUMED requires exact authorization IDs")
        elif any(value is not None for value in authorization_fields):
            raise ValueError("only APPROVAL_AUTHORIZATION_CONSUMED may include authorization IDs")
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
        elif self.kind is LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED:
            self._validate_control_event(
                state=RuntimeState.WAITING_FOR_APPROVAL,
                component=None,
                reason_code="human_approved",
            )
        elif self.kind is LifecycleEventKind.AUTHORIZATION_REJECTED:
            self._validate_authorization_rejection()
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

    def _validate_authorization_rejection(self) -> None:
        if self.previous_state is not None:
            raise ValueError("AUTHORIZATION_REJECTED event cannot include previous_state")
        if (
            self.state is not RuntimeState.WAITING_FOR_APPROVAL
            or self.component is not None
            or self.reason_code is None
            or self.reason_code
            in {
                "approval_required",
                "human_approved",
                "human_rejected",
                "not_required",
            }
        ):
            raise ValueError("AUTHORIZATION_REJECTED event has contradictory fields")

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

    model_config = _STRICT_FROZEN_CONFIG

    code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    component: RuntimeComponent
    message: str = Field(min_length=1)


class RuntimeOutcome(BaseModel):
    """The immutable result of one invocation, not an authorization or authenticity token."""

    model_config = _STRICT_FROZEN_CONFIG

    status: RuntimeOutcomeStatus
    task: Task
    plan: ExecutionPlan | None = None
    policy_decision: PolicyDecision | None = None
    execution_authorization: ExecutionAttemptAuthorization | None = None
    execution_report: ExecutionReport | None = None
    execution_uncertainty: ExecutionUncertainty | None = None
    verification_result: VerificationResult | None = None
    final_effect_disposition: VerificationEffectDisposition = VerificationEffectDisposition.NONE
    human_intervention_required: bool = False
    results: tuple[ToolResult[SystemStatus], ...] = ()
    events: tuple[LifecycleEvent, ...] = Field(min_length=1)
    failure: RuntimeFailure | None = None

    @model_validator(mode="after")
    def validate_lifecycle_consistency(self) -> Self:
        """Require outcome, Task, plan, results, and events to tell one story."""
        self._validate_events()
        self._validate_policy_decision()
        self._validate_execution_binding()
        self._validate_plan_and_results()
        self._validate_verification_binding()
        self._validate_terminal_shape()
        return self

    def _validate_verification_binding(self) -> None:
        """Bind VerificationResult and final effect closure to execution evidence."""
        verification = self.verification_result
        report = self.execution_report
        plan = self.plan
        authorization = self.execution_authorization
        if (
            self.failure is not None
            and self.failure.component is RuntimeComponent.VERIFIER
            and (verification is None or verification.status is not VerificationStatus.FAILED)
        ):
            raise ValueError("Verifier failure requires failed verification evidence")
        if verification is not None:
            if plan is None or report is None or authorization is None:
                raise ValueError("VerificationResult requires exact execution evidence")
            if (
                verification.task_id != self.task.task_id
                or verification.plan_id != plan.plan_id
                or verification.plan_digest != canonical_json_sha256(plan)
                or verification.execution_attempt_id != authorization.execution_attempt_id
                or verification.execution_report_hash != report.content_hash
                or tuple(
                    (
                        check.criterion_id,
                        check.evidence_step_id,
                        check.evaluator_version,
                    )
                    for check in verification.checks
                )
                != tuple(
                    (
                        criterion.criterion_id,
                        criterion.evidence_step_id,
                        criterion.evaluator_version,
                    )
                    for criterion in plan.verification_criteria
                )
            ):
                raise ValueError("VerificationResult must bind the exact Plan and report")
            if len(verification.evidence_references) != len(report.results) or len(
                report.records
            ) != len(report.results):
                raise ValueError("VerificationResult must reference every execution result")
            for expected_index, reference in enumerate(verification.evidence_references):
                step = plan.steps[expected_index]
                record = report.records[expected_index]
                result = report.results[expected_index]
                if (
                    reference.step_index != expected_index
                    or reference.step_id != step.step_id
                    or reference.invocation_id != result.invocation_id
                    or reference.invocation_id != record.invocation_id
                    or reference.tool_id != step.tool_id
                    or reference.tool_version != step.tool_version
                    or reference.contract_hash != step.contract_hash
                    or reference.implementation_hash != step.implementation_hash
                    or reference.arguments_hash != canonical_json_sha256(step.arguments)
                    or reference.target != result.target
                    or reference.result_hash != canonical_json_sha256(result)
                    or (
                        reference.accepted_at is not None
                        and reference.accepted_at > verification.evaluated_at
                    )
                ):
                    raise ValueError("Verification evidence must bind every exact execution result")
            verifier_completed = any(
                event.kind is LifecycleEventKind.COMPONENT_COMPLETED
                and event.component is RuntimeComponent.VERIFIER
                for event in self.events
            )
            if verification.status is VerificationStatus.PASSED and not verifier_completed:
                raise ValueError("Passed verification requires Verifier completion evidence")
        report_unknown = report is not None and any(
            record.effect_disposition is EffectDisposition.UNKNOWN for record in report.records
        )
        uncertainty_unknown = (
            self.execution_uncertainty is not None
            and self.execution_uncertainty.effect_disposition is EffectDisposition.UNKNOWN
        )
        mutation_pending = report is not None and any(
            record.effect_disposition is EffectDisposition.PENDING_VERIFICATION
            for record in report.records
        )
        if report_unknown or uncertainty_unknown:
            expected_effect = VerificationEffectDisposition.UNKNOWN
            expected_human = True
        elif mutation_pending:
            verified = (
                verification is not None
                and verification.status is VerificationStatus.PASSED
                and verification.effect_disposition is VerificationEffectDisposition.VERIFIED
            )
            expected_effect = (
                VerificationEffectDisposition.VERIFIED
                if verified
                else VerificationEffectDisposition.UNKNOWN
            )
            expected_human = not verified
        else:
            expected_effect = VerificationEffectDisposition.NONE
            expected_human = False
        if (
            self.final_effect_disposition is not expected_effect
            or self.human_intervention_required is not expected_human
        ):
            raise ValueError("Runtime final effect closure contradicts execution evidence")
        if verification is not None and (
            verification.effect_disposition is not expected_effect
            or verification.human_intervention_required is not expected_human
        ):
            raise ValueError("Verification and Runtime effect closure must agree")

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
        authorization_consumed_count = 0
        approval_pause_seen = False
        authorization_consumed_seen = False
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
                approval_pause_seen = True
            elif event.kind is LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED:
                if not approval_pause_seen or authorization_consumed_seen:
                    raise ValueError(
                        "human authorization consumption requires one earlier approval pause"
                    )
                authorization_consumed_count += 1
                authorization_consumed_seen = True
            elif event.kind is LifecycleEventKind.AUTHORIZATION_REJECTED:
                if not approval_pause_seen or authorization_consumed_seen:
                    raise ValueError(
                        "authorization rejection requires an unconsumed approval pause"
                    )
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
        if authorization_consumed_count > 1:
            raise ValueError("a Runtime outcome can consume only one Plan authorization")
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
            failed_stage = (
                self.task.state_history[-2]
                if self.task.state is RuntimeState.FAILED and len(self.task.state_history) >= 2
                else None
            )
            completed_stage = _COMPONENT_STATES.get(self.failure.component)
            if completed_stage is failed_stage:
                raise ValueError("a component cannot fail in the same stage it already completed")

    def _validate_plan_and_results(self) -> None:
        completed_components = {
            event.component
            for event in self.events
            if event.kind is LifecycleEventKind.COMPONENT_COMPLETED
        }
        planner_completed = RuntimeComponent.PLANNER in completed_components
        if self.plan is None:
            if (
                planner_completed
                or self.results
                or self.execution_authorization is not None
                or self.execution_report is not None
                or self.execution_uncertainty is not None
            ):
                raise ValueError(
                    "a completed Planner and execution evidence require an execution plan"
                )
            return
        if not planner_completed:
            raise ValueError("an execution plan requires a completed Planner stage")
        if self.plan.task_id != self.task.task_id or self.plan.target != self.task.target:
            raise ValueError("execution plan must belong to the outcome Task and target")

        report = self.execution_report
        if report is None:
            if self.results:
                raise ValueError("Incomplete Executor results require a bound ExecutionReport")
            return
        if len(report.records) > len(self.plan.steps):
            raise ValueError("ExecutionReport cannot expand the approved Plan")
        for record, step in zip(report.records, self.plan.steps, strict=False):
            expected_target = TargetReference(
                target_id=self.plan.target,
                resource_type="local_system",
                resource_id=step.arguments.target,
            )
            if (
                record.step_id != step.step_id
                or record.role is not step.role
                or record.tool_id != step.tool_id
                or record.tool_version != step.tool_version
                or record.contract_hash != step.contract_hash
                or record.implementation_hash != step.implementation_hash
                or record.arguments_hash != canonical_json_sha256(step.arguments)
                or record.target != expected_target
            ):
                raise ValueError("execution record identity must match its planned step")
        if self.results != report.results:
            raise ValueError("Runtime results must exactly equal ExecutionReport results")
        if report.status is ExecutionReportStatus.READY_FOR_VERIFIER and (
            len(report.records) != len(self.plan.steps)
            or len(report.results) != len(self.plan.steps)
            or any(not result.success for result in report.results)
        ):
            raise ValueError("READY_FOR_VERIFIER must cover every successful planned Step")
        if report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH and (
            len(report.records) >= len(self.plan.steps)
            or self.plan.steps[len(report.records)].role is not StepRole.VERIFY
            or any(not result.success for result in report.results)
        ):
            raise ValueError(
                "AWAITING_VERIFICATION_DISPATCH requires a successful execution prefix"
            )

    def _validate_policy_decision(self) -> None:
        completed_components = {
            event.component
            for event in self.events
            if event.kind is LifecycleEventKind.COMPONENT_COMPLETED
        }
        policy_completed = RuntimeComponent.POLICY in completed_components
        decision = self.policy_decision
        if decision is None:
            if (
                policy_completed
                or RuntimeState.WAITING_FOR_APPROVAL in self.task.state_history
                or RuntimeState.EXECUTING in self.task.state_history
                or RuntimeState.VERIFYING in self.task.state_history
                or self.task.state is RuntimeState.COMPLETED
            ):
                raise ValueError("a completed Policy stage requires its structured decision")
            return
        if self.plan is None or RuntimeState.POLICY_CHECK not in self.task.state_history:
            raise ValueError("a Policy decision requires a plan evaluated in POLICY_CHECK")
        expected_target = TargetReference(
            target_id=self.plan.target,
            resource_type="local_system",
            resource_id=self.plan.target,
        )
        if (
            decision.task_id != self.task.task_id
            or decision.plan_id != self.plan.plan_id
            or decision.operator_id != self.task.user
            or decision.target != expected_target
            or len(decision.step_decisions) != len(self.plan.steps)
        ):
            raise ValueError("Policy decision identity must match the Task and Plan")
        for step, step_decision in zip(
            self.plan.steps,
            decision.step_decisions,
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
                raise ValueError("Policy step decision must match its ordered planned step")
        if decision.effect is PolicyEffect.DENY:
            normal_denial = (
                self.status is RuntimeOutcomeStatus.FAILED
                and self.failure is not None
                and self.failure.component is RuntimeComponent.POLICY
                and self.failure.code == "policy_denied"
                and self.task.state_history[-2] is RuntimeState.POLICY_CHECK
            )
            clock_failure = (
                self.status is RuntimeOutcomeStatus.FAILED
                and self.failure is not None
                and self.failure.component is RuntimeComponent.RUNTIME
                and self.failure.code == "invalid_clock"
                and self.task.state_history[-2] is RuntimeState.POLICY_CHECK
            )
            if not normal_denial and not clock_failure:
                raise ValueError("a denied Policy decision must fail before approval or execution")
            return
        if not policy_completed and not (
            self.status is RuntimeOutcomeStatus.FAILED
            and self.failure is not None
            and self.failure.code == "invalid_clock"
            and self.task.state_history[-2] is RuntimeState.POLICY_CHECK
        ):
            raise ValueError("an allowed Policy decision requires a completed Policy stage")
        if (
            decision.approval_requirement is PolicyApprovalRequirement.NOT_REQUIRED
            and self.status is RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
        ):
            raise ValueError("NOT_REQUIRED Policy decisions cannot remain approval-paused")

    def _validate_execution_binding(self) -> None:
        authorization = self.execution_authorization
        report = self.execution_report
        uncertainty = self.execution_uncertainty
        execution_started = RuntimeState.EXECUTING in self.task.state_history
        if authorization is None and (report is not None or uncertainty is not None):
            raise ValueError("Execution evidence requires exact authorization")
        if authorization is not None and report is None and uncertainty is None:
            raise ValueError("Execution authorization requires report or uncertainty evidence")
        if execution_started and authorization is None:
            raise ValueError("executed work requires exact authorization and report evidence")
        if authorization is None:
            return
        if self.plan is None or self.policy_decision is None:
            raise ValueError("execution evidence requires an exact Plan and PolicyDecision")

        decision = self.policy_decision
        plan_digest = canonical_json_sha256(self.plan)
        policy_decision_hash = canonical_json_sha256(decision)
        if (
            decision.effect is not PolicyEffect.ALLOW
            or decision.approval_requirement is None
            or authorization.task_id != self.task.task_id
            or authorization.plan_id != self.plan.plan_id
            or authorization.plan_digest != plan_digest
            or authorization.policy_decision_hash != policy_decision_hash
            or authorization.approval_requirement is not decision.approval_requirement
        ):
            raise ValueError(
                "Execution authorization must bind the exact Task, Plan, and PolicyDecision"
            )
        if report is not None and (
            report.execution_attempt_id != authorization.execution_attempt_id
            or report.authorization_hash != authorization.content_hash
            or report.task_id != authorization.task_id
            or report.plan_id != authorization.plan_id
            or report.plan_digest != authorization.plan_digest
            or report.policy_decision_hash != authorization.policy_decision_hash
            or report.approval_id != authorization.approval_id
        ):
            raise ValueError("ExecutionReport must bind the exact execution authorization")
        if uncertainty is not None and (
            uncertainty.execution_attempt_id != authorization.execution_attempt_id
            or uncertainty.authorization_hash != authorization.content_hash
            or self.status is not RuntimeOutcomeStatus.FAILED
            or self.failure is None
            or self.failure.code != uncertainty.reason_code
            or (
                report is None
                and (
                    uncertainty.prior_report_hash is not None
                    or self.results
                    or (
                        execution_started
                        and uncertainty.dispatch_status is not DispatchStatus.UNKNOWN
                    )
                )
            )
            or (
                report is not None
                and (
                    report.status is not ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
                    or uncertainty.prior_report_hash != report.content_hash
                    or self.results != report.results
                    or (
                        report.records
                        and (
                            uncertainty.dispatch_status is not DispatchStatus.UNKNOWN
                            or uncertainty.effect_disposition is not EffectDisposition.UNKNOWN
                            or not uncertainty.human_intervention_required
                        )
                    )
                )
            )
        ):
            raise ValueError(
                "Execution uncertainty must bind the failed Attempt and any trusted prior report"
            )

        consumed_events = tuple(
            event
            for event in self.events
            if event.kind is LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED
        )
        if decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL:
            if authorization.approval_id is None:
                raise ValueError("human-required execution requires human Approval evidence")
            if execution_started and (
                len(consumed_events) != 1
                or consumed_events[0].approval_id != authorization.approval_id
                or consumed_events[0].execution_attempt_id != authorization.execution_attempt_id
            ):
                raise ValueError(
                    "human-required execution requires one matching consumed authorization event"
                )
        elif authorization.approval_id is not None or consumed_events:
            raise ValueError("NOT_REQUIRED execution cannot claim human Approval evidence")

        if not execution_started:
            valid_pre_execution_abort = (
                uncertainty is not None
                and report is None
                and uncertainty.prior_report_hash is None
                and uncertainty.dispatch_status is DispatchStatus.NOT_DISPATCHED
                and uncertainty.effect_disposition is EffectDisposition.NONE
                and not uncertainty.human_intervention_required
            ) or (
                report is not None
                and self.status is RuntimeOutcomeStatus.FAILED
                and self.failure is not None
                and self.failure.component is RuntimeComponent.RUNTIME
                and self.failure.code == "invalid_clock"
                and len(self.task.state_history) >= 2
                and self.task.state_history[-2] is RuntimeState.WAITING_FOR_APPROVAL
                and not report.results
                and report.status is ExecutionReportStatus.FAILED
                and report.next_state is ExecutionNextState.FAILED
            )
            if not valid_pre_execution_abort:
                raise ValueError(
                    "execution evidence before EXECUTING requires a failed aborted attempt"
                )

    def _validate_terminal_shape(self) -> None:
        final_event = self.events[-1]
        if self.status is RuntimeOutcomeStatus.COMPLETED:
            if (
                self.task.state is not RuntimeState.COMPLETED
                or self.plan is None
                or self.execution_authorization is None
                or self.execution_report is None
                or self.execution_uncertainty is not None
                or self.execution_report.status is not ExecutionReportStatus.READY_FOR_VERIFIER
                or self.execution_report.next_state is not ExecutionNextState.VERIFYING
                or len(self.results) != len(self.plan.steps)
                or any(not result.success for result in self.results)
                or self.verification_result is None
                or self.verification_result.status is not VerificationStatus.PASSED
                or self.final_effect_disposition is VerificationEffectDisposition.UNKNOWN
                or self.human_intervention_required
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
                or self.execution_authorization is not None
                or self.execution_report is not None
                or self.execution_uncertainty is not None
                or self.verification_result is not None
                or self.final_effect_disposition is not VerificationEffectDisposition.NONE
                or self.human_intervention_required
                or self.failure is not None
                or final_event.kind
                not in {
                    LifecycleEventKind.PAUSED,
                    LifecycleEventKind.AUTHORIZATION_REJECTED,
                }
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
                final_event.reason_code not in {"invalid_clock", "execution_abort_uncertain"}
            ):
                raise ValueError("Runtime failure has an unsupported Phase 1 reason")
        else:
            previous_state = self.task.state_history[-2]
            expected_failure_components = _FAILURE_STATE_COMPONENTS.get(
                previous_state,
                frozenset(),
            )
            if final_event.component not in expected_failure_components:
                raise ValueError("failure component must match the stage that entered FAILED")
        if final_event.component is RuntimeComponent.EXECUTOR and (
            (
                self.execution_report is None
                or self.execution_report.status is not ExecutionReportStatus.FAILED
                or self.execution_report.next_state is not ExecutionNextState.FAILED
            )
            and self.execution_uncertainty is None
        ):
            raise ValueError(
                "Executor failure requires a failed report or bound uncertainty evidence"
            )
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
