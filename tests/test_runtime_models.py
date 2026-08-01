from datetime import UTC, datetime, timedelta, tzinfo
from types import MappingProxyType
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_server.approval.engine import ApprovalEngine
from ai_server.executor.service import Executor
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, StepRole
from ai_server.models.executor import (
    DispatchStatus,
    EffectDisposition,
    ExecutionAttemptAuthorization,
    ExecutionReport,
    ExecutionReportStatus,
    ExecutionUncertainty,
)
from ai_server.models.policy import (
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEvaluationContext,
)
from ai_server.models.runtime import (
    LifecycleEvent,
    LifecycleEventKind,
    RuntimeComponent,
    RuntimeFailure,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
)
from ai_server.models.task import Task
from ai_server.models.tool import ToolMetadata, ToolReference
from ai_server.models.verification import VerificationResult
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine
from ai_server.runtime.engine import RuntimeEngine
from ai_server.runtime.state import RuntimeState
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.gateway import ToolGateway
from ai_server.tools.hashing import canonical_json_sha256


def assign_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


def completed_outcome() -> RuntimeOutcome:
    timestamp = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
    return RuntimeEngine(clock=lambda: timestamp).run(Task(request=SUPPORTED_REQUEST))


def waiting_runtime_and_outcome() -> tuple[RuntimeEngine, RuntimeOutcome]:
    registry = build_default_registry()
    runtime = RuntimeEngine(registry=registry)
    trusted_evaluate = runtime._policy.evaluate

    def approval_evaluate(
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        decision = trusted_evaluate(plan, context)
        steps = tuple(
            step.model_copy(
                update={"approval_requirement": (PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL)}
            )
            for step in decision.step_decisions
        )
        return decision.model_copy(
            update={
                "approval_requirement": (PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL),
                "step_decisions": steps,
            }
        )

    runtime._policy.evaluate = approval_evaluate  # type: ignore[method-assign]
    return runtime, runtime.run(Task(request=SUPPORTED_REQUEST))


def waiting_outcome() -> RuntimeOutcome:
    _, outcome = waiting_runtime_and_outcome()
    return outcome


def rehash_authorization(
    authorization: ExecutionAttemptAuthorization,
    **updates: object,
) -> ExecutionAttemptAuthorization:
    draft = authorization.model_copy(
        update={
            **updates,
            "content_hash": "0" * 64,
        }
    )
    document = draft.model_dump(mode="python", warnings="error")
    document["content_hash"] = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    return ExecutionAttemptAuthorization.model_validate(document, strict=True)


def rehash_report(
    report: ExecutionReport,
    **updates: object,
) -> ExecutionReport:
    draft = report.model_copy(
        update={
            **updates,
            "content_hash": "0" * 64,
        }
    )
    document = draft.model_dump(mode="python", warnings="error")
    document["content_hash"] = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    return ExecutionReport.model_validate(document, strict=True)


def rehash_verification(
    result: VerificationResult,
    **updates: object,
) -> VerificationResult:
    """Build a structurally valid hostile verification result with a new Hash."""
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


def execution_uncertainty(
    authorization: ExecutionAttemptAuthorization,
    *,
    dispatch_status: DispatchStatus,
    effect_disposition: EffectDisposition,
    human_intervention_required: bool,
    prior_report_hash: str | None = None,
) -> ExecutionUncertainty:
    """Build one exact hash-bound uncertainty fixture."""
    draft = ExecutionUncertainty.model_construct(
        execution_attempt_id=authorization.execution_attempt_id,
        authorization_hash=authorization.content_hash,
        prior_report_hash=prior_report_hash,
        dispatch_status=dispatch_status,
        effect_disposition=effect_disposition,
        human_intervention_required=human_intervention_required,
        content_hash="0" * 64,
    )
    payload = draft.model_dump(mode="python", warnings="error")
    payload["content_hash"] = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    return ExecutionUncertainty.model_validate(payload, strict=True)


class ObserveVerifyPlanner(Planner):
    """Build one read-only execution Step followed by one verification Step."""

    def create_plan(
        self,
        context: RuntimeContext,
        metadata: ToolMetadata,
    ) -> ExecutionPlan:
        """Extend the deterministic Mock plan with one explicit VERIFY Step."""
        plan = super().create_plan(context, metadata)
        verify = plan.steps[0].model_copy(
            update={
                "step_id": "verify-system-status",
                "role": StepRole.VERIFY,
            }
        )
        return ExecutionPlan(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            target=plan.target,
            steps=(*plan.steps, verify),
            verification_criteria=(
                plan.verification_criteria[0].model_copy(
                    update={"evidence_step_id": verify.step_id}
                ),
            ),
        )


def declare_mock_verification_tool(
    runtime: RuntimeEngine,
    *extra_policies: PolicyEngine,
) -> None:
    """Install a synthetic reviewed self-reference for explicit VERIFY tests."""
    key = ("get_system_status", "1.0.0")
    metadata = runtime._catalog[key]
    verification = metadata.verification.model_copy(
        update={"tools": (ToolReference(tool_id=metadata.tool_id, version=metadata.version),)}
    )
    snapshot = MappingProxyType({key: metadata.model_copy(update={"verification": verification})})
    runtime._catalog = snapshot
    runtime._policy._metadata = snapshot
    for policy in extra_policies:
        policy._metadata = snapshot


def test_runtime_lifecycle_models_round_trip_and_are_frozen() -> None:
    completed = completed_outcome()
    failed = RuntimeEngine().run(Task(request="unsupported"))

    assert RuntimeOutcome.model_validate_json(completed.model_dump_json()) == completed
    assert RuntimeOutcome.model_validate_json(failed.model_dump_json()) == failed
    assert isinstance(completed.events, tuple)
    assert isinstance(completed.results, tuple)

    models_and_fields = (
        (completed, "status", RuntimeOutcomeStatus.FAILED),
        (completed.events[0], "sequence", 99),
        (
            RuntimeFailure(
                code="safe_failure",
                component=RuntimeComponent.RUNTIME,
                message="Safe failure.",
            ),
            "code",
            "changed",
        ),
    )
    for model, name, value in models_and_fields:
        with pytest.raises(ValidationError):
            assign_attribute(model, name, value)


def test_execution_uncertainty_is_strict_frozen_hash_bound_evidence() -> None:
    completed = completed_outcome()
    assert completed.execution_authorization is not None
    uncertainty = execution_uncertainty(
        completed.execution_authorization,
        dispatch_status=DispatchStatus.UNKNOWN,
        effect_disposition=EffectDisposition.UNKNOWN,
        human_intervention_required=True,
    )

    assert ExecutionUncertainty.model_validate_json(uncertainty.model_dump_json()) == uncertainty
    with pytest.raises(ValidationError):
        assign_attribute(uncertainty, "dispatch_status", DispatchStatus.NOT_DISPATCHED)
    with pytest.raises(ValidationError, match="content hash"):
        ExecutionUncertainty.model_validate(
            uncertainty.model_copy(update={"content_hash": "f" * 64}).model_dump(mode="python"),
            strict=True,
        )


@pytest.mark.parametrize(
    ("dispatch_status", "effect_disposition", "human_intervention_required"),
    [
        (DispatchStatus.NOT_DISPATCHED, EffectDisposition.UNKNOWN, False),
        (DispatchStatus.NOT_DISPATCHED, EffectDisposition.NONE, True),
        (DispatchStatus.UNKNOWN, EffectDisposition.NONE, True),
        (DispatchStatus.UNKNOWN, EffectDisposition.UNKNOWN, False),
        (DispatchStatus.HANDLER_DISPATCHED, EffectDisposition.NONE, False),
    ],
)
def test_execution_uncertainty_rejects_contradictory_dispatch_facts(
    dispatch_status: DispatchStatus,
    effect_disposition: EffectDisposition,
    human_intervention_required: bool,
) -> None:
    completed = completed_outcome()
    assert completed.execution_authorization is not None

    with pytest.raises(ValidationError, match="inconsistent"):
        execution_uncertainty(
            completed.execution_authorization,
            dispatch_status=dispatch_status,
            effect_disposition=effect_disposition,
            human_intervention_required=human_intervention_required,
        )


def test_lifecycle_event_requires_aware_utc_timestamp() -> None:
    task = Task(request=SUPPORTED_REQUEST)

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        LifecycleEvent(
            task_id=task.task_id,
            sequence=0,
            occurred_at=datetime(2026, 7, 25, 8, 0),
            kind=LifecycleEventKind.STATE_ENTERED,
            state=RuntimeState.RECEIVED,
        )


def test_lifecycle_event_datetime_hooks_cannot_raise_baseexception() -> None:
    class ExitingTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta:
            del value
            raise SystemExit("SENSITIVE_EVENT_TIMEZONE_MARKER")

        def dst(self, value: datetime | None) -> timedelta:
            del value
            return timedelta(0)

        def tzname(self, value: datetime | None) -> str:
            del value
            return "untrusted-test"

    with pytest.raises(ValidationError) as caught:
        LifecycleEvent(
            task_id=uuid4(),
            sequence=0,
            occurred_at=datetime(2026, 7, 25, 8, 0, tzinfo=ExitingTimezone()),
            kind=LifecycleEventKind.STATE_ENTERED,
            state=RuntimeState.RECEIVED,
        )

    assert "SENSITIVE_EVENT_TIMEZONE_MARKER" not in str(caught.value)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("sequence", "sequences"),
        ("task_id", "belong"),
        ("timestamp", "backwards"),
        ("history", "state-entry events"),
        ("arguments_hash", "Runtime results"),
        ("target_scope", "Runtime results"),
        ("status", "FAILED outcome"),
        ("missing_failure", "FAILED outcome"),
    ],
)
def test_runtime_outcome_rejects_contradictory_lifecycle_data(
    mutation: str,
    message: str,
) -> None:
    completed = completed_outcome()
    payload = completed.model_dump(mode="python")

    if mutation == "sequence":
        payload["events"][1]["sequence"] = 7
    elif mutation == "task_id":
        payload["events"][1]["task_id"] = uuid4()
    elif mutation == "timestamp":
        payload["events"][1]["occurred_at"] = datetime(2025, 1, 1, tzinfo=UTC)
    elif mutation == "history":
        payload["task"]["state_history"] = [
            RuntimeState.RECEIVED,
            RuntimeState.CONTEXT_BUILDING,
            RuntimeState.PLANNING,
            RuntimeState.POLICY_CHECK,
            RuntimeState.WAITING_FOR_APPROVAL,
            RuntimeState.EXECUTING,
            RuntimeState.FAILED,
        ]
        payload["task"]["state"] = RuntimeState.FAILED
    elif mutation == "arguments_hash":
        payload["results"][0]["arguments_hash"] = "d" * 64
    elif mutation == "target_scope":
        payload["results"][0]["target"]["resource_type"] = "other_resource"
    elif mutation == "status":
        payload["status"] = RuntimeOutcomeStatus.FAILED
    else:
        failed = RuntimeEngine().run(Task(request="unsupported"))
        payload = failed.model_dump(mode="python")
        payload["failure"] = None

    with pytest.raises(ValidationError, match=message):
        RuntimeOutcome.model_validate(payload)


def test_event_shapes_reject_contradictory_fields() -> None:
    task = Task(request=SUPPORTED_REQUEST)
    timestamp = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)

    invalid_payloads = (
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.STATE_ENTERED,
            "state": RuntimeState.RECEIVED,
            "component": RuntimeComponent.RUNTIME,
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.COMPONENT_COMPLETED,
            "state": RuntimeState.PLANNING,
            "component": RuntimeComponent.EXECUTOR,
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.APPROVAL_DECISION_RECORDED,
            "state": RuntimeState.EXECUTING,
            "reason_code": "not_required",
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.APPROVAL_DECISION_RECORDED,
            "state": RuntimeState.WAITING_FOR_APPROVAL,
            "reason_code": "approval_required",
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.PAUSED,
            "state": RuntimeState.POLICY_CHECK,
            "reason_code": "approval_required",
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.FAILED,
            "state": RuntimeState.FAILED,
        },
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            LifecycleEvent.model_validate(payload)


def test_outcome_requires_exact_component_completion_evidence() -> None:
    completed = completed_outcome()
    payload = completed.model_dump(mode="python")
    payload["events"] = [
        event for event in payload["events"] if event["component"] != RuntimeComponent.PLANNER
    ]
    for sequence, event in enumerate(payload["events"]):
        event["sequence"] = sequence
    payload["events"] = tuple(payload["events"])

    with pytest.raises(ValidationError, match="component-completion events"):
        RuntimeOutcome.model_validate(payload)


def test_failure_component_must_match_failed_stage() -> None:
    failed = RuntimeEngine().run(Task(request="unsupported"))
    payload = failed.model_dump(mode="python")
    payload["failure"]["component"] = RuntimeComponent.CONTEXT_BUILDER
    payload["events"][-1]["component"] = RuntimeComponent.CONTEXT_BUILDER

    with pytest.raises(ValidationError, match="failed component|failure component"):
        RuntimeOutcome.model_validate(payload)


def test_plan_and_results_require_completed_producer_stages() -> None:
    waiting = waiting_outcome()
    completed = completed_outcome()

    missing_plan = waiting.model_dump(mode="python")
    missing_plan["plan"] = None
    with pytest.raises(ValidationError, match="requires a plan"):
        RuntimeOutcome.model_validate(missing_plan)

    premature_result = waiting.model_dump(mode="python")
    premature_result["results"] = completed.model_dump(mode="python")["results"]
    with pytest.raises(ValidationError, match="Incomplete Executor results"):
        RuntimeOutcome.model_validate(premature_result)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "completed Policy stage requires"),
        ("task_id", "Policy decision identity"),
        ("plan_id", "Policy decision identity"),
        ("operator_id", "Policy decision identity"),
        ("target", "Policy decision identity"),
        ("step_id", "ordered planned step"),
        ("arguments_hash", "ordered planned step"),
    ],
)
def test_runtime_outcome_requires_policy_decision_bound_to_task_and_plan(
    mutation: str,
    message: str,
) -> None:
    completed = completed_outcome()
    payload = completed.model_dump(mode="python")

    if mutation == "missing":
        payload["policy_decision"] = None
    elif mutation in {"task_id", "plan_id"}:
        payload["policy_decision"][mutation] = uuid4()
    elif mutation == "operator_id":
        payload["policy_decision"]["operator_id"] = "other-user"
    elif mutation == "target":
        payload["policy_decision"]["target"]["target_id"] = "other-target"
    elif mutation == "step_id":
        payload["policy_decision"]["step_decisions"][0]["step_id"] = "other-step"
    else:
        payload["policy_decision"]["step_decisions"][0]["arguments_hash"] = "d" * 64

    with pytest.raises(ValidationError, match=message):
        RuntimeOutcome.model_validate(payload)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "paused_too"])
def test_automatic_approval_gate_requires_one_not_required_decision(
    mutation: str,
) -> None:
    completed = completed_outcome()
    payload = completed.model_dump(mode="python")
    payload["events"] = list(payload["events"])
    decision_index = next(
        index
        for index, event in enumerate(payload["events"])
        if event["kind"] is LifecycleEventKind.APPROVAL_DECISION_RECORDED
    )

    if mutation == "missing":
        del payload["events"][decision_index]
    else:
        extra = payload["events"][decision_index].copy()
        if mutation == "paused_too":
            extra.update(
                {
                    "kind": LifecycleEventKind.PAUSED,
                    "reason_code": "approval_required",
                }
            )
        payload["events"].insert(decision_index + 1, extra)
    for sequence, event in enumerate(payload["events"]):
        event["sequence"] = sequence
    payload["events"] = tuple(payload["events"])

    with pytest.raises(ValidationError, match="exactly one approval decision"):
        RuntimeOutcome.model_validate(payload)


def test_waiting_history_requires_pause_and_defined_terminal_reason() -> None:
    waiting = waiting_outcome()
    without_pause = waiting.model_dump(mode="python")
    without_pause["events"] = [
        event for event in without_pause["events"] if event["kind"] != LifecycleEventKind.PAUSED
    ]
    for sequence, event in enumerate(without_pause["events"]):
        event["sequence"] = sequence
    without_pause["events"] = tuple(without_pause["events"])

    with pytest.raises(ValidationError, match="approval decision"):
        RuntimeOutcome.model_validate(without_pause)

    rejected = RuntimeEngine().reject(waiting)
    unsupported_failure = rejected.model_dump(mode="python")
    unsupported_failure["events"][-1].update(
        {
            "kind": LifecycleEventKind.FAILED,
            "reason_code": "runtime_failure",
        }
    )
    unsupported_failure["failure"].update(
        {
            "code": "runtime_failure",
            "message": "Safe.",
        }
    )
    with pytest.raises(ValidationError, match="unsupported Phase 1 reason"):
        RuntimeOutcome.model_validate(unsupported_failure)


def test_lifecycle_authorization_fields_exist_only_on_consumption() -> None:
    task_id = uuid4()
    approval_id = uuid4()
    attempt_id = uuid4()
    timestamp = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)

    consumed = LifecycleEvent(
        task_id=task_id,
        sequence=0,
        occurred_at=timestamp,
        kind=LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED,
        state=RuntimeState.WAITING_FOR_APPROVAL,
        reason_code="human_approved",
        approval_id=approval_id,
        execution_attempt_id=attempt_id,
    )

    assert consumed.approval_id == approval_id
    assert consumed.execution_attempt_id == attempt_id

    invalid_payloads = (
        consumed.model_copy(update={"execution_attempt_id": None}).model_dump(mode="python"),
        LifecycleEvent(
            task_id=task_id,
            sequence=0,
            occurred_at=timestamp,
            kind=LifecycleEventKind.STATE_ENTERED,
            state=RuntimeState.RECEIVED,
        ).model_dump(mode="python")
        | {
            "approval_id": approval_id,
            "execution_attempt_id": attempt_id,
        },
        {
            "task_id": task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.AUTHORIZATION_REJECTED,
            "state": RuntimeState.WAITING_FOR_APPROVAL,
            "reason_code": "human_approved",
        },
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            LifecycleEvent.model_validate(payload)


def test_human_execution_binds_consumed_approval_and_attempt() -> None:
    runtime, waiting = waiting_runtime_and_outcome()
    review = runtime.prepare_approval_review(waiting)
    approval = runtime.commit_approval(waiting, review.review_id)

    completed = runtime.resume_approved(waiting, approval.approval_id)

    assert completed.status is RuntimeOutcomeStatus.COMPLETED
    assert completed.execution_authorization is not None
    assert completed.execution_report is not None
    assert completed.execution_authorization.approval_id == approval.approval_id
    assert completed.execution_report.approval_id == approval.approval_id
    consumed = tuple(
        event
        for event in completed.events
        if event.kind is LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED
    )
    assert len(consumed) == 1
    assert consumed[0].approval_id == approval.approval_id
    assert (
        consumed[0].execution_attempt_id == completed.execution_authorization.execution_attempt_id
    )
    assert sum(event.kind is LifecycleEventKind.PAUSED for event in completed.events) == 1
    assert RuntimeOutcome.model_validate_json(completed.model_dump_json()) == completed


def test_unknown_approval_records_non_terminal_rejection_and_stays_paused() -> None:
    runtime, waiting = waiting_runtime_and_outcome()

    rejected = runtime.resume_approved(waiting, uuid4())

    assert rejected.status is RuntimeOutcomeStatus.WAITING_FOR_APPROVAL
    assert rejected.task.state is RuntimeState.WAITING_FOR_APPROVAL
    assert rejected.events[-1].kind is LifecycleEventKind.AUTHORIZATION_REJECTED
    assert rejected.events[-1].reason_code == "unknown_approval"
    assert rejected.failure is None
    assert rejected.execution_authorization is None
    assert rejected.execution_report is None
    assert rejected.results == ()
    assert sum(event.kind is LifecycleEventKind.PAUSED for event in rejected.events) == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("authorization_task", "authorization must bind"),
        ("authorization_plan", "authorization must bind"),
        ("report_task", "ExecutionReport must bind"),
        ("report_authorization", "ExecutionReport must bind"),
        ("results", "exactly equal"),
    ],
)
def test_runtime_outcome_rejects_rehashed_execution_forgery(
    mutation: str,
    message: str,
) -> None:
    completed = completed_outcome()
    assert completed.execution_authorization is not None
    assert completed.execution_report is not None
    payload = completed.model_dump(mode="python")

    if mutation == "authorization_task":
        payload["execution_authorization"] = rehash_authorization(
            completed.execution_authorization,
            task_id=uuid4(),
        )
    elif mutation == "authorization_plan":
        payload["execution_authorization"] = rehash_authorization(
            completed.execution_authorization,
            plan_digest="f" * 64,
        )
    elif mutation == "report_task":
        payload["execution_report"] = rehash_report(
            completed.execution_report,
            task_id=uuid4(),
        )
    elif mutation == "report_authorization":
        payload["execution_report"] = rehash_report(
            completed.execution_report,
            authorization_hash="f" * 64,
        )
    else:
        payload["results"] = ()

    with pytest.raises(ValidationError, match=message):
        RuntimeOutcome.model_validate(payload)


@pytest.mark.parametrize("mutation", ["check_binding", "evidence_result_hash"])
def test_runtime_outcome_rejects_rehashed_verification_evidence_forgery(
    mutation: str,
) -> None:
    completed = completed_outcome()
    assert completed.verification_result is not None
    verification = completed.verification_result
    if mutation == "check_binding":
        forged_checks = (
            verification.checks[0].model_copy(update={"evidence_step_id": "forged-step"}),
            *verification.checks[1:],
        )
        forged = rehash_verification(verification, checks=forged_checks)
    else:
        forged_references = (
            verification.evidence_references[0].model_copy(update={"result_hash": "f" * 64}),
            *verification.evidence_references[1:],
        )
        forged = rehash_verification(
            verification,
            evidence_references=forged_references,
        )
    payload = completed.model_dump(mode="python")
    payload["verification_result"] = forged

    with pytest.raises(ValidationError, match="Verification"):
        RuntimeOutcome.model_validate(payload, strict=True)


def test_runtime_outcome_requires_verifier_failure_evidence() -> None:
    class MismatchedCriterionPlanner(Planner):
        def create_plan(
            self,
            context: RuntimeContext,
            metadata: ToolMetadata,
        ) -> ExecutionPlan:
            plan = super().create_plan(context, metadata)
            criterion = plan.verification_criteria[0].model_copy(update={"expected": "not-mock"})
            return plan.model_copy(update={"verification_criteria": (criterion,)})

    failed = RuntimeEngine(planner=MismatchedCriterionPlanner()).run(
        Task(request=SUPPORTED_REQUEST)
    )
    assert failed.failure is not None
    assert failed.failure.component is RuntimeComponent.VERIFIER
    assert failed.verification_result is not None

    payload = failed.model_dump(mode="python")
    payload["verification_result"] = None

    with pytest.raises(ValidationError, match="failed verification evidence"):
        RuntimeOutcome.model_validate(payload, strict=True)


@pytest.mark.parametrize("missing_field", ["execution_authorization", "execution_report"])
def test_executed_outcome_requires_authorization_and_report_together(
    missing_field: str,
) -> None:
    payload = completed_outcome().model_dump(mode="python")
    payload[missing_field] = None

    with pytest.raises(ValidationError, match="Execution"):
        RuntimeOutcome.model_validate(payload)


def test_executor_may_fail_during_verifying_after_action_phase_completed() -> None:
    registry = build_default_registry()
    policy = PolicyEngine(registry)
    approval = ApprovalEngine(
        registry.metadata_snapshot(),
        policy.approval_constraints,
    )
    gateway_ticks = iter((0, 0, 0, 10**12))
    executor = Executor(
        ToolGateway(registry, clock=lambda: next(gateway_ticks)),
        policy,
        approval,
        clock=lambda: 0,
    )
    runtime = RuntimeEngine(
        registry=registry,
        planner=ObserveVerifyPlanner(),
        executor=executor,
    )
    declare_mock_verification_tool(runtime, policy)

    failed = runtime.run(Task(request=SUPPORTED_REQUEST))

    assert failed.status is RuntimeOutcomeStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.component is RuntimeComponent.EXECUTOR
    assert failed.task.state_history[-2] is RuntimeState.VERIFYING
    assert failed.execution_report is not None
    assert failed.execution_report.status is ExecutionReportStatus.FAILED
    assert len(failed.results) == 2
    assert failed.results[0].success is True
    assert failed.results[1].success is False
    assert any(
        event.kind is LifecycleEventKind.COMPONENT_COMPLETED
        and event.component is RuntimeComponent.EXECUTOR
        and event.state is RuntimeState.EXECUTING
        for event in failed.events
    )
    assert RuntimeOutcome.model_validate_json(failed.model_dump_json()) == failed


def test_runtime_outcome_public_enums_are_exact_and_stable() -> None:
    assert tuple(RuntimeOutcomeStatus) == (
        RuntimeOutcomeStatus.COMPLETED,
        RuntimeOutcomeStatus.FAILED,
        RuntimeOutcomeStatus.WAITING_FOR_APPROVAL,
    )
    assert tuple(LifecycleEventKind) == (
        LifecycleEventKind.STATE_ENTERED,
        LifecycleEventKind.COMPONENT_COMPLETED,
        LifecycleEventKind.APPROVAL_DECISION_RECORDED,
        LifecycleEventKind.APPROVAL_AUTHORIZATION_CONSUMED,
        LifecycleEventKind.AUTHORIZATION_REJECTED,
        LifecycleEventKind.PAUSED,
        LifecycleEventKind.REJECTED,
        LifecycleEventKind.FAILED,
    )
    assert tuple(RuntimeComponent) == (
        RuntimeComponent.RUNTIME,
        RuntimeComponent.CONTEXT_BUILDER,
        RuntimeComponent.PLANNER,
        RuntimeComponent.POLICY,
        RuntimeComponent.APPROVAL,
        RuntimeComponent.EXECUTOR,
        RuntimeComponent.VERIFIER,
    )
