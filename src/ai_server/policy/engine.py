"""Deterministic, fail-closed Policy evaluation over reviewed local artifacts."""

from types import MappingProxyType
from typing import cast
from uuid import UUID

from pydantic import ValidationError

from ai_server.models.execution import ExecutionPlan, ExecutionStep
from ai_server.models.policy import (
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
    PolicyCapabilityRule,
    PolicyDecision,
    PolicyEffect,
    PolicyEvaluationContext,
    PolicyReasonCode,
    StepPolicyDecision,
)
from ai_server.models.system_status import GetSystemStatusArguments
from ai_server.models.tool import RiskLevel, TargetReference, ToolMetadata
from ai_server.policy.artifact_loader import load_policy_artifacts
from ai_server.policy.errors import PolicyConfigurationError, PolicyEvaluationError
from ai_server.tools.hashing import CanonicalizationError, canonical_json_sha256
from ai_server.tools.registry import ToolKey, ToolRegistry

_RISK_RANK = {
    RiskLevel.L0: 0,
    RiskLevel.L1: 1,
    RiskLevel.L2: 2,
    RiskLevel.L3: 3,
}
_APPROVAL_RANK = {
    PolicyApprovalRequirement.NOT_REQUIRED: 0,
    PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL: 1,
}
_CONFIRMATION_RANK = {
    ManualConfirmationRequirement.NOT_REQUIRED: 0,
    ManualConfirmationRequirement.PER_INVOCATION: 1,
}


class PolicyEngine:
    """Evaluate immutable plans against one reviewed Profile and frozen Registry."""

    def __init__(self, registry: ToolRegistry) -> None:
        """Bind Policy to one frozen authoritative Registry and reviewed Profile."""
        if type(registry) is not ToolRegistry or not registry.is_frozen:
            raise PolicyConfigurationError("Policy requires one frozen authoritative Tool Registry")
        self._metadata = _validated_metadata_snapshot(registry)
        artifacts = load_policy_artifacts(self._metadata)
        self._profile = artifacts.profile
        self._policy_hash = artifacts.profile_hash

    @property
    def policy_id(self) -> str:
        """Return the immutable active Policy Profile identifier."""
        return self._profile.policy_id

    @property
    def policy_version(self) -> str:
        """Return the immutable active Policy Profile version."""
        return self._profile.version

    @property
    def policy_hash(self) -> str:
        """Return the reviewed RFC 8785 canonical Policy Profile hash."""
        return self._policy_hash

    def evaluate(
        self,
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        """Return one deterministic structured decision without invoking a Tool."""
        trusted_plan = _validate_plan(plan)
        trusted_context = _validate_context(context)
        try:
            step_decisions = tuple(
                self._evaluate_step(trusted_plan, step, trusted_context)
                for step in trusted_plan.steps
            )
            denied = next(
                (decision for decision in step_decisions if decision.effect is PolicyEffect.DENY),
                None,
            )
            resolved_decisions = tuple(
                decision for decision in step_decisions if decision.resolved_risk is not None
            )
            effective_risk = (
                max(
                    (cast(RiskLevel, decision.resolved_risk) for decision in resolved_decisions),
                    key=_RISK_RANK.__getitem__,
                )
                if resolved_decisions
                else None
            )
            approval_requirement = (
                max(
                    (
                        cast(
                            PolicyApprovalRequirement,
                            decision.approval_requirement,
                        )
                        for decision in resolved_decisions
                    ),
                    key=_APPROVAL_RANK.__getitem__,
                )
                if resolved_decisions
                else None
            )
            manual_confirmation_requirement = (
                max(
                    (
                        cast(
                            ManualConfirmationRequirement,
                            decision.manual_confirmation_requirement,
                        )
                        for decision in resolved_decisions
                    ),
                    key=_CONFIRMATION_RANK.__getitem__,
                )
                if resolved_decisions
                else None
            )
            return PolicyDecision(
                policy_id=self._profile.policy_id,
                policy_version=self._profile.version,
                policy_hash=self._policy_hash,
                task_id=trusted_plan.task_id,
                plan_id=trusted_plan.plan_id,
                operator_id=trusted_context.operator_id,
                target=trusted_context.target,
                effect=PolicyEffect.DENY if denied is not None else PolicyEffect.ALLOW,
                reason_code=(
                    denied.reason_code if denied is not None else PolicyReasonCode.ALLOWED
                ),
                effective_risk=effective_risk,
                approval_requirement=approval_requirement,
                manual_confirmation_requirement=manual_confirmation_requirement,
                step_decisions=step_decisions,
            )
        except PolicyEvaluationError:
            raise
        except (
            CanonicalizationError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            raise PolicyEvaluationError(
                "Policy could not produce a trustworthy structured decision"
            ) from None
        except BaseException:
            raise PolicyEvaluationError("Policy evaluation failed safely") from None

    def _evaluate_step(
        self,
        plan: ExecutionPlan,
        step: ExecutionStep,
        context: PolicyEvaluationContext,
    ) -> StepPolicyDecision:
        arguments_hash = canonical_json_sha256(step.arguments)
        metadata = self._metadata.get((step.tool_id, step.tool_version))
        if metadata is None:
            return _step_decision(
                step,
                arguments_hash=arguments_hash,
                reason=PolicyReasonCode.UNKNOWN_TOOL,
            )

        approval, confirmation = _risk_requirements(metadata.risk_level)
        rule = self._matching_rule(step, context)
        if (
            rule is not None
            and rule.minimum_approval is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
        ):
            approval = PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL

        reason = self._denial_reason(
            plan=plan,
            step=step,
            context=context,
            metadata=metadata,
            rule=rule,
        )
        return _step_decision(
            step,
            arguments_hash=arguments_hash,
            metadata=metadata,
            effect=(
                PolicyEffect.ALLOW if reason is PolicyReasonCode.ALLOWED else PolicyEffect.DENY
            ),
            reason=reason,
            approval=approval,
            confirmation=confirmation,
        )

    def _denial_reason(
        self,
        *,
        plan: ExecutionPlan,
        step: ExecutionStep,
        context: PolicyEvaluationContext,
        metadata: ToolMetadata,
        rule: PolicyCapabilityRule | None,
    ) -> PolicyReasonCode:
        if context.target.target_id != plan.target or context.target.resource_id != plan.target:
            return PolicyReasonCode.TARGET_MISMATCH
        if (
            step.contract_hash != metadata.contract_hash
            or step.implementation_hash != metadata.implementation_hash
        ):
            return PolicyReasonCode.TOOL_INTEGRITY_MISMATCH
        if not _target_is_in_scope(step, context.target, metadata):
            return PolicyReasonCode.TARGET_SCOPE_MISMATCH
        if metadata.risk_level is RiskLevel.L3:
            return PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE

        operator_rules = tuple(
            candidate
            for candidate in self._profile.rules
            if candidate.operator_id == context.operator_id
        )
        if not operator_rules:
            return PolicyReasonCode.OPERATOR_NOT_ALLOWED
        target_rules = tuple(
            candidate for candidate in operator_rules if candidate.target == context.target
        )
        if not target_rules:
            return PolicyReasonCode.TARGET_NOT_ALLOWED
        if rule is None:
            if metadata.risk_level is RiskLevel.L1:
                return PolicyReasonCode.L1_RULE_MISSING
            return PolicyReasonCode.TOOL_NOT_ALLOWED
        return PolicyReasonCode.ALLOWED

    def _matching_rule(
        self,
        step: ExecutionStep,
        context: PolicyEvaluationContext,
    ) -> PolicyCapabilityRule | None:
        return next(
            (
                rule
                for rule in self._profile.rules
                if rule.operator_id == context.operator_id
                and rule.target == context.target
                and rule.tool_id == step.tool_id
                and rule.tool_version == step.tool_version
                and rule.contract_hash == step.contract_hash
                and rule.implementation_hash == step.implementation_hash
            ),
            None,
        )


def _validated_metadata_snapshot(
    registry: ToolRegistry,
) -> MappingProxyType[ToolKey, ToolMetadata]:
    try:
        raw_snapshot = registry.metadata_snapshot()
        if type(raw_snapshot) is not MappingProxyType:
            raise TypeError
        validated: dict[ToolKey, ToolMetadata] = {}
        for raw_key, raw_metadata in raw_snapshot.items():
            if (
                type(raw_key) is not tuple
                or len(raw_key) != 2
                or any(type(part) is not str for part in raw_key)
                or type(raw_metadata) is not ToolMetadata
            ):
                raise TypeError
            metadata = ToolMetadata.model_validate(
                raw_metadata.model_dump(mode="python", warnings="error"),
                strict=True,
            )
            key = (metadata.tool_id, metadata.version)
            if raw_key != key or key in validated:
                raise TypeError
            validated[key] = metadata
        return MappingProxyType(validated)
    except BaseException:
        raise PolicyConfigurationError(
            "Policy rejected malformed authoritative Tool Metadata"
        ) from None


def _validate_plan(plan: ExecutionPlan) -> ExecutionPlan:
    try:
        if (
            type(plan) is not ExecutionPlan
            or type(plan.plan_id) is not UUID
            or type(plan.task_id) is not UUID
            or type(plan.steps) is not tuple
            or not plan.steps
            or any(
                type(step) is not ExecutionStep
                or type(step.arguments) is not GetSystemStatusArguments
                for step in plan.steps
            )
        ):
            raise TypeError
        validated = ExecutionPlan.model_validate(
            plan.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        if (
            type(validated.plan_id) is not UUID
            or type(validated.task_id) is not UUID
            or type(validated.steps) is not tuple
            or any(
                type(step) is not ExecutionStep
                or type(step.arguments) is not GetSystemStatusArguments
                for step in validated.steps
            )
        ):
            raise TypeError
        return validated
    except BaseException:
        raise PolicyEvaluationError("Policy rejected a malformed ExecutionPlan") from None


def _validate_context(context: PolicyEvaluationContext) -> PolicyEvaluationContext:
    try:
        if (
            type(context) is not PolicyEvaluationContext
            or type(context.target) is not TargetReference
        ):
            raise TypeError
        validated = PolicyEvaluationContext.model_validate(
            context.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        if type(validated.target) is not TargetReference:
            raise TypeError
        return validated
    except BaseException:
        raise PolicyEvaluationError("Policy rejected a malformed evaluation context") from None


def _target_is_in_scope(
    step: ExecutionStep,
    target: TargetReference,
    metadata: ToolMetadata,
) -> bool:
    try:
        arguments = step.arguments.model_dump(mode="python", warnings="error")
        selector = arguments.get(metadata.target_scope.selector_field)
        return (
            type(selector) is str
            and metadata.target_scope.maximum_targets == 1
            and metadata.target_scope.allow_dynamic_expansion is False
            and target.resource_type == metadata.target_scope.resource_type
            and target.target_id == selector
            and target.resource_id == selector
        )
    except BaseException:
        return False


def _risk_requirements(
    risk: RiskLevel,
) -> tuple[PolicyApprovalRequirement, ManualConfirmationRequirement]:
    if risk in {RiskLevel.L2, RiskLevel.L3}:
        approval = PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
    else:
        approval = PolicyApprovalRequirement.NOT_REQUIRED
    confirmation = (
        ManualConfirmationRequirement.PER_INVOCATION
        if risk is RiskLevel.L3
        else ManualConfirmationRequirement.NOT_REQUIRED
    )
    return approval, confirmation


def _step_decision(
    step: ExecutionStep,
    *,
    arguments_hash: str,
    metadata: ToolMetadata | None = None,
    effect: PolicyEffect = PolicyEffect.DENY,
    reason: PolicyReasonCode,
    approval: PolicyApprovalRequirement | None = None,
    confirmation: ManualConfirmationRequirement | None = None,
) -> StepPolicyDecision:
    return StepPolicyDecision(
        step_id=step.step_id,
        tool_id=step.tool_id,
        tool_version=step.tool_version,
        contract_hash=step.contract_hash,
        implementation_hash=step.implementation_hash,
        arguments_hash=arguments_hash,
        resolved_risk=metadata.risk_level if metadata is not None else None,
        effect=effect,
        reason_code=reason,
        approval_requirement=approval,
        manual_confirmation_requirement=confirmation,
    )


__all__ = ["PolicyEngine"]
