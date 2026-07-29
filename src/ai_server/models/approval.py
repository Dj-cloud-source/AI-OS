"""Strict immutable contracts for process-local execution approval."""

import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Self, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from ai_server.models.execution import StepRole
from ai_server.models.policy import (
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
)
from ai_server.models.tool import (
    BoundedIdentifier,
    HashDigest,
    RedactionRequirement,
    RiskLevel,
    RollbackRequirement,
    SemanticVersion,
    TargetReference,
    ToolId,
    ToolSideEffects,
    ToolTargetScope,
    VerificationRequirement,
)
from ai_server.tools.hashing import CanonicalizationError, canonical_json_sha256

_STRICT_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)
_ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    ["']?
    ([a-z][a-z0-9_-]{1,64})
    ["']?
    \s*[:=]\s*
    ["']?
    ([^\s,;}]+)
    """
)
_CREDENTIAL_URI_PATTERN = re.compile(r"(?i)[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
_SENSITIVE_KEYS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "authtoken",
        "authorization",
        "awssecretaccesskey",
        "bearertoken",
        "clientsecret",
        "connectionstring",
        "cookie",
        "credential",
        "credentials",
        "databaseurl",
        "dsn",
        "githubtoken",
        "idtoken",
        "passphrase",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
        "sessionid",
        "sessiontoken",
        "sshkey",
        "token",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "accesskey",
    "apikey",
    "cookie",
    "credential",
    "credentials",
    "passphrase",
    "password",
    "privatekey",
    "secret",
    "secretkey",
    "token",
)


class ApprovalValidationVerdict(StrEnum):
    """Structured usability states returned by the Approval Engine."""

    VALID_UNCONSUMED = "VALID_UNCONSUMED"
    VALID_FOR_BOUND_ATTEMPT = "VALID_FOR_BOUND_ATTEMPT"
    INVALID = "INVALID"


class ApprovalValidationReason(StrEnum):
    """Stable, non-sensitive Approval validation reasons."""

    VALID_UNCONSUMED = "valid_unconsumed"
    VALID_FOR_BOUND_ATTEMPT = "valid_for_bound_attempt"
    UNKNOWN_APPROVAL = "unknown_approval"
    UNKNOWN_CONFIRMATION = "unknown_confirmation"
    POLICY_DENIED = "policy_denied"
    POLICY_MISMATCH = "policy_mismatch"
    WRONG_APPROVAL_MODE = "wrong_approval_mode"
    PLAN_HASH_MISMATCH = "plan_hash_mismatch"
    PLAN_SNAPSHOT_MISMATCH = "plan_snapshot_mismatch"
    OPERATOR_MISMATCH = "operator_mismatch"
    TARGET_MISMATCH = "target_mismatch"
    TOOL_INTEGRITY_MISMATCH = "tool_integrity_mismatch"
    ARGUMENTS_MISMATCH = "arguments_mismatch"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_INVALIDATED = "approval_invalidated"
    APPROVAL_NOT_CONSUMED = "approval_not_consumed"
    APPROVAL_ALREADY_CONSUMED = "approval_already_consumed"
    EXECUTION_ATTEMPT_MISMATCH = "execution_attempt_mismatch"
    EXECUTION_ATTEMPT_CLOSED = "execution_attempt_closed"
    INVALID_CLOCK = "invalid_clock"
    CONFIRMATION_MISSING = "confirmation_missing"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    CONFIRMATION_ALREADY_CONSUMED = "confirmation_already_consumed"


class ApprovalAuditEventKind(StrEnum):
    """Append-only process-local Approval audit event kinds."""

    REVIEW_PREPARED = "REVIEW_PREPARED"
    PLAN_APPROVAL_ISSUED = "PLAN_APPROVAL_ISSUED"
    PLAN_APPROVAL_REJECTED = "PLAN_APPROVAL_REJECTED"
    PLAN_APPROVAL_INVALIDATED = "PLAN_APPROVAL_INVALIDATED"
    PLAN_APPROVAL_EXPIRED = "PLAN_APPROVAL_EXPIRED"
    PLAN_APPROVAL_CONSUMED = "PLAN_APPROVAL_CONSUMED"
    L3_CONFIRMATION_ISSUED = "L3_CONFIRMATION_ISSUED"
    L3_CONFIRMATION_EXPIRED = "L3_CONFIRMATION_EXPIRED"
    L3_CONFIRMATION_INVALIDATED = "L3_CONFIRMATION_INVALIDATED"
    L3_CONFIRMATION_CONSUMED = "L3_CONFIRMATION_CONSUMED"
    ATTEMPT_CLOSED = "ATTEMPT_CLOSED"


class ApprovalInvalidationReason(StrEnum):
    """Bounded reasons that may permanently invalidate authorization."""

    REVOKED_BY_APPROVER = "revoked_by_approver"
    PLAN_CHANGED = "plan_changed"
    POLICY_CHANGED = "policy_changed"
    SECURITY_CONDITION_CHANGED = "security_condition_changed"


def _require_utc(value: datetime) -> datetime:
    """Require an exact timezone-aware UTC offset without normalizing input."""
    try:
        if value.utcoffset() != timedelta(0):
            raise ValueError
    except BaseException:
        raise ValueError("Approval timestamps must use timezone-aware UTC") from None
    return value


def _require_exact_uuid(value: object) -> UUID:
    """Reject coercion and UUID subclasses at authorization boundaries."""
    if type(value) is not UUID:
        raise ValueError("Approval identifiers must be exact UUID values")
    return value


def _require_exact_utc_datetime(value: object) -> datetime:
    """Reject coercion and datetime subclasses, then require UTC."""
    if type(value) is not datetime:
        raise ValueError("Approval timestamps must be exact datetime values")
    return _require_utc(value)


ExactUUID = Annotated[UUID, BeforeValidator(_require_exact_uuid)]
ExactUtcDateTime = Annotated[datetime, BeforeValidator(_require_exact_utc_datetime)]


def _content_hash(model: BaseModel, field_name: str) -> str:
    """Hash a model while excluding its self-referential content-hash field."""
    try:
        return canonical_json_sha256(
            model.model_dump(mode="json", exclude={field_name}, warnings="error")
        )
    except (CanonicalizationError, TypeError, ValueError):
        raise ValueError("Approval content cannot be canonicalized safely") from None


def _freeze_json(value: JsonValue) -> JsonValue:
    """Recursively replace mutable JSON containers with immutable equivalents."""
    if type(value) is dict:
        return cast(
            JsonValue,
            MappingProxyType({key: _freeze_json(nested) for key, nested in value.items()}),
        )
    if type(value) is list:
        return cast(JsonValue, tuple(_freeze_json(nested) for nested in value))
    return value


def _thaw_json(value: object) -> JsonValue:
    """Return fresh built-in JSON containers for hashing and serialization."""
    if isinstance(value, Mapping):
        return {
            str(key): _thaw_json(nested)
            for key, nested in cast(Mapping[object, object], value).items()
        }
    if isinstance(value, tuple):
        return [_thaw_json(nested) for nested in value]
    if value is None or type(value) in {bool, int, float, str}:
        return cast(JsonValue, value)
    raise ValueError("Approval arguments contain an unsupported value")


def _is_safe_review_text(value: str) -> bool:
    """Reject terminal controls and obvious credential material from human review."""
    if any(unicodedata.category(character).startswith("C") for character in value):
        return False
    normalized = value.upper()
    if "-----BEGIN" in normalized and "PRIVATE KEY-----" in normalized:
        return False
    if _CREDENTIAL_URI_PATTERN.search(value.strip("\"' ")):
        return False
    for match in _ASSIGNMENT_PATTERN.finditer(value):
        key, assigned_value = match.groups()
        if _is_sensitive_key(key) or _CREDENTIAL_URI_PATTERN.search(assigned_value.strip("\"'")):
            return False
    return True


def _is_sensitive_key(value: object) -> bool:
    """Classify a normalized assignment or argument key without inspecting its value."""
    if type(value) is not str:
        return False
    normalized = "".join(character for character in value.lower() if character.isalnum())
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _contains_sensitive_json(value: object) -> bool:
    """Detect obvious credential keys and private-key material in review arguments."""
    if isinstance(value, Mapping):
        for key, nested in cast(Mapping[object, object], value).items():
            if _is_sensitive_key(key):
                return True
            if _contains_sensitive_json(nested):
                return True
        return False
    if isinstance(value, tuple):
        return any(_contains_sensitive_json(nested) for nested in value)
    if type(value) is str:
        return not _is_safe_review_text(value)
    return False


class PlanStepApprovalSnapshot(BaseModel):
    """One behavior-complete, safely reviewable Plan step snapshot."""

    model_config = _STRICT_FROZEN_CONFIG

    step_index: int = Field(ge=0, le=63)
    step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    role: StepRole
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    arguments: Mapping[str, JsonValue]
    arguments_hash: HashDigest
    target: TargetReference
    target_scope: ToolTargetScope
    side_effects: ToolSideEffects
    registry_risk_level: RiskLevel
    registry_redaction: RedactionRequirement
    registry_verification: VerificationRequirement
    registry_rollback: RollbackRequirement
    reason: str = Field(min_length=1, max_length=4096)
    impact: str = Field(min_length=1, max_length=4096)
    verification: str = Field(min_length=1, max_length=4096)
    recovery: str = Field(min_length=1, max_length=4096)
    skill_provenance: None = None
    limitations: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("arguments", mode="after")
    @classmethod
    def freeze_arguments(
        cls,
        value: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        """Deep-freeze exact review arguments before exposing the snapshot."""
        return cast(Mapping[str, JsonValue], _freeze_json(dict(value)))

    @field_serializer("arguments")
    def serialize_arguments(self, value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        """Serialize immutable arguments as fresh built-in JSON containers."""
        thawed = _thaw_json(value)
        if type(thawed) is not dict:
            raise ValueError("Approval arguments must serialize as an object")
        return thawed

    @model_validator(mode="after")
    def validate_step_snapshot(self) -> Self:
        """Bind exact arguments and reject duplicate or empty limitations."""
        try:
            expected_arguments_hash = canonical_json_sha256(_thaw_json(self.arguments))
        except (CanonicalizationError, ValueError):
            raise ValueError("Approval arguments are not canonicalizable") from None
        if _contains_sensitive_json(self.arguments):
            raise ValueError("Approval arguments are not safe to display")
        if self.arguments_hash != expected_arguments_hash:
            raise ValueError("Approval arguments hash does not match exact arguments")
        if any(not limitation or len(limitation) > 1024 for limitation in self.limitations):
            raise ValueError("Approval limitations must be bounded non-empty strings")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("Approval limitations must be unique")
        review_text = (
            self.reason,
            self.impact,
            self.verification,
            self.recovery,
            *self.limitations,
        )
        if any(not _is_safe_review_text(value) for value in review_text):
            raise ValueError("Approval review text is not safe to display")
        return self


class PlanApprovalSnapshot(BaseModel):
    """The sole canonical Plan Hash input used for execution approval."""

    model_config = _STRICT_FROZEN_CONFIG

    snapshot_schema_version: Literal["1"] = "1"
    plan_schema_version: Literal["1"]
    task_id: ExactUUID
    plan_id: ExactUUID
    operator_id: BoundedIdentifier
    target: TargetReference
    execution_order: tuple[str, ...] = Field(min_length=1, max_length=64)
    steps: tuple[PlanStepApprovalSnapshot, ...] = Field(min_length=1, max_length=64)
    skill_provenance: None = None
    limitations: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_snapshot_order(self) -> Self:
        """Require an explicit one-to-one ordered Step commitment."""
        step_ids = tuple(step.step_id for step in self.steps)
        if self.execution_order != step_ids:
            raise ValueError("Approval execution order must match the ordered steps")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Approval step IDs must be unique")
        if tuple(step.step_index for step in self.steps) != tuple(range(len(self.steps))):
            raise ValueError("Approval step indexes must be contiguous and ordered")
        if any(not limitation or len(limitation) > 1024 for limitation in self.limitations):
            raise ValueError("Approval limitations must be bounded non-empty strings")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("Approval limitations must be unique")
        if any(not _is_safe_review_text(value) for value in self.limitations):
            raise ValueError("Approval limitations are not safe to display")
        return self


class ApprovalStepBinding(BaseModel):
    """Non-secret ordered commitment retained by an Approval Record."""

    model_config = _STRICT_FROZEN_CONFIG

    step_index: int = Field(ge=0, le=63)
    step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    role: StepRole
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    arguments_hash: HashDigest
    target: TargetReference


class ApprovalReview(BaseModel):
    """A short-lived exact Plan review retained by one Approval Engine."""

    model_config = _STRICT_FROZEN_CONFIG

    review_schema_version: Literal["1"] = "1"
    review_id: ExactUUID
    task_id: ExactUUID
    plan_id: ExactUUID
    plan_hash: HashDigest
    policy_id: BoundedIdentifier
    policy_version: SemanticVersion
    policy_hash: HashDigest
    policy_decision_hash: HashDigest
    operator_id: BoundedIdentifier
    target: TargetReference
    effective_risk: RiskLevel
    approval_requirement: PolicyApprovalRequirement
    manual_confirmation_requirement: ManualConfirmationRequirement
    prepared_at: ExactUtcDateTime
    expires_at: ExactUtcDateTime
    snapshot: PlanApprovalSnapshot

    @model_validator(mode="after")
    def validate_review_binding(self) -> Self:
        """Bind the Review to one exact canonical snapshot and Policy decision."""
        if self.expires_at <= self.prepared_at:
            raise ValueError("Approval Review expiration must follow preparation")
        if self.approval_requirement is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL:
            raise ValueError("Approval Review requires human plan approval")
        if (
            self.task_id != self.snapshot.task_id
            or self.plan_id != self.snapshot.plan_id
            or self.operator_id != self.snapshot.operator_id
            or self.target != self.snapshot.target
            or self.plan_hash != canonical_json_sha256(self.snapshot)
        ):
            raise ValueError("Approval Review does not match its canonical snapshot")
        return self


class ApprovalRecord(BaseModel):
    """Immutable human authorization issued for one exact Plan snapshot."""

    model_config = _STRICT_FROZEN_CONFIG

    record_schema_version: Literal["1"] = "1"
    approval_id: ExactUUID
    review_id: ExactUUID
    decision: Literal["APPROVED"] = "APPROVED"
    task_id: ExactUUID
    plan_id: ExactUUID
    plan_hash: HashDigest
    policy_id: BoundedIdentifier
    policy_version: SemanticVersion
    policy_hash: HashDigest
    policy_decision_hash: HashDigest
    operator_id: BoundedIdentifier
    target: TargetReference
    effective_risk: RiskLevel
    approval_requirement: PolicyApprovalRequirement
    manual_confirmation_requirement: ManualConfirmationRequirement
    approver: Literal["local-owner"]
    reviewed_at: ExactUtcDateTime
    issued_at: ExactUtcDateTime
    expires_at: ExactUtcDateTime
    steps: tuple[ApprovalStepBinding, ...] = Field(min_length=1, max_length=64)
    content_hash: HashDigest

    @model_validator(mode="after")
    def validate_record_binding(self) -> Self:
        """Require ordered commitments, valid times, mode, and content hash."""
        if not (self.reviewed_at <= self.issued_at < self.expires_at):
            raise ValueError("Approval timestamps are not ordered")
        if self.approval_requirement is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL:
            raise ValueError("Approval Record requires human plan approval")
        if tuple(step.step_index for step in self.steps) != tuple(range(len(self.steps))):
            raise ValueError("Approval step bindings must be contiguous and ordered")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("Approval step bindings must be unique")
        if self.content_hash != _content_hash(self, "content_hash"):
            raise ValueError("Approval Record content hash is invalid")
        return self


class ManualConfirmationRecord(BaseModel):
    """One immediate, single-use human confirmation for an exact L3 invocation."""

    model_config = _STRICT_FROZEN_CONFIG

    confirmation_schema_version: Literal["1"] = "1"
    confirmation_id: ExactUUID
    approval_id: ExactUUID
    approval_record_hash: HashDigest
    task_id: ExactUUID
    plan_id: ExactUUID
    plan_hash: HashDigest
    policy_decision_hash: HashDigest
    execution_attempt_id: ExactUUID
    invocation_id: ExactUUID
    step_index: int = Field(ge=0, le=63)
    step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    role: StepRole
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    arguments_hash: HashDigest
    target: TargetReference
    confirmer: Literal["local-owner"]
    issued_at: ExactUtcDateTime
    expires_at: ExactUtcDateTime
    content_hash: HashDigest

    @model_validator(mode="after")
    def validate_confirmation_binding(self) -> Self:
        """Require an L3 Action commitment, valid lifetime, and content hash."""
        if self.expires_at <= self.issued_at:
            raise ValueError("Confirmation expiration must follow issuance")
        if self.role is not StepRole.ACTION:
            raise ValueError("L3 confirmation requires an ACTION step")
        if self.content_hash != _content_hash(self, "content_hash"):
            raise ValueError("Confirmation Record content hash is invalid")
        return self


class ApprovalValidationResult(BaseModel):
    """Structured fail-closed result of an Approval or Confirmation check."""

    model_config = _STRICT_FROZEN_CONFIG

    validation_schema_version: Literal["1"] = "1"
    verdict: ApprovalValidationVerdict
    reason: ApprovalValidationReason
    checked_at: ExactUtcDateTime | None
    approval_id: ExactUUID | None = None
    confirmation_id: ExactUUID | None = None
    plan_hash: HashDigest | None = None
    execution_attempt_id: ExactUUID | None = None

    @model_validator(mode="after")
    def validate_verdict_reason(self) -> Self:
        """Require each successful verdict to use its one stable reason."""
        expected_reason = {
            ApprovalValidationVerdict.VALID_UNCONSUMED: (ApprovalValidationReason.VALID_UNCONSUMED),
            ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT: (
                ApprovalValidationReason.VALID_FOR_BOUND_ATTEMPT
            ),
        }
        if self.verdict in expected_reason and self.reason is not expected_reason[self.verdict]:
            raise ValueError("Valid Approval verdict uses an inconsistent reason")
        if self.verdict is ApprovalValidationVerdict.INVALID and self.reason in {
            ApprovalValidationReason.VALID_UNCONSUMED,
            ApprovalValidationReason.VALID_FOR_BOUND_ATTEMPT,
        }:
            raise ValueError("Invalid Approval verdict cannot use a valid reason")
        if (self.checked_at is None) != (self.reason is ApprovalValidationReason.INVALID_CLOCK):
            raise ValueError("Only an invalid clock may omit the validation timestamp")
        return self


class ApprovalAuditEvent(BaseModel):
    """One append-only, non-secret fact in the process-local Approval ledger."""

    model_config = _STRICT_FROZEN_CONFIG

    event_schema_version: Literal["1"] = "1"
    event_id: ExactUUID
    sequence: int = Field(ge=0)
    kind: ApprovalAuditEventKind
    occurred_at: ExactUtcDateTime
    review_id: ExactUUID | None = None
    approval_id: ExactUUID | None = None
    confirmation_id: ExactUUID | None = None
    task_id: ExactUUID | None = None
    plan_id: ExactUUID | None = None
    plan_hash: HashDigest | None = None
    policy_id: BoundedIdentifier | None = None
    policy_version: SemanticVersion | None = None
    policy_hash: HashDigest | None = None
    policy_decision_hash: HashDigest | None = None
    operator_id: BoundedIdentifier | None = None
    target: TargetReference | None = None
    effective_risk: RiskLevel | None = None
    approval_requirement: PolicyApprovalRequirement | None = None
    manual_confirmation_requirement: ManualConfirmationRequirement | None = None
    actor: Literal["local-owner"] | None = None
    approval_record_hash: HashDigest | None = None
    confirmation_record_hash: HashDigest | None = None
    expires_at: ExactUtcDateTime | None = None
    step_bindings: tuple[ApprovalStepBinding, ...] = Field(default=(), max_length=64)
    execution_attempt_id: ExactUUID | None = None
    invocation_id: ExactUUID | None = None
    step_index: int | None = Field(default=None, ge=0, le=63)
    step_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    tool_id: ToolId | None = None
    tool_version: SemanticVersion | None = None
    contract_hash: HashDigest | None = None
    implementation_hash: HashDigest | None = None
    arguments_hash: HashDigest | None = None
    reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @model_validator(mode="after")
    def validate_audit_commitments(self) -> Self:
        """Require ordered unique step commitments without retaining raw arguments."""
        indexes = tuple(binding.step_index for binding in self.step_bindings)
        if indexes != tuple(sorted(indexes)) or len(indexes) != len(set(indexes)):
            raise ValueError("Approval audit step bindings must be unique and ordered")
        if len({binding.step_id for binding in self.step_bindings}) != len(self.step_bindings):
            raise ValueError("Approval audit step IDs must be unique")
        return self


__all__ = [
    "ApprovalAuditEvent",
    "ApprovalAuditEventKind",
    "ApprovalInvalidationReason",
    "ApprovalRecord",
    "ApprovalReview",
    "ApprovalStepBinding",
    "ApprovalValidationReason",
    "ApprovalValidationResult",
    "ApprovalValidationVerdict",
    "ManualConfirmationRecord",
    "PlanApprovalSnapshot",
    "PlanStepApprovalSnapshot",
]
