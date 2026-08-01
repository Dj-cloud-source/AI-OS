from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from pydantic import JsonValue, ValidationError

from ai_server.models.approval import (
    ApprovalAuditEventKind,
    ApprovalValidationReason,
    ApprovalValidationResult,
    ApprovalValidationVerdict,
    PlanApprovalSnapshot,
    PlanStepApprovalSnapshot,
)
from ai_server.models.execution import StepRole
from ai_server.models.tool import (
    RedactionRequirement,
    RiskLevel,
    RollbackRequirement,
    RollbackStrategy,
    SideEffectKind,
    TargetReference,
    ToolSideEffects,
    ToolTargetScope,
    VerificationRequirement,
)
from ai_server.models.verification import EqualityCriterion
from ai_server.tools.hashing import canonical_json_sha256


def assign_attribute(instance: object, name: str, value: object) -> None:
    """Exercise frozen model assignment without weakening static typing."""
    setattr(instance, name, value)


def fixed_snapshot() -> PlanApprovalSnapshot:
    arguments: dict[str, JsonValue] = {
        "target": "local-mock",
        "options": {"labels": ["安全"]},
    }
    step = PlanStepApprovalSnapshot(
        step_index=0,
        step_id="get-system-status",
        role=StepRole.OBSERVE,
        tool_id="get_system_status",
        tool_version="1.0.0",
        contract_hash="9" * 64,
        implementation_hash="6" * 64,
        arguments=arguments,
        arguments_hash=canonical_json_sha256(arguments),
        target=TargetReference(
            target_id="local-mock",
            resource_type="local_system",
            resource_id="local-mock",
        ),
        target_scope=ToolTargetScope(
            resource_type="local_system",
            maximum_targets=1,
            selector_field="target",
            allow_dynamic_expansion=False,
        ),
        side_effects=ToolSideEffects(
            mutates_remote_state=False,
            kind=SideEffectKind.NONE,
        ),
        registry_risk_level=RiskLevel.L0,
        registry_redaction=RedactionRequirement(
            profile_id="tool-redaction-default",
            profile_version="1.0.0",
            safe_evidence_fields=("source", "simulated", "target", "hostname"),
            max_retained_payload_bytes=4096,
        ),
        registry_verification=VerificationRequirement(
            required=True,
            evidence_fields=("simulated", "source", "target"),
        ),
        registry_rollback=RollbackRequirement(
            required=False,
            available=False,
            strategy=RollbackStrategy.NOT_REQUIRED,
        ),
        reason="收集确定性的本地模拟状态。",
        impact="No external effect.",
        verification="Require structured simulated evidence.",
        recovery="Rollback is not required.",
    )
    return PlanApprovalSnapshot(
        plan_schema_version="2",
        task_id=UUID("11111111-1111-4111-8111-111111111111"),
        plan_id=UUID("22222222-2222-4222-8222-222222222222"),
        operator_id="local-user",
        target=step.target,
        execution_order=(step.step_id,),
        steps=(step,),
        verification_criteria=(
            EqualityCriterion(
                criterion_id="mock-source",
                evidence_step_id=step.step_id,
                source="evidence",
                field="source",
                expected="mock",
            ),
        ),
    )


def test_plan_approval_snapshot_has_stable_golden_hash() -> None:
    snapshot = fixed_snapshot()

    assert canonical_json_sha256(snapshot) == (
        "c0de7501860eba5395af7f7799733d18d39cb24bd619d2c09df3918714290e8f"
    )


def test_snapshot_v2_hash_binds_complete_ordered_verification_criteria() -> None:
    snapshot = fixed_snapshot()
    first = snapshot.verification_criteria[0]
    second = first.model_copy(
        update={
            "criterion_id": "mock-target",
            "field": "target",
            "expected": "local-mock",
        }
    )
    changed = snapshot.model_copy(
        update={"verification_criteria": (first.model_copy(update={"expected": "other"}),)}
    )
    ordered = snapshot.model_copy(update={"verification_criteria": (first, second)})
    reordered = snapshot.model_copy(update={"verification_criteria": (second, first)})

    assert canonical_json_sha256(changed) != canonical_json_sha256(snapshot)
    assert canonical_json_sha256(ordered) != canonical_json_sha256(reordered)
    for version_field in ("snapshot_schema_version", "plan_schema_version"):
        with pytest.raises(ValidationError):
            PlanApprovalSnapshot.model_validate(
                snapshot.model_dump(mode="python") | {version_field: "1"},
                strict=True,
            )


def test_snapshot_rejects_argument_order_or_hash_drift() -> None:
    snapshot = fixed_snapshot()
    payload = snapshot.model_dump(mode="python")
    payload["steps"][0]["arguments_hash"] = "d" * 64

    with pytest.raises(ValidationError, match="arguments hash"):
        PlanApprovalSnapshot.model_validate(payload)

    payload = snapshot.model_dump(mode="python")
    payload["execution_order"] = ("other-step",)
    with pytest.raises(ValidationError, match="execution order"):
        PlanApprovalSnapshot.model_validate(payload)


def test_snapshot_models_are_strict_frozen_and_forbid_unknown_fields() -> None:
    snapshot = fixed_snapshot()

    with pytest.raises(ValidationError):
        assign_attribute(
            snapshot,
            "plan_id",
            UUID("33333333-3333-4333-8333-333333333333"),
        )
    payload = snapshot.model_dump(mode="python")
    payload["authority"] = "model"
    with pytest.raises(ValidationError):
        PlanApprovalSnapshot.model_validate(payload)


def test_snapshot_arguments_are_recursively_immutable() -> None:
    arguments = fixed_snapshot().steps[0].arguments

    with pytest.raises(TypeError):
        arguments["target"] = "other"  # type: ignore[index]
    nested = cast(dict[str, object], arguments["options"])
    with pytest.raises(TypeError):
        nested["labels"] = ["other"]


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "clientSecret",
        "access_token",
        "AWS_SECRET_ACCESS_KEY",
        "github-token",
        "database_url",
        "connectionString",
        "session_id",
    ],
)
def test_snapshot_rejects_sensitive_argument_keys(sensitive_key: str) -> None:
    payload = fixed_snapshot().model_dump(mode="python")
    arguments: dict[str, JsonValue] = {
        "target": "local-mock",
        sensitive_key: "sensitive-value",
    }
    payload["steps"][0]["arguments"] = arguments
    payload["steps"][0]["arguments_hash"] = canonical_json_sha256(arguments)

    with pytest.raises(ValidationError, match="not safe to display"):
        PlanApprovalSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    "sensitive_value",
    [
        "password=hunter2",
        "postgres://user:pw@host/db",
        "refresh_token=abc123",
        "terminal\u001bcontrol",
    ],
)
def test_snapshot_rejects_sensitive_nested_argument_values(sensitive_value: str) -> None:
    payload = fixed_snapshot().model_dump(mode="python")
    arguments: dict[str, JsonValue] = {
        "target": "local-mock",
        "options": {"notes": ["safe", sensitive_value]},
    }
    payload["steps"][0]["arguments"] = arguments
    payload["steps"][0]["arguments_hash"] = canonical_json_sha256(arguments)

    with pytest.raises(ValidationError, match="not safe to display"):
        PlanApprovalSnapshot.model_validate(payload)


def test_snapshot_and_validation_reject_uuid_and_datetime_subclasses() -> None:
    class UUIDSubclass(UUID):
        pass

    class DateTimeSubclass(datetime):
        pass

    snapshot_payload = fixed_snapshot().model_dump(mode="python")
    snapshot_payload["task_id"] = UUIDSubclass(str(snapshot_payload["task_id"]))
    with pytest.raises(ValidationError, match="exact UUID"):
        PlanApprovalSnapshot.model_validate(snapshot_payload, strict=True)

    checked_at = DateTimeSubclass(2026, 7, 29, 8, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="exact datetime"):
        ApprovalValidationResult(
            verdict=ApprovalValidationVerdict.INVALID,
            reason=ApprovalValidationReason.UNKNOWN_APPROVAL,
            checked_at=checked_at,
        )


def test_validation_result_requires_consistent_reason_and_clock_evidence() -> None:
    checked_at = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="inconsistent"):
        ApprovalValidationResult(
            verdict=ApprovalValidationVerdict.VALID_UNCONSUMED,
            reason=ApprovalValidationReason.APPROVAL_EXPIRED,
            checked_at=checked_at,
        )
    with pytest.raises(ValidationError, match="invalid clock"):
        ApprovalValidationResult(
            verdict=ApprovalValidationVerdict.INVALID,
            reason=ApprovalValidationReason.UNKNOWN_APPROVAL,
            checked_at=None,
        )
    invalid_clock = ApprovalValidationResult(
        verdict=ApprovalValidationVerdict.INVALID,
        reason=ApprovalValidationReason.INVALID_CLOCK,
        checked_at=None,
    )
    assert invalid_clock.checked_at is None


def test_approval_public_enums_are_exact_and_stable() -> None:
    assert tuple(ApprovalValidationVerdict) == (
        ApprovalValidationVerdict.VALID_UNCONSUMED,
        ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT,
        ApprovalValidationVerdict.INVALID,
    )
    assert tuple(ApprovalAuditEventKind) == (
        ApprovalAuditEventKind.REVIEW_PREPARED,
        ApprovalAuditEventKind.PLAN_APPROVAL_ISSUED,
        ApprovalAuditEventKind.PLAN_APPROVAL_REJECTED,
        ApprovalAuditEventKind.PLAN_APPROVAL_INVALIDATED,
        ApprovalAuditEventKind.PLAN_APPROVAL_EXPIRED,
        ApprovalAuditEventKind.PLAN_APPROVAL_CONSUMED,
        ApprovalAuditEventKind.L3_CONFIRMATION_ISSUED,
        ApprovalAuditEventKind.L3_CONFIRMATION_EXPIRED,
        ApprovalAuditEventKind.L3_CONFIRMATION_INVALIDATED,
        ApprovalAuditEventKind.L3_CONFIRMATION_CONSUMED,
        ApprovalAuditEventKind.ATTEMPT_CLOSED,
    )
