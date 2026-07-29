"""Strict immutable models for the versioned Tool Protocol."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializeAsAny,
    field_validator,
    model_validator,
)

type ToolId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
]
type SemanticVersion = Annotated[
    str,
    Field(
        max_length=64,
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        ),
    ),
]
type HashDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type BoundedIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
    ),
]
type FieldName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$"),
]

_STRICT_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)
_EXECUTION_APPROVAL_BINDINGS = ("plan_hash", "arguments", "expiration")
_TOOL_RESULT_ENVELOPE_FIELDS = frozenset(
    {
        "invocation_id",
        "plan_step_id",
        "tool_id",
        "tool_version",
        "contract_hash",
        "arguments_hash",
        "target",
        "success",
        "data",
        "evidence",
        "error",
        "duration_ms",
    }
)


class RiskLevel(StrEnum):
    """Static Tool risk levels owned by immutable Tool metadata."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class ApprovalImplication(StrEnum):
    """System-derived execution approval implication for one risk level."""

    AUTOMATIC_EXECUTION = "automatic_execution"
    POLICY_CONTROLLED_EXECUTION = "policy_controlled_execution"
    EXPLICIT_HUMAN_APPROVAL = "explicit_human_approval"
    EXPLICIT_HUMAN_APPROVAL_AND_MANUAL_CONFIRMATION = (
        "explicit_human_approval_and_manual_confirmation"
    )


class ApprovalBinding(StrEnum):
    """Execution approval fields to which an approval is cryptographically bound."""

    PLAN_HASH = "plan_hash"
    ARGUMENTS = "arguments"
    EXPIRATION = "expiration"


class SideEffectKind(StrEnum):
    """Bounded side-effect classes understood by deterministic Policy."""

    NONE = "none"
    READ_ONLY = "read_only"
    SERVICE_STATE_CHANGE = "service_state_change"
    CONTAINER_STATE_CHANGE = "container_state_change"
    CONFIGURATION_CHANGE = "configuration_change"
    DELETION = "deletion"


class ToolErrorCategory(StrEnum):
    """Stable high-level categories for redacted Tool errors."""

    VALIDATION = "validation"
    RESOLUTION = "resolution"
    INTEGRITY = "integrity"
    TARGET = "target"
    POLICY_BOUNDARY = "policy_boundary"
    TIMEOUT = "timeout"
    EXECUTION = "execution"
    OUTPUT = "output"
    SAFETY = "safety"
    INTERNAL = "internal"


class RollbackStrategy(StrEnum):
    """Declared rollback boundary for one Tool version."""

    NOT_REQUIRED = "not_required"
    MANUAL = "manual"
    SEPARATE_PLAN = "separate_plan"


class ToolRegistryStatus(StrEnum):
    """Mutable availability status stored outside an immutable Tool contract."""

    DESIGN_ONLY = "design_only"
    REGISTERED = "registered"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class ApprovalRequirement(BaseModel):
    """System-derived approval metadata embedded in a Tool contract."""

    model_config = _STRICT_FROZEN_CONFIG

    derived_from_risk_level: Literal[True]
    implication: ApprovalImplication
    binds: tuple[ApprovalBinding, ...]


class ToolSideEffects(BaseModel):
    """Declared side effects for one bounded Tool operation."""

    model_config = _STRICT_FROZEN_CONFIG

    mutates_remote_state: bool
    kind: SideEffectKind

    @model_validator(mode="after")
    def validate_mutation_class(self) -> Self:
        """Require the mutation flag and side-effect class to agree."""
        read_only_kinds = {SideEffectKind.NONE, SideEffectKind.READ_ONLY}
        if self.mutates_remote_state == (self.kind in read_only_kinds):
            raise ValueError("Tool side-effect kind does not match mutation declaration")
        return self


class ToolTargetScope(BaseModel):
    """Static resource scope that prevents Tool target expansion."""

    model_config = _STRICT_FROZEN_CONFIG

    resource_type: BoundedIdentifier
    maximum_targets: int = Field(ge=1, le=100)
    selector_field: FieldName
    allow_dynamic_expansion: Literal[False]


class RedactionRequirement(BaseModel):
    """Versioned redaction rules that bound retained Tool data."""

    model_config = _STRICT_FROZEN_CONFIG

    profile_id: BoundedIdentifier
    profile_version: SemanticVersion
    input_fields: tuple[FieldName, ...] = ()
    output_fields: tuple[FieldName, ...] = ()
    safe_evidence_fields: tuple[FieldName, ...] = ()
    max_retained_payload_bytes: int = Field(ge=0, le=1_048_576)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> Self:
        """Reject duplicate paths within each redaction field list."""
        field_groups = (
            self.input_fields,
            self.output_fields,
            self.safe_evidence_fields,
        )
        if any(len(fields) != len(set(fields)) for fields in field_groups):
            raise ValueError("Redaction field lists must not contain duplicates")
        return self


class ToolErrorDefinition(BaseModel):
    """One stable error that a Tool contract declares before registration."""

    model_config = _STRICT_FROZEN_CONFIG

    code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    category: ToolErrorCategory
    message: str = Field(min_length=1, max_length=256)
    retryable: bool


class ToolReference(BaseModel):
    """Exact reference to a registered Tool version."""

    model_config = _STRICT_FROZEN_CONFIG

    tool_id: ToolId
    version: SemanticVersion


class VerificationRequirement(BaseModel):
    """Independent verification evidence and exact read-only Tool references."""

    model_config = _STRICT_FROZEN_CONFIG

    required: bool
    evidence_fields: tuple[FieldName, ...] = ()
    tools: tuple[ToolReference, ...] = ()

    @model_validator(mode="after")
    def validate_unique_entries(self) -> Self:
        """Reject duplicate evidence fields and duplicate Tool references."""
        identities = tuple((tool.tool_id, tool.version) for tool in self.tools)
        if len(self.evidence_fields) != len(set(self.evidence_fields)):
            raise ValueError("Verification evidence fields must be unique")
        if len(identities) != len(set(identities)):
            raise ValueError("Verification Tool references must be unique")
        return self


class RollbackRequirement(BaseModel):
    """Explicit rollback availability and separate-plan boundary."""

    model_config = _STRICT_FROZEN_CONFIG

    required: bool
    available: bool
    strategy: RollbackStrategy

    @model_validator(mode="after")
    def validate_strategy(self) -> Self:
        """Require rollback flags and strategy to agree."""
        if not self.required:
            if self.available or self.strategy is not RollbackStrategy.NOT_REQUIRED:
                raise ValueError("Unneeded rollback must use the not_required strategy")
        elif self.strategy is RollbackStrategy.NOT_REQUIRED:
            raise ValueError("Required rollback must declare manual or separate_plan")
        elif self.strategy is RollbackStrategy.SEPARATE_PLAN and not self.available:
            raise ValueError("A separate rollback plan requires an available capability")
        return self


class ReplayFixtureReference(BaseModel):
    """Stable package-relative reference to one sanitized replay fixture."""

    model_config = _STRICT_FROZEN_CONFIG

    fixture_id: BoundedIdentifier
    path: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$",
    )

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        """Reject empty, current-directory, and parent-directory path components."""
        if any(component in {"", ".", ".."} for component in value.split("/")):
            raise ValueError("Replay fixture path must be normalized and relative")
        return value


class ToolContract(BaseModel):
    """Immutable, machine-readable contract for one exact Tool version."""

    model_config = _STRICT_FROZEN_CONFIG

    contract_schema_version: Literal["1"]
    schema_dialect: Literal["https://json-schema.org/draft/2020-12/schema"]
    tool_id: ToolId
    version: SemanticVersion
    implementation_hash: HashDigest | None
    description: str = Field(min_length=1, max_length=512)
    risk_level: RiskLevel
    approval: ApprovalRequirement
    side_effects: ToolSideEffects
    target_scope: ToolTargetScope
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    redaction: RedactionRequirement
    errors: tuple[ToolErrorDefinition, ...] = Field(min_length=1, max_length=64)
    timeout_ms: int = Field(ge=1, le=3_600_000)
    idempotent: bool
    automatic_retry: bool
    verification: VerificationRequirement
    rollback: RollbackRequirement
    replay_fixtures: tuple[ReplayFixtureReference, ...] = Field(
        min_length=1,
        max_length=128,
    )

    @model_validator(mode="after")
    def validate_contract_invariants(self) -> Self:
        """Enforce deterministic approval, schema, retry, and identity invariants."""
        expected_implication = {
            RiskLevel.L0: ApprovalImplication.AUTOMATIC_EXECUTION,
            RiskLevel.L1: ApprovalImplication.POLICY_CONTROLLED_EXECUTION,
            RiskLevel.L2: ApprovalImplication.EXPLICIT_HUMAN_APPROVAL,
            RiskLevel.L3: (ApprovalImplication.EXPLICIT_HUMAN_APPROVAL_AND_MANUAL_CONFIRMATION),
        }[self.risk_level]
        expected_bindings: tuple[str, ...] = (
            () if self.risk_level in {RiskLevel.L0, RiskLevel.L1} else _EXECUTION_APPROVAL_BINDINGS
        )
        actual_bindings = tuple(binding.value for binding in self.approval.binds)
        if self.approval.implication is not expected_implication:
            raise ValueError("Approval implication does not match authoritative risk level")
        if actual_bindings != expected_bindings:
            raise ValueError("Approval bindings do not match authoritative risk level")
        if self.automatic_retry and not self.idempotent:
            raise ValueError("Automatic retry requires an idempotent Tool")

        for field_name, schema in (
            ("input_schema", self.input_schema),
            ("output_schema", self.output_schema),
        ):
            if (
                schema.get("type") != "object"
                or schema.get("additionalProperties") is not False
                or not isinstance(schema.get("properties"), dict)
                or not isinstance(schema.get("$id"), str)
            ):
                raise ValueError(f"{field_name} must be an identified strict object JSON Schema")
            declared_dialect = schema.get("$schema")
            if declared_dialect not in {None, self.schema_dialect}:
                raise ValueError(f"{field_name} declares a mismatched JSON Schema dialect")
        input_properties = cast(dict[str, JsonValue], self.input_schema["properties"])
        input_required = self.input_schema.get("required")
        if (
            self.target_scope.selector_field not in input_properties
            or type(input_required) is not list
            or self.target_scope.selector_field not in input_required
        ):
            raise ValueError("input_schema must require the declared target selector")
        output_properties = cast(dict[str, JsonValue], self.output_schema["properties"])
        output_required = self.output_schema.get("required")
        if (
            set(output_properties) != _TOOL_RESULT_ENVELOPE_FIELDS
            or type(output_required) is not list
            or set(output_required) != _TOOL_RESULT_ENVELOPE_FIELDS
        ):
            raise ValueError("output_schema must describe the complete ToolResult envelope")

        error_codes = tuple(error.code for error in self.errors)
        fixture_ids = tuple(fixture.fixture_id for fixture in self.replay_fixtures)
        if len(error_codes) != len(set(error_codes)):
            raise ValueError("Tool error codes must be unique")
        if len(fixture_ids) != len(set(fixture_ids)):
            raise ValueError("Replay fixture IDs must be unique")
        return self


class ToolMetadata(BaseModel):
    """Read-only Registry projection and sole authority for Tool risk."""

    model_config = _STRICT_FROZEN_CONFIG

    tool_id: ToolId
    version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    description: str = Field(min_length=1, max_length=512)
    risk_level: RiskLevel
    timeout_ms: int = Field(ge=1, le=3_600_000)
    idempotent: bool
    input_schema_id: str = Field(min_length=1, max_length=512)
    output_schema_id: str = Field(min_length=1, max_length=512)
    input_model: str = Field(min_length=1, max_length=256)
    output_model: str = Field(min_length=1, max_length=256)


class ToolRegistryRecord(BaseModel):
    """Mutable-status record binding review evidence to an immutable contract."""

    model_config = _STRICT_FROZEN_CONFIG

    record_schema_version: Literal["1"] = "1"
    tool_id: ToolId
    version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest | None
    status: ToolRegistryStatus
    reviewer: str = Field(min_length=1, max_length=128)
    reviewed_at: datetime
    registered_at: datetime | None

    @model_validator(mode="after")
    def validate_registration(self) -> Self:
        """Require UTC review timestamps and complete registered bindings."""
        timestamps = tuple(
            timestamp
            for timestamp in (self.reviewed_at, self.registered_at)
            if timestamp is not None
        )
        if any(timestamp.utcoffset() != timedelta(0) for timestamp in timestamps):
            raise ValueError("Tool Registry timestamps must use UTC")
        if self.status is ToolRegistryStatus.REGISTERED and (
            self.implementation_hash is None or self.registered_at is None
        ):
            raise ValueError("Registered Tools require implementation hash and timestamp")
        return self


class TargetReference(BaseModel):
    """Opaque structured target identity with no connection or credential fields."""

    model_config = _STRICT_FROZEN_CONFIG

    target_id: BoundedIdentifier
    resource_type: BoundedIdentifier
    resource_id: BoundedIdentifier


class ToolCall[ArgumentsT: BaseModel](BaseModel):
    """Exact hash-bound invocation request sent from Executor to Tool Gateway."""

    model_config = _STRICT_FROZEN_CONFIG

    invocation_id: UUID
    plan_step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    arguments_hash: HashDigest
    target: TargetReference
    arguments: ArgumentsT


class ToolError(BaseModel):
    """Stable structured Tool error containing only a sanitized message."""

    model_config = _STRICT_FROZEN_CONFIG

    code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    category: ToolErrorCategory
    message: str = Field(min_length=1, max_length=256)
    retryable: bool


class ToolResult[PayloadT: BaseModel](BaseModel):
    """Gateway-owned structured outcome with typed success data or one safe error."""

    model_config = _STRICT_FROZEN_CONFIG

    invocation_id: UUID
    plan_step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    arguments_hash: HashDigest
    target: TargetReference
    success: bool
    data: SerializeAsAny[PayloadT] | None
    evidence: dict[str, JsonValue] = Field(default_factory=dict, max_length=128)
    error: ToolError | None
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_success_error_exclusivity(self) -> Self:
        """Require typed data only on success and a structured error only on failure."""
        if self.success:
            if self.data is None or self.error is not None:
                raise ValueError("Successful ToolResult requires data and forbids error")
        elif self.data is not None or self.error is None:
            raise ValueError("Failed ToolResult requires error and forbids data")
        return self


__all__ = [
    "ApprovalBinding",
    "ApprovalImplication",
    "ApprovalRequirement",
    "BoundedIdentifier",
    "FieldName",
    "HashDigest",
    "RedactionRequirement",
    "ReplayFixtureReference",
    "RiskLevel",
    "RollbackRequirement",
    "RollbackStrategy",
    "SemanticVersion",
    "SideEffectKind",
    "TargetReference",
    "ToolCall",
    "ToolContract",
    "ToolError",
    "ToolErrorCategory",
    "ToolErrorDefinition",
    "ToolId",
    "ToolMetadata",
    "ToolReference",
    "ToolRegistryRecord",
    "ToolRegistryStatus",
    "ToolResult",
    "ToolSideEffects",
    "ToolTargetScope",
    "VerificationRequirement",
]
