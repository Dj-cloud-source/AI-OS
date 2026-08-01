from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest

from ai_server.approval.engine import ApprovalEngine
from ai_server.executor.errors import ExecutionAttemptError, ExecutionAuthorizationError
from ai_server.executor.service import Executor, ManualConfirmationReader
from ai_server.models.approval import (
    ApprovalAuditEventKind,
    ApprovalInvalidationReason,
    ApprovalRecord,
    ApprovalValidationReason,
    ApprovalValidationResult,
    ApprovalValidationVerdict,
    ManualConfirmationRecord,
)
from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, StepRole
from ai_server.models.executor import (
    DispatchStatus,
    ExecutionAttemptAuthorization,
    ExecutionReport,
    ExecutionReportStatus,
    ManualConfirmationChallenge,
)
from ai_server.models.policy import (
    ApprovalConstraints,
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEffect,
    PolicyEvaluationContext,
    PolicyReasonCode,
)
from ai_server.models.system_status import GetSystemStatusArguments
from ai_server.models.task import Task
from ai_server.models.tool import RiskLevel, TargetReference, ToolCall, ToolReference
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine
from ai_server.policy.errors import PolicyEvaluationError
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.gateway import GatewayDispatchReceipt, ToolGateway
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.tools.registry import ToolRegistry


class MutableUtcClock:
    """Controllable, monotonic UTC clock used only by process-local tests."""

    def __init__(self) -> None:
        self.now = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class AuthorizationHarness:
    """Synthetic L2/L3 authority around the production L0 Mock Tool only."""

    registry: ToolRegistry
    policy: PolicyEngine
    approval: ApprovalEngine
    gateway: ToolGateway
    executor: Executor
    approval_clock: MutableUtcClock
    plan: ExecutionPlan
    decision: PolicyDecision
    current_decision: list[PolicyDecision]


def _constant_executor_clock() -> int:
    return 0


def _make_plan(registry: ToolRegistry, roles: tuple[StepRole, ...]) -> ExecutionPlan:
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    task = Task(request=SUPPORTED_REQUEST)
    context = RuntimeContext(
        task_id=task.task_id,
        request=task.request,
        user=task.user,
        target=task.target,
    )
    base = Planner().create_plan(context, metadata)
    steps = tuple(
        base.steps[0].model_copy(
            update={
                "step_id": f"authorization-step-{index}",
                "role": role,
            }
        )
        for index, role in enumerate(roles)
    )
    return ExecutionPlan(
        plan_id=base.plan_id,
        task_id=base.task_id,
        target=base.target,
        steps=steps,
        verification_criteria=tuple(
            criterion.model_copy(
                update={
                    "criterion_id": f"{criterion.criterion_id}-{step_index}",
                    "evidence_step_id": step.step_id,
                }
            )
            for step_index, step in enumerate(steps)
            for criterion in base.verification_criteria
        ),
    )


def _declare_mock_verification_tool(
    policy: PolicyEngine,
    *,
    risk: RiskLevel | None = None,
) -> None:
    """Install a test-only self-reference for explicit VERIFY Step fixtures."""
    key = ("get_system_status", "1.0.0")
    metadata = policy.metadata_for(*key)
    assert metadata is not None
    verification = metadata.verification.model_copy(
        update={"tools": (ToolReference(tool_id=key[0], version=key[1]),)}
    )
    updates: dict[str, object] = {"verification": verification}
    if risk is not None:
        updates["risk_level"] = risk
    policy._metadata = MappingProxyType({key: metadata.model_copy(update=updates)})


def _synthetic_decision(
    policy: PolicyEngine,
    plan: ExecutionPlan,
    risk: RiskLevel,
) -> PolicyDecision:
    context = PolicyEvaluationContext(
        operator_id="local-user",
        target=TargetReference(
            target_id=plan.target,
            resource_type="local_system",
            resource_id=plan.target,
        ),
    )
    base = policy.evaluate(plan, context)
    confirmation = (
        ManualConfirmationRequirement.PER_INVOCATION
        if risk is RiskLevel.L3
        else ManualConfirmationRequirement.NOT_REQUIRED
    )
    step_decisions = tuple(
        step.model_copy(
            update={
                "resolved_risk": risk,
                "effect": PolicyEffect.ALLOW,
                "reason_code": PolicyReasonCode.ALLOWED,
                "approval_requirement": PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
                "manual_confirmation_requirement": confirmation,
            }
        )
        for step in base.step_decisions
    )
    draft = base.model_copy(
        update={
            "effective_risk": risk,
            "effect": PolicyEffect.ALLOW,
            "reason_code": PolicyReasonCode.ALLOWED,
            "approval_requirement": PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
            "manual_confirmation_requirement": confirmation,
            "step_decisions": step_decisions,
        }
    )
    return PolicyDecision.model_validate(
        draft.model_dump(mode="python", warnings="error"),
        strict=True,
    )


def _make_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    risk: RiskLevel,
    roles: tuple[StepRole, ...] = (StepRole.ACTION,),
) -> AuthorizationHarness:
    if risk not in {RiskLevel.L2, RiskLevel.L3}:
        raise AssertionError("Authorization harness supports only synthetic L2/L3 decisions")
    registry = build_default_registry()
    plan = _make_plan(registry, roles)
    policy = PolicyEngine(registry)
    if StepRole.VERIFY in roles:
        _declare_mock_verification_tool(policy)
    decision = _synthetic_decision(policy, plan, risk)
    current_decision = [decision]

    def evaluate(
        candidate_plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        del candidate_plan, context
        return current_decision[0]

    monkeypatch.setattr(policy, "evaluate", evaluate)
    catalog = dict(policy._metadata)
    key = ("get_system_status", "1.0.0")
    catalog[key] = catalog[key].model_copy(update={"risk_level": risk})
    approval_clock = MutableUtcClock()
    approval = ApprovalEngine(
        catalog,
        ApprovalConstraints(
            review_session_ttl_seconds=300,
            plan_approval_ttl_seconds=300,
            l3_confirmation_ttl_seconds=30,
        ),
        clock=approval_clock,
    )
    gateway = ToolGateway(registry)
    executor = Executor(
        gateway,
        policy,
        approval,
        clock=_constant_executor_clock,
    )
    return AuthorizationHarness(
        registry=registry,
        policy=policy,
        approval=approval,
        gateway=gateway,
        executor=executor,
        approval_clock=approval_clock,
        plan=plan,
        decision=decision,
        current_decision=current_decision,
    )


def _issue_approval(harness: AuthorizationHarness) -> ApprovalRecord:
    review = harness.approval.prepare_review(harness.plan, harness.decision)
    return harness.approval.commit(
        review.review_id,
        harness.plan,
        harness.decision,
    )


def _begin(
    harness: AuthorizationHarness,
    record: ApprovalRecord,
) -> ExecutionAttemptAuthorization:
    return harness.executor.begin_attempt(
        harness.plan,
        harness.decision,
        record.approval_id,
    )


def _record_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    harness: AuthorizationHarness,
) -> list[ToolCall[GetSystemStatusArguments]]:
    calls: list[ToolCall[GetSystemStatusArguments]] = []
    original = harness.gateway._invoke_with_receipt

    def invoke(call: ToolCall[GetSystemStatusArguments]) -> GatewayDispatchReceipt:
        calls.append(call)
        return original(call)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", invoke)
    return calls


def _confirm(challenge: ManualConfirmationChallenge) -> str:
    return f"CONFIRM {challenge.challenge_hash}"


def test_default_registry_remains_l0_only_despite_synthetic_authorization_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = build_default_registry().metadata_snapshot()
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    after = build_default_registry().metadata_snapshot()

    assert set(before) == {("get_system_status", "1.0.0")}
    assert all(metadata.risk_level is RiskLevel.L0 for metadata in before.values())
    assert all(metadata.risk_level is RiskLevel.L0 for metadata in after.values())
    assert all(
        metadata.risk_level is RiskLevel.L0
        for metadata in harness.registry.metadata_snapshot().values()
    )


def test_executor_rejects_more_than_64_steps_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L2)
    calls = _record_dispatches(monkeypatch, harness)
    template = harness.plan.steps[0]
    oversized = ExecutionPlan.model_construct(
        schema_version=harness.plan.schema_version,
        plan_id=harness.plan.plan_id,
        task_id=harness.plan.task_id,
        target=harness.plan.target,
        steps=tuple(
            template.model_copy(update={"step_id": f"oversized-step-{index}"})
            for index in range(65)
        ),
        verification_criteria=harness.plan.verification_criteria,
    )

    with pytest.raises(ExecutionAuthorizationError) as caught:
        harness.executor.begin_attempt(oversized, harness.decision, None)

    assert caught.value.reason_code == "plan_malformed"
    assert calls == []


def test_l2_exact_approval_authorizes_once_and_binds_execution_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L2)
    calls = _record_dispatches(monkeypatch, harness)
    record = _issue_approval(harness)

    authorization = _begin(harness, record)
    report = harness.executor.execute_actions(authorization)

    assert len(calls) == 1
    assert report.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert authorization.approval_id == record.approval_id
    assert authorization.approval_plan_hash == record.plan_hash
    assert authorization.approval_record_hash == record.content_hash
    assert authorization.approval_expires_at == record.expires_at
    assert report.authorization_hash == authorization.content_hash
    assert report.records[0].dispatch_status is DispatchStatus.HANDLER_DISPATCHED
    assert report.records[0].confirmation_id is None
    assert tuple(event.kind for event in harness.approval.events)[-2:] == (
        ApprovalAuditEventKind.PLAN_APPROVAL_CONSUMED,
        ApprovalAuditEventKind.ATTEMPT_CLOSED,
    )


def test_l2_missing_approval_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L2)
    calls = _record_dispatches(monkeypatch, harness)

    with pytest.raises(ExecutionAuthorizationError) as caught:
        harness.executor.begin_attempt(harness.plan, harness.decision, None)

    assert caught.value.reason_code == "approval_missing"
    assert calls == []


def test_l2_expired_approval_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L2)
    calls = _record_dispatches(monkeypatch, harness)
    record = _issue_approval(harness)
    harness.approval_clock.advance(301)

    with pytest.raises(ExecutionAuthorizationError) as caught:
        _begin(harness, record)

    assert caught.value.reason_code == ApprovalValidationReason.APPROVAL_EXPIRED.value
    assert calls == []
    assert harness.approval.events[-1].kind is ApprovalAuditEventKind.PLAN_APPROVAL_EXPIRED


def test_l2_approval_expiring_between_action_and_verification_stops_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(
        monkeypatch,
        risk=RiskLevel.L2,
        roles=(StepRole.ACTION, StepRole.VERIFY),
    )
    calls = _record_dispatches(monkeypatch, harness)
    authorization = _begin(harness, _issue_approval(harness))

    action_report = harness.executor.execute_actions(authorization)
    harness.approval_clock.advance(301)
    verification_report = harness.executor.execute_verification(authorization)

    assert action_report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
    assert verification_report.status is ExecutionReportStatus.FAILED
    assert verification_report.failure_code == ApprovalValidationReason.APPROVAL_EXPIRED.value
    assert len(calls) == 1
    assert len(verification_report.records) == 2
    assert verification_report.records[1].dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert harness.approval.events[-2].kind is ApprovalAuditEventKind.PLAN_APPROVAL_EXPIRED
    assert harness.approval.events[-1].kind is ApprovalAuditEventKind.ATTEMPT_CLOSED


def test_l2_rejected_review_never_becomes_execution_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L2)
    calls = _record_dispatches(monkeypatch, harness)
    review = harness.approval.prepare_review(harness.plan, harness.decision)
    rejection = harness.approval.reject(
        review.review_id,
        harness.plan,
        harness.decision,
    )

    with pytest.raises(ExecutionAuthorizationError) as caught:
        harness.executor.begin_attempt(
            harness.plan,
            harness.decision,
            review.review_id,
        )

    assert rejection.kind is ApprovalAuditEventKind.PLAN_APPROVAL_REJECTED
    assert caught.value.reason_code == ApprovalValidationReason.UNKNOWN_APPROVAL.value
    assert calls == []


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    [
        ("plan", ApprovalValidationReason.PLAN_HASH_MISMATCH),
        ("policy", ApprovalValidationReason.POLICY_MISMATCH),
    ],
)
def test_l2_changed_authorization_inputs_invalidate_exact_approval(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    expected_reason: ApprovalValidationReason,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L2)
    calls = _record_dispatches(monkeypatch, harness)
    record = _issue_approval(harness)
    plan = harness.plan
    decision = harness.decision
    if change == "plan":
        changed_step = plan.steps[0].model_copy(
            update={"reason": "Changed after the exact human review."}
        )
        plan = ExecutionPlan(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            target=plan.target,
            steps=(changed_step,),
            verification_criteria=plan.verification_criteria,
        )
    else:
        decision = PolicyDecision.model_validate(
            harness.decision.model_copy(update={"policy_hash": "f" * 64}).model_dump(
                mode="python", warnings="error"
            ),
            strict=True,
        )
        harness.current_decision[0] = decision

    with pytest.raises(ExecutionAuthorizationError) as caught:
        harness.executor.begin_attempt(plan, decision, record.approval_id)

    assert caught.value.reason_code == expected_reason.value
    assert calls == []


def test_l2_consumed_approval_cannot_open_a_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L2)
    calls = _record_dispatches(monkeypatch, harness)
    record = _issue_approval(harness)

    first = _begin(harness, record)
    with pytest.raises(ExecutionAuthorizationError) as caught:
        _begin(harness, record)

    assert first.approval_id == record.approval_id
    assert caught.value.reason_code == ApprovalValidationReason.EXECUTION_ATTEMPT_MISMATCH.value
    assert calls == []


def test_l3_is_rejected_for_observe_steps_before_approval_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(
        monkeypatch,
        risk=RiskLevel.L3,
        roles=(StepRole.OBSERVE,),
    )
    calls = _record_dispatches(monkeypatch, harness)

    with pytest.raises(ExecutionAuthorizationError) as caught:
        harness.executor.begin_attempt(harness.plan, harness.decision, None)

    assert caught.value.reason_code == "l3_role_invalid"
    assert calls == []
    assert harness.approval.events == ()


def test_l3_verify_is_rejected_by_policy_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_default_registry()
    policy = PolicyEngine(registry)
    _declare_mock_verification_tool(policy, risk=RiskLevel.L3)
    plan = _make_plan(registry, (StepRole.OBSERVE, StepRole.VERIFY))
    gateway = ToolGateway(registry)
    calls: list[ToolCall[GetSystemStatusArguments]] = []
    original = gateway._invoke_with_receipt

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        calls.append(call)
        return original(call)

    monkeypatch.setattr(gateway, "_invoke_with_receipt", recording_invoke)

    with pytest.raises(PolicyEvaluationError, match="read-only"):
        policy.evaluate(
            plan,
            PolicyEvaluationContext(
                operator_id="local-user",
                target=TargetReference(
                    target_id=plan.target,
                    resource_type="local_system",
                    resource_id=plan.target,
                ),
            ),
        )

    assert calls == []


def test_l3_challenge_binds_every_invocation_field_and_full_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    calls = _record_dispatches(monkeypatch, harness)
    record = _issue_approval(harness)
    authorization = _begin(harness, record)
    challenges: list[ManualConfirmationChallenge] = []

    def reader(challenge: ManualConfirmationChallenge) -> str:
        challenges.append(challenge)
        return _confirm(challenge)

    report = harness.executor.execute_actions(authorization, reader)

    assert len(calls) == len(challenges) == 1
    challenge = challenges[0]
    step = harness.plan.steps[0]
    call = calls[0]
    assert challenge.authorization_hash == authorization.content_hash
    assert challenge.approval_id == record.approval_id
    assert challenge.approval_plan_hash == record.plan_hash
    assert challenge.approval_record_hash == record.content_hash
    assert challenge.approval_expires_at == record.expires_at
    assert challenge.execution_attempt_id == authorization.execution_attempt_id
    assert challenge.invocation_id == call.invocation_id
    assert challenge.step_index == 0
    assert challenge.step_id == step.step_id
    assert challenge.role is StepRole.ACTION
    assert challenge.tool_id == step.tool_id
    assert challenge.tool_version == step.tool_version
    assert challenge.contract_hash == step.contract_hash
    assert challenge.implementation_hash == step.implementation_hash
    assert challenge.arguments_hash == canonical_json_sha256(step.arguments)
    assert challenge.target == call.target
    assert challenge.challenge_hash == canonical_json_sha256(
        challenge.model_dump(
            mode="json",
            exclude={"challenge_hash"},
            warnings="error",
        )
    )
    assert report.records[0].confirmation_id is not None
    assert report.records[0].confirmation_record_hash is not None


def test_l3_confirmation_reader_cannot_reenter_or_abort_active_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(
        monkeypatch,
        risk=RiskLevel.L3,
    )
    calls = _record_dispatches(monkeypatch, harness)
    authorization = _begin(harness, _issue_approval(harness))
    reentry_failures = 0

    def reentrant_reader(challenge: ManualConfirmationChallenge) -> str:
        nonlocal reentry_failures
        with pytest.raises(ExecutionAttemptError, match="active call"):
            harness.executor.execute_actions(authorization, _confirm)
        reentry_failures += 1
        with pytest.raises(ExecutionAttemptError, match="active call"):
            harness.executor.abort_attempt(authorization)
        reentry_failures += 1
        return _confirm(challenge)

    report = harness.executor.execute_actions(authorization, reentrant_reader)

    assert reentry_failures == 2
    assert report.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert len(calls) == 1
    assert tuple(record.step_index for record in report.records) == (0,)


@pytest.mark.parametrize(
    "response_kind",
    ["missing", "truncated", "lowercase", "trailing-space", "hash-only"],
)
def test_l3_missing_or_non_exact_confirmation_causes_zero_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    response_kind: str,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    calls = _record_dispatches(monkeypatch, harness)
    authorization = _begin(harness, _issue_approval(harness))
    reader: ManualConfirmationReader | None
    if response_kind == "missing":
        reader = None
    else:

        def wrong_reader(challenge: ManualConfirmationChallenge) -> str:
            if response_kind == "truncated":
                return f"CONFIRM {challenge.challenge_hash[:-1]}"
            if response_kind == "lowercase":
                return f"confirm {challenge.challenge_hash}"
            if response_kind == "trailing-space":
                return f"CONFIRM {challenge.challenge_hash} "
            return challenge.challenge_hash

        reader = wrong_reader

    report = harness.executor.execute_actions(authorization, reader)

    expected = (
        "l3_confirmation_unavailable" if response_kind == "missing" else "l3_confirmation_rejected"
    )
    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == expected
    assert report.records[0].dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert report.records[0].confirmation_id is None
    assert calls == []


def test_each_l3_step_requires_a_distinct_current_invocation_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(
        monkeypatch,
        risk=RiskLevel.L3,
        roles=(StepRole.ACTION, StepRole.ACTION),
    )
    calls = _record_dispatches(monkeypatch, harness)
    authorization = _begin(harness, _issue_approval(harness))
    challenges: list[ManualConfirmationChallenge] = []

    def reader(challenge: ManualConfirmationChallenge) -> str:
        challenges.append(challenge)
        return _confirm(challenge)

    report = harness.executor.execute_actions(authorization, reader)

    assert report.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert len(calls) == len(challenges) == len(report.records) == 2
    assert tuple(challenge.step_index for challenge in challenges) == (0, 1)
    assert len({challenge.invocation_id for challenge in challenges}) == 2
    assert len({challenge.challenge_hash for challenge in challenges}) == 2
    assert tuple(challenge.invocation_id for challenge in challenges) == tuple(
        call.invocation_id for call in calls
    )
    assert all(record.confirmation_id is not None for record in report.records)
    assert (
        tuple(event.kind for event in harness.approval.events).count(
            ApprovalAuditEventKind.L3_CONFIRMATION_ISSUED
        )
        == 2
    )
    assert (
        tuple(event.kind for event in harness.approval.events).count(
            ApprovalAuditEventKind.L3_CONFIRMATION_CONSUMED
        )
        == 2
    )


def test_l3_confirmation_expiration_after_issue_prevents_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    calls = _record_dispatches(monkeypatch, harness)
    authorization = _begin(harness, _issue_approval(harness))
    original = harness.approval.issue_l3_confirmation

    def issue_then_expire(
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        *,
        execution_attempt_id: UUID,
        invocation_id: UUID,
        step_id: str,
    ) -> ManualConfirmationRecord:
        confirmation = original(
            approval_id,
            plan,
            decision,
            execution_attempt_id=execution_attempt_id,
            invocation_id=invocation_id,
            step_id=step_id,
        )
        harness.approval_clock.advance(31)
        return confirmation

    monkeypatch.setattr(harness.approval, "issue_l3_confirmation", issue_then_expire)

    report = harness.executor.execute_actions(authorization, _confirm)

    assert report.failure_code == ApprovalValidationReason.CONFIRMATION_EXPIRED.value
    assert report.records[0].confirmation_id is None
    assert report.records[0].dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert calls == []
    assert ApprovalAuditEventKind.L3_CONFIRMATION_EXPIRED in tuple(
        event.kind for event in harness.approval.events
    )


def test_l3_confirmation_for_another_invocation_is_rejected_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    calls = _record_dispatches(monkeypatch, harness)
    authorization = _begin(harness, _issue_approval(harness))
    original = harness.approval.issue_l3_confirmation
    other_invocation_id = uuid4()

    def issue_for_other_invocation(
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        *,
        execution_attempt_id: UUID,
        invocation_id: UUID,
        step_id: str,
    ) -> ManualConfirmationRecord:
        del invocation_id
        return original(
            approval_id,
            plan,
            decision,
            execution_attempt_id=execution_attempt_id,
            invocation_id=other_invocation_id,
            step_id=step_id,
        )

    monkeypatch.setattr(
        harness.approval,
        "issue_l3_confirmation",
        issue_for_other_invocation,
    )

    report = harness.executor.execute_actions(authorization, _confirm)

    assert report.failure_code == ApprovalValidationReason.CONFIRMATION_MISMATCH.value
    assert report.records[0].confirmation_id is None
    assert report.records[0].dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert calls == []
    assert ApprovalAuditEventKind.L3_CONFIRMATION_INVALIDATED in tuple(
        event.kind for event in harness.approval.events
    )


def test_l3_revalidates_current_approval_after_human_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    calls = _record_dispatches(monkeypatch, harness)
    record = _issue_approval(harness)
    authorization = _begin(harness, record)

    def invalidate_then_confirm(challenge: ManualConfirmationChallenge) -> str:
        harness.approval.invalidate(
            record.approval_id,
            ApprovalInvalidationReason.SECURITY_CONDITION_CHANGED,
        )
        return _confirm(challenge)

    report = harness.executor.execute_actions(authorization, invalidate_then_confirm)

    assert report.failure_code == "l3_confirmation_issue_failed"
    assert report.records[0].dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert calls == []


def test_l3_consumption_is_immediately_followed_by_exact_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    authorization = _begin(harness, _issue_approval(harness))
    original_consume = harness.approval.consume_l3_confirmation
    original_dispatch = harness.gateway._invoke_with_receipt
    sequence: list[tuple[str, UUID]] = []

    def consume(
        confirmation_id: UUID | None,
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        *,
        execution_attempt_id: UUID,
        invocation_id: UUID,
        step_id: str,
    ) -> ApprovalValidationResult:
        result = original_consume(
            confirmation_id,
            approval_id,
            plan,
            decision,
            execution_attempt_id=execution_attempt_id,
            invocation_id=invocation_id,
            step_id=step_id,
        )
        assert result.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
        sequence.append(("consume", invocation_id))
        return result

    def dispatch(call: ToolCall[GetSystemStatusArguments]) -> GatewayDispatchReceipt:
        assert harness.approval.events[-1].kind is ApprovalAuditEventKind.L3_CONFIRMATION_CONSUMED
        sequence.append(("dispatch", call.invocation_id))
        return original_dispatch(call)

    monkeypatch.setattr(harness.approval, "consume_l3_confirmation", consume)
    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", dispatch)

    report = harness.executor.execute_actions(authorization, _confirm)

    assert report.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert sequence == [
        ("consume", report.records[0].invocation_id),
        ("dispatch", report.records[0].invocation_id),
    ]


def test_l3_consumed_confirmation_is_burned_on_dispatch_boundary_crash_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    record = _issue_approval(harness)
    authorization = _begin(harness, record)
    challenges: list[ManualConfirmationChallenge] = []
    gateway_calls = 0

    def reader(challenge: ManualConfirmationChallenge) -> str:
        challenges.append(challenge)
        return _confirm(challenge)

    def crash(call: ToolCall[GetSystemStatusArguments]) -> GatewayDispatchReceipt:
        nonlocal gateway_calls
        gateway_calls += 1
        del call
        raise SystemExit("simulated process-boundary crash")

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", crash)

    report = harness.executor.execute_actions(authorization, reader)

    assert gateway_calls == 1
    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == "gateway_dispatch_unknown"
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert ApprovalAuditEventKind.L3_CONFIRMATION_CONSUMED in tuple(
        event.kind for event in harness.approval.events
    )
    assert harness.approval.events[-1].kind is ApprovalAuditEventKind.ATTEMPT_CLOSED
    with pytest.raises(ExecutionAttemptError, match="closed"):
        harness.executor.execute_actions(authorization, reader)
    assert gateway_calls == 1

    challenge = challenges[0]
    confirmation_id = report.records[0].confirmation_id
    replay = harness.approval.consume_l3_confirmation(
        confirmation_id,
        record.approval_id,
        harness.plan,
        harness.decision,
        execution_attempt_id=authorization.execution_attempt_id,
        invocation_id=challenge.invocation_id,
        step_id=challenge.step_id,
    )
    assert replay.reason is ApprovalValidationReason.CONFIRMATION_ALREADY_CONSUMED


def test_concurrent_l3_attempt_resume_dispatches_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _make_harness(monkeypatch, risk=RiskLevel.L3)
    authorization = _begin(harness, _issue_approval(harness))
    original = harness.gateway._invoke_with_receipt
    counter_lock = Lock()
    gateway_calls = 0
    reader_calls = 0

    def reader(challenge: ManualConfirmationChallenge) -> str:
        nonlocal reader_calls
        with counter_lock:
            reader_calls += 1
        return _confirm(challenge)

    def dispatch(call: ToolCall[GetSystemStatusArguments]) -> GatewayDispatchReceipt:
        nonlocal gateway_calls
        with counter_lock:
            gateway_calls += 1
        return original(call)

    def resume() -> ExecutionReport | ExecutionAttemptError:
        try:
            return harness.executor.execute_actions(authorization, reader)
        except ExecutionAttemptError as error:
            return error

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", dispatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: resume(), range(2)))

    reports = tuple(outcome for outcome in outcomes if type(outcome) is ExecutionReport)
    failures = tuple(outcome for outcome in outcomes if type(outcome) is ExecutionAttemptError)
    assert len(reports) == len(failures) == 1
    assert reports[0].status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert gateway_calls == reader_calls == 1
