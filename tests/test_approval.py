from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_server.approval.engine import ApprovalEngine
from ai_server.approval.errors import (
    ApprovalConfigurationError,
    ApprovalReviewError,
    ApprovalStateError,
)
from ai_server.models.approval import (
    ApprovalAuditEventKind,
    ApprovalInvalidationReason,
    ApprovalRecord,
    ApprovalValidationReason,
    ApprovalValidationVerdict,
    ManualConfirmationRecord,
)
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, StepRole
from ai_server.models.policy import (
    ApprovalConstraints,
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEffect,
    PolicyEvaluationContext,
    PolicyReasonCode,
)
from ai_server.models.task import Task
from ai_server.models.tool import RiskLevel, TargetReference, ToolMetadata
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.registry import ToolRegistry


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def assign_attribute(instance: object, name: str, value: object) -> None:
    """Exercise frozen model assignment without weakening static typing."""
    setattr(instance, name, value)


def build_plan_and_decision(
    *,
    risk: RiskLevel = RiskLevel.L0,
) -> tuple[
    ToolRegistry,
    dict[tuple[str, str], ToolMetadata],
    ExecutionPlan,
    PolicyDecision,
]:
    registry = build_default_registry()
    catalog = dict(registry.metadata_snapshot())
    metadata = next(iter(catalog.values()))
    task = Task(request=SUPPORTED_REQUEST)
    context = RuntimeContext(
        task_id=task.task_id,
        request=task.request,
        user=task.user,
        target=task.target,
    )
    plan = Planner().create_plan(context, metadata)
    decision = PolicyEngine(registry).evaluate(
        plan,
        PolicyEvaluationContext(
            operator_id=task.user,
            target=TargetReference(
                target_id=task.target,
                resource_type="local_system",
                resource_id=task.target,
            ),
        ),
    )
    if risk is RiskLevel.L0:
        step_decisions = tuple(
            step.model_copy(
                update={"approval_requirement": (PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL)}
            )
            for step in decision.step_decisions
        )
        return (
            registry,
            catalog,
            plan,
            decision.model_copy(
                update={
                    "approval_requirement": (PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL),
                    "step_decisions": step_decisions,
                }
            ),
        )

    if risk not in {RiskLevel.L2, RiskLevel.L3}:
        raise AssertionError("Synthetic approval tests only support L0, L2, or L3")
    synthetic_metadata = metadata.model_copy(update={"risk_level": risk})
    catalog[(synthetic_metadata.tool_id, synthetic_metadata.version)] = synthetic_metadata
    action_step = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
    plan = plan.model_copy(update={"steps": (action_step,)})
    confirmation_requirement = (
        ManualConfirmationRequirement.PER_INVOCATION
        if risk is RiskLevel.L3
        else ManualConfirmationRequirement.NOT_REQUIRED
    )
    step_decision = decision.step_decisions[0].model_copy(
        update={
            "resolved_risk": risk,
            "effect": PolicyEffect.ALLOW,
            "reason_code": PolicyReasonCode.ALLOWED,
            "approval_requirement": PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
            "manual_confirmation_requirement": confirmation_requirement,
        }
    )
    return (
        registry,
        catalog,
        plan,
        decision.model_copy(
            update={
                "effective_risk": risk,
                "effect": PolicyEffect.ALLOW,
                "reason_code": PolicyReasonCode.ALLOWED,
                "approval_requirement": (PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL),
                "manual_confirmation_requirement": confirmation_requirement,
                "step_decisions": (step_decision,),
            }
        ),
    )


def make_engine(
    catalog: dict[tuple[str, str], ToolMetadata],
    clock: MutableClock,
) -> ApprovalEngine:
    return ApprovalEngine(
        catalog,
        ApprovalConstraints(
            review_session_ttl_seconds=300,
            plan_approval_ttl_seconds=300,
            l3_confirmation_ttl_seconds=30,
        ),
        clock=clock,
    )


def issue_approval(
    engine: ApprovalEngine,
    plan: ExecutionPlan,
    decision: PolicyDecision,
) -> ApprovalRecord:
    review = engine.prepare_review(plan, decision)
    return engine.commit(review.review_id, plan, decision)


def test_review_and_commit_bind_exact_snapshot_without_raw_arguments_in_record() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    clock = MutableClock()
    engine = make_engine(catalog, clock)

    review = engine.prepare_review(plan, decision)
    record = engine.commit(review.review_id, plan, decision)

    assert review.plan_hash == record.plan_hash
    assert review.snapshot.execution_order == ("get-system-status",)
    assert review.snapshot.steps[0].arguments == {"target": "local-mock"}
    assert record.approver == "local-owner"
    assert record.expires_at - record.issued_at == timedelta(seconds=300)
    assert '"arguments":' not in record.model_dump_json()
    assert record.steps[0].arguments_hash == review.snapshot.steps[0].arguments_hash
    assert tuple(event.kind for event in engine.events) == (
        ApprovalAuditEventKind.REVIEW_PREPARED,
        ApprovalAuditEventKind.PLAN_APPROVAL_ISSUED,
    )
    assert [event.sequence for event in engine.events] == [0, 1]
    assert '"arguments":' not in "".join(event.model_dump_json() for event in engine.events)
    assert engine.events[-1].step_bindings == record.steps


def test_l2_review_commit_consume_and_revalidate_without_dispatch() -> None:
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L2)
    engine = make_engine(catalog, MutableClock())
    attempt_id = uuid4()

    record = issue_approval(engine, plan, decision)
    consumed = engine.consume_for_attempt(record.approval_id, plan, decision, attempt_id)
    revalidated = engine.validate_for_attempt(
        record.approval_id,
        plan,
        decision,
        attempt_id,
    )

    assert record.effective_risk is RiskLevel.L2
    assert record.manual_confirmation_requirement is ManualConfirmationRequirement.NOT_REQUIRED
    assert consumed.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
    assert revalidated.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
    assert all(event.invocation_id is None for event in engine.events)


def test_sibling_approvals_for_same_exact_plan_cannot_authorize_two_attempts() -> None:
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L2)
    engine = make_engine(catalog, MutableClock())
    first = issue_approval(engine, plan, decision)
    second = issue_approval(engine, plan, decision)
    first_attempt = uuid4()
    second_attempt = uuid4()

    consumed = engine.consume_for_attempt(
        first.approval_id,
        plan,
        decision,
        first_attempt,
    )
    sibling = engine.consume_for_attempt(
        second.approval_id,
        plan,
        decision,
        second_attempt,
    )

    assert consumed.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
    assert sibling.verdict is ApprovalValidationVerdict.INVALID
    assert sibling.reason is ApprovalValidationReason.APPROVAL_ALREADY_CONSUMED
    assert (
        tuple(event.kind for event in engine.events).count(
            ApprovalAuditEventKind.PLAN_APPROVAL_CONSUMED
        )
        == 1
    )


def test_approval_record_and_review_are_strict_frozen_and_hash_bound() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    engine = make_engine(catalog, MutableClock())
    review = engine.prepare_review(plan, decision)
    record = engine.commit(review.review_id, plan, decision)

    with pytest.raises(ValidationError):
        assign_attribute(record, "approver", "other")
    payload = record.model_dump(mode="python")
    payload["plan_hash"] = "d" * 64
    with pytest.raises(ValidationError, match="content hash"):
        ApprovalRecord.model_validate(payload)
    review_payload = review.model_dump(mode="python")
    review_payload["snapshot"]["steps"][0]["arguments"]["target"] = "other"
    with pytest.raises(ValidationError):
        type(review).model_validate(review_payload)


def test_consume_binds_one_attempt_and_replay_or_other_attempt_fails() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)
    first_attempt = uuid4()
    second_attempt = uuid4()

    before = engine.validate_for_dispatch(record.approval_id, plan, decision)
    consumed = engine.consume_for_attempt(
        record.approval_id,
        plan,
        decision,
        first_attempt,
    )
    same_attempt = engine.validate_for_attempt(
        record.approval_id,
        plan,
        decision,
        first_attempt,
    )
    replay = engine.consume_for_attempt(
        record.approval_id,
        plan,
        decision,
        first_attempt,
    )
    other_attempt = engine.consume_for_attempt(
        record.approval_id,
        plan,
        decision,
        second_attempt,
    )

    assert before.verdict is ApprovalValidationVerdict.VALID_UNCONSUMED
    assert consumed.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
    assert same_attempt.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
    assert replay.reason is ApprovalValidationReason.APPROVAL_ALREADY_CONSUMED
    assert other_attempt.reason is ApprovalValidationReason.EXECUTION_ATTEMPT_MISMATCH

    engine.close_attempt(record.approval_id, first_attempt)
    closed = engine.validate_for_attempt(
        record.approval_id,
        plan,
        decision,
        first_attempt,
    )
    assert closed.reason is ApprovalValidationReason.EXECUTION_ATTEMPT_CLOSED


def test_record_for_attempt_requires_exact_open_attempt_binding() -> None:
    """Approval evidence is readable only for its one live consumed attempt."""
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L2)
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)
    bound_attempt = uuid4()
    other_attempt = uuid4()

    with pytest.raises(ApprovalStateError, match="not bound"):
        engine.record_for_attempt(record.approval_id, bound_attempt)
    with pytest.raises(ApprovalStateError, match="unknown"):
        engine.record_for_attempt(uuid4(), bound_attempt)

    consumed = engine.consume_for_attempt(
        record.approval_id,
        plan,
        decision,
        bound_attempt,
    )
    assert consumed.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
    assert engine.record_for_attempt(record.approval_id, bound_attempt) == record

    with pytest.raises(ApprovalStateError, match="not bound"):
        engine.record_for_attempt(record.approval_id, other_attempt)

    engine.close_attempt(record.approval_id, bound_attempt)
    with pytest.raises(ApprovalStateError, match="already closed"):
        engine.record_for_attempt(record.approval_id, bound_attempt)


def test_forged_or_cross_engine_approval_id_is_never_authoritative() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    clock = MutableClock()
    issuing_engine = make_engine(catalog, clock)
    record = issue_approval(issuing_engine, plan, decision)
    other_engine = make_engine(catalog, clock)

    forged = issuing_engine.validate_for_dispatch(uuid4(), plan, decision)
    copied = other_engine.validate_for_dispatch(record.approval_id, plan, decision)

    assert forged.reason is ApprovalValidationReason.UNKNOWN_APPROVAL
    assert copied.reason is ApprovalValidationReason.UNKNOWN_APPROVAL


@pytest.mark.parametrize(
    "mutation",
    [
        "task_id",
        "plan_id",
        "target",
        "step_order",
        "role",
        "tool_id",
        "tool_version",
        "contract_hash",
        "implementation_hash",
        "arguments",
        "reason",
        "impact",
        "verification",
        "verification_criteria",
        "recovery",
        "policy_hash",
    ],
)
def test_any_plan_or_policy_drift_invalidates_approval(mutation: str) -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)
    changed_plan = plan
    changed_decision = decision
    if mutation == "task_id":
        changed_plan = plan.model_copy(update={"task_id": uuid4()})
    elif mutation == "plan_id":
        changed_plan = plan.model_copy(update={"plan_id": uuid4()})
    elif mutation == "target":
        changed_decision = decision.model_copy(
            update={
                "target": TargetReference(
                    target_id="other",
                    resource_type="local_system",
                    resource_id="other",
                )
            }
        )
    elif mutation == "step_order":
        second = plan.steps[0].model_copy(update={"step_id": "second"})
        changed_plan = plan.model_copy(update={"steps": (second, *plan.steps)})
    elif mutation == "role":
        step = plan.steps[0].model_copy(update={"role": StepRole.ACTION})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    elif mutation == "tool_id":
        step = plan.steps[0].model_copy(update={"tool_id": "other_tool"})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    elif mutation == "tool_version":
        step = plan.steps[0].model_copy(update={"tool_version": "2.0.0"})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    elif mutation == "arguments":
        arguments = plan.steps[0].arguments.model_copy(update={"target": "other"})
        step = plan.steps[0].model_copy(update={"arguments": arguments})
        changed_plan = plan.model_copy(update={"target": "other", "steps": (step,)})
    elif mutation == "contract_hash":
        step = plan.steps[0].model_copy(update={"contract_hash": "d" * 64})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    elif mutation == "implementation_hash":
        step = plan.steps[0].model_copy(update={"implementation_hash": "d" * 64})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    elif mutation == "reason":
        step = plan.steps[0].model_copy(update={"reason": "Changed reason."})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    elif mutation == "impact":
        step = plan.steps[0].model_copy(update={"impact": "Changed impact."})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    elif mutation == "verification":
        step = plan.steps[0].model_copy(update={"verification": "Changed criterion."})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    elif mutation == "verification_criteria":
        criterion = plan.verification_criteria[0].model_copy(update={"expected": "other"})
        changed_plan = plan.model_copy(update={"verification_criteria": (criterion,)})
    elif mutation == "recovery":
        step = plan.steps[0].model_copy(update={"recovery": "Changed recovery."})
        changed_plan = plan.model_copy(update={"steps": (step,)})
    else:
        changed_decision = decision.model_copy(update={"policy_hash": "d" * 64})

    result = engine.validate_for_dispatch(
        record.approval_id,
        changed_plan,
        changed_decision,
    )

    assert result.verdict is ApprovalValidationVerdict.INVALID
    assert engine.events[-1].kind is ApprovalAuditEventKind.PLAN_APPROVAL_INVALIDATED


def test_reordering_an_otherwise_consistent_multistep_plan_invalidates_approval() -> None:
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L2)
    second_step = plan.steps[0].model_copy(update={"step_id": "second-action"})
    plan = plan.model_copy(update={"steps": (*plan.steps, second_step)})
    second_decision = decision.step_decisions[0].model_copy(update={"step_id": second_step.step_id})
    decision = decision.model_copy(
        update={"step_decisions": (*decision.step_decisions, second_decision)}
    )
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)
    reordered_plan = plan.model_copy(update={"steps": tuple(reversed(plan.steps))})
    reordered_decision = decision.model_copy(
        update={"step_decisions": tuple(reversed(decision.step_decisions))}
    )
    reordered_review = engine.prepare_review(reordered_plan, reordered_decision)

    result = engine.validate_for_dispatch(
        record.approval_id,
        reordered_plan,
        reordered_decision,
    )

    assert reordered_review.plan_hash != record.plan_hash
    assert result.verdict is ApprovalValidationVerdict.INVALID
    assert result.reason is ApprovalValidationReason.POLICY_MISMATCH
    assert engine.events[-1].kind is ApprovalAuditEventKind.PLAN_APPROVAL_INVALIDATED


def test_reject_and_expiry_are_terminal_and_boundary_is_inclusive() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    clock = MutableClock()
    engine = make_engine(catalog, clock)
    review = engine.prepare_review(plan, decision)
    rejected = engine.reject(review.review_id, plan, decision)

    assert rejected.kind is ApprovalAuditEventKind.PLAN_APPROVAL_REJECTED
    with pytest.raises(ApprovalStateError):
        engine.commit(review.review_id, plan, decision)

    second = engine.prepare_review(plan, decision)
    record = engine.commit(second.review_id, plan, decision)
    clock.advance(300)
    result = engine.validate_for_dispatch(record.approval_id, plan, decision)

    assert result.reason is ApprovalValidationReason.APPROVAL_EXPIRED
    assert engine.events[-1].kind is ApprovalAuditEventKind.PLAN_APPROVAL_EXPIRED


def test_review_commit_expiry_boundary_is_inclusive() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    clock = MutableClock()
    engine = make_engine(catalog, clock)
    review = engine.prepare_review(plan, decision)

    clock.advance(300)

    with pytest.raises(ApprovalStateError, match="expired"):
        engine.commit(review.review_id, plan, decision)
    assert all(
        event.kind is not ApprovalAuditEventKind.PLAN_APPROVAL_ISSUED for event in engine.events
    )


def test_other_operator_and_declared_input_redaction_fail_before_review() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    engine = make_engine(catalog, MutableClock())
    other_operator = decision.model_copy(update={"operator_id": "other-user"})

    with pytest.raises(ApprovalReviewError):
        engine.prepare_review(plan, other_operator)
    assert engine.events == ()

    metadata = next(iter(catalog.values()))
    redacted_metadata = metadata.model_copy(
        update={"redaction": metadata.redaction.model_copy(update={"input_fields": ("target",)})}
    )
    redacted_catalog = dict(catalog)
    redacted_catalog[(metadata.tool_id, metadata.version)] = redacted_metadata
    redacted_engine = make_engine(redacted_catalog, MutableClock())

    with pytest.raises(ApprovalReviewError):
        redacted_engine.prepare_review(plan, decision)
    assert redacted_engine.events == ()


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "password=hunter2",
        "client_secret=abc123",
        "auth_token: abc123",
        "passphrase=hunter2",
        "authorization: Bearer_abc",
        "access_token=abc123",
        "refresh_token=abc123",
        "AWS_SECRET_ACCESS_KEY=abc123",
        "GITHUB_TOKEN=abc123",
        "database_url=postgres://user:pw@host/db",
        "endpoint=postgres://user:pw@host/db",
        '{"password":"hunter2"}',
        '{"access_token":"abc123"}',
        "use postgres://user:pw@host/db now",
        "-----BEGIN PRIVATE KEY-----SENSITIVE-----END PRIVATE KEY-----",
        "terminal\u001bcontrol",
    ],
)
def test_unsafe_human_review_text_fails_closed(unsafe_text: str) -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    step = plan.steps[0].model_copy(update={"reason": unsafe_text})
    changed_plan = plan.model_copy(update={"steps": (step,)})
    engine = make_engine(catalog, MutableClock())

    with pytest.raises(ApprovalReviewError) as caught:
        engine.prepare_review(changed_plan, decision)

    assert unsafe_text not in str(caught.value)
    assert engine.events == ()


def test_explicit_invalidation_is_irreversible() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)

    event = engine.invalidate(
        record.approval_id,
        ApprovalInvalidationReason.REVOKED_BY_APPROVER,
    )
    result = engine.validate_for_dispatch(record.approval_id, plan, decision)

    assert event.kind is ApprovalAuditEventKind.PLAN_APPROVAL_INVALIDATED
    assert event.actor == "local-owner"
    assert result.reason is ApprovalValidationReason.APPROVAL_INVALIDATED
    with pytest.raises(ApprovalStateError):
        engine.invalidate(
            record.approval_id,
            ApprovalInvalidationReason.REVOKED_BY_APPROVER,
        )


def test_concurrent_consumers_allow_exactly_one_attempt() -> None:
    _, catalog, plan, decision = build_plan_and_decision()
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)
    attempts = (uuid4(), uuid4())

    def consume(attempt: UUID) -> ApprovalValidationVerdict:
        return engine.consume_for_attempt(
            record.approval_id,
            plan,
            decision,
            attempt,
        ).verdict

    with ThreadPoolExecutor(max_workers=2) as pool:
        verdicts = tuple(pool.map(consume, attempts))

    assert verdicts.count(ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT) == 1
    assert verdicts.count(ApprovalValidationVerdict.INVALID) == 1
    assert (
        sum(event.kind is ApprovalAuditEventKind.PLAN_APPROVAL_CONSUMED for event in engine.events)
        == 1
    )


def test_l3_confirmation_is_exact_short_lived_and_single_use() -> None:
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L3)
    clock = MutableClock()
    engine = make_engine(catalog, clock)
    record = issue_approval(engine, plan, decision)
    attempt_id = uuid4()
    invocation_id = uuid4()
    engine.consume_for_attempt(record.approval_id, plan, decision, attempt_id)

    confirmation = engine.issue_l3_confirmation(
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=invocation_id,
        step_id=plan.steps[0].step_id,
    )
    consumed = engine.consume_l3_confirmation(
        confirmation.confirmation_id,
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=invocation_id,
        step_id=plan.steps[0].step_id,
    )
    replay = engine.consume_l3_confirmation(
        confirmation.confirmation_id,
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=invocation_id,
        step_id=plan.steps[0].step_id,
    )

    assert confirmation.expires_at - confirmation.issued_at == timedelta(seconds=30)
    assert consumed.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
    assert replay.reason is ApprovalValidationReason.CONFIRMATION_ALREADY_CONSUMED
    assert '"arguments":' not in confirmation.model_dump_json()


def test_l3_without_confirmation_fails_closed() -> None:
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L3)
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)
    attempt_id = uuid4()
    invocation_id = uuid4()
    engine.consume_for_attempt(record.approval_id, plan, decision, attempt_id)

    missing = engine.consume_l3_confirmation(
        None,
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=invocation_id,
        step_id=plan.steps[0].step_id,
    )

    assert missing.reason is ApprovalValidationReason.CONFIRMATION_MISSING
    assert all(event.invocation_id != invocation_id for event in engine.events)


def test_multiple_l3_steps_require_independent_confirmations() -> None:
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L3)
    second_step = plan.steps[0].model_copy(update={"step_id": "second-action"})
    plan = plan.model_copy(update={"steps": (*plan.steps, second_step)})
    second_decision = decision.step_decisions[0].model_copy(update={"step_id": second_step.step_id})
    decision = decision.model_copy(
        update={"step_decisions": (*decision.step_decisions, second_decision)}
    )
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)
    attempt_id = uuid4()
    first_invocation = uuid4()
    second_invocation = uuid4()
    engine.consume_for_attempt(record.approval_id, plan, decision, attempt_id)
    first = engine.issue_l3_confirmation(
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=first_invocation,
        step_id=plan.steps[0].step_id,
    )
    second = engine.issue_l3_confirmation(
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=second_invocation,
        step_id=plan.steps[1].step_id,
    )

    crossed = engine.consume_l3_confirmation(
        first.confirmation_id,
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=second_invocation,
        step_id=plan.steps[1].step_id,
    )
    second_consumed = engine.consume_l3_confirmation(
        second.confirmation_id,
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=second_invocation,
        step_id=plan.steps[1].step_id,
    )

    assert crossed.reason is ApprovalValidationReason.CONFIRMATION_MISMATCH
    assert second_consumed.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
    assert engine.events[-1].step_id == plan.steps[1].step_id


def test_l3_confirmation_expiry_wrong_invocation_and_missing_base_fail_closed() -> None:
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L3)
    clock = MutableClock()
    engine = make_engine(catalog, clock)
    record = issue_approval(engine, plan, decision)
    attempt_id = uuid4()

    with pytest.raises(ApprovalStateError):
        engine.issue_l3_confirmation(
            record.approval_id,
            plan,
            decision,
            execution_attempt_id=attempt_id,
            invocation_id=uuid4(),
            step_id=plan.steps[0].step_id,
        )

    engine.consume_for_attempt(record.approval_id, plan, decision, attempt_id)
    invocation_id = uuid4()
    confirmation = engine.issue_l3_confirmation(
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=invocation_id,
        step_id=plan.steps[0].step_id,
    )
    wrong = engine.consume_l3_confirmation(
        confirmation.confirmation_id,
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=uuid4(),
        step_id=plan.steps[0].step_id,
    )

    assert wrong.reason is ApprovalValidationReason.CONFIRMATION_MISMATCH

    second_engine = make_engine(catalog, clock)
    second_record = issue_approval(second_engine, plan, decision)
    second_attempt = uuid4()
    second_engine.consume_for_attempt(
        second_record.approval_id,
        plan,
        decision,
        second_attempt,
    )
    second_confirmation = second_engine.issue_l3_confirmation(
        second_record.approval_id,
        plan,
        decision,
        execution_attempt_id=second_attempt,
        invocation_id=uuid4(),
        step_id=plan.steps[0].step_id,
    )
    clock.advance(30)
    expired = second_engine.consume_l3_confirmation(
        second_confirmation.confirmation_id,
        second_record.approval_id,
        plan,
        decision,
        execution_attempt_id=second_attempt,
        invocation_id=second_confirmation.invocation_id,
        step_id=plan.steps[0].step_id,
    )
    assert expired.reason is ApprovalValidationReason.CONFIRMATION_EXPIRED


def test_invalid_clock_and_id_factory_fail_closed_without_authorization() -> None:
    _, catalog, plan, decision = build_plan_and_decision()

    def broken_clock() -> datetime:
        raise SystemExit("SENSITIVE_CLOCK")

    engine = ApprovalEngine(
        catalog,
        ApprovalConstraints(
            review_session_ttl_seconds=300,
            plan_approval_ttl_seconds=300,
            l3_confirmation_ttl_seconds=30,
        ),
        clock=broken_clock,
    )
    with pytest.raises(ApprovalConfigurationError, match="clock"):
        engine.prepare_review(plan, decision)
    assert engine.events == ()

    fixed = MutableClock()
    duplicate = uuid4()
    duplicate_engine = ApprovalEngine(
        catalog,
        ApprovalConstraints(
            review_session_ttl_seconds=300,
            plan_approval_ttl_seconds=300,
            l3_confirmation_ttl_seconds=30,
        ),
        clock=fixed,
        id_factory=lambda: duplicate,
    )
    with pytest.raises(ApprovalConfigurationError, match="factory"):
        duplicate_engine.prepare_review(plan, decision)
    assert duplicate_engine.events == ()


def test_hostile_plan_serialization_fails_closed_without_reflecting_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_SERIALIZATION_MARKER"
    _, catalog, plan, decision = build_plan_and_decision()
    engine = make_engine(catalog, MutableClock())

    def explode(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(ExecutionPlan, "model_dump", explode)

    with pytest.raises(ApprovalReviewError) as caught:
        engine.prepare_review(plan, decision)

    assert marker not in str(caught.value)
    assert engine.events == ()


def test_ineligible_policy_decisions_never_create_review() -> None:
    registry = build_default_registry()
    catalog = dict(registry.metadata_snapshot())
    metadata = next(iter(catalog.values()))
    task = Task(request=SUPPORTED_REQUEST)
    plan = Planner().create_plan(
        RuntimeContext(
            task_id=task.task_id,
            request=task.request,
            user=task.user,
            target=task.target,
        ),
        metadata,
    )
    automatic = PolicyEngine(registry).evaluate(
        plan,
        PolicyEvaluationContext(
            operator_id=task.user,
            target=TargetReference(
                target_id=task.target,
                resource_type="local_system",
                resource_id=task.target,
            ),
        ),
    )
    engine = make_engine(catalog, MutableClock())

    with pytest.raises(ApprovalReviewError):
        engine.prepare_review(plan, automatic)
    assert engine.events == ()


def test_confirmation_content_hash_is_validated() -> None:
    _, catalog, plan, decision = build_plan_and_decision(risk=RiskLevel.L3)
    engine = make_engine(catalog, MutableClock())
    record = issue_approval(engine, plan, decision)
    attempt_id = uuid4()
    engine.consume_for_attempt(record.approval_id, plan, decision, attempt_id)
    confirmation = engine.issue_l3_confirmation(
        record.approval_id,
        plan,
        decision,
        execution_attempt_id=attempt_id,
        invocation_id=uuid4(),
        step_id=plan.steps[0].step_id,
    )
    payload = confirmation.model_dump(mode="python")
    payload["arguments_hash"] = "d" * 64

    with pytest.raises(ValidationError, match="content hash"):
        ManualConfirmationRecord.model_validate(payload)
