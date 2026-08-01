"""Strict immutable contracts for governed Tool execution attempts."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_server.models.execution import StepRole
from ai_server.models.policy import PolicyApprovalRequirement
from ai_server.models.system_status import SystemStatus
from ai_server.models.tool import (
    HashDigest,
    SemanticVersion,
    TargetReference,
    ToolId,
    ToolResult,
)
from ai_server.tools.hashing import canonical_json_sha256

_STRICT_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


class DispatchStatus(StrEnum):
    """Whether the registered Tool handler definitely started."""

    NOT_DISPATCHED = "NOT_DISPATCHED"
    HANDLER_DISPATCHED = "HANDLER_DISPATCHED"
    UNKNOWN = "UNKNOWN"


class EffectDisposition(StrEnum):
    """Executor knowledge about a Tool invocation's external effect."""

    NONE = "NONE"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    UNKNOWN = "UNKNOWN"


class ExecutionReportStatus(StrEnum):
    """Progress represented by one cumulative execution report."""

    AWAITING_VERIFICATION_DISPATCH = "AWAITING_VERIFICATION_DISPATCH"
    READY_FOR_VERIFIER = "READY_FOR_VERIFIER"
    FAILED = "FAILED"


class ExecutionNextState(StrEnum):
    """The only Runtime state facts an Executor may recommend."""

    VERIFYING = "VERIFYING"
    FAILED = "FAILED"


class ExecutionEventKind(StrEnum):
    """Bounded non-secret execution event kinds."""

    ATTEMPT_AUTHORIZED = "ATTEMPT_AUTHORIZED"
    STEP_FINISHED = "STEP_FINISHED"
    PHASE_READY = "PHASE_READY"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"
    ATTEMPT_CLOSED = "ATTEMPT_CLOSED"


class ExecutionAttemptAuthorization(BaseModel):
    """Process-local authorization receipt for one single-use execution attempt."""

    model_config = _STRICT_FROZEN_CONFIG

    authorization_schema_version: Literal["1"] = "1"
    execution_attempt_id: UUID
    task_id: UUID
    plan_id: UUID
    plan_digest: HashDigest
    policy_decision_hash: HashDigest
    approval_requirement: PolicyApprovalRequirement
    approval_id: UUID | None = None
    approval_plan_hash: HashDigest | None = None
    approval_record_hash: HashDigest | None = None
    approval_expires_at: datetime | None = None
    content_hash: HashDigest

    @field_validator("approval_expires_at")
    @classmethod
    def validate_approval_expiration(cls, value: datetime | None) -> datetime | None:
        """Require an exact timezone-aware UTC Approval expiration."""
        if value is None:
            return None
        try:
            if (
                type(value) is not datetime
                or value.tzinfo is None
                or value.utcoffset() != timedelta(0)
            ):
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
            raise ValueError("approval_expires_at must be timezone-aware UTC") from None

    @model_validator(mode="after")
    def validate_authorization_binding(self) -> Self:
        """Bind human authorization fields and the immutable content hash."""
        approval_fields = (
            self.approval_id,
            self.approval_plan_hash,
            self.approval_record_hash,
            self.approval_expires_at,
        )
        if self.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL:
            if any(value is None for value in approval_fields):
                raise ValueError("Human execution authorization requires exact Approval evidence")
        elif any(value is not None for value in approval_fields):
            raise ValueError("NOT_REQUIRED authorization cannot claim human Approval evidence")
        if self.content_hash != _content_hash(self, "content_hash"):
            raise ValueError("Execution authorization content hash is invalid")
        return self


class ManualConfirmationChallenge(BaseModel):
    """Exact human-visible commitment for one immediate L3 invocation."""

    model_config = _STRICT_FROZEN_CONFIG

    challenge_schema_version: Literal["1"] = "1"
    authorization_hash: HashDigest
    approval_id: UUID
    approval_plan_hash: HashDigest
    approval_record_hash: HashDigest
    approval_expires_at: datetime
    execution_attempt_id: UUID
    invocation_id: UUID
    step_index: int = Field(ge=0, le=63)
    step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    role: Literal[StepRole.ACTION]
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    arguments_hash: HashDigest
    target: TargetReference
    challenge_hash: HashDigest

    @field_validator("approval_expires_at")
    @classmethod
    def validate_expiration(cls, value: datetime) -> datetime:
        """Require a built-in timezone-aware UTC timestamp."""
        try:
            if (
                type(value) is not datetime
                or value.tzinfo is None
                or value.utcoffset() != timedelta(0)
            ):
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
            raise ValueError("approval_expires_at must be timezone-aware UTC") from None

    @model_validator(mode="after")
    def validate_challenge_hash(self) -> Self:
        """Require the displayed Hash to bind every invocation commitment."""
        if self.challenge_hash != _content_hash(self, "challenge_hash"):
            raise ValueError("Manual confirmation Challenge Hash is invalid")
        return self


class StepExecutionRecord(BaseModel):
    """One ordered, redacted invocation fact retained by Executor."""

    model_config = _STRICT_FROZEN_CONFIG

    record_schema_version: Literal["1"] = "1"
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
    invocation_id: UUID
    dispatch_status: DispatchStatus
    effect_disposition: EffectDisposition
    confirmation_id: UUID | None = None
    confirmation_record_hash: HashDigest | None = None
    result: ToolResult[SystemStatus] | None = None
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @model_validator(mode="after")
    def validate_record_consistency(self) -> Self:
        """Require identity, confirmation, result, and effect facts to agree."""
        if (self.confirmation_id is None) != (self.confirmation_record_hash is None):
            raise ValueError("L3 confirmation ID and record Hash must appear together")
        if self.confirmation_id is not None and self.role is not StepRole.ACTION:
            raise ValueError("Only ACTION steps may retain L3 confirmation evidence")
        if self.result is not None:
            if (
                self.result.invocation_id != self.invocation_id
                or self.result.plan_step_id != self.step_id
                or self.result.tool_id != self.tool_id
                or self.result.tool_version != self.tool_version
                or self.result.contract_hash != self.contract_hash
                or self.result.arguments_hash != self.arguments_hash
                or self.result.target != self.target
            ):
                raise ValueError("Execution record result identity is inconsistent")
            if self.result.success:
                expected_failure = None
            elif self.result.error is None:
                raise ValueError("Failed ToolResult requires a structured error")
            else:
                expected_failure = self.result.error.code
            if self.failure_code != expected_failure:
                raise ValueError("Execution record failure code must match its ToolResult")
            if (
                self.result.success
                and self.dispatch_status is not DispatchStatus.HANDLER_DISPATCHED
            ):
                raise ValueError("A successful ToolResult requires definite handler dispatch")
        elif self.failure_code is None:
            raise ValueError("A record without ToolResult requires an explicit failure code")
        if self.dispatch_status is DispatchStatus.NOT_DISPATCHED and (
            self.effect_disposition is not EffectDisposition.NONE
        ):
            raise ValueError("Definite non-dispatch cannot claim a possible external effect")
        if self.dispatch_status is DispatchStatus.UNKNOWN and (
            self.effect_disposition is not EffectDisposition.UNKNOWN
        ):
            raise ValueError("Unknown dispatch requires unknown external-effect certainty")
        if self.effect_disposition is EffectDisposition.PENDING_VERIFICATION and (
            self.dispatch_status is not DispatchStatus.HANDLER_DISPATCHED
            or self.result is None
            or not self.result.success
        ):
            raise ValueError("Pending effect requires a successful dispatched invocation")
        if (
            self.result is not None
            and self.result.success
            and (self.effect_disposition is EffectDisposition.UNKNOWN)
        ):
            raise ValueError("Successful evidence cannot retain unknown effect certainty")
        return self


class ExecutionEvent(BaseModel):
    """One ordered non-secret fact within an execution attempt."""

    model_config = _STRICT_FROZEN_CONFIG

    event_schema_version: Literal["1"] = "1"
    sequence: int = Field(ge=0)
    kind: ExecutionEventKind
    execution_attempt_id: UUID
    step_index: int | None = Field(default=None, ge=0, le=63)
    step_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    invocation_id: UUID | None = None
    dispatch_status: DispatchStatus | None = None
    effect_disposition: EffectDisposition | None = None
    reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @model_validator(mode="after")
    def validate_event_shape(self) -> Self:
        """Require event-specific fields to be complete and non-contradictory."""
        step_fields = (
            self.step_index,
            self.step_id,
            self.invocation_id,
            self.dispatch_status,
            self.effect_disposition,
        )
        if self.kind is ExecutionEventKind.STEP_FINISHED:
            if any(value is None for value in step_fields):
                raise ValueError("STEP_FINISHED requires complete invocation facts")
        elif any(value is not None for value in step_fields):
            raise ValueError("Attempt-level execution events cannot claim Step facts")
        if self.kind is ExecutionEventKind.ATTEMPT_FAILED:
            if self.reason_code is None:
                raise ValueError("ATTEMPT_FAILED requires a reason code")
        elif self.reason_code is not None:
            raise ValueError("Only ATTEMPT_FAILED may include a reason code")
        return self


class ExecutionReport(BaseModel):
    """Cumulative structured outcome from one governed execution attempt."""

    model_config = _STRICT_FROZEN_CONFIG

    report_schema_version: Literal["1"] = "1"
    execution_attempt_id: UUID
    authorization_hash: HashDigest
    task_id: UUID
    plan_id: UUID
    plan_digest: HashDigest
    policy_decision_hash: HashDigest
    approval_id: UUID | None = None
    status: ExecutionReportStatus
    next_state: ExecutionNextState
    records: tuple[StepExecutionRecord, ...] = Field(default=(), max_length=64)
    events: tuple[ExecutionEvent, ...] = Field(min_length=1, max_length=256)
    total_duration_ms: int | None = Field(default=None, ge=0)
    failed_step_index: int | None = Field(default=None, ge=0, le=63)
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    human_intervention_required: bool
    content_hash: HashDigest

    @model_validator(mode="after")
    def validate_report_consistency(self) -> Self:
        """Require progress, events, records, and terminal facts to agree."""
        if tuple(event.sequence for event in self.events) != tuple(range(len(self.events))):
            raise ValueError("Execution event sequences must be contiguous")
        if any(event.execution_attempt_id != self.execution_attempt_id for event in self.events):
            raise ValueError("Execution events must belong to the report Attempt")
        if self.events[0].kind is not ExecutionEventKind.ATTEMPT_AUTHORIZED:
            raise ValueError("Execution reports must start with ATTEMPT_AUTHORIZED")
        if sum(event.kind is ExecutionEventKind.ATTEMPT_AUTHORIZED for event in self.events) != 1:
            raise ValueError("Execution reports require exactly one authorization event")
        if tuple(record.step_index for record in self.records) != tuple(range(len(self.records))):
            raise ValueError("Execution records must be a contiguous ordered prefix")
        invocation_ids = tuple(record.invocation_id for record in self.records)
        if len(invocation_ids) != len(set(invocation_ids)):
            raise ValueError("Execution records must use unique invocation IDs")
        step_events = tuple(
            event for event in self.events if event.kind is ExecutionEventKind.STEP_FINISHED
        )
        if len(step_events) != len(self.records) or any(
            event.step_index != record.step_index
            or event.step_id != record.step_id
            or event.invocation_id != record.invocation_id
            or event.dispatch_status is not record.dispatch_status
            or event.effect_disposition is not record.effect_disposition
            for event, record in zip(step_events, self.records, strict=True)
        ):
            raise ValueError("STEP_FINISHED events must exactly mirror execution records")
        failed = self.status is ExecutionReportStatus.FAILED
        if self.total_duration_ms is None and (
            not failed
            or self.failure_code
            not in {
                "executor_clock_failed",
                "runtime_clock_failed",
            }
        ):
            raise ValueError("Only an explicit clock failure may omit execution duration")
        if failed:
            if (
                self.next_state is not ExecutionNextState.FAILED
                or self.failure_code is None
                or len(self.events) < 2
                or self.events[-2].kind is not ExecutionEventKind.ATTEMPT_FAILED
                or self.events[-2].reason_code != self.failure_code
                or self.events[-1].kind is not ExecutionEventKind.ATTEMPT_CLOSED
            ):
                raise ValueError(
                    "Failed execution reports require matching failure and closure evidence"
                )
        elif self.next_state is not ExecutionNextState.VERIFYING:
            raise ValueError("Successful execution reports must proceed to verification")
        elif (
            self.failure_code is not None
            or self.failed_step_index is not None
            or any(record.failure_code is not None for record in self.records)
        ):
            raise ValueError("Successful execution reports require only successful records")
        elif self.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH:
            if self.events[-1].kind is not ExecutionEventKind.PHASE_READY or any(
                event.kind
                in {
                    ExecutionEventKind.ATTEMPT_FAILED,
                    ExecutionEventKind.ATTEMPT_CLOSED,
                }
                for event in self.events
            ):
                raise ValueError(
                    "Awaiting-verification reports require an open phase-ready Attempt"
                )
        elif (
            self.status is not ExecutionReportStatus.READY_FOR_VERIFIER
            or self.events[-1].kind is not ExecutionEventKind.ATTEMPT_CLOSED
            or any(event.kind is ExecutionEventKind.ATTEMPT_FAILED for event in self.events)
        ):
            raise ValueError("Verifier-ready reports require a successfully closed Attempt")
        closed_events = sum(
            event.kind is ExecutionEventKind.ATTEMPT_CLOSED for event in self.events
        )
        if closed_events != (
            0 if self.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH else 1
        ):
            raise ValueError("Execution report closure evidence is inconsistent")
        failed_events = sum(
            event.kind is ExecutionEventKind.ATTEMPT_FAILED for event in self.events
        )
        if failed_events != (1 if failed else 0):
            raise ValueError("Execution report failure evidence is inconsistent")
        if self.failed_step_index is not None and (
            (
                self.records
                and self.records[-1].failure_code is not None
                and self.failed_step_index != self.records[-1].step_index
            )
            or (
                (not self.records or self.records[-1].failure_code is None)
                and self.failed_step_index != len(self.records)
            )
        ):
            raise ValueError("Failed Step must identify the failed record or stopped-before index")
        unknown_effect = any(
            record.effect_disposition is EffectDisposition.UNKNOWN for record in self.records
        )
        if self.human_intervention_required is not unknown_effect:
            raise ValueError("Human intervention flag must match unknown effect evidence")
        if not failed and (unknown_effect or self.human_intervention_required):
            raise ValueError("Successful reports cannot contain uncertain external effects")
        if self.content_hash != _content_hash(self, "content_hash"):
            raise ValueError("Execution report content hash is invalid")
        return self

    @property
    def results(self) -> tuple[ToolResult[SystemStatus], ...]:
        """Return ordered trustworthy ToolResults retained by the report."""
        return tuple(record.result for record in self.records if record.result is not None)


class ExecutionUncertainty(BaseModel):
    """Attempt-level closure evidence after a safe abort cannot be confirmed."""

    model_config = _STRICT_FROZEN_CONFIG

    uncertainty_schema_version: Literal["1"] = "1"
    execution_attempt_id: UUID
    authorization_hash: HashDigest
    uncertainty_kind: Literal["ATTEMPT_CLOSURE_UNCONFIRMED"] = "ATTEMPT_CLOSURE_UNCONFIRMED"
    prior_report_hash: HashDigest | None = None
    dispatch_status: DispatchStatus
    effect_disposition: EffectDisposition
    human_intervention_required: bool
    reason_code: Literal["execution_abort_uncertain"] = "execution_abort_uncertain"
    content_hash: HashDigest

    @model_validator(mode="after")
    def validate_uncertainty(self) -> Self:
        """Bind exact closure and possible unreported-dispatch uncertainty."""
        known_no_dispatch = (
            self.dispatch_status is DispatchStatus.NOT_DISPATCHED
            and self.effect_disposition is EffectDisposition.NONE
            and not self.human_intervention_required
        )
        unknown_dispatch = (
            self.dispatch_status is DispatchStatus.UNKNOWN
            and self.effect_disposition is EffectDisposition.UNKNOWN
            and self.human_intervention_required
        )
        if not (known_no_dispatch or unknown_dispatch):
            raise ValueError("Execution uncertainty dispatch and effect facts are inconsistent")
        if self.content_hash != _content_hash(self, "content_hash"):
            raise ValueError("Execution uncertainty content hash is invalid")
        return self


def _content_hash(model: BaseModel, field_name: str) -> str:
    document = model.model_dump(mode="json", exclude={field_name}, warnings="error")
    return canonical_json_sha256(document)


__all__ = [
    "DispatchStatus",
    "EffectDisposition",
    "ExecutionAttemptAuthorization",
    "ExecutionEvent",
    "ExecutionEventKind",
    "ExecutionNextState",
    "ExecutionReport",
    "ExecutionReportStatus",
    "ExecutionUncertainty",
    "ManualConfirmationChallenge",
    "StepExecutionRecord",
]
