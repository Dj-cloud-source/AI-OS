"""Strict immutable models for deterministic Policy evaluation."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_server.models.tool import (
    BoundedIdentifier,
    HashDigest,
    RiskLevel,
    SemanticVersion,
    TargetReference,
    ToolId,
)

_STRICT_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


class PolicyEffect(StrEnum):
    """Final deterministic effect of a Policy evaluation."""

    ALLOW = "ALLOW"
    DENY = "DENY"


class PolicyApprovalRequirement(StrEnum):
    """Minimum plan-level approval gate derived by Policy."""

    NOT_REQUIRED = "NOT_REQUIRED"
    HUMAN_PLAN_APPROVAL = "HUMAN_PLAN_APPROVAL"


class ManualConfirmationRequirement(StrEnum):
    """Minimum immediate confirmation required for each invocation."""

    NOT_REQUIRED = "NOT_REQUIRED"
    PER_INVOCATION = "PER_INVOCATION"


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


class PolicyReasonCode(StrEnum):
    """Stable, non-sensitive reason codes emitted by Policy."""

    ALLOWED = "allowed"
    OPERATOR_NOT_ALLOWED = "operator_not_allowed"
    TARGET_MISMATCH = "target_mismatch"
    TARGET_NOT_ALLOWED = "target_not_allowed"
    UNKNOWN_TOOL = "unknown_tool"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    TOOL_INTEGRITY_MISMATCH = "tool_integrity_mismatch"
    TARGET_SCOPE_MISMATCH = "target_scope_mismatch"
    L1_RULE_MISSING = "l1_rule_missing"
    L3_CONFIRMATION_UNAVAILABLE = "l3_confirmation_unavailable"


class PolicyEvaluationContext(BaseModel):
    """Runtime-owned operator and target identity supplied to Policy."""

    model_config = _STRICT_FROZEN_CONFIG

    schema_version: Literal["1"] = "1"
    operator_id: BoundedIdentifier
    target: TargetReference


class PolicyCapabilityRule(BaseModel):
    """One exact reviewed operator-target-Tool capability."""

    model_config = _STRICT_FROZEN_CONFIG

    rule_id: BoundedIdentifier
    operator_id: BoundedIdentifier
    target: TargetReference
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    minimum_approval: PolicyApprovalRequirement


class ApprovalConstraints(BaseModel):
    """Reviewed upper-bounded lifetimes for local approval evidence."""

    model_config = _STRICT_FROZEN_CONFIG

    review_session_ttl_seconds: int = Field(ge=1, le=300)
    plan_approval_ttl_seconds: int = Field(ge=1, le=300)
    l3_confirmation_ttl_seconds: int = Field(ge=1, le=30)


class PolicyProfile(BaseModel):
    """Versioned immutable collection of exact Policy capability rules."""

    model_config = _STRICT_FROZEN_CONFIG

    profile_schema_version: Literal["1", "2"] = "1"
    policy_id: BoundedIdentifier
    version: SemanticVersion
    rules: tuple[PolicyCapabilityRule, ...] = Field(min_length=1, max_length=10_000)
    approval_constraints: ApprovalConstraints | None = None

    @model_validator(mode="after")
    def validate_unique_rules(self) -> Self:
        """Reject invalid version fields and duplicate exact capabilities."""
        if self.profile_schema_version == "1" and self.approval_constraints is not None:
            raise ValueError("Policy Profile Schema v1 cannot define approval constraints")
        if self.profile_schema_version == "2" and self.approval_constraints is None:
            raise ValueError("Policy Profile Schema v2 requires approval constraints")
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("Policy rule IDs must be unique")
        capability_keys = tuple(
            (
                rule.operator_id,
                rule.target.target_id,
                rule.target.resource_type,
                rule.target.resource_id,
                rule.tool_id,
                rule.tool_version,
                rule.contract_hash,
                rule.implementation_hash,
            )
            for rule in self.rules
        )
        if len(capability_keys) != len(set(capability_keys)):
            raise ValueError("Exact Policy capabilities must be unique")
        return self


class PolicyReviewRecord(BaseModel):
    """Human review evidence bound to one immutable Policy Profile hash."""

    model_config = _STRICT_FROZEN_CONFIG

    record_schema_version: Literal["1"] = "1"
    policy_id: BoundedIdentifier
    version: SemanticVersion
    content_hash: HashDigest
    status: Literal["active"]
    reviewer: Literal["local-owner"]
    reviewed_at: datetime
    activated_at: datetime

    @model_validator(mode="after")
    def validate_review_timestamps(self) -> Self:
        """Require ordered UTC review and activation timestamps."""
        if self.reviewed_at.utcoffset() != timedelta(
            0
        ) or self.activated_at.utcoffset() != timedelta(0):
            raise ValueError("Policy review timestamps must use UTC")
        if self.activated_at < self.reviewed_at:
            raise ValueError("Policy activation cannot predate review")
        return self


class StepPolicyDecision(BaseModel):
    """Structured Policy outcome for one exact plan step."""

    model_config = _STRICT_FROZEN_CONFIG

    step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    arguments_hash: HashDigest
    resolved_risk: RiskLevel | None
    effect: PolicyEffect
    reason_code: PolicyReasonCode
    approval_requirement: PolicyApprovalRequirement | None
    manual_confirmation_requirement: ManualConfirmationRequirement | None

    @model_validator(mode="after")
    def validate_decision_fields(self) -> Self:
        """Require resolved Tools and decision fields to remain internally consistent."""
        resolution_fields = (
            self.resolved_risk,
            self.approval_requirement,
            self.manual_confirmation_requirement,
        )
        if any(value is None for value in resolution_fields) and not all(
            value is None for value in resolution_fields
        ):
            raise ValueError("Resolved Policy fields must be present or absent together")
        if self.resolved_risk is None and self.reason_code is not PolicyReasonCode.UNKNOWN_TOOL:
            raise ValueError("Only unknown Tools may omit resolved Policy fields")
        if self.effect is PolicyEffect.ALLOW:
            if self.reason_code is not PolicyReasonCode.ALLOWED or self.resolved_risk is None:
                raise ValueError("Allowed steps require an allowed reason and resolved Tool")
        elif self.reason_code is PolicyReasonCode.ALLOWED:
            raise ValueError("Denied steps cannot use the allowed reason")
        if self.reason_code is PolicyReasonCode.UNKNOWN_TOOL and any(
            value is not None for value in resolution_fields
        ):
            raise ValueError("Unknown Tools cannot claim resolved Policy fields")
        if (
            self.reason_code is PolicyReasonCode.L1_RULE_MISSING
            and self.resolved_risk is not RiskLevel.L1
        ):
            raise ValueError("An L1 missing-rule reason requires resolved L1 risk")
        if (
            self.reason_code is PolicyReasonCode.L3_CONFIRMATION_UNAVAILABLE
            and self.resolved_risk is not RiskLevel.L3
        ):
            raise ValueError("An unavailable L3 confirmation reason requires resolved L3 risk")
        if self.resolved_risk in {RiskLevel.L2, RiskLevel.L3} and (
            self.approval_requirement is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
        ):
            raise ValueError("L2 and L3 require human plan approval")
        if self.resolved_risk is RiskLevel.L3 and (
            self.manual_confirmation_requirement is not ManualConfirmationRequirement.PER_INVOCATION
        ):
            raise ValueError("L3 requires per-invocation confirmation")
        if self.resolved_risk not in {None, RiskLevel.L3} and (
            self.manual_confirmation_requirement is not ManualConfirmationRequirement.NOT_REQUIRED
        ):
            raise ValueError("Only L3 may require per-invocation confirmation")
        if (
            self.manual_confirmation_requirement is ManualConfirmationRequirement.PER_INVOCATION
            and (
                self.resolved_risk is not RiskLevel.L3
                or self.approval_requirement is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
            )
        ):
            raise ValueError("Per-invocation confirmation requires L3 and human approval")
        return self


class PolicyDecision(BaseModel):
    """Hash-bound deterministic Policy outcome for one immutable plan."""

    model_config = _STRICT_FROZEN_CONFIG

    decision_schema_version: Literal["1"] = "1"
    policy_id: BoundedIdentifier
    policy_version: SemanticVersion
    policy_hash: HashDigest
    task_id: UUID
    plan_id: UUID
    operator_id: BoundedIdentifier
    target: TargetReference
    effect: PolicyEffect
    reason_code: PolicyReasonCode
    effective_risk: RiskLevel | None
    approval_requirement: PolicyApprovalRequirement | None
    manual_confirmation_requirement: ManualConfirmationRequirement | None
    step_decisions: tuple[StepPolicyDecision, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aggregate_decision(self) -> Self:
        """Require aggregate effect, reason, and step outcomes to agree."""
        step_ids = tuple(decision.step_id for decision in self.step_decisions)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Policy step decision IDs must be unique")
        first_denied = next(
            (decision for decision in self.step_decisions if decision.effect is PolicyEffect.DENY),
            None,
        )
        if self.effect is PolicyEffect.ALLOW:
            if (
                self.reason_code is not PolicyReasonCode.ALLOWED
                or self.effective_risk is None
                or self.approval_requirement is None
                or self.manual_confirmation_requirement is None
                or any(
                    decision.effect is not PolicyEffect.ALLOW for decision in self.step_decisions
                )
            ):
                raise ValueError("Allowed Policy decisions require all steps to be allowed")
        elif first_denied is None or self.reason_code is not first_denied.reason_code:
            raise ValueError("Denied Policy decisions require at least one denied step")
        resolved_decisions = tuple(
            decision for decision in self.step_decisions if decision.resolved_risk is not None
        )
        if not resolved_decisions:
            if any(
                value is not None
                for value in (
                    self.effective_risk,
                    self.approval_requirement,
                    self.manual_confirmation_requirement,
                )
            ):
                raise ValueError("A fully unresolved plan requires absent aggregate Policy fields")
        else:
            resolved_risks = tuple(
                decision.resolved_risk
                for decision in resolved_decisions
                if decision.resolved_risk is not None
            )
            approval_requirements = tuple(
                decision.approval_requirement
                for decision in resolved_decisions
                if decision.approval_requirement is not None
            )
            confirmation_requirements = tuple(
                decision.manual_confirmation_requirement
                for decision in resolved_decisions
                if decision.manual_confirmation_requirement is not None
            )
            expected_risk = max(resolved_risks, key=_RISK_RANK.__getitem__)
            expected_approval = max(
                approval_requirements,
                key=_APPROVAL_RANK.__getitem__,
            )
            expected_confirmation = max(
                confirmation_requirements,
                key=_CONFIRMATION_RANK.__getitem__,
            )
            if (
                self.effective_risk is not expected_risk
                or self.approval_requirement is not expected_approval
                or self.manual_confirmation_requirement is not expected_confirmation
            ):
                raise ValueError("Aggregate Policy fields must match resolved step maxima")
        if (
            self.manual_confirmation_requirement is ManualConfirmationRequirement.PER_INVOCATION
            and (
                self.effective_risk is not RiskLevel.L3
                or self.approval_requirement is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
            )
        ):
            raise ValueError("Per-invocation confirmation requires L3 and human approval")
        return self


__all__ = [
    "ApprovalConstraints",
    "ManualConfirmationRequirement",
    "PolicyApprovalRequirement",
    "PolicyCapabilityRule",
    "PolicyDecision",
    "PolicyEffect",
    "PolicyEvaluationContext",
    "PolicyProfile",
    "PolicyReasonCode",
    "PolicyReviewRecord",
    "StepPolicyDecision",
]
