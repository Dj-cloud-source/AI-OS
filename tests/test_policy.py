"""Behavior and trust-boundary tests for the deterministic Policy Engine."""

from collections.abc import Mapping
from datetime import UTC, datetime
from inspect import signature
from types import MappingProxyType
from uuid import UUID

import pytest

import ai_server.policy.engine as policy_engine_module
from ai_server.context.builder import ContextBuilder
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.policy import (
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
    PolicyCapabilityRule,
    PolicyEffect,
    PolicyEvaluationContext,
    PolicyProfile,
    PolicyReasonCode,
    PolicyReviewRecord,
)
from ai_server.models.system_status import GetSystemStatusArguments
from ai_server.models.task import Task
from ai_server.models.tool import (
    RiskLevel,
    SideEffectKind,
    TargetReference,
    ToolMetadata,
    ToolSideEffects,
    ToolTargetScope,
)
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.artifact_loader import ValidatedPolicyArtifacts
from ai_server.policy.engine import PolicyEngine
from ai_server.policy.errors import PolicyConfigurationError, PolicyEvaluationError
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.registry import ToolKey, ToolRegistry

CONTRACT_HASH = "a" * 64
IMPLEMENTATION_HASH = "b" * 64
POLICY_HASH = "c" * 64
TASK_ID = UUID("00000000-0000-4000-8000-000000000001")
PLAN_ID = UUID("00000000-0000-4000-8000-000000000002")
TARGET = TargetReference(
    target_id="local-mock",
    resource_type="local_system",
    resource_id="local-mock",
)
ALTERNATE_TARGET = TargetReference(
    target_id="local-mock",
    resource_type="alternate_system",
    resource_id="local-mock",
)


def make_metadata(
    *,
    tool_id: str = "get_system_status",
    version: str = "1.0.0",
    contract_hash: str = CONTRACT_HASH,
    implementation_hash: str = IMPLEMENTATION_HASH,
    risk_level: RiskLevel = RiskLevel.L0,
    resource_type: str = "local_system",
    maximum_targets: int = 1,
) -> ToolMetadata:
    """Build one strict synthetic Tool Metadata projection."""
    return ToolMetadata(
        tool_id=tool_id,
        version=version,
        contract_hash=contract_hash,
        implementation_hash=implementation_hash,
        description="Return deterministic simulated system status.",
        risk_level=risk_level,
        side_effects=ToolSideEffects(
            mutates_remote_state=False,
            kind=SideEffectKind.NONE,
        ),
        target_scope=ToolTargetScope(
            resource_type=resource_type,
            maximum_targets=maximum_targets,
            selector_field="target",
            allow_dynamic_expansion=False,
        ),
        timeout_ms=1000,
        idempotent=True,
        input_schema_id="urn:ai-server:tool:get-system-status:input-v1",
        output_schema_id="urn:ai-server:tool:get-system-status:output-v1",
        input_model="GetSystemStatusArguments",
        output_model="SystemStatus",
    )


def make_step(
    metadata: ToolMetadata,
    *,
    step_id: str = "get-system-status",
    reason: str = "Collect deterministic simulated status.",
) -> ExecutionStep:
    """Build a plan step bound to exact Tool identity and integrity hashes."""
    return ExecutionStep(
        step_id=step_id,
        role=StepRole.OBSERVE,
        tool_id=metadata.tool_id,
        tool_version=metadata.version,
        contract_hash=metadata.contract_hash,
        implementation_hash=metadata.implementation_hash,
        arguments=GetSystemStatusArguments(),
        reason=reason,
        impact="No external impact.",
        verification="Verify structured mock evidence.",
        recovery="No rollback is required.",
    )


def make_plan(
    *metadata: ToolMetadata,
    reasons: tuple[str, ...] | None = None,
) -> ExecutionPlan:
    """Build one immutable deterministic plan from one or more metadata entries."""
    entries = metadata or (make_metadata(),)
    step_reasons = reasons or tuple("Collect status." for _ in entries)
    return ExecutionPlan(
        plan_id=PLAN_ID,
        task_id=TASK_ID,
        target="local-mock",
        steps=tuple(
            make_step(
                entry,
                step_id=f"status-{index}",
                reason=step_reasons[index - 1],
            )
            for index, entry in enumerate(entries, start=1)
        ),
    )


def make_context(
    *,
    operator_id: str = "local-user",
    target: TargetReference = TARGET,
) -> PolicyEvaluationContext:
    """Build trusted Runtime-owned Policy evaluation context."""
    return PolicyEvaluationContext(operator_id=operator_id, target=target)


def make_rule(
    metadata: ToolMetadata,
    *,
    rule_id: str = "status-rule",
    operator_id: str = "local-user",
    target: TargetReference = TARGET,
    minimum_approval: PolicyApprovalRequirement = (PolicyApprovalRequirement.NOT_REQUIRED),
) -> PolicyCapabilityRule:
    """Build one exact operator-target-Tool capability rule."""
    return PolicyCapabilityRule(
        rule_id=rule_id,
        operator_id=operator_id,
        target=target,
        tool_id=metadata.tool_id,
        tool_version=metadata.version,
        contract_hash=metadata.contract_hash,
        implementation_hash=metadata.implementation_hash,
        minimum_approval=minimum_approval,
    )


def make_synthetic_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    metadata: tuple[ToolMetadata, ...],
    rules: tuple[PolicyCapabilityRule, ...],
) -> PolicyEngine:
    """Construct Policy over a controlled frozen Registry and reviewed Profile."""
    snapshot = MappingProxyType({(entry.tool_id, entry.version): entry for entry in metadata})
    profile = PolicyProfile(
        policy_id="synthetic-policy",
        version="1.0.0",
        rules=rules,
    )
    reviewed_at = datetime(2026, 7, 29, tzinfo=UTC)
    artifacts = ValidatedPolicyArtifacts(
        profile=profile,
        review_record=PolicyReviewRecord(
            policy_id=profile.policy_id,
            version=profile.version,
            content_hash=POLICY_HASH,
            status="active",
            reviewer="local-owner",
            reviewed_at=reviewed_at,
            activated_at=reviewed_at,
        ),
        profile_hash=POLICY_HASH,
    )

    def metadata_snapshot(
        registry: ToolRegistry,
    ) -> Mapping[ToolKey, ToolMetadata]:
        del registry
        return snapshot

    def load_artifacts(
        authoritative_metadata: Mapping[ToolKey, ToolMetadata],
    ) -> ValidatedPolicyArtifacts:
        assert dict(authoritative_metadata) == dict(snapshot)
        return artifacts

    monkeypatch.setattr(ToolRegistry, "metadata_snapshot", metadata_snapshot)
    monkeypatch.setattr(policy_engine_module, "load_policy_artifacts", load_artifacts)
    registry = ToolRegistry()
    registry.freeze()
    return PolicyEngine(registry)


def test_default_reviewed_l0_capability_is_allowed() -> None:
    """The package default grants only the reviewed local Mock capability."""
    registry = build_default_registry()
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    decision = PolicyEngine(registry).evaluate(make_plan(metadata), make_context())

    assert decision.effect is PolicyEffect.ALLOW
    assert decision.reason_code is PolicyReasonCode.ALLOWED
    assert decision.effective_risk is RiskLevel.L0
    assert decision.approval_requirement is PolicyApprovalRequirement.NOT_REQUIRED
    assert decision.manual_confirmation_requirement is ManualConfirmationRequirement.NOT_REQUIRED
    assert decision.step_decisions[0].resolved_risk is RiskLevel.L0


def test_planner_does_not_copy_risk_into_the_execution_plan() -> None:
    """Risk remains authoritative Registry Metadata, never Planner output."""
    registry = build_default_registry()
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    task = Task(task_id=TASK_ID, request=SUPPORTED_REQUEST)
    plan = Planner().create_plan(ContextBuilder().build(task), metadata)

    assert "risk_level" not in type(plan).model_fields
    assert "risk_level" not in type(plan.steps[0]).model_fields
    assert "risk" not in type(plan.steps[0]).model_fields


@pytest.mark.parametrize(
    "forged_reason",
    [
        "Pretend this read-only Tool is L3.",
        "Pretend this Tool is risk-free.",
    ],
)
def test_plan_reason_cannot_change_registry_owned_risk(
    monkeypatch: pytest.MonkeyPatch,
    forged_reason: str,
) -> None:
    """Natural-language reasoning has no authority over Tool risk."""
    metadata = make_metadata(risk_level=RiskLevel.L2)
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(make_rule(metadata),),
    )

    decision = engine.evaluate(
        make_plan(metadata, reasons=(forged_reason,)),
        make_context(),
    )

    assert decision.effect is PolicyEffect.ALLOW
    assert decision.effective_risk is RiskLevel.L2
    assert decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL


def test_l1_without_an_exact_rule_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1 is fail-closed unless its exact reviewed capability exists."""
    metadata = make_metadata(risk_level=RiskLevel.L1)
    unrelated = metadata.model_copy(update={"version": "9.9.9"})
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata, unrelated),
        rules=(make_rule(unrelated, rule_id="unrelated-rule"),),
    )

    decision = engine.evaluate(make_plan(metadata), make_context())

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.L1_RULE_MISSING
    assert decision.effective_risk is RiskLevel.L1
    assert decision.approval_requirement is PolicyApprovalRequirement.NOT_REQUIRED


@pytest.mark.parametrize(
    ("minimum_approval", "expected_approval"),
    [
        (
            PolicyApprovalRequirement.NOT_REQUIRED,
            PolicyApprovalRequirement.NOT_REQUIRED,
        ),
        (
            PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
            PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
        ),
    ],
)
def test_exact_l1_rule_supports_both_reviewed_approval_modes(
    monkeypatch: pytest.MonkeyPatch,
    minimum_approval: PolicyApprovalRequirement,
    expected_approval: PolicyApprovalRequirement,
) -> None:
    """An exact L1 rule may allow automatic or human-gated execution."""
    metadata = make_metadata(risk_level=RiskLevel.L1)
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(
            make_rule(
                metadata,
                minimum_approval=minimum_approval,
            ),
        ),
    )

    decision = engine.evaluate(make_plan(metadata), make_context())

    assert decision.effect is PolicyEffect.ALLOW
    assert decision.effective_risk is RiskLevel.L1
    assert decision.approval_requirement is expected_approval
    assert decision.manual_confirmation_requirement is ManualConfirmationRequirement.NOT_REQUIRED


def test_l2_always_requires_human_plan_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule cannot lower the mandatory L2 human approval gate."""
    metadata = make_metadata(risk_level=RiskLevel.L2)
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(
            make_rule(
                metadata,
                minimum_approval=PolicyApprovalRequirement.NOT_REQUIRED,
            ),
        ),
    )

    decision = engine.evaluate(make_plan(metadata), make_context())

    assert decision.effect is PolicyEffect.ALLOW
    assert decision.effective_risk is RiskLevel.L2
    assert decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
    assert decision.manual_confirmation_requirement is ManualConfirmationRequirement.NOT_REQUIRED


def test_l3_is_denied_until_per_invocation_confirmation_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 reports L3 gates but denies execution until Phase 4."""
    metadata = make_metadata(risk_level=RiskLevel.L3)
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(make_rule(metadata),),
    )

    decision = engine.evaluate(make_plan(metadata), make_context())

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE
    assert decision.effective_risk is RiskLevel.L3
    assert decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
    assert decision.manual_confirmation_requirement is ManualConfirmationRequirement.PER_INVOCATION


@pytest.mark.parametrize(
    "rule_variant",
    ["missing-capability", "different-operator"],
)
def test_structurally_valid_l3_uses_the_unavailable_confirmation_reason(
    monkeypatch: pytest.MonkeyPatch,
    rule_variant: str,
) -> None:
    """L3 is hard-denied before ordinary capability authorization checks."""
    metadata = make_metadata(risk_level=RiskLevel.L3)
    authoritative_metadata: tuple[ToolMetadata, ...]
    if rule_variant == "missing-capability":
        unrelated = metadata.model_copy(update={"version": "9.9.9"})
        authoritative_metadata = (metadata, unrelated)
        rules = (make_rule(unrelated, rule_id="unrelated-rule"),)
    else:
        authoritative_metadata = (metadata,)
        rules = (
            make_rule(
                metadata,
                rule_id="different-operator-rule",
                operator_id="different-user",
            ),
        )
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=authoritative_metadata,
        rules=rules,
    )

    decision = engine.evaluate(make_plan(metadata), make_context())

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE
    assert decision.effective_risk is RiskLevel.L3
    assert decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
    assert decision.manual_confirmation_requirement is ManualConfirmationRequirement.PER_INVOCATION


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool_id", "unknown_tool"),
        ("tool_version", "1.0.1"),
    ],
)
def test_unknown_tool_or_version_is_unresolved_and_denied(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    """Policy resolves only an exact Tool ID and version pair."""
    metadata = make_metadata()
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(make_rule(metadata),),
    )
    original = make_plan(metadata)
    unknown_step = original.steps[0].model_copy(update={field: value})
    unknown_plan = original.model_copy(update={"steps": (unknown_step,)})

    decision = engine.evaluate(unknown_plan, make_context())

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.UNKNOWN_TOOL
    assert decision.effective_risk is None
    assert decision.approval_requirement is None
    assert decision.manual_confirmation_requirement is None
    assert decision.step_decisions[0].resolved_risk is None


@pytest.mark.parametrize(
    ("field", "forged_hash"),
    [
        ("contract_hash", "d" * 64),
        ("implementation_hash", "e" * 64),
    ],
)
def test_both_integrity_hashes_are_bound_to_registry_metadata(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    forged_hash: str,
) -> None:
    """Contract and implementation hash tampering fail closed."""
    metadata = make_metadata()
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(make_rule(metadata),),
    )
    original = make_plan(metadata)
    forged_step = original.steps[0].model_copy(update={field: forged_hash})
    forged_plan = original.model_copy(update={"steps": (forged_step,)})

    decision = engine.evaluate(forged_plan, make_context())

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.TOOL_INTEGRITY_MISMATCH


def test_operator_must_match_the_exact_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Tool grant owned by another operator cannot be borrowed."""
    metadata = make_metadata()
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(
            make_rule(
                metadata,
                operator_id="different-user",
            ),
        ),
    )

    decision = engine.evaluate(make_plan(metadata), make_context())

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.OPERATOR_NOT_ALLOWED


def test_target_must_match_the_exact_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator grant for another exact Target cannot be recombined."""
    selected = make_metadata(resource_type="alternate_system")
    local_only = make_metadata(
        tool_id="local_status_tool",
        resource_type="local_system",
    )
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(selected, local_only),
        rules=(
            make_rule(
                local_only,
                target=TARGET,
            ),
        ),
    )

    decision = engine.evaluate(
        make_plan(selected),
        make_context(target=ALTERNATE_TARGET),
    )

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.TARGET_NOT_ALLOWED


def test_operator_target_and_tool_grants_do_not_form_a_cross_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Separately present dimensions never synthesize an unreviewed grant."""
    selected = make_metadata(resource_type="alternate_system")
    other_tool = make_metadata(
        tool_id="other_status_tool",
        resource_type="alternate_system",
    )
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(selected, other_tool),
        rules=(
            make_rule(
                other_tool,
                rule_id="local-other-capability",
                target=ALTERNATE_TARGET,
            ),
            make_rule(
                selected,
                rule_id="other-user-selected-capability",
                operator_id="different-user",
                target=ALTERNATE_TARGET,
            ),
        ),
    )

    decision = engine.evaluate(
        make_plan(selected),
        make_context(target=ALTERNATE_TARGET),
    )

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.TOOL_NOT_ALLOWED


@pytest.mark.parametrize(
    "metadata",
    [
        make_metadata(resource_type="other_resource"),
        make_metadata(maximum_targets=2),
    ],
    ids=["resource-type", "target-count"],
)
def test_static_tool_target_scope_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
    metadata: ToolMetadata,
) -> None:
    """Tool target scope cannot be widened by a matching Policy rule."""
    reviewed_target = TargetReference(
        target_id="local-mock",
        resource_type=metadata.target_scope.resource_type,
        resource_id="local-mock",
    )
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(make_rule(metadata, target=reviewed_target),),
    )

    decision = engine.evaluate(make_plan(metadata), make_context())

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.TARGET_SCOPE_MISMATCH


def test_multi_step_decision_uses_maximum_risk_and_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Risk and approval maxima are independently aggregated across steps."""
    human_l0 = make_metadata(version="1.0.0", risk_level=RiskLevel.L0)
    automatic_l1 = make_metadata(version="2.0.0", risk_level=RiskLevel.L1)
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(human_l0, automatic_l1),
        rules=(
            make_rule(
                human_l0,
                rule_id="human-l0",
                minimum_approval=PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL,
            ),
            make_rule(
                automatic_l1,
                rule_id="automatic-l1",
                minimum_approval=PolicyApprovalRequirement.NOT_REQUIRED,
            ),
        ),
    )

    decision = engine.evaluate(
        make_plan(human_l0, automatic_l1),
        make_context(),
    )

    assert decision.effect is PolicyEffect.ALLOW
    assert decision.effective_risk is RiskLevel.L1
    assert decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
    assert tuple(step.resolved_risk for step in decision.step_decisions) == (
        RiskLevel.L0,
        RiskLevel.L1,
    )


def test_any_denied_step_denies_the_entire_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy evaluates all steps and denies the aggregate on any denial."""
    allowed_l0 = make_metadata(version="1.0.0", risk_level=RiskLevel.L0)
    missing_l1 = make_metadata(version="2.0.0", risk_level=RiskLevel.L1)
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(allowed_l0, missing_l1),
        rules=(make_rule(allowed_l0, rule_id="allowed-l0"),),
    )

    decision = engine.evaluate(
        make_plan(allowed_l0, missing_l1),
        make_context(),
    )

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is PolicyReasonCode.L1_RULE_MISSING
    assert decision.effective_risk is RiskLevel.L1
    assert tuple(step.effect for step in decision.step_decisions) == (
        PolicyEffect.ALLOW,
        PolicyEffect.DENY,
    )


@pytest.mark.parametrize(
    (
        "known_risk",
        "expected_reason",
        "expected_confirmation",
    ),
    [
        (
            RiskLevel.L2,
            PolicyReasonCode.UNKNOWN_TOOL,
            ManualConfirmationRequirement.NOT_REQUIRED,
        ),
        (
            RiskLevel.L3,
            PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE,
            ManualConfirmationRequirement.PER_INVOCATION,
        ),
    ],
)
def test_unknown_step_does_not_hide_known_risk_or_approval_maxima(
    monkeypatch: pytest.MonkeyPatch,
    known_risk: RiskLevel,
    expected_reason: PolicyReasonCode,
    expected_confirmation: ManualConfirmationRequirement,
) -> None:
    """Mixed resolution remains denied while retaining known safety requirements."""
    known = make_metadata(risk_level=known_risk)
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(known,),
        rules=(make_rule(known),),
    )
    original = make_plan(known, known)
    unknown = original.steps[1].model_copy(update={"tool_id": "unknown_tool"})
    mixed_plan = original.model_copy(update={"steps": (original.steps[0], unknown)})

    decision = engine.evaluate(mixed_plan, make_context())

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code is expected_reason
    assert decision.effective_risk is known_risk
    assert decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
    assert decision.manual_confirmation_requirement is expected_confirmation
    assert tuple(step.resolved_risk for step in decision.step_decisions) == (
        known_risk,
        None,
    )


def test_identical_inputs_produce_byte_equivalent_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy has no clock, randomness, or model-dependent output."""
    metadata = make_metadata(risk_level=RiskLevel.L1)
    engine = make_synthetic_engine(
        monkeypatch,
        metadata=(metadata,),
        rules=(make_rule(metadata),),
    )
    plan = make_plan(metadata)
    context = make_context()

    first = engine.evaluate(plan, context).model_dump_json()
    second = engine.evaluate(plan, context).model_dump_json()

    assert first.encode() == second.encode()


def test_plan_serialization_failure_is_redacted_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected plan serialization failures cannot leak attacker data."""
    marker = "SENSITIVE_PLAN_SERIALIZATION_MARKER"
    registry = build_default_registry()
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    engine = PolicyEngine(registry)
    plan = make_plan(metadata)

    def exploding_model_dump(
        instance: ExecutionPlan,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        del instance, args, kwargs
        raise SystemExit(marker)

    monkeypatch.setattr(ExecutionPlan, "model_dump", exploding_model_dump)

    with pytest.raises(
        PolicyEvaluationError,
        match="malformed ExecutionPlan",
    ) as caught:
        engine.evaluate(plan, make_context())

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_context_serialization_failure_is_redacted_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected context serialization failures cannot leak attacker data."""
    marker = "SENSITIVE_CONTEXT_SERIALIZATION_MARKER"
    registry = build_default_registry()
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    engine = PolicyEngine(registry)
    context = make_context()

    def exploding_model_dump(
        instance: PolicyEvaluationContext,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        del instance, args, kwargs
        raise SystemExit(marker)

    monkeypatch.setattr(
        PolicyEvaluationContext,
        "model_dump",
        exploding_model_dump,
    )

    with pytest.raises(
        PolicyEvaluationError,
        match="malformed evaluation context",
    ) as caught:
        engine.evaluate(make_plan(metadata), context)

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_execution_plan_subclass_is_rejected_without_leaking() -> None:
    """Only the exact validated Plan model may cross the Policy boundary."""
    marker = "SENSITIVE_PLAN_SUBCLASS_MARKER"

    class MaliciousExecutionPlan(ExecutionPlan):
        pass

    registry = build_default_registry()
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    plan = make_plan(metadata)
    malicious = MaliciousExecutionPlan.model_validate(
        plan.model_dump()
        | {
            "steps": [
                plan.steps[0].model_dump()
                | {
                    "reason": marker,
                }
            ],
        }
    )

    with pytest.raises(
        PolicyEvaluationError,
        match="malformed ExecutionPlan",
    ) as caught:
        PolicyEngine(registry).evaluate(malicious, make_context())

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_evaluation_context_subclass_is_rejected_without_leaking() -> None:
    """Only the exact Runtime-owned Context model may cross Policy."""
    marker = "SENSITIVE_CONTEXT_SUBCLASS_MARKER"

    class MaliciousContext(PolicyEvaluationContext):
        pass

    registry = build_default_registry()
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    malicious = MaliciousContext(
        operator_id="local-user",
        target=TARGET,
        schema_version="1",
    )
    object.__setattr__(malicious, "operator_id", marker)

    with pytest.raises(
        PolicyEvaluationError,
        match="malformed evaluation context",
    ) as caught:
        PolicyEngine(registry).evaluate(make_plan(metadata), malicious)

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_engine_requires_an_exact_frozen_registry() -> None:
    """Policy construction accepts no mutable, fake, or subclass Registry."""
    unfrozen = ToolRegistry()

    with pytest.raises(
        PolicyConfigurationError,
        match="frozen authoritative Tool Registry",
    ):
        PolicyEngine(unfrozen)

    class RegistrySubclass(ToolRegistry):
        pass

    subclass = RegistrySubclass()
    subclass.freeze()
    with pytest.raises(
        PolicyConfigurationError,
        match="frozen authoritative Tool Registry",
    ):
        PolicyEngine(subclass)

    engine = PolicyEngine(build_default_registry())
    assert engine.policy_id == "local-default"


def test_evaluate_accepts_no_caller_supplied_catalog() -> None:
    """Registry Metadata is constructor-bound and cannot be swapped per call."""
    parameters = tuple(signature(PolicyEngine.evaluate).parameters)
    assert parameters == ("self", "plan", "context")

    registry = build_default_registry()
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    engine = PolicyEngine(registry)
    with pytest.raises(TypeError):
        engine.evaluate(  # type: ignore[call-arg]
            make_plan(metadata),
            make_context(),
            registry.metadata_snapshot(),
        )
