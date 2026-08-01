"""Strict models for deterministic verification criteria and outcomes."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ai_server.models.tool import (
    HashDigest,
    SemanticVersion,
    TargetReference,
    ToolId,
)
from ai_server.tools.hashing import canonical_json_sha256

_STRICT_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)

type CriterionId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
]
type StepId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
]
type UtilizationPercent = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


def _exact_utc(value: datetime, field_name: str) -> datetime:
    """Return a normalized built-in UTC datetime or reject the value."""
    try:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
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
    except BaseException:
        raise ValueError(f"{field_name} must be an exact timezone-aware UTC datetime") from None


class _CriterionBase(BaseModel):
    """Common immutable fields shared by every deterministic criterion."""

    model_config = _STRICT_FROZEN_CONFIG

    criterion_id: CriterionId
    evidence_step_id: StepId
    mandatory: Literal[True] = True
    maximum_age_ms: int = Field(default=30_000, ge=0, le=30_000)
    evaluator_version: Literal["1"] = "1"


class EqualityCriterion(_CriterionBase):
    """Require one bounded scalar evidence field to equal an expected value."""

    kind: Literal["equals"] = "equals"
    source: Literal["data", "evidence"]
    field: Literal["source", "simulated", "target", "hostname"]
    expected: Annotated[str, Field(max_length=128)] | bool

    @field_validator("expected", mode="before")
    @classmethod
    def validate_exact_expected(cls, value: object) -> str | bool:
        """Reject coercion and subclasses for equality expected values."""
        if type(value) is str:
            return value
        if type(value) is bool:
            return value
        raise ValueError("Equality expected value must be an exact string or boolean")


class NumericBoundsCriterion(_CriterionBase):
    """Require one utilization value to remain within inclusive bounds."""

    kind: Literal["numeric_bounds"] = "numeric_bounds"
    field: Literal["cpu_percent", "memory_percent", "disk_percent"]
    minimum: UtilizationPercent
    maximum: UtilizationPercent

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        """Require at least one finite bound and a non-inverted interval."""
        if not isfinite(self.minimum) or not isfinite(self.maximum):
            raise ValueError("Numeric bounds must be finite")
        if self.minimum > self.maximum:
            raise ValueError("Numeric minimum cannot exceed maximum")
        return self


class ExpectedStateCriterion(_CriterionBase):
    """Require one exact service to have the declared bounded state."""

    kind: Literal["expected_state"] = "expected_state"
    service_name: str = Field(min_length=1, max_length=128)
    expected_state: Literal["running", "stopped"]


class HealthStatusCriterion(_CriterionBase):
    """Require aggregate service and utilization health to match an expectation."""

    kind: Literal["health_status"] = "health_status"
    expected_status: Literal["healthy", "unhealthy"]
    maximum_utilization_percent: UtilizationPercent


type VerificationCriterion = Annotated[
    EqualityCriterion | NumericBoundsCriterion | ExpectedStateCriterion | HealthStatusCriterion,
    Field(discriminator="kind"),
]

VERIFICATION_CRITERION_TYPES: tuple[type[BaseModel], ...] = (
    EqualityCriterion,
    NumericBoundsCriterion,
    ExpectedStateCriterion,
    HealthStatusCriterion,
)


class VerificationContext(BaseModel):
    """Exact execution and time bindings supplied to the pure Verifier."""

    model_config = _STRICT_FROZEN_CONFIG

    context_schema_version: Literal["1"] = "1"
    task_id: UUID
    plan_id: UUID
    plan_digest: HashDigest
    execution_attempt_id: UUID
    execution_report_hash: HashDigest
    evidence_accepted_at: datetime | None = None
    evaluated_at: datetime
    collection_duration_ms: int = Field(ge=0, le=3_600_000)
    mutating_effect_pending: bool

    @field_validator("evidence_accepted_at", "evaluated_at")
    @classmethod
    def validate_utc_timestamp(
        cls,
        value: datetime | None,
        info: ValidationInfo,
    ) -> datetime | None:
        """Require every supplied timestamp to be an exact built-in UTC datetime."""
        if value is None:
            return None
        return _exact_utc(value, info.field_name or "timestamp")

    @model_validator(mode="after")
    def validate_timestamp_order(self) -> Self:
        """Reject evidence that claims to have been accepted in the future."""
        if self.evidence_accepted_at is not None and self.evidence_accepted_at > self.evaluated_at:
            raise ValueError("evidence_accepted_at cannot be later than evaluated_at")
        return self


class VerificationStatus(StrEnum):
    """Terminal deterministic verification outcomes."""

    PASSED = "PASSED"
    FAILED = "FAILED"


class VerificationFailureReason(StrEnum):
    """Stable, non-secret reasons why verification failed closed."""

    MALFORMED_PLAN = "MALFORMED_PLAN"
    MALFORMED_EVIDENCE = "MALFORMED_EVIDENCE"
    PLAN_BINDING_MISMATCH = "PLAN_BINDING_MISMATCH"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    EXTRA_EVIDENCE = "EXTRA_EVIDENCE"
    EVIDENCE_ORDER_MISMATCH = "EVIDENCE_ORDER_MISMATCH"
    EVIDENCE_IDENTITY_MISMATCH = "EVIDENCE_IDENTITY_MISMATCH"
    TOOL_VERSION_MISMATCH = "TOOL_VERSION_MISMATCH"
    TARGET_MISMATCH = "TARGET_MISMATCH"
    DUPLICATE_INVOCATION_ID = "DUPLICATE_INVOCATION_ID"
    UNSUCCESSFUL_TOOL_RESULT = "UNSUCCESSFUL_TOOL_RESULT"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    CRITERION_EVIDENCE_MISSING = "CRITERION_EVIDENCE_MISSING"
    CRITERION_MISMATCH = "CRITERION_MISMATCH"
    VERIFIER_RESULT_INVALID = "VERIFIER_RESULT_INVALID"
    VERIFIER_FAILED = "VERIFIER_FAILED"
    CLOCK_UNAVAILABLE = "CLOCK_UNAVAILABLE"


class VerificationCheckStatus(StrEnum):
    """One deterministic criterion outcome."""

    PASSED = "PASSED"
    FAILED = "FAILED"


class VerificationEffectDisposition(StrEnum):
    """Post-verification knowledge about a pending external effect."""

    NONE = "NONE"
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"


class VerificationEvidenceReference(BaseModel):
    """Hash-only provenance for one evaluated ToolResult."""

    model_config = _STRICT_FROZEN_CONFIG

    step_index: int = Field(ge=0, le=63)
    step_id: StepId
    invocation_id: UUID
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    arguments_hash: HashDigest
    target: TargetReference
    result_hash: HashDigest
    accepted_at: datetime | None

    @field_validator("accepted_at")
    @classmethod
    def validate_accepted_at(cls, value: datetime | None) -> datetime | None:
        """Require an optional evidence acceptance timestamp to use exact UTC."""
        if value is None:
            return None
        return _exact_utc(value, "accepted_at")

    @model_validator(mode="after")
    def validate_exact_target(self) -> Self:
        """Reject target subclasses at the persisted verification boundary."""
        if type(self.target) is not TargetReference:
            raise ValueError("Verification evidence target must be exact")
        return self


class VerificationCheckResult(BaseModel):
    """One criterion decision without retaining its observed raw value."""

    model_config = _STRICT_FROZEN_CONFIG

    criterion_id: CriterionId
    evidence_step_id: StepId
    evaluator_version: Literal["1"]
    status: VerificationCheckStatus
    failure_reason: VerificationFailureReason | None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """Require passed checks to omit failure reasons and failed checks to include one."""
        if (self.status is VerificationCheckStatus.PASSED) == (self.failure_reason is not None):
            raise ValueError("Check outcome and failure reason are inconsistent")
        return self


class VerificationResult(BaseModel):
    """Immutable hash-bound result of deterministic evidence evaluation."""

    model_config = _STRICT_FROZEN_CONFIG

    result_schema_version: Literal["1"] = "1"
    task_id: UUID
    plan_id: UUID
    plan_digest: HashDigest
    execution_attempt_id: UUID
    execution_report_hash: HashDigest
    evaluated_at: datetime
    status: VerificationStatus
    checks: tuple[VerificationCheckResult, ...] = Field(max_length=128)
    evidence_references: tuple[VerificationEvidenceReference, ...] = Field(max_length=64)
    failure_reasons: tuple[VerificationFailureReason, ...] = Field(max_length=32)
    effect_disposition: VerificationEffectDisposition
    human_intervention_required: bool
    content_hash: HashDigest

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        """Require the evaluation timestamp to use exact UTC."""
        return _exact_utc(value, "evaluated_at")

    @model_validator(mode="after")
    def validate_result_consistency(self) -> Self:
        """Require status, checks, effect knowledge, and content hash to agree."""
        if (
            type(self.checks) is not tuple
            or type(self.evidence_references) is not tuple
            or type(self.failure_reasons) is not tuple
            or any(type(check) is not VerificationCheckResult for check in self.checks)
            or any(
                type(reference) is not VerificationEvidenceReference
                for reference in self.evidence_references
            )
        ):
            raise ValueError("Verification result contains malformed nested records")
        if len(self.failure_reasons) != len(set(self.failure_reasons)):
            raise ValueError("Verification failure reasons must be unique")
        criterion_ids = tuple(check.criterion_id for check in self.checks)
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("Verification checks must identify unique criteria")
        reference_indexes = tuple(reference.step_index for reference in self.evidence_references)
        if tuple(sorted(reference_indexes)) != reference_indexes or len(reference_indexes) != len(
            set(reference_indexes)
        ):
            raise ValueError("Verification evidence references must be ordered and unique")
        check_reasons = tuple(
            dict.fromkeys(
                check.failure_reason for check in self.checks if check.failure_reason is not None
            )
        )
        if self.checks and check_reasons != self.failure_reasons:
            raise ValueError("Verification reasons must match ordered failed checks")
        if self.status is VerificationStatus.PASSED:
            if (
                not self.checks
                or not self.evidence_references
                or any(check.status is not VerificationCheckStatus.PASSED for check in self.checks)
                or any(reference.accepted_at is None for reference in self.evidence_references)
                or self.failure_reasons
                or self.effect_disposition is VerificationEffectDisposition.UNKNOWN
                or self.human_intervention_required
            ):
                raise ValueError("Passed verification result is inconsistent")
        elif (
            not self.failure_reasons
            or (
                bool(self.checks)
                and not any(check.status is VerificationCheckStatus.FAILED for check in self.checks)
            )
            or self.effect_disposition is VerificationEffectDisposition.VERIFIED
            or self.human_intervention_required
            is not (self.effect_disposition is VerificationEffectDisposition.UNKNOWN)
        ):
            raise ValueError("Failed verification result is inconsistent")
        if self.content_hash != canonical_json_sha256(
            self.model_dump(mode="json", exclude={"content_hash"}, warnings="error")
        ):
            raise ValueError("Verification result content hash is invalid")
        return self


__all__ = [
    "EqualityCriterion",
    "ExpectedStateCriterion",
    "HealthStatusCriterion",
    "NumericBoundsCriterion",
    "VERIFICATION_CRITERION_TYPES",
    "VerificationCheckStatus",
    "VerificationContext",
    "VerificationCriterion",
    "VerificationEffectDisposition",
    "VerificationEvidenceReference",
    "VerificationFailureReason",
    "VerificationCheckResult",
    "VerificationResult",
    "VerificationStatus",
]
