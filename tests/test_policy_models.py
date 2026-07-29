from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_server.models.policy import (
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
    PolicyCapabilityRule,
    PolicyDecision,
    PolicyEffect,
    PolicyEvaluationContext,
    PolicyProfile,
    PolicyReasonCode,
    PolicyReviewRecord,
    StepPolicyDecision,
)
from ai_server.models.tool import RiskLevel, TargetReference

CONTRACT_HASH = "a" * 64
IMPLEMENTATION_HASH = "b" * 64
ARGUMENTS_HASH = "c" * 64
PROFILE_HASH = "d" * 64
TARGET = TargetReference(
    target_id="local-mock",
    resource_type="local_system",
    resource_id="local-mock",
)


def assign_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


def make_rule(
    *,
    rule_id: str = "status-rule",
    minimum_approval: PolicyApprovalRequirement = PolicyApprovalRequirement.NOT_REQUIRED,
) -> PolicyCapabilityRule:
    return PolicyCapabilityRule(
        rule_id=rule_id,
        operator_id="local-user",
        target=TARGET,
        tool_id="get_system_status",
        tool_version="1.0.0",
        contract_hash=CONTRACT_HASH,
        implementation_hash=IMPLEMENTATION_HASH,
        minimum_approval=minimum_approval,
    )


def make_step_decision(
    *,
    step_id: str = "status",
    effect: PolicyEffect = PolicyEffect.ALLOW,
    reason_code: PolicyReasonCode = PolicyReasonCode.ALLOWED,
    resolved_risk: RiskLevel | None = RiskLevel.L0,
    approval: PolicyApprovalRequirement | None = PolicyApprovalRequirement.NOT_REQUIRED,
    confirmation: ManualConfirmationRequirement | None = (
        ManualConfirmationRequirement.NOT_REQUIRED
    ),
) -> StepPolicyDecision:
    return StepPolicyDecision(
        step_id=step_id,
        tool_id="get_system_status",
        tool_version="1.0.0",
        contract_hash=CONTRACT_HASH,
        implementation_hash=IMPLEMENTATION_HASH,
        arguments_hash=ARGUMENTS_HASH,
        resolved_risk=resolved_risk,
        effect=effect,
        reason_code=reason_code,
        approval_requirement=approval,
        manual_confirmation_requirement=confirmation,
    )


def test_policy_context_profile_and_review_record_round_trip() -> None:
    context = PolicyEvaluationContext(operator_id="local-user", target=TARGET)
    profile = PolicyProfile(
        policy_id="local-default",
        version="1.0.0",
        rules=(make_rule(),),
    )
    reviewed_at = datetime(2026, 7, 29, tzinfo=UTC)
    review = PolicyReviewRecord(
        policy_id=profile.policy_id,
        version=profile.version,
        content_hash=PROFILE_HASH,
        status="active",
        reviewer="local-owner",
        reviewed_at=reviewed_at,
        activated_at=reviewed_at,
    )

    assert PolicyEvaluationContext.model_validate_json(context.model_dump_json()) == context
    assert PolicyProfile.model_validate_json(profile.model_dump_json()) == profile
    assert PolicyReviewRecord.model_validate_json(review.model_dump_json()) == review


@pytest.mark.parametrize(
    "model",
    [
        PolicyEvaluationContext(operator_id="local-user", target=TARGET),
        make_rule(),
        PolicyProfile(
            policy_id="local-default",
            version="1.0.0",
            rules=(make_rule(),),
        ),
        make_step_decision(),
    ],
)
def test_policy_models_are_frozen(model: object) -> None:
    with pytest.raises(ValidationError):
        assign_attribute(model, "unexpected", True)


def test_policy_models_reject_extra_and_non_strict_python_values() -> None:
    with pytest.raises(ValidationError):
        PolicyEvaluationContext.model_validate(
            {
                "operator_id": "local-user",
                "target": TARGET,
                "unexpected": True,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        PolicyProfile.model_validate(
            {
                "policy_id": "local-default",
                "version": "1.0.0",
                "rules": [make_rule()],
            },
            strict=True,
        )


@pytest.mark.parametrize(
    "rules",
    [
        (make_rule(), make_rule()),
        (make_rule(rule_id="one"), make_rule(rule_id="two")),
    ],
    ids=["duplicate-id", "duplicate-capability"],
)
def test_policy_profile_rejects_duplicate_rules(
    rules: tuple[PolicyCapabilityRule, ...],
) -> None:
    with pytest.raises(ValidationError):
        PolicyProfile(
            policy_id="local-default",
            version="1.0.0",
            rules=rules,
        )


def test_policy_review_requires_ordered_utc_timestamps() -> None:
    utc_time = datetime(2026, 7, 29, tzinfo=UTC)
    non_utc = utc_time.astimezone(timezone(timedelta(hours=8)))

    with pytest.raises(ValidationError):
        PolicyReviewRecord(
            policy_id="local-default",
            version="1.0.0",
            content_hash=PROFILE_HASH,
            status="active",
            reviewer="local-owner",
            reviewed_at=non_utc,
            activated_at=non_utc,
        )
    with pytest.raises(ValidationError):
        PolicyReviewRecord(
            policy_id="local-default",
            version="1.0.0",
            content_hash=PROFILE_HASH,
            status="active",
            reviewer="local-owner",
            reviewed_at=utc_time,
            activated_at=utc_time - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError):
        PolicyReviewRecord(
            policy_id="local-default",
            version="1.0.0",
            content_hash=PROFILE_HASH,
            status="active",
            reviewer="model",  # type: ignore[arg-type]
            reviewed_at=utc_time,
            activated_at=utc_time,
        )


def test_unknown_tool_decision_requires_all_resolution_fields_to_be_absent() -> None:
    decision = make_step_decision(
        effect=PolicyEffect.DENY,
        reason_code=PolicyReasonCode.UNKNOWN_TOOL,
        resolved_risk=None,
        approval=None,
        confirmation=None,
    )

    assert decision.resolved_risk is None
    with pytest.raises(ValidationError):
        make_step_decision(
            effect=PolicyEffect.DENY,
            reason_code=PolicyReasonCode.UNKNOWN_TOOL,
            resolved_risk=RiskLevel.L0,
        )
    with pytest.raises(ValidationError):
        make_step_decision(
            effect=PolicyEffect.DENY,
            reason_code=PolicyReasonCode.UNKNOWN_TOOL,
            resolved_risk=None,
        )
    with pytest.raises(ValidationError):
        make_step_decision(
            effect=PolicyEffect.DENY,
            reason_code=PolicyReasonCode.TOOL_NOT_ALLOWED,
            resolved_risk=None,
            approval=None,
            confirmation=None,
        )


def test_risk_specific_denial_reasons_require_matching_resolved_risk() -> None:
    l1_denial = make_step_decision(
        effect=PolicyEffect.DENY,
        reason_code=PolicyReasonCode.L1_RULE_MISSING,
        resolved_risk=RiskLevel.L1,
    )

    assert l1_denial.resolved_risk is RiskLevel.L1
    with pytest.raises(ValidationError):
        make_step_decision(
            effect=PolicyEffect.DENY,
            reason_code=PolicyReasonCode.L1_RULE_MISSING,
            resolved_risk=RiskLevel.L0,
        )
    with pytest.raises(ValidationError):
        make_step_decision(
            effect=PolicyEffect.DENY,
            reason_code=PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE,
            resolved_risk=RiskLevel.L1,
        )


def test_l3_confirmation_requires_human_approval_and_l3_risk() -> None:
    denied = make_step_decision(
        effect=PolicyEffect.DENY,
        reason_code=PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE,
        resolved_risk=RiskLevel.L3,
        approval=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
        confirmation=ManualConfirmationRequirement.PER_INVOCATION,
    )

    assert denied.effect is PolicyEffect.DENY
    with pytest.raises(ValidationError):
        make_step_decision(
            resolved_risk=RiskLevel.L1,
            approval=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
            confirmation=ManualConfirmationRequirement.PER_INVOCATION,
        )
    with pytest.raises(ValidationError):
        make_step_decision(
            resolved_risk=RiskLevel.L3,
            approval=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
            confirmation=ManualConfirmationRequirement.NOT_REQUIRED,
        )
    with pytest.raises(ValidationError):
        make_step_decision(
            resolved_risk=RiskLevel.L2,
            approval=PolicyApprovalRequirement.NOT_REQUIRED,
            confirmation=ManualConfirmationRequirement.NOT_REQUIRED,
        )


def test_policy_decision_requires_aggregate_effect_to_match_steps() -> None:
    allowed = make_step_decision()
    task_id = uuid4()
    plan_id = uuid4()
    decision = PolicyDecision(
        policy_id="local-default",
        policy_version="1.0.0",
        policy_hash=PROFILE_HASH,
        task_id=task_id,
        plan_id=plan_id,
        operator_id="local-user",
        target=TARGET,
        effect=PolicyEffect.ALLOW,
        reason_code=PolicyReasonCode.ALLOWED,
        effective_risk=RiskLevel.L0,
        approval_requirement=PolicyApprovalRequirement.NOT_REQUIRED,
        manual_confirmation_requirement=ManualConfirmationRequirement.NOT_REQUIRED,
        step_decisions=(allowed,),
    )

    assert decision.step_decisions == (allowed,)
    with pytest.raises(ValidationError):
        PolicyDecision(
            policy_id="local-default",
            policy_version="1.0.0",
            policy_hash=PROFILE_HASH,
            task_id=task_id,
            plan_id=plan_id,
            operator_id="local-user",
            target=TARGET,
            effect=PolicyEffect.DENY,
            reason_code=PolicyReasonCode.TOOL_NOT_ALLOWED,
            effective_risk=RiskLevel.L0,
            approval_requirement=PolicyApprovalRequirement.NOT_REQUIRED,
            manual_confirmation_requirement=(ManualConfirmationRequirement.NOT_REQUIRED),
            step_decisions=(allowed,),
        )


def test_policy_decision_rejects_duplicate_step_ids() -> None:
    denied = make_step_decision(
        effect=PolicyEffect.DENY,
        reason_code=PolicyReasonCode.TOOL_NOT_ALLOWED,
    )

    with pytest.raises(ValidationError):
        PolicyDecision(
            policy_id="local-default",
            policy_version="1.0.0",
            policy_hash=PROFILE_HASH,
            task_id=uuid4(),
            plan_id=uuid4(),
            operator_id="local-user",
            target=TARGET,
            effect=PolicyEffect.DENY,
            reason_code=PolicyReasonCode.TOOL_NOT_ALLOWED,
            effective_risk=RiskLevel.L0,
            approval_requirement=PolicyApprovalRequirement.NOT_REQUIRED,
            manual_confirmation_requirement=(ManualConfirmationRequirement.NOT_REQUIRED),
            step_decisions=(denied, denied),
        )


def test_denied_policy_reason_matches_first_denied_step() -> None:
    denied = make_step_decision(
        effect=PolicyEffect.DENY,
        reason_code=PolicyReasonCode.TOOL_NOT_ALLOWED,
    )

    with pytest.raises(ValidationError):
        PolicyDecision(
            policy_id="local-default",
            policy_version="1.0.0",
            policy_hash=PROFILE_HASH,
            task_id=uuid4(),
            plan_id=uuid4(),
            operator_id="local-user",
            target=TARGET,
            effect=PolicyEffect.DENY,
            reason_code=PolicyReasonCode.TARGET_NOT_ALLOWED,
            effective_risk=RiskLevel.L0,
            approval_requirement=PolicyApprovalRequirement.NOT_REQUIRED,
            manual_confirmation_requirement=(ManualConfirmationRequirement.NOT_REQUIRED),
            step_decisions=(denied,),
        )


def test_policy_decision_uses_explicit_step_maxima() -> None:
    low = make_step_decision(step_id="observe")
    high = make_step_decision(
        step_id="change",
        resolved_risk=RiskLevel.L2,
        approval=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
    )

    decision = PolicyDecision(
        policy_id="local-default",
        policy_version="1.0.0",
        policy_hash=PROFILE_HASH,
        task_id=uuid4(),
        plan_id=uuid4(),
        operator_id="local-user",
        target=TARGET,
        effect=PolicyEffect.ALLOW,
        reason_code=PolicyReasonCode.ALLOWED,
        effective_risk=RiskLevel.L2,
        approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
        manual_confirmation_requirement=ManualConfirmationRequirement.NOT_REQUIRED,
        step_decisions=(low, high),
    )

    assert decision.effective_risk is RiskLevel.L2
    with pytest.raises(ValidationError):
        PolicyDecision(
            policy_id="local-default",
            policy_version="1.0.0",
            policy_hash=PROFILE_HASH,
            task_id=decision.task_id,
            plan_id=decision.plan_id,
            operator_id="local-user",
            target=TARGET,
            effect=PolicyEffect.ALLOW,
            reason_code=PolicyReasonCode.ALLOWED,
            effective_risk=RiskLevel.L0,
            approval_requirement=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
            manual_confirmation_requirement=(ManualConfirmationRequirement.NOT_REQUIRED),
            step_decisions=(low, high),
        )


def test_fully_unresolved_plan_requires_absent_aggregate_fields() -> None:
    unknown = make_step_decision(
        effect=PolicyEffect.DENY,
        reason_code=PolicyReasonCode.UNKNOWN_TOOL,
        resolved_risk=None,
        approval=None,
        confirmation=None,
    )
    common = {
        "policy_id": "local-default",
        "policy_version": "1.0.0",
        "policy_hash": PROFILE_HASH,
        "task_id": uuid4(),
        "plan_id": uuid4(),
        "operator_id": "local-user",
        "target": TARGET,
        "effect": PolicyEffect.DENY,
        "reason_code": PolicyReasonCode.UNKNOWN_TOOL,
        "step_decisions": (unknown,),
    }

    decision = PolicyDecision.model_validate(
        {
            **common,
            "effective_risk": None,
            "approval_requirement": None,
            "manual_confirmation_requirement": None,
        },
        strict=True,
    )

    assert decision.effective_risk is None
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(
            {
                **common,
                "effective_risk": RiskLevel.L0,
                "approval_requirement": PolicyApprovalRequirement.NOT_REQUIRED,
                "manual_confirmation_requirement": (ManualConfirmationRequirement.NOT_REQUIRED),
            },
            strict=True,
        )


def test_mixed_resolution_preserves_known_aggregate_maxima() -> None:
    known = make_step_decision(
        step_id="known",
        effect=PolicyEffect.DENY,
        reason_code=PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE,
        resolved_risk=RiskLevel.L3,
        approval=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
        confirmation=ManualConfirmationRequirement.PER_INVOCATION,
    )
    unknown = make_step_decision(
        step_id="unknown",
        effect=PolicyEffect.DENY,
        reason_code=PolicyReasonCode.UNKNOWN_TOOL,
        resolved_risk=None,
        approval=None,
        confirmation=None,
    )
    common = {
        "policy_id": "local-default",
        "policy_version": "1.0.0",
        "policy_hash": PROFILE_HASH,
        "task_id": uuid4(),
        "plan_id": uuid4(),
        "operator_id": "local-user",
        "target": TARGET,
        "effect": PolicyEffect.DENY,
        "reason_code": PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE,
        "step_decisions": (known, unknown),
    }

    decision = PolicyDecision.model_validate(
        {
            **common,
            "effective_risk": RiskLevel.L3,
            "approval_requirement": PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
            "manual_confirmation_requirement": ManualConfirmationRequirement.PER_INVOCATION,
        },
        strict=True,
    )

    assert decision.effective_risk is RiskLevel.L3
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(
            {
                **common,
                "effective_risk": None,
                "approval_requirement": None,
                "manual_confirmation_requirement": None,
            },
            strict=True,
        )
