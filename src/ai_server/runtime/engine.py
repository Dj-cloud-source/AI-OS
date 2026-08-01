"""Fail-closed local Runtime orchestration."""

import json
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from pydantic import ValidationError

from ai_server.approval.engine import ApprovalEngine
from ai_server.context.builder import ContextBuilder
from ai_server.executor.errors import (
    ExecutionAuthorizationError,
    safe_execution_authorization_reason,
)
from ai_server.executor.service import Executor, ManualConfirmationReader
from ai_server.models.approval import ApprovalRecord, ApprovalReview
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.executor import (
    DispatchStatus,
    EffectDisposition,
    ExecutionAttemptAuthorization,
    ExecutionEventKind,
    ExecutionNextState,
    ExecutionReport,
    ExecutionReportStatus,
    ExecutionUncertainty,
)
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
from ai_server.models.tool import (
    RiskLevel,
    TargetReference,
    ToolCall,
    ToolError,
    ToolMetadata,
    ToolResult,
)
from ai_server.models.verification import (
    VERIFICATION_CRITERION_TYPES,
    EqualityCriterion,
    ExpectedStateCriterion,
    HealthStatusCriterion,
    NumericBoundsCriterion,
    VerificationCheckResult,
    VerificationCheckStatus,
    VerificationContext,
    VerificationEffectDisposition,
    VerificationEvidenceReference,
    VerificationFailureReason,
    VerificationResult,
    VerificationStatus,
)
from ai_server.planner.service import Planner
from ai_server.policy.engine import PolicyEngine
from ai_server.runtime.errors import (
    ApprovalRequiredError,
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
from ai_server.verifier.service import (
    Verifier,
    build_verification_failure,
    evaluate_verification,
)

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
_RETRYABLE_APPROVAL_GATE_REASONS = frozenset(
    {
        "approval_expired",
        "approval_missing",
        "approval_rejected",
        "unknown_approval",
    }
)


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


def _safe_log_approval_record(record: ApprovalRecord) -> None:
    """Emit a non-secret Approval issuance audit without exact arguments."""
    with suppress(BaseException):  # noqa: B036 - logging cannot control Runtime.
        logger.info(
            json.dumps(
                {
                    "event": "plan_approval_issued",
                    "approval_id": str(record.approval_id),
                    "review_id": str(record.review_id),
                    "task_id": str(record.task_id),
                    "plan_id": str(record.plan_id),
                    "plan_hash": record.plan_hash,
                    "policy_id": record.policy_id,
                    "policy_version": record.policy_version,
                    "policy_hash": record.policy_hash,
                    "policy_decision_hash": record.policy_decision_hash,
                    "operator_id": record.operator_id,
                    "approver": record.approver,
                    "effective_risk": record.effective_risk.value,
                    "approval_requirement": record.approval_requirement.value,
                    "manual_confirmation_requirement": (
                        record.manual_confirmation_requirement.value
                    ),
                    "issued_at": record.issued_at.isoformat(),
                    "expires_at": record.expires_at.isoformat(),
                    "content_hash": record.content_hash,
                    "steps": [
                        {
                            "step_index": step.step_index,
                            "step_id": step.step_id,
                            "tool_id": step.tool_id,
                            "tool_version": step.tool_version,
                            "contract_hash": step.contract_hash,
                            "implementation_hash": step.implementation_hash,
                            "arguments_hash": step.arguments_hash,
                        }
                        for step in record.steps
                    ],
                },
                sort_keys=True,
            )
        )


def _build_execution_uncertainty(
    authorization: ExecutionAttemptAuthorization,
    *,
    dispatch_was_attempted: bool,
    prior_report: ExecutionReport | None,
) -> ExecutionUncertainty:
    """Build closure evidence without inventing Step or invocation facts."""
    dispatch_status = (
        DispatchStatus.UNKNOWN if dispatch_was_attempted else DispatchStatus.NOT_DISPATCHED
    )
    effect_disposition = (
        EffectDisposition.UNKNOWN if dispatch_was_attempted else EffectDisposition.NONE
    )
    draft = ExecutionUncertainty.model_construct(
        execution_attempt_id=authorization.execution_attempt_id,
        authorization_hash=authorization.content_hash,
        uncertainty_kind="ATTEMPT_CLOSURE_UNCONFIRMED",
        prior_report_hash=(prior_report.content_hash if prior_report is not None else None),
        dispatch_status=dispatch_status,
        effect_disposition=effect_disposition,
        human_intervention_required=dispatch_was_attempted,
        reason_code="execution_abort_uncertain",
        content_hash="0" * 64,
    )
    content_hash = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    return ExecutionUncertainty(
        execution_attempt_id=authorization.execution_attempt_id,
        authorization_hash=authorization.content_hash,
        prior_report_hash=(prior_report.content_hash if prior_report is not None else None),
        dispatch_status=dispatch_status,
        effect_disposition=effect_disposition,
        human_intervention_required=dispatch_was_attempted,
        content_hash=content_hash,
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


def _final_effect_fields(
    report: ExecutionReport | None,
    uncertainty: ExecutionUncertainty | None,
    verification_result: VerificationResult | None,
) -> tuple[VerificationEffectDisposition, bool]:
    """Derive final effect certainty without rewriting immutable execution facts."""
    if uncertainty is not None and (uncertainty.effect_disposition is EffectDisposition.UNKNOWN):
        return VerificationEffectDisposition.UNKNOWN, True
    if report is None:
        return VerificationEffectDisposition.NONE, False
    if any(record.effect_disposition is EffectDisposition.UNKNOWN for record in report.records):
        return VerificationEffectDisposition.UNKNOWN, True
    mutation_pending = any(
        record.effect_disposition is EffectDisposition.PENDING_VERIFICATION
        for record in report.records
    )
    if not mutation_pending:
        return VerificationEffectDisposition.NONE, False
    if (
        verification_result is not None
        and verification_result.status is VerificationStatus.PASSED
        and verification_result.effect_disposition is VerificationEffectDisposition.VERIFIED
    ):
        return VerificationEffectDisposition.VERIFIED, False
    return VerificationEffectDisposition.UNKNOWN, True


def _conservative_collection_duration_ms(
    report_duration_ms: int,
    accepted_at: datetime,
    events: tuple[LifecycleEvent, ...],
) -> int:
    """Bound evidence age by both Executor timing and Runtime lifecycle time."""
    execution_started_at = next(
        (
            event.occurred_at
            for event in reversed(events)
            if event.kind is LifecycleEventKind.STATE_ENTERED
            and event.state is RuntimeState.EXECUTING
        ),
        accepted_at,
    )
    elapsed = accepted_at - execution_started_at
    elapsed_ms = (
        elapsed.days * 86_400_000 + elapsed.seconds * 1_000 + (elapsed.microseconds + 999) // 1_000
    )
    return min(max(report_duration_ms, elapsed_ms), 3_600_000)


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
        and type(plan.verification_criteria) is tuple
        and all(
            type(step) is ExecutionStep and type(step.arguments) is GetSystemStatusArguments
            for step in plan.steps
        )
        and all(
            type(criterion) in VERIFICATION_CRITERION_TYPES
            for criterion in plan.verification_criteria
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


def _verification_result_has_trusted_nested_fields(result: VerificationResult) -> bool:
    """Reject untrusted verification identities, containers, and nested records."""
    if (
        type(result) is not VerificationResult
        or not _is_builtin_uuid(result.task_id)
        or not _is_builtin_uuid(result.plan_id)
        or not _is_builtin_uuid(result.execution_attempt_id)
        or type(result.evaluated_at) is not datetime
        or result.evaluated_at.tzinfo is not UTC
        or type(result.status) is not VerificationStatus
        or type(result.checks) is not tuple
        or type(result.evidence_references) is not tuple
        or type(result.failure_reasons) is not tuple
        or type(result.effect_disposition) is not VerificationEffectDisposition
        or type(result.human_intervention_required) is not bool
    ):
        return False
    if any(
        type(check) is not VerificationCheckResult
        or type(check.status) is not VerificationCheckStatus
        or (
            check.failure_reason is not None
            and type(check.failure_reason) is not VerificationFailureReason
        )
        for check in result.checks
    ):
        return False
    if any(
        type(reference) is not VerificationEvidenceReference
        or not _is_builtin_uuid(reference.invocation_id)
        or type(reference.target) is not TargetReference
        or (
            reference.accepted_at is not None
            and (
                type(reference.accepted_at) is not datetime
                or reference.accepted_at.tzinfo is not UTC
            )
        )
        for reference in result.evidence_references
    ):
        return False
    return all(type(reason) is VerificationFailureReason for reason in result.failure_reasons)


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
    if (
        outcome.execution_authorization is not None
        and type(outcome.execution_authorization) is not ExecutionAttemptAuthorization
    ):
        return False
    if (
        outcome.execution_report is not None
        and type(outcome.execution_report) is not ExecutionReport
    ):
        return False
    if (
        outcome.execution_uncertainty is not None
        and type(outcome.execution_uncertainty) is not ExecutionUncertainty
    ):
        return False
    if (
        outcome.verification_result is not None
        and not _verification_result_has_trusted_nested_fields(outcome.verification_result)
    ):
        return False
    if (
        type(outcome.final_effect_disposition) is not VerificationEffectDisposition
        or type(outcome.human_intervention_required) is not bool
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
            or (event.approval_id is not None and type(event.approval_id) is not UUID)
            or (
                event.execution_attempt_id is not None
                and type(event.execution_attempt_id) is not UUID
            )
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

    @property
    def last_timestamp(self) -> datetime:
        """Return the latest trusted UTC timestamp for fail-closed evidence."""
        if self._last_timestamp is None:
            raise InvalidClockError("Runtime has no trusted lifecycle timestamp")
        return self._last_timestamp

    def capture_timestamp(self) -> datetime | None:
        """Capture one trusted Runtime timestamp without inventing a lifecycle event."""
        timestamp = self._read_clock()
        if timestamp is not None:
            self._last_timestamp = timestamp
        return timestamp

    def record(
        self,
        *,
        kind: LifecycleEventKind,
        state: RuntimeState,
        previous_state: RuntimeState | None = None,
        component: RuntimeComponent | None = None,
        reason_code: str | None = None,
        approval_id: UUID | None = None,
        execution_attempt_id: UUID | None = None,
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
            approval_id=approval_id,
            execution_attempt_id=execution_attempt_id,
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
        approval_id: UUID | None = None,
        execution_attempt_id: UUID | None = None,
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
            approval_id=approval_id,
            execution_attempt_id=execution_attempt_id,
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
        approval_id: UUID | None,
        execution_attempt_id: UUID | None,
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
                approval_id=approval_id,
                execution_attempt_id=execution_attempt_id,
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
    """Own Task state and orchestrate trusted in-process Runtime components."""

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
        """Compose the Runtime; injected Python components are trusted test/composition seams."""
        self._context_builder = context_builder if context_builder is not None else ContextBuilder()
        self._planner = planner if planner is not None else Planner()
        self._verifier = verifier if verifier is not None else Verifier()
        self._clock = clock
        self._registry = self._validate_registry(
            registry if registry is not None else self._build_default_registry()
        )
        self._catalog = self._registry.metadata_snapshot()
        self._tool_metadata = self._resolve_runtime_metadata(self._catalog)
        self._result_validator = ToolGateway(self._registry)
        self._policy = PolicyEngine(self._registry)
        self._approval = ApprovalEngine(
            self._catalog,
            self._policy.approval_constraints,
            clock=self._clock,
        )
        self._executor = (
            executor
            if executor is not None
            else Executor(
                ToolGateway(self._registry),
                self._policy,
                self._approval,
            )
        )

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

        return self._begin_and_continue(
            task=task,
            recorder=recorder,
            plan=plan,
            policy_decision=policy_decision,
            approval_id=None,
            confirmation_reader=None,
            record_human_authorization=False,
        )

    def resume_approved(
        self,
        outcome: RuntimeOutcome,
        approval_id: UUID,
        *,
        confirmation_reader: ManualConfirmationReader | None = None,
    ) -> RuntimeOutcome:
        """Resume one exact process-local Approval and dispatch it at most once."""
        trusted_outcome, plan, decision = self._approval_inputs(outcome)
        if type(approval_id) is not UUID:
            raise InvalidRuntimeOutcomeError("Approval identity must be an exact UUID")
        if confirmation_reader is not None and not callable(confirmation_reader):
            raise InvalidRuntimeOutcomeError("Manual confirmation reader is malformed")
        recorder = _LifecycleRecorder(
            task_id=trusted_outcome.task.task_id,
            clock=self._clock,
            existing=trusted_outcome.events,
        )
        if (
            decision.manual_confirmation_requirement is ManualConfirmationRequirement.PER_INVOCATION
            and confirmation_reader is None
        ):
            return self._approval_gate_rejected(
                outcome=trusted_outcome,
                recorder=recorder,
                reason_code="l3_confirmation_unavailable",
            )
        return self._begin_and_continue(
            task=trusted_outcome.task,
            recorder=recorder,
            plan=plan,
            policy_decision=decision,
            approval_id=approval_id,
            confirmation_reader=confirmation_reader,
            record_human_authorization=True,
        )

    def _begin_and_continue(
        self,
        *,
        task: Task,
        recorder: _LifecycleRecorder,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        approval_id: UUID | None,
        confirmation_reader: ManualConfirmationReader | None,
        record_human_authorization: bool,
    ) -> RuntimeOutcome:
        try:
            raw_authorization = self._executor.begin_attempt(
                plan,
                policy_decision,
                approval_id,
            )
            authorization = self._validate_execution_authorization(
                raw_authorization,
                plan=plan,
                policy_decision=policy_decision,
                approval_id=approval_id,
            )
        except ExecutionAuthorizationError as error:
            reason_code = safe_execution_authorization_reason(error)
            if record_human_authorization and reason_code in _RETRYABLE_APPROVAL_GATE_REASONS:
                waiting_outcome = RuntimeOutcome(
                    status=RuntimeOutcomeStatus.WAITING_FOR_APPROVAL,
                    task=task,
                    plan=plan,
                    policy_decision=policy_decision,
                    events=recorder.events,
                )
                return self._approval_gate_rejected(
                    outcome=waiting_outcome,
                    recorder=recorder,
                    reason_code=reason_code,
                )
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.APPROVAL,
                error=error,
                plan=plan,
                policy_decision=policy_decision,
            )
        except BaseException as error:
            return self._failure_outcome(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.APPROVAL,
                error=error,
                plan=plan,
                policy_decision=policy_decision,
            )

        if record_human_authorization and not recorder.record(
            kind=LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED,
            state=task.state,
            reason_code="human_approved",
            approval_id=authorization.approval_id,
            execution_attempt_id=authorization.execution_attempt_id,
        ):
            recorder.record_with_last_timestamp(
                kind=LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED,
                state=task.state,
                reason_code="human_approved",
                approval_id=authorization.approval_id,
                execution_attempt_id=authorization.execution_attempt_id,
            )
            report = self._abort_attempt_safely(
                authorization,
                plan=plan,
                policy_decision=policy_decision,
                reason_code="runtime_clock_failed",
            )
            return self._clock_failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=report.results if report is not None else (),
                execution_authorization=authorization,
                execution_report=report,
                verification="not_run",
            )

        task, transition_recorded = self._transition(
            task,
            RuntimeState.EXECUTING,
            recorder,
        )
        if not transition_recorded:
            report = self._abort_attempt_safely(
                authorization,
                plan=plan,
                policy_decision=policy_decision,
                reason_code="runtime_clock_failed",
            )
            return self._clock_failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=report.results if report is not None else (),
                execution_authorization=authorization,
                execution_report=report,
                verification="not_run",
            )

        try:
            raw_report = self._executor.execute_actions(
                authorization,
                confirmation_reader,
            )
            report = self._validate_execution_report(
                raw_report,
                authorization=authorization,
                plan=plan,
                policy_decision=policy_decision,
            )
        except BaseException as error:
            report = self._abort_attempt_safely(
                authorization,
                plan=plan,
                policy_decision=policy_decision,
                reason_code="executor_failure",
            )
            return self._failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.EXECUTOR,
                error=error,
                plan=plan,
                policy_decision=policy_decision,
                results=report.results if report is not None else (),
                execution_authorization=authorization,
                execution_report=report,
                verification="not_run",
            )

        if report.next_state is ExecutionNextState.FAILED:
            return self._failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.EXECUTOR,
                error=ToolExecutionError("Executor stopped after a governed failure"),
                plan=plan,
                policy_decision=policy_decision,
                results=report.results,
                execution_authorization=authorization,
                execution_report=report,
                verification="not_run",
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
            if report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH:
                report = (
                    self._abort_attempt_safely(
                        authorization,
                        plan=plan,
                        policy_decision=policy_decision,
                        reason_code="runtime_clock_failed",
                        prior_report=report,
                    )
                    or report
                )
            return self._clock_failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=report.results,
                execution_authorization=authorization,
                execution_report=report,
                execution_dispatch_was_attempted=bool(report.records),
                verification="not_run",
            )

        task, transition_recorded = self._transition(
            task,
            RuntimeState.VERIFYING,
            recorder,
        )
        if not transition_recorded:
            if report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH:
                report = (
                    self._abort_attempt_safely(
                        authorization,
                        plan=plan,
                        policy_decision=policy_decision,
                        reason_code="runtime_clock_failed",
                        prior_report=report,
                    )
                    or report
                )
            return self._clock_failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=report.results,
                execution_authorization=authorization,
                execution_report=report,
                execution_dispatch_was_attempted=bool(report.records),
                verification="not_run",
            )

        if report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH:
            try:
                raw_report = self._executor.execute_verification(authorization)
                report = self._validate_execution_report(
                    raw_report,
                    authorization=authorization,
                    plan=plan,
                    policy_decision=policy_decision,
                    prior_report=report,
                )
            except BaseException as error:
                report = (
                    self._abort_attempt_safely(
                        authorization,
                        plan=plan,
                        policy_decision=policy_decision,
                        reason_code="executor_failure",
                        prior_report=report,
                    )
                    or report
                )
                return self._failure_outcome_with_execution_audit(
                    task=task,
                    recorder=recorder,
                    component=RuntimeComponent.EXECUTOR,
                    error=error,
                    plan=plan,
                    policy_decision=policy_decision,
                    results=report.results,
                    execution_authorization=authorization,
                    execution_report=report,
                    verification="not_run",
                )
            if report.next_state is ExecutionNextState.FAILED:
                return self._failure_outcome_with_execution_audit(
                    task=task,
                    recorder=recorder,
                    component=RuntimeComponent.EXECUTOR,
                    error=ToolExecutionError("Executor stopped after verification evidence failed"),
                    plan=plan,
                    policy_decision=policy_decision,
                    results=report.results,
                    execution_authorization=authorization,
                    execution_report=report,
                    verification="not_run",
                )

        results = self._validate_results(report.results, plan)
        accepted_at = recorder.capture_timestamp()
        verification_context = VerificationContext(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            plan_digest=canonical_json_sha256(plan),
            execution_attempt_id=authorization.execution_attempt_id,
            execution_report_hash=report.content_hash,
            evidence_accepted_at=accepted_at,
            evaluated_at=accepted_at if accepted_at is not None else recorder.last_timestamp,
            collection_duration_ms=(
                _conservative_collection_duration_ms(
                    cast(int, report.total_duration_ms),
                    accepted_at,
                    recorder.events,
                )
                if accepted_at is not None
                else cast(int, report.total_duration_ms)
            ),
            mutating_effect_pending=any(
                record.effect_disposition is EffectDisposition.PENDING_VERIFICATION
                for record in report.records
            ),
        )
        if accepted_at is None:
            clock_failure_result = build_verification_failure(
                plan,
                results,
                verification_context,
                VerificationFailureReason.CLOCK_UNAVAILABLE,
            )
            return self._clock_failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
                execution_authorization=authorization,
                execution_report=report,
                verification_result=clock_failure_result,
                verification="failed",
            )
        try:
            expected_verification = evaluate_verification(
                plan,
                results,
                verification_context,
            )
            verifier_plan = ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="error"),
                strict=True,
            )
            verifier_results = tuple(
                ToolResult[SystemStatus].model_validate(
                    result.model_dump(mode="python", warnings="error"),
                    strict=True,
                )
                for result in results
            )
            verifier_context = VerificationContext.model_validate(
                verification_context.model_dump(mode="python", warnings="error"),
                strict=True,
            )
            verify = cast(Callable[..., object], self._verifier.verify)
            raw_verification_result = verify(
                verifier_plan,
                verifier_results,
                verifier_context,
            )
        except BaseException as error:
            verification_result = build_verification_failure(
                plan,
                results,
                verification_context,
                VerificationFailureReason.VERIFIER_FAILED,
            )
            return self._failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.VERIFIER,
                error=error,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
                execution_authorization=authorization,
                execution_report=report,
                verification_result=verification_result,
                verification="failed",
            )
        try:
            if type(raw_verification_result) is not VerificationResult:
                raise VerificationError("Verifier returned an invalid completion signal")
            verification_result = VerificationResult.model_validate(
                raw_verification_result.model_dump(mode="python", warnings="error"),
                strict=True,
            )
        except BaseException as error:
            del error
            verification_result = build_verification_failure(
                plan,
                results,
                verification_context,
                VerificationFailureReason.VERIFIER_RESULT_INVALID,
            )
            return self._failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.VERIFIER,
                error=VerificationError("Verifier returned an invalid structured result"),
                plan=plan,
                policy_decision=policy_decision,
                results=results,
                execution_authorization=authorization,
                execution_report=report,
                verification_result=verification_result,
                verification="failed",
            )
        if verification_result != expected_verification:
            verification_result = build_verification_failure(
                plan,
                results,
                verification_context,
                VerificationFailureReason.VERIFIER_RESULT_INVALID,
            )
            return self._failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.VERIFIER,
                error=VerificationError("Verifier returned an invalid structured result"),
                plan=plan,
                policy_decision=policy_decision,
                results=results,
                execution_authorization=authorization,
                execution_report=report,
                verification_result=verification_result,
                verification="failed",
            )
        if verification_result.status is VerificationStatus.FAILED:
            return self._failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                component=RuntimeComponent.VERIFIER,
                error=VerificationError("Mandatory verification criteria did not pass"),
                plan=plan,
                policy_decision=policy_decision,
                results=results,
                execution_authorization=authorization,
                execution_report=report,
                verification_result=verification_result,
                verification="failed",
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
            return self._clock_failure_outcome_with_execution_audit(
                task=task,
                recorder=recorder,
                plan=plan,
                policy_decision=policy_decision,
                results=results,
                execution_authorization=authorization,
                execution_report=report,
                verification_result=verification_result,
                verification="passed",
            )
        self._log_execution_audit(
            task,
            plan,
            authorization,
            report,
            verification="passed",
        )
        self._log_verification_audit(verification_result)
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
                execution_authorization=authorization,
                execution_report=report,
                verification_result=verification_result,
            )
        final_effect, human_intervention_required = _final_effect_fields(
            report,
            None,
            verification_result,
        )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.COMPLETED,
            task=task,
            plan=plan,
            policy_decision=policy_decision,
            results=results,
            events=recorder.events,
            execution_authorization=authorization,
            execution_report=report,
            verification_result=verification_result,
            final_effect_disposition=final_effect,
            human_intervention_required=human_intervention_required,
        )

    def _approval_gate_rejected(
        self,
        *,
        outcome: RuntimeOutcome,
        recorder: _LifecycleRecorder,
        reason_code: str,
    ) -> RuntimeOutcome:
        if not recorder.record(
            kind=LifecycleEventKind.AUTHORIZATION_REJECTED,
            state=RuntimeState.WAITING_FOR_APPROVAL,
            reason_code=reason_code,
        ):
            return self._clock_failure_outcome(
                task=outcome.task,
                recorder=recorder,
                plan=outcome.plan,
                policy_decision=outcome.policy_decision,
            )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.WAITING_FOR_APPROVAL,
            task=outcome.task,
            plan=outcome.plan,
            policy_decision=outcome.policy_decision,
            events=recorder.events,
        )

    def _abort_attempt_safely(
        self,
        authorization: ExecutionAttemptAuthorization,
        *,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        reason_code: str,
        prior_report: ExecutionReport | None = None,
    ) -> ExecutionReport | None:
        """Abort once and retain only a fully revalidated authoritative report."""
        try:
            report = self._executor.abort_attempt(
                authorization,
                reason_code=reason_code,
            )
            return self._validate_execution_report(
                report,
                authorization=authorization,
                plan=plan,
                policy_decision=policy_decision,
                prior_report=prior_report,
            )
        except BaseException:
            return None

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

    def prepare_approval_review(self, outcome: RuntimeOutcome) -> ApprovalReview:
        """Prepare an exact process-local Review for a human-approval pause."""
        _, plan, decision = self._approval_inputs(outcome)
        return self._approval.prepare_review(plan, decision)

    def commit_approval(
        self,
        outcome: RuntimeOutcome,
        review_id: UUID,
    ) -> ApprovalRecord:
        """Commit one exact Review without resuming or dispatching the Plan."""
        _, plan, decision = self._approval_inputs(outcome)
        record = self._approval.commit(review_id, plan, decision)
        _safe_log_approval_record(record)
        return record

    def reject_approval(
        self,
        outcome: RuntimeOutcome,
        review_id: UUID,
    ) -> RuntimeOutcome:
        """Reject one exact Review and close its paused Runtime outcome."""
        trusted_outcome, plan, decision = self._approval_inputs(outcome)
        self._approval.reject(review_id, plan, decision)
        return self.reject(trusted_outcome)

    def _approval_inputs(
        self,
        outcome: RuntimeOutcome,
    ) -> tuple[RuntimeOutcome, ExecutionPlan, PolicyDecision]:
        """Rebuild and bind an Approval request to this Runtime's current Policy."""
        trusted_outcome = self._validate_outcome(outcome)
        if (
            trusted_outcome.status is not RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
            or trusted_outcome.task.state is not RuntimeState.WAITING_FOR_APPROVAL
            or trusted_outcome.plan is None
            or trusted_outcome.policy_decision is None
            or trusted_outcome.failure is not None
            or trusted_outcome.results
            or trusted_outcome.execution_authorization is not None
            or trusted_outcome.execution_report is not None
        ):
            raise InvalidRuntimeOutcomeError(
                "Approval requires an exact WAITING_FOR_APPROVAL outcome"
            )
        context = PolicyEvaluationContext(
            operator_id=trusted_outcome.task.user,
            target=TargetReference(
                target_id=trusted_outcome.task.target,
                resource_type="local_system",
                resource_id=trusted_outcome.task.target,
            ),
        )
        decision = self._validate_policy_decision(
            trusted_outcome.policy_decision,
            plan=trusted_outcome.plan,
            context=context,
        )
        if (
            decision.effect is not PolicyEffect.ALLOW
            or decision.approval_requirement is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
        ):
            raise InvalidRuntimeOutcomeError("Approval requires a human-approval Policy Decision")
        return trusted_outcome, trusted_outcome.plan, decision

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
            raise InvalidStateTransitionError("WAITING_FOR_APPROVAL work must use resume_approved")
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
            if any(step.arguments.target != validated.target for step in validated.steps):
                raise TypeError
            self._validate_verification_boundaries(validated)
            return validated
        except BaseException:
            raise PlanMismatchError("Planner returned a malformed execution plan") from None

    def _validate_verification_boundaries(self, plan: ExecutionPlan) -> None:
        """Bind criteria and VERIFY Steps to immutable read-only Tool Metadata."""
        steps_by_id = {step.step_id: step for step in plan.steps}
        for criterion in plan.verification_criteria:
            source_step = steps_by_id[criterion.evidence_step_id]
            metadata = self._catalog.get((source_step.tool_id, source_step.tool_version))
            if type(metadata) is not ToolMetadata:
                continue
            if (
                type(criterion) is EqualityCriterion
                and criterion.source == "evidence"
                and criterion.field not in metadata.verification.evidence_fields
            ):
                raise TypeError

        verify_steps = tuple(step for step in plan.steps if step.role is StepRole.VERIFY)
        source_steps = tuple(step for step in plan.steps if step.role is not StepRole.VERIFY)
        criterion_sources = frozenset(
            criterion.evidence_step_id for criterion in plan.verification_criteria
        )
        mutating_source_steps = tuple(
            step
            for step in source_steps
            if type(metadata := self._catalog.get((step.tool_id, step.tool_version)))
            is ToolMetadata
            and metadata.side_effects.mutates_remote_state
        )
        if any(step.role is not StepRole.ACTION for step in mutating_source_steps):
            raise TypeError
        if len(mutating_source_steps) > 1:
            raise TypeError
        for verify_step in verify_steps:
            metadata = self._catalog.get((verify_step.tool_id, verify_step.tool_version))
            if type(metadata) is ToolMetadata and (
                metadata.side_effects.mutates_remote_state or metadata.risk_level is RiskLevel.L3
            ):
                raise TypeError
            declared = any(
                type(source_metadata) is ToolMetadata
                and source_metadata.verification.required
                and any(
                    reference.tool_id == verify_step.tool_id
                    and reference.version == verify_step.tool_version
                    for reference in source_metadata.verification.tools
                )
                for source_step in source_steps
                for source_metadata in (
                    self._catalog.get((source_step.tool_id, source_step.tool_version)),
                )
            )
            if verify_step.step_id not in criterion_sources or not declared:
                raise TypeError

        for step in source_steps:
            metadata = self._catalog.get((step.tool_id, step.tool_version))
            if type(metadata) is not ToolMetadata:
                continue
            required_tools = metadata.verification.tools
            covered_required_tools = all(
                any(
                    verify_step.tool_id == reference.tool_id
                    and verify_step.tool_version == reference.version
                    and verify_step.step_id in criterion_sources
                    for verify_step in verify_steps
                )
                for reference in required_tools
            )
            mutation_postconditions_cover = all(
                any(
                    verify_step.tool_id == reference.tool_id
                    and verify_step.tool_version == reference.version
                    and any(
                        criterion.evidence_step_id == verify_step.step_id
                        and type(criterion)
                        in {
                            ExpectedStateCriterion,
                            HealthStatusCriterion,
                            NumericBoundsCriterion,
                        }
                        for criterion in plan.verification_criteria
                    )
                    for verify_step in verify_steps
                )
                for reference in required_tools
            )
            if (
                not metadata.side_effects.mutates_remote_state
                and metadata.verification.required
                and required_tools
                and not covered_required_tools
            ):
                raise TypeError
            if metadata.side_effects.mutates_remote_state and (
                not metadata.verification.required
                or not required_tools
                or not covered_required_tools
                or not mutation_postconditions_cover
            ):
                raise TypeError

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
                        and step_decision.manual_confirmation_requirement
                        is not ManualConfirmationRequirement.PER_INVOCATION
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
                    or not self._result_matches_step_contract(result, step, plan)
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

    def _validate_execution_authorization(
        self,
        authorization: ExecutionAttemptAuthorization,
        *,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        approval_id: UUID | None,
    ) -> ExecutionAttemptAuthorization:
        """Rebuild one attempt authorization and bind it to Runtime authority."""
        try:
            if type(authorization) is not ExecutionAttemptAuthorization:
                raise TypeError
            validated = ExecutionAttemptAuthorization.model_validate(
                authorization.model_dump(mode="python", warnings="error"),
                strict=True,
            )
            if (
                type(validated.execution_attempt_id) is not UUID
                or type(validated.task_id) is not UUID
                or type(validated.plan_id) is not UUID
                or type(validated.approval_requirement) is not PolicyApprovalRequirement
                or (validated.approval_id is not None and type(validated.approval_id) is not UUID)
                or policy_decision.effect is not PolicyEffect.ALLOW
                or policy_decision.approval_requirement is None
                or validated.task_id != plan.task_id
                or validated.plan_id != plan.plan_id
                or validated.plan_digest != canonical_json_sha256(plan)
                or validated.policy_decision_hash != canonical_json_sha256(policy_decision)
                or validated.approval_requirement is not policy_decision.approval_requirement
            ):
                raise TypeError
            if validated.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL:
                if type(approval_id) is not UUID or validated.approval_id != approval_id:
                    raise TypeError
                record = self._approval.record_for_attempt(
                    approval_id,
                    validated.execution_attempt_id,
                )
                if (
                    type(record) is not ApprovalRecord
                    or record.approval_id != validated.approval_id
                    or record.task_id != validated.task_id
                    or record.plan_id != validated.plan_id
                    or record.plan_hash != validated.approval_plan_hash
                    or record.content_hash != validated.approval_record_hash
                    or record.expires_at != validated.approval_expires_at
                    or record.policy_decision_hash != validated.policy_decision_hash
                    or record.approval_requirement is not validated.approval_requirement
                ):
                    raise TypeError
            elif approval_id is not None:
                raise TypeError
            return validated
        except BaseException:
            raise ExecutionAuthorizationError(
                "Executor returned invalid authorization evidence",
                reason_code="authorization_evidence_invalid",
            ) from None

    def _validate_execution_report(
        self,
        report: ExecutionReport,
        *,
        authorization: ExecutionAttemptAuthorization,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        prior_report: ExecutionReport | None = None,
    ) -> ExecutionReport:
        """Rebuild and bind one Executor report to exact Runtime authority."""
        try:
            if type(report) is not ExecutionReport:
                raise TypeError
            validated = ExecutionReport.model_validate(
                report.model_dump(mode="python", warnings="error"),
                strict=True,
            )
            if (
                validated.execution_attempt_id != authorization.execution_attempt_id
                or validated.authorization_hash != authorization.content_hash
                or validated.task_id != plan.task_id
                or validated.plan_id != plan.plan_id
                or validated.plan_digest != canonical_json_sha256(plan)
                or validated.plan_digest != authorization.plan_digest
                or validated.policy_decision_hash != canonical_json_sha256(policy_decision)
                or validated.policy_decision_hash != authorization.policy_decision_hash
                or validated.approval_id != authorization.approval_id
                or len(validated.records) > len(plan.steps)
                or (
                    validated.status is not ExecutionReportStatus.FAILED
                    and validated.total_duration_ms is None
                )
            ):
                raise TypeError
            if prior_report is not None and (
                type(prior_report) is not ExecutionReport
                or prior_report.status is not ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
                or validated.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
                or len(validated.records) < len(prior_report.records)
                or validated.records[: len(prior_report.records)] != prior_report.records
                or len(validated.events) <= len(prior_report.events)
                or validated.events[: len(prior_report.events)] != prior_report.events
                or (
                    validated.total_duration_ms is not None
                    and prior_report.total_duration_ms is not None
                    and validated.total_duration_ms < prior_report.total_duration_ms
                )
            ):
                raise TypeError
            for step_index, (record, step) in enumerate(
                zip(validated.records, plan.steps, strict=False)
            ):
                step_decision = policy_decision.step_decisions[step_index]
                expected_target = TargetReference(
                    target_id=plan.target,
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
                    or (
                        record.result is not None
                        and not self._result_matches_step_contract(
                            record.result,
                            step,
                            plan,
                        )
                    )
                ):
                    raise TypeError
                metadata = self._catalog.get((step.tool_id, step.tool_version))
                if type(metadata) is not ToolMetadata:
                    raise TypeError
                mutates_remote_state = metadata.side_effects.mutates_remote_state
                if record.dispatch_status is DispatchStatus.NOT_DISPATCHED:
                    expected_effect = EffectDisposition.NONE
                elif record.dispatch_status is DispatchStatus.UNKNOWN:
                    expected_effect = EffectDisposition.UNKNOWN
                elif record.result is not None and record.result.success:
                    expected_effect = (
                        EffectDisposition.PENDING_VERIFICATION
                        if mutates_remote_state
                        else EffectDisposition.NONE
                    )
                else:
                    expected_effect = (
                        EffectDisposition.UNKNOWN
                        if mutates_remote_state
                        else EffectDisposition.NONE
                    )
                if record.effect_disposition is not expected_effect:
                    raise TypeError
                confirmation_required = (
                    step_decision.manual_confirmation_requirement
                    is ManualConfirmationRequirement.PER_INVOCATION
                )
                if not confirmation_required:
                    if (
                        record.confirmation_id is not None
                        or record.confirmation_record_hash is not None
                    ):
                        raise TypeError
                elif (
                    record.dispatch_status is not DispatchStatus.NOT_DISPATCHED
                    and record.confirmation_id is None
                ):
                    raise TypeError
                if record.confirmation_id is not None:
                    confirmation = self._approval.consumed_confirmation_for_attempt(
                        record.confirmation_id,
                        authorization.execution_attempt_id,
                        record.invocation_id,
                    )
                    if (
                        record.confirmation_record_hash != confirmation.content_hash
                        or confirmation.approval_id != authorization.approval_id
                        or confirmation.task_id != plan.task_id
                        or confirmation.plan_id != plan.plan_id
                        or confirmation.execution_attempt_id != authorization.execution_attempt_id
                        or confirmation.invocation_id != record.invocation_id
                        or confirmation.step_index != record.step_index
                        or confirmation.step_id != record.step_id
                        or confirmation.role is not record.role
                        or confirmation.tool_id != record.tool_id
                        or confirmation.tool_version != record.tool_version
                        or confirmation.contract_hash != record.contract_hash
                        or confirmation.implementation_hash != record.implementation_hash
                        or confirmation.arguments_hash != record.arguments_hash
                        or confirmation.target != record.target
                    ):
                        raise TypeError
            step_events = tuple(
                event
                for event in validated.events
                if event.kind is ExecutionEventKind.STEP_FINISHED
            )
            if len(step_events) != len(validated.records) or any(
                event.step_index != record.step_index
                or event.step_id != record.step_id
                or event.invocation_id != record.invocation_id
                or event.dispatch_status is not record.dispatch_status
                or event.effect_disposition is not record.effect_disposition
                for event, record in zip(
                    step_events,
                    validated.records,
                    strict=True,
                )
            ):
                raise TypeError
            if validated.status is ExecutionReportStatus.READY_FOR_VERIFIER and (
                len(validated.records) != len(plan.steps)
                or len(validated.results) != len(plan.steps)
                or any(not result.success for result in validated.results)
                or validated.human_intervention_required
                or any(
                    record.effect_disposition is EffectDisposition.UNKNOWN
                    for record in validated.records
                )
            ):
                raise TypeError
            if validated.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH and (
                len(validated.records) >= len(plan.steps)
                or plan.steps[len(validated.records)].role is not StepRole.VERIFY
                or any(not result.success for result in validated.results)
            ):
                raise TypeError
            if (
                validated.status is ExecutionReportStatus.FAILED
                and validated.failed_step_index is not None
                and validated.failed_step_index >= len(plan.steps)
            ):
                raise TypeError
            if (validated.status is ExecutionReportStatus.FAILED) is not (
                validated.next_state is ExecutionNextState.FAILED
            ):
                raise TypeError
            return validated
        except BaseException:
            raise ToolExecutionError(
                "Executor returned an invalid structured ExecutionReport"
            ) from None

    def _result_matches_step_contract(
        self,
        result: ToolResult[SystemStatus],
        step: ExecutionStep,
        plan: ExecutionPlan,
    ) -> bool:
        """Revalidate one retained result through Runtime's independent Gateway."""
        try:
            call = ToolCall[GetSystemStatusArguments](
                invocation_id=result.invocation_id,
                plan_step_id=step.step_id,
                tool_id=step.tool_id,
                tool_version=step.tool_version,
                contract_hash=step.contract_hash,
                implementation_hash=step.implementation_hash,
                arguments_hash=canonical_json_sha256(step.arguments),
                target=TargetReference(
                    target_id=plan.target,
                    resource_type="local_system",
                    resource_id=step.arguments.target,
                ),
                arguments=step.arguments,
            )
            return self._result_validator.validate_result(call, result) is True
        except BaseException:
            return False

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

    def _failure_outcome_with_execution_audit(
        self,
        *,
        task: Task,
        recorder: _LifecycleRecorder,
        component: RuntimeComponent,
        error: BaseException,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        results: tuple[ToolResult[SystemStatus], ...],
        execution_authorization: ExecutionAttemptAuthorization,
        execution_report: ExecutionReport | None,
        verification_result: VerificationResult | None = None,
        verification: Literal["passed", "failed", "not_run"],
    ) -> RuntimeOutcome:
        """Build one failed outcome and emit its final execution evidence once."""
        outcome = self._failure_outcome(
            task=task,
            recorder=recorder,
            component=component,
            error=error,
            plan=plan,
            policy_decision=policy_decision,
            results=results,
            execution_authorization=execution_authorization,
            execution_report=execution_report,
            verification_result=verification_result,
        )
        self._log_runtime_execution_evidence(outcome, verification=verification)
        return outcome

    def _clock_failure_outcome_with_execution_audit(
        self,
        *,
        task: Task,
        recorder: _LifecycleRecorder,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        results: tuple[ToolResult[SystemStatus], ...],
        execution_authorization: ExecutionAttemptAuthorization,
        execution_report: ExecutionReport | None,
        verification_result: VerificationResult | None = None,
        verification: Literal["passed", "failed", "not_run"],
        execution_dispatch_was_attempted: bool = False,
    ) -> RuntimeOutcome:
        """Build one clock-failed outcome and audit final execution evidence once."""
        outcome = self._clock_failure_outcome(
            task=task,
            recorder=recorder,
            plan=plan,
            policy_decision=policy_decision,
            results=results,
            execution_authorization=execution_authorization,
            execution_report=execution_report,
            verification_result=verification_result,
            execution_dispatch_was_attempted=execution_dispatch_was_attempted,
        )
        self._log_runtime_execution_evidence(outcome, verification=verification)
        return outcome

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
        execution_authorization: ExecutionAttemptAuthorization | None = None,
        execution_report: ExecutionReport | None = None,
        verification_result: VerificationResult | None = None,
    ) -> RuntimeOutcome:
        code, message = self._safe_failure(component, error)
        uncertainty = None
        if (
            component is RuntimeComponent.EXECUTOR
            and execution_authorization is not None
            and (
                execution_report is None
                or execution_report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
            )
        ):
            uncertainty = _build_execution_uncertainty(
                execution_authorization,
                dispatch_was_attempted=True,
                prior_report=execution_report,
            )
            if execution_report is None:
                results = ()
            code = uncertainty.reason_code
            message = "Execution dispatch or effect could not be determined safely."
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
                execution_authorization=execution_authorization,
                execution_report=execution_report,
                verification_result=verification_result,
                execution_dispatch_was_attempted=(
                    uncertainty is not None
                    and uncertainty.dispatch_status is DispatchStatus.UNKNOWN
                ),
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
                execution_authorization=execution_authorization,
                execution_report=execution_report,
                verification_result=verification_result,
                execution_dispatch_was_attempted=(
                    uncertainty is not None
                    and uncertainty.dispatch_status is DispatchStatus.UNKNOWN
                ),
            )
        final_effect, human_intervention_required = _final_effect_fields(
            execution_report,
            uncertainty,
            verification_result,
        )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.FAILED,
            task=task,
            plan=plan,
            policy_decision=policy_decision,
            results=results,
            events=recorder.events,
            failure=failure,
            execution_authorization=execution_authorization,
            execution_report=execution_report,
            execution_uncertainty=uncertainty,
            verification_result=verification_result,
            final_effect_disposition=final_effect,
            human_intervention_required=human_intervention_required,
        )

    @staticmethod
    def _clock_failure_outcome(
        *,
        task: Task,
        recorder: _LifecycleRecorder,
        plan: ExecutionPlan | None = None,
        policy_decision: PolicyDecision | None = None,
        results: tuple[ToolResult[SystemStatus], ...] = (),
        execution_authorization: ExecutionAttemptAuthorization | None = None,
        execution_report: ExecutionReport | None = None,
        verification_result: VerificationResult | None = None,
        execution_dispatch_was_attempted: bool = False,
    ) -> RuntimeOutcome:
        uncertainty = None
        if execution_authorization is not None and (
            execution_report is None
            or execution_report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
        ):
            uncertainty = _build_execution_uncertainty(
                execution_authorization,
                dispatch_was_attempted=execution_dispatch_was_attempted,
                prior_report=execution_report,
            )
            if execution_report is None:
                results = ()
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
        failure = (
            RuntimeFailure(
                code=uncertainty.reason_code,
                component=RuntimeComponent.RUNTIME,
                message="Execution abort could not be confirmed safely.",
            )
            if uncertainty is not None
            else RuntimeFailure(
                code=InvalidClockError.code,
                component=RuntimeComponent.RUNTIME,
                message="Runtime lifecycle clock failed safely.",
            )
        )
        recorder.record_with_last_timestamp(
            kind=LifecycleEventKind.FAILED,
            state=task.state,
            component=failure.component,
            reason_code=failure.code,
        )
        final_effect, human_intervention_required = _final_effect_fields(
            execution_report,
            uncertainty,
            verification_result,
        )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.FAILED,
            task=task,
            plan=plan,
            policy_decision=policy_decision,
            results=results,
            events=recorder.events,
            failure=failure,
            execution_authorization=execution_authorization,
            execution_report=execution_report,
            execution_uncertainty=uncertainty,
            verification_result=verification_result,
            final_effect_disposition=final_effect,
            human_intervention_required=human_intervention_required,
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
        if component is RuntimeComponent.APPROVAL:
            if isinstance(error, ExecutionAuthorizationError):
                return (
                    safe_execution_authorization_reason(error),
                    "Execution authorization failed safely.",
                )
            return "approval_failure", "Approval validation failed safely."
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
    def _log_runtime_execution_evidence(
        outcome: RuntimeOutcome,
        *,
        verification: Literal["passed", "failed", "not_run"],
    ) -> None:
        """Emit trusted report and unattributed uncertainty evidence once."""
        plan = outcome.plan
        authorization = outcome.execution_authorization
        if plan is None or authorization is None:
            return
        if outcome.execution_report is not None:
            RuntimeEngine._log_execution_audit(
                outcome.task,
                plan,
                authorization,
                outcome.execution_report,
                verification=verification,
            )
        if outcome.verification_result is not None:
            RuntimeEngine._log_verification_audit(outcome.verification_result)
        uncertainty = outcome.execution_uncertainty
        if uncertainty is not None:
            _safe_log(
                {
                    "event": "execution_uncertainty_audit",
                    "task_id": str(outcome.task.task_id),
                    "plan_id": str(plan.plan_id),
                    "approval_id": (
                        str(authorization.approval_id)
                        if authorization.approval_id is not None
                        else None
                    ),
                    "execution_attempt_id": str(authorization.execution_attempt_id),
                    "authorization_hash": authorization.content_hash,
                    "prior_report_hash": uncertainty.prior_report_hash,
                    "uncertainty_hash": uncertainty.content_hash,
                    "uncertainty_kind": uncertainty.uncertainty_kind,
                    "dispatch_status": uncertainty.dispatch_status.value,
                    "effect_disposition": uncertainty.effect_disposition.value,
                    "human_intervention_required": (uncertainty.human_intervention_required),
                    "reason_code": uncertainty.reason_code,
                    "verification": verification,
                }
            )

    @staticmethod
    def _log_verification_audit(result: VerificationResult) -> None:
        """Emit hash-only structured verification evidence without raw values."""
        _safe_log(
            {
                "event": "verification_audit",
                "task_id": str(result.task_id),
                "plan_id": str(result.plan_id),
                "plan_digest": result.plan_digest,
                "execution_attempt_id": str(result.execution_attempt_id),
                "execution_report_hash": result.execution_report_hash,
                "verification_result_hash": result.content_hash,
                "status": result.status.value,
                "failure_reasons": tuple(reason.value for reason in result.failure_reasons),
                "effect_disposition": result.effect_disposition.value,
                "human_intervention_required": result.human_intervention_required,
                "checks": tuple(
                    {
                        "criterion_id": check.criterion_id,
                        "evidence_step_id": check.evidence_step_id,
                        "evaluator_version": check.evaluator_version,
                        "status": check.status.value,
                        "failure_reason": (
                            check.failure_reason.value if check.failure_reason is not None else None
                        ),
                    }
                    for check in result.checks
                ),
                "evidence_references": tuple(
                    {
                        "step_index": reference.step_index,
                        "step_id": reference.step_id,
                        "invocation_id": str(reference.invocation_id),
                        "result_hash": reference.result_hash,
                    }
                    for reference in result.evidence_references
                ),
            }
        )

    @staticmethod
    def _log_execution_audit(
        task: Task,
        plan: ExecutionPlan,
        authorization: ExecutionAttemptAuthorization,
        report: ExecutionReport,
        *,
        verification: Literal["passed", "failed", "not_run"],
    ) -> None:
        for index, step in enumerate(plan.steps):
            record = report.records[index] if index < len(report.records) else None
            result = record.result if record is not None else None
            result_status: Literal["success", "invalid", "execution_failed"]
            duration_ms: int | None
            if isinstance(result, ToolResult):
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
                    "approval_id": (
                        str(authorization.approval_id)
                        if authorization.approval_id is not None
                        else None
                    ),
                    "execution_attempt_id": str(authorization.execution_attempt_id),
                    "report_hash": report.content_hash,
                    "step_index": index,
                    "role": step.role.value,
                    "invocation_id": (str(record.invocation_id) if record is not None else None),
                    "dispatch_status": (
                        record.dispatch_status.value if record is not None else None
                    ),
                    "effect_disposition": (
                        record.effect_disposition.value if record is not None else None
                    ),
                    "failure_code": record.failure_code if record is not None else None,
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
