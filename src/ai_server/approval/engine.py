"""Process-local, fail-closed execution Approval Engine."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from types import MappingProxyType
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, JsonValue, ValidationError

from ai_server.approval.errors import (
    ApprovalConfigurationError,
    ApprovalReviewError,
    ApprovalStateError,
)
from ai_server.models.approval import (
    ApprovalAuditEvent,
    ApprovalAuditEventKind,
    ApprovalInvalidationReason,
    ApprovalRecord,
    ApprovalReview,
    ApprovalStepBinding,
    ApprovalValidationReason,
    ApprovalValidationResult,
    ApprovalValidationVerdict,
    ManualConfirmationRecord,
    PlanApprovalSnapshot,
    PlanStepApprovalSnapshot,
)
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.policy import (
    ApprovalConstraints,
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEffect,
    StepPolicyDecision,
)
from ai_server.models.tool import RiskLevel, TargetReference, ToolMetadata
from ai_server.models.verification import VERIFICATION_CRITERION_TYPES
from ai_server.tools.hashing import CanonicalizationError, canonical_json_sha256

type Clock = Callable[[], datetime]
type IdFactory = Callable[[], UUID]
type ToolKey = tuple[str, str]
type LocalOwner = Literal["local-owner"]


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _StoredReview:
    review: ApprovalReview
    plan: ExecutionPlan
    decision: PolicyDecision


@dataclass(frozen=True, slots=True)
class _StoredApproval:
    record: ApprovalRecord
    snapshot: PlanApprovalSnapshot
    decision: PolicyDecision


@dataclass(frozen=True, slots=True)
class _LedgerState:
    reviews: Mapping[UUID, _StoredReview] = field(default_factory=lambda: MappingProxyType({}))
    approvals: Mapping[UUID, _StoredApproval] = field(default_factory=lambda: MappingProxyType({}))
    approval_by_review: Mapping[UUID, UUID] = field(default_factory=lambda: MappingProxyType({}))
    confirmations: Mapping[UUID, ManualConfirmationRecord] = field(
        default_factory=lambda: MappingProxyType({})
    )
    confirmation_by_invocation: Mapping[UUID, UUID] = field(
        default_factory=lambda: MappingProxyType({})
    )
    rejected_reviews: frozenset[UUID] = frozenset()
    invalidated_approvals: Mapping[UUID, ApprovalInvalidationReason] = field(
        default_factory=lambda: MappingProxyType({})
    )
    expired_approvals: frozenset[UUID] = frozenset()
    consumed_approvals: Mapping[UUID, UUID] = field(default_factory=lambda: MappingProxyType({}))
    consumed_plan_hashes: Mapping[str, tuple[UUID, UUID]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    closed_attempts: frozenset[tuple[UUID, UUID]] = frozenset()
    expired_confirmations: frozenset[UUID] = frozenset()
    invalidated_confirmations: frozenset[UUID] = frozenset()
    consumed_confirmations: frozenset[UUID] = frozenset()
    events: tuple[ApprovalAuditEvent, ...] = ()


class ApprovalEngine:
    """Issue and validate exact process-local human authorization evidence."""

    def __init__(
        self,
        metadata: Mapping[ToolKey, ToolMetadata],
        constraints: ApprovalConstraints,
        *,
        clock: Clock = _utc_now,
        id_factory: IdFactory = uuid4,
    ) -> None:
        """Bind one immutable Tool catalog, reviewed constraints, clock, and ID source."""
        self._metadata = _validate_metadata(metadata)
        self._constraints = _validate_constraints(constraints)
        if not callable(clock) or not callable(id_factory):
            raise ApprovalConfigurationError("Approval Engine configuration is malformed")
        self._clock = clock
        self._id_factory = id_factory
        self._lock = RLock()
        self._state = _LedgerState()
        self._last_timestamp: datetime | None = None

    @property
    def constraints(self) -> ApprovalConstraints:
        """Return the reviewed immutable Approval lifetime constraints."""
        return self._constraints

    @property
    def events(self) -> tuple[ApprovalAuditEvent, ...]:
        """Return an immutable snapshot of non-secret process-local audit events."""
        with self._lock:
            return self._state.events

    def prepare_review(
        self,
        plan: ExecutionPlan,
        decision: PolicyDecision,
    ) -> ApprovalReview:
        """Create a short-lived human review for one exact allowed Plan."""
        trusted_plan, trusted_decision, snapshot = self._rebuild_review_inputs(plan, decision)
        try:
            plan_hash = canonical_json_sha256(snapshot)
            decision_hash = canonical_json_sha256(trusted_decision)
        except CanonicalizationError:
            raise ApprovalReviewError("Approval review inputs are not canonicalizable") from None
        with self._lock:
            prepared_at = self._read_clock_locked()
            review_id, event_id = self._new_ids_locked(2)
            review = ApprovalReview(
                review_id=review_id,
                task_id=snapshot.task_id,
                plan_id=snapshot.plan_id,
                plan_hash=plan_hash,
                policy_id=trusted_decision.policy_id,
                policy_version=trusted_decision.policy_version,
                policy_hash=trusted_decision.policy_hash,
                policy_decision_hash=decision_hash,
                operator_id=snapshot.operator_id,
                target=snapshot.target,
                effective_risk=cast(RiskLevel, trusted_decision.effective_risk),
                approval_requirement=cast(
                    PolicyApprovalRequirement,
                    trusted_decision.approval_requirement,
                ),
                manual_confirmation_requirement=cast(
                    ManualConfirmationRequirement,
                    trusted_decision.manual_confirmation_requirement,
                ),
                prepared_at=prepared_at,
                expires_at=prepared_at
                + timedelta(seconds=self._constraints.review_session_ttl_seconds),
                snapshot=snapshot,
            )
            event = _review_event(
                event_id=event_id,
                sequence=len(self._state.events),
                kind=ApprovalAuditEventKind.REVIEW_PREPARED,
                occurred_at=prepared_at,
                review=review,
            )
            reviews = dict(self._state.reviews)
            reviews[review_id] = _StoredReview(
                review=review,
                plan=trusted_plan,
                decision=trusted_decision,
            )
            self._state = replace(
                self._state,
                reviews=MappingProxyType(reviews),
                events=(*self._state.events, event),
            )
            return review

    def commit(
        self,
        review_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
    ) -> ApprovalRecord:
        """Record an explicit local-owner Commit for one unexpired exact Review."""
        _require_uuid(review_id, "Review ID")
        trusted_plan, trusted_decision, current_snapshot = self._rebuild_review_inputs(
            plan,
            decision,
        )
        with self._lock:
            issued_at = self._read_clock_locked()
            stored = self._state.reviews.get(review_id)
            if stored is None:
                raise ApprovalStateError("Approval Review is unknown")
            if review_id in self._state.rejected_reviews:
                raise ApprovalStateError("Rejected Approval Review cannot be committed")
            if review_id in self._state.approval_by_review:
                raise ApprovalStateError("Approval Review was already committed")
            if issued_at >= stored.review.expires_at:
                raise ApprovalStateError("Approval Review has expired")
            if (
                trusted_plan != stored.plan
                or trusted_decision != stored.decision
                or current_snapshot != stored.review.snapshot
                or canonical_json_sha256(current_snapshot) != stored.review.plan_hash
                or canonical_json_sha256(trusted_decision) != stored.review.policy_decision_hash
            ):
                raise ApprovalStateError("Approval Review content changed before Commit")
            approval_id, event_id = self._new_ids_locked(2)
            record = _build_approval_record(
                approval_id=approval_id,
                review=stored.review,
                issued_at=issued_at,
                expires_at=issued_at
                + timedelta(seconds=self._constraints.plan_approval_ttl_seconds),
            )
            event = _approval_event(
                event_id=event_id,
                sequence=len(self._state.events),
                kind=ApprovalAuditEventKind.PLAN_APPROVAL_ISSUED,
                occurred_at=issued_at,
                record=record,
                actor="local-owner",
            )
            approvals = dict(self._state.approvals)
            approvals[approval_id] = _StoredApproval(
                record=record,
                snapshot=current_snapshot,
                decision=trusted_decision,
            )
            approval_by_review = dict(self._state.approval_by_review)
            approval_by_review[review_id] = approval_id
            self._state = replace(
                self._state,
                approvals=MappingProxyType(approvals),
                approval_by_review=MappingProxyType(approval_by_review),
                events=(*self._state.events, event),
            )
            return record

    def reject(
        self,
        review_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
    ) -> ApprovalAuditEvent:
        """Record an explicit local-owner rejection of one pending Review."""
        _require_uuid(review_id, "Review ID")
        trusted_plan, trusted_decision, current_snapshot = self._rebuild_review_inputs(
            plan,
            decision,
        )
        with self._lock:
            occurred_at = self._read_clock_locked()
            stored = self._state.reviews.get(review_id)
            if stored is None:
                raise ApprovalStateError("Approval Review is unknown")
            if review_id in self._state.rejected_reviews:
                raise ApprovalStateError("Approval Review was already rejected")
            if review_id in self._state.approval_by_review:
                raise ApprovalStateError("Committed Approval Review cannot be rejected")
            if occurred_at >= stored.review.expires_at:
                raise ApprovalStateError("Approval Review has expired")
            if (
                trusted_plan != stored.plan
                or trusted_decision != stored.decision
                or current_snapshot != stored.review.snapshot
                or canonical_json_sha256(current_snapshot) != stored.review.plan_hash
                or canonical_json_sha256(trusted_decision) != stored.review.policy_decision_hash
            ):
                raise ApprovalStateError("Approval Review content changed before rejection")
            (event_id,) = self._new_ids_locked(1)
            event = _review_event(
                event_id=event_id,
                sequence=len(self._state.events),
                kind=ApprovalAuditEventKind.PLAN_APPROVAL_REJECTED,
                occurred_at=occurred_at,
                review=stored.review,
                actor="local-owner",
                reason_code="human_rejected",
            )
            self._state = replace(
                self._state,
                rejected_reviews=self._state.rejected_reviews | {review_id},
                events=(*self._state.events, event),
            )
            return event

    def validate_for_dispatch(
        self,
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        *,
        execution_attempt_id: UUID | None = None,
    ) -> ApprovalValidationResult:
        """Validate one Approval without consuming or dispatching anything."""
        _require_uuid(approval_id, "Approval ID")
        if execution_attempt_id is not None:
            _require_uuid(execution_attempt_id, "Execution attempt ID")
        with self._lock:
            try:
                checked_at = self._read_clock_locked()
            except ApprovalConfigurationError:
                return _invalid_result(
                    reason=ApprovalValidationReason.INVALID_CLOCK,
                    checked_at=None,
                    approval_id=approval_id,
                    execution_attempt_id=execution_attempt_id,
                )
            return self._validate_approval_locked(
                approval_id,
                plan,
                decision,
                execution_attempt_id=execution_attempt_id,
                checked_at=checked_at,
            )

    def consume_for_attempt(
        self,
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        execution_attempt_id: UUID,
    ) -> ApprovalValidationResult:
        """Atomically bind one valid unconsumed Approval to one execution attempt."""
        _require_uuid(approval_id, "Approval ID")
        _require_uuid(execution_attempt_id, "Execution attempt ID")
        with self._lock:
            try:
                checked_at = self._read_clock_locked()
            except ApprovalConfigurationError:
                return _invalid_result(
                    reason=ApprovalValidationReason.INVALID_CLOCK,
                    checked_at=None,
                    approval_id=approval_id,
                    execution_attempt_id=execution_attempt_id,
                )
            validation = self._validate_approval_locked(
                approval_id,
                plan,
                decision,
                execution_attempt_id=execution_attempt_id,
                checked_at=checked_at,
            )
            if validation.verdict is ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT:
                return _invalid_result(
                    reason=ApprovalValidationReason.APPROVAL_ALREADY_CONSUMED,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    plan_hash=validation.plan_hash,
                    execution_attempt_id=execution_attempt_id,
                )
            if validation.verdict is not ApprovalValidationVerdict.VALID_UNCONSUMED:
                return validation
            stored = self._state.approvals[approval_id]
            (event_id,) = self._new_ids_locked(1)
            event = _approval_event(
                event_id=event_id,
                sequence=len(self._state.events),
                kind=ApprovalAuditEventKind.PLAN_APPROVAL_CONSUMED,
                occurred_at=checked_at,
                record=stored.record,
                execution_attempt_id=execution_attempt_id,
            )
            consumed = dict(self._state.consumed_approvals)
            consumed[approval_id] = execution_attempt_id
            consumed_plan_hashes = dict(self._state.consumed_plan_hashes)
            consumed_plan_hashes[stored.record.plan_hash] = (
                approval_id,
                execution_attempt_id,
            )
            self._state = replace(
                self._state,
                consumed_approvals=MappingProxyType(consumed),
                consumed_plan_hashes=MappingProxyType(consumed_plan_hashes),
                events=(*self._state.events, event),
            )
            return ApprovalValidationResult(
                verdict=ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT,
                reason=ApprovalValidationReason.VALID_FOR_BOUND_ATTEMPT,
                checked_at=checked_at,
                approval_id=approval_id,
                plan_hash=stored.record.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )

    def validate_for_attempt(
        self,
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        execution_attempt_id: UUID,
    ) -> ApprovalValidationResult:
        """Revalidate an already consumed Approval for its one bound attempt."""
        validation = self.validate_for_dispatch(
            approval_id,
            plan,
            decision,
            execution_attempt_id=execution_attempt_id,
        )
        if validation.verdict is ApprovalValidationVerdict.VALID_UNCONSUMED:
            return _invalid_result(
                reason=ApprovalValidationReason.APPROVAL_NOT_CONSUMED,
                checked_at=validation.checked_at,
                approval_id=approval_id,
                plan_hash=validation.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )
        return validation

    def record_for_attempt(
        self,
        approval_id: UUID,
        execution_attempt_id: UUID,
    ) -> ApprovalRecord:
        """Return immutable non-secret evidence for one currently bound attempt."""
        _require_uuid(approval_id, "Approval ID")
        _require_uuid(execution_attempt_id, "Execution attempt ID")
        with self._lock:
            stored = self._state.approvals.get(approval_id)
            if stored is None:
                raise ApprovalStateError("Approval is unknown")
            if self._state.consumed_approvals.get(approval_id) != execution_attempt_id:
                raise ApprovalStateError("Approval is not bound to this execution attempt")
            if (approval_id, execution_attempt_id) in self._state.closed_attempts:
                raise ApprovalStateError("Execution attempt is already closed")
            return stored.record

    def invalidate(
        self,
        approval_id: UUID,
        reason: ApprovalInvalidationReason,
    ) -> ApprovalAuditEvent:
        """Permanently invalidate one issued Approval without undoing effects."""
        _require_uuid(approval_id, "Approval ID")
        if type(reason) is not ApprovalInvalidationReason:
            raise ApprovalStateError("Approval invalidation reason is malformed")
        with self._lock:
            occurred_at = self._read_clock_locked()
            return self._invalidate_approval_locked(
                approval_id,
                reason=reason,
                occurred_at=occurred_at,
            )

    def close_attempt(
        self,
        approval_id: UUID,
        execution_attempt_id: UUID,
    ) -> ApprovalAuditEvent:
        """Close a bound attempt so its Approval can never authorize more work."""
        _require_uuid(approval_id, "Approval ID")
        _require_uuid(execution_attempt_id, "Execution attempt ID")
        with self._lock:
            occurred_at = self._read_clock_locked()
            stored = self._state.approvals.get(approval_id)
            if stored is None:
                raise ApprovalStateError("Approval is unknown")
            if self._state.consumed_approvals.get(approval_id) != execution_attempt_id:
                raise ApprovalStateError("Approval is not bound to this execution attempt")
            key = (approval_id, execution_attempt_id)
            if key in self._state.closed_attempts:
                raise ApprovalStateError("Execution attempt was already closed")
            (event_id,) = self._new_ids_locked(1)
            event = _approval_event(
                event_id=event_id,
                sequence=len(self._state.events),
                kind=ApprovalAuditEventKind.ATTEMPT_CLOSED,
                occurred_at=occurred_at,
                record=stored.record,
                execution_attempt_id=execution_attempt_id,
            )
            self._state = replace(
                self._state,
                closed_attempts=self._state.closed_attempts | {key},
                events=(*self._state.events, event),
            )
            return event

    def issue_l3_confirmation(
        self,
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        *,
        execution_attempt_id: UUID,
        invocation_id: UUID,
        step_id: str,
    ) -> ManualConfirmationRecord:
        """Issue one immediate local-owner confirmation for an exact L3 invocation."""
        _require_uuid(approval_id, "Approval ID")
        _require_uuid(execution_attempt_id, "Execution attempt ID")
        _require_uuid(invocation_id, "Invocation ID")
        if type(step_id) is not str:
            raise ApprovalStateError("Confirmation Step ID is malformed")
        with self._lock:
            issued_at = self._read_clock_locked()
            validation = self._validate_approval_locked(
                approval_id,
                plan,
                decision,
                execution_attempt_id=execution_attempt_id,
                checked_at=issued_at,
            )
            if validation.verdict is not ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT:
                raise ApprovalStateError("L3 confirmation requires a valid consumed Approval")
            if invocation_id in self._state.confirmation_by_invocation:
                raise ApprovalStateError("Invocation already has an L3 confirmation")
            stored = self._state.approvals[approval_id]
            snapshot_step, decision_step = _resolve_l3_step(
                stored.snapshot,
                decision,
                step_id,
            )
            confirmation_id, event_id = self._new_ids_locked(2)
            record = _build_confirmation_record(
                confirmation_id=confirmation_id,
                approval=stored.record,
                snapshot_step=snapshot_step,
                policy_decision_hash=canonical_json_sha256(decision),
                execution_attempt_id=execution_attempt_id,
                invocation_id=invocation_id,
                issued_at=issued_at,
                expires_at=issued_at
                + timedelta(seconds=self._constraints.l3_confirmation_ttl_seconds),
            )
            if decision_step.resolved_risk is not RiskLevel.L3:
                raise ApprovalStateError("Manual confirmation is only valid for L3")
            event = _confirmation_event(
                event_id=event_id,
                sequence=len(self._state.events),
                kind=ApprovalAuditEventKind.L3_CONFIRMATION_ISSUED,
                occurred_at=issued_at,
                record=record,
                actor="local-owner",
            )
            confirmations = dict(self._state.confirmations)
            confirmations[confirmation_id] = record
            by_invocation = dict(self._state.confirmation_by_invocation)
            by_invocation[invocation_id] = confirmation_id
            self._state = replace(
                self._state,
                confirmations=MappingProxyType(confirmations),
                confirmation_by_invocation=MappingProxyType(by_invocation),
                events=(*self._state.events, event),
            )
            return record

    def consume_l3_confirmation(
        self,
        confirmation_id: UUID | None,
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        *,
        execution_attempt_id: UUID,
        invocation_id: UUID,
        step_id: str,
    ) -> ApprovalValidationResult:
        """Atomically consume one exact L3 confirmation without dispatching a Tool."""
        identifiers = (approval_id, execution_attempt_id, invocation_id)
        if any(type(identifier) is not UUID for identifier in identifiers):
            raise ApprovalStateError("Confirmation identity is malformed")
        if type(step_id) is not str:
            raise ApprovalStateError("Confirmation Step ID is malformed")
        with self._lock:
            try:
                checked_at = self._read_clock_locked()
            except ApprovalConfigurationError:
                return _invalid_result(
                    reason=ApprovalValidationReason.INVALID_CLOCK,
                    checked_at=None,
                    approval_id=approval_id,
                    confirmation_id=confirmation_id,
                    execution_attempt_id=execution_attempt_id,
                )
            if confirmation_id is None:
                return _invalid_result(
                    reason=ApprovalValidationReason.CONFIRMATION_MISSING,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    execution_attempt_id=execution_attempt_id,
                )
            if type(confirmation_id) is not UUID:
                raise ApprovalStateError("Confirmation identity is malformed")
            record = self._state.confirmations.get(confirmation_id)
            if record is None:
                return _invalid_result(
                    reason=ApprovalValidationReason.UNKNOWN_CONFIRMATION,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    confirmation_id=confirmation_id,
                    execution_attempt_id=execution_attempt_id,
                )
            if confirmation_id in self._state.consumed_confirmations:
                return _invalid_result(
                    reason=ApprovalValidationReason.CONFIRMATION_ALREADY_CONSUMED,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    confirmation_id=confirmation_id,
                    plan_hash=record.plan_hash,
                    execution_attempt_id=execution_attempt_id,
                )
            if confirmation_id in self._state.expired_confirmations:
                return _invalid_result(
                    reason=ApprovalValidationReason.CONFIRMATION_EXPIRED,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    confirmation_id=confirmation_id,
                    plan_hash=record.plan_hash,
                    execution_attempt_id=execution_attempt_id,
                )
            if confirmation_id in self._state.invalidated_confirmations:
                return _invalid_result(
                    reason=ApprovalValidationReason.CONFIRMATION_MISMATCH,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    confirmation_id=confirmation_id,
                    plan_hash=record.plan_hash,
                    execution_attempt_id=execution_attempt_id,
                )
            if checked_at >= record.expires_at:
                self._expire_confirmation_locked(record, checked_at)
                return _invalid_result(
                    reason=ApprovalValidationReason.CONFIRMATION_EXPIRED,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    confirmation_id=confirmation_id,
                    plan_hash=record.plan_hash,
                    execution_attempt_id=execution_attempt_id,
                )
            base_validation = self._validate_approval_locked(
                approval_id,
                plan,
                decision,
                execution_attempt_id=execution_attempt_id,
                checked_at=checked_at,
            )
            if base_validation.verdict is not ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT:
                return base_validation.model_copy(update={"confirmation_id": confirmation_id})
            try:
                stored = self._state.approvals[approval_id]
                snapshot_step, _ = _resolve_l3_step(stored.snapshot, decision, step_id)
                matches = (
                    record.approval_id == approval_id
                    and record.approval_record_hash == stored.record.content_hash
                    and record.plan_hash == stored.record.plan_hash
                    and record.policy_decision_hash == canonical_json_sha256(decision)
                    and record.execution_attempt_id == execution_attempt_id
                    and record.invocation_id == invocation_id
                    and record.step_index == snapshot_step.step_index
                    and record.step_id == snapshot_step.step_id
                    and record.tool_id == snapshot_step.tool_id
                    and record.tool_version == snapshot_step.tool_version
                    and record.contract_hash == snapshot_step.contract_hash
                    and record.implementation_hash == snapshot_step.implementation_hash
                    and record.arguments_hash == snapshot_step.arguments_hash
                    and record.target == snapshot_step.target
                )
            except BaseException:
                matches = False
            if not matches:
                self._invalidate_confirmation_locked(record, checked_at)
                return _invalid_result(
                    reason=ApprovalValidationReason.CONFIRMATION_MISMATCH,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    confirmation_id=confirmation_id,
                    plan_hash=record.plan_hash,
                    execution_attempt_id=execution_attempt_id,
                )
            (event_id,) = self._new_ids_locked(1)
            event = _confirmation_event(
                event_id=event_id,
                sequence=len(self._state.events),
                kind=ApprovalAuditEventKind.L3_CONFIRMATION_CONSUMED,
                occurred_at=checked_at,
                record=record,
            )
            self._state = replace(
                self._state,
                consumed_confirmations=self._state.consumed_confirmations | {confirmation_id},
                events=(*self._state.events, event),
            )
            return ApprovalValidationResult(
                verdict=ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT,
                reason=ApprovalValidationReason.VALID_FOR_BOUND_ATTEMPT,
                checked_at=checked_at,
                approval_id=approval_id,
                confirmation_id=confirmation_id,
                plan_hash=record.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )

    def consumed_confirmation_for_attempt(
        self,
        confirmation_id: UUID,
        execution_attempt_id: UUID,
        invocation_id: UUID,
    ) -> ManualConfirmationRecord:
        """Return exact consumed L3 evidence bound to one Attempt invocation."""
        if any(
            type(identifier) is not UUID
            for identifier in (
                confirmation_id,
                execution_attempt_id,
                invocation_id,
            )
        ):
            raise ApprovalStateError("Confirmation lookup identity is malformed")
        with self._lock:
            record = self._state.confirmations.get(confirmation_id)
            if (
                type(record) is not ManualConfirmationRecord
                or confirmation_id not in self._state.consumed_confirmations
                or self._state.confirmation_by_invocation.get(invocation_id) != confirmation_id
                or record.execution_attempt_id != execution_attempt_id
                or record.invocation_id != invocation_id
            ):
                raise ApprovalStateError(
                    "Consumed confirmation evidence is unavailable or mismatched"
                )
            try:
                return ManualConfirmationRecord.model_validate(
                    record.model_dump(mode="python", warnings="error"),
                    strict=True,
                )
            except BaseException:
                raise ApprovalStateError("Consumed confirmation evidence is malformed") from None

    def _rebuild_review_inputs(
        self,
        plan: ExecutionPlan,
        decision: PolicyDecision,
    ) -> tuple[ExecutionPlan, PolicyDecision, PlanApprovalSnapshot]:
        try:
            trusted_plan = _validate_plan(plan)
            trusted_decision = _validate_decision(decision)
            snapshot = _build_snapshot(
                trusted_plan,
                trusted_decision,
                self._metadata,
            )
            return trusted_plan, trusted_decision, snapshot
        except ApprovalReviewError:
            raise
        except BaseException:
            raise ApprovalReviewError("Approval rejected unsafe review inputs") from None

    def _validate_approval_locked(
        self,
        approval_id: UUID,
        plan: ExecutionPlan,
        decision: PolicyDecision,
        *,
        execution_attempt_id: UUID | None,
        checked_at: datetime,
    ) -> ApprovalValidationResult:
        stored = self._state.approvals.get(approval_id)
        if stored is None:
            return _invalid_result(
                reason=ApprovalValidationReason.UNKNOWN_APPROVAL,
                checked_at=checked_at,
                approval_id=approval_id,
                execution_attempt_id=execution_attempt_id,
            )
        record = stored.record
        if approval_id in self._state.invalidated_approvals:
            return _invalid_result(
                reason=ApprovalValidationReason.APPROVAL_INVALIDATED,
                checked_at=checked_at,
                approval_id=approval_id,
                plan_hash=record.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )
        if approval_id in self._state.expired_approvals or checked_at >= record.expires_at:
            if approval_id not in self._state.expired_approvals:
                self._expire_approval_locked(record, checked_at)
            return _invalid_result(
                reason=ApprovalValidationReason.APPROVAL_EXPIRED,
                checked_at=checked_at,
                approval_id=approval_id,
                plan_hash=record.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )
        reason = self._binding_mismatch_reason(stored, plan, decision)
        if reason is not None:
            self._invalidate_approval_locked(
                approval_id,
                reason=_invalidation_reason(reason),
                occurred_at=checked_at,
            )
            return _invalid_result(
                reason=reason,
                checked_at=checked_at,
                approval_id=approval_id,
                plan_hash=record.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )
        bound_attempt = self._state.consumed_approvals.get(approval_id)
        if bound_attempt is None:
            if record.plan_hash in self._state.consumed_plan_hashes:
                return _invalid_result(
                    reason=ApprovalValidationReason.APPROVAL_ALREADY_CONSUMED,
                    checked_at=checked_at,
                    approval_id=approval_id,
                    plan_hash=record.plan_hash,
                    execution_attempt_id=execution_attempt_id,
                )
            return ApprovalValidationResult(
                verdict=ApprovalValidationVerdict.VALID_UNCONSUMED,
                reason=ApprovalValidationReason.VALID_UNCONSUMED,
                checked_at=checked_at,
                approval_id=approval_id,
                plan_hash=record.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )
        if (approval_id, bound_attempt) in self._state.closed_attempts:
            return _invalid_result(
                reason=ApprovalValidationReason.EXECUTION_ATTEMPT_CLOSED,
                checked_at=checked_at,
                approval_id=approval_id,
                plan_hash=record.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )
        if execution_attempt_id != bound_attempt:
            return _invalid_result(
                reason=(
                    ApprovalValidationReason.APPROVAL_ALREADY_CONSUMED
                    if execution_attempt_id is None
                    else ApprovalValidationReason.EXECUTION_ATTEMPT_MISMATCH
                ),
                checked_at=checked_at,
                approval_id=approval_id,
                plan_hash=record.plan_hash,
                execution_attempt_id=execution_attempt_id,
            )
        return ApprovalValidationResult(
            verdict=ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT,
            reason=ApprovalValidationReason.VALID_FOR_BOUND_ATTEMPT,
            checked_at=checked_at,
            approval_id=approval_id,
            plan_hash=record.plan_hash,
            execution_attempt_id=execution_attempt_id,
        )

    def _binding_mismatch_reason(
        self,
        stored: _StoredApproval,
        plan: ExecutionPlan,
        decision: PolicyDecision,
    ) -> ApprovalValidationReason | None:
        try:
            trusted_plan = _validate_plan(plan)
            trusted_decision = _validate_decision(decision, allow_denied=True)
        except ApprovalReviewError:
            return ApprovalValidationReason.PLAN_SNAPSHOT_MISMATCH
        if trusted_decision.effect is PolicyEffect.DENY:
            return ApprovalValidationReason.POLICY_DENIED
        if (
            trusted_decision.approval_requirement
            is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
        ):
            return ApprovalValidationReason.WRONG_APPROVAL_MODE
        record = stored.record
        if trusted_decision.operator_id != record.operator_id:
            return ApprovalValidationReason.OPERATOR_MISMATCH
        if trusted_decision.target != record.target:
            return ApprovalValidationReason.TARGET_MISMATCH
        if (
            trusted_decision.policy_id != record.policy_id
            or trusted_decision.policy_version != record.policy_version
            or trusted_decision.policy_hash != record.policy_hash
            or canonical_json_sha256(trusted_decision) != record.policy_decision_hash
        ):
            return ApprovalValidationReason.POLICY_MISMATCH
        try:
            snapshot = _build_snapshot(trusted_plan, trusted_decision, self._metadata)
            plan_hash = canonical_json_sha256(snapshot)
        except ApprovalReviewError:
            return ApprovalValidationReason.TOOL_INTEGRITY_MISMATCH
        except CanonicalizationError:
            return ApprovalValidationReason.PLAN_SNAPSHOT_MISMATCH
        if plan_hash != record.plan_hash:
            return ApprovalValidationReason.PLAN_HASH_MISMATCH
        if snapshot != stored.snapshot:
            return ApprovalValidationReason.PLAN_SNAPSHOT_MISMATCH
        bindings = _snapshot_bindings(snapshot)
        if any(
            current.arguments_hash != expected.arguments_hash
            for current, expected in zip(bindings, record.steps, strict=True)
        ):
            return ApprovalValidationReason.ARGUMENTS_MISMATCH
        if bindings != record.steps:
            return ApprovalValidationReason.TOOL_INTEGRITY_MISMATCH
        return None

    def _invalidate_approval_locked(
        self,
        approval_id: UUID,
        *,
        reason: ApprovalInvalidationReason,
        occurred_at: datetime,
    ) -> ApprovalAuditEvent:
        stored = self._state.approvals.get(approval_id)
        if stored is None:
            raise ApprovalStateError("Approval is unknown")
        if approval_id in self._state.invalidated_approvals:
            raise ApprovalStateError("Approval was already invalidated")
        (event_id,) = self._new_ids_locked(1)
        event = _approval_event(
            event_id=event_id,
            sequence=len(self._state.events),
            kind=ApprovalAuditEventKind.PLAN_APPROVAL_INVALIDATED,
            occurred_at=occurred_at,
            record=stored.record,
            actor=(
                "local-owner" if reason is ApprovalInvalidationReason.REVOKED_BY_APPROVER else None
            ),
            reason_code=reason.value,
        )
        invalidated = dict(self._state.invalidated_approvals)
        invalidated[approval_id] = reason
        self._state = replace(
            self._state,
            invalidated_approvals=MappingProxyType(invalidated),
            events=(*self._state.events, event),
        )
        return event

    def _expire_approval_locked(
        self,
        record: ApprovalRecord,
        occurred_at: datetime,
    ) -> None:
        (event_id,) = self._new_ids_locked(1)
        event = _approval_event(
            event_id=event_id,
            sequence=len(self._state.events),
            kind=ApprovalAuditEventKind.PLAN_APPROVAL_EXPIRED,
            occurred_at=occurred_at,
            record=record,
            reason_code="approval_expired",
        )
        self._state = replace(
            self._state,
            expired_approvals=self._state.expired_approvals | {record.approval_id},
            events=(*self._state.events, event),
        )

    def _expire_confirmation_locked(
        self,
        record: ManualConfirmationRecord,
        occurred_at: datetime,
    ) -> None:
        (event_id,) = self._new_ids_locked(1)
        event = _confirmation_event(
            event_id=event_id,
            sequence=len(self._state.events),
            kind=ApprovalAuditEventKind.L3_CONFIRMATION_EXPIRED,
            occurred_at=occurred_at,
            record=record,
            reason_code="confirmation_expired",
        )
        self._state = replace(
            self._state,
            expired_confirmations=self._state.expired_confirmations | {record.confirmation_id},
            events=(*self._state.events, event),
        )

    def _invalidate_confirmation_locked(
        self,
        record: ManualConfirmationRecord,
        occurred_at: datetime,
    ) -> None:
        (event_id,) = self._new_ids_locked(1)
        event = _confirmation_event(
            event_id=event_id,
            sequence=len(self._state.events),
            kind=ApprovalAuditEventKind.L3_CONFIRMATION_INVALIDATED,
            occurred_at=occurred_at,
            record=record,
            reason_code="confirmation_mismatch",
        )
        self._state = replace(
            self._state,
            invalidated_confirmations=self._state.invalidated_confirmations
            | {record.confirmation_id},
            events=(*self._state.events, event),
        )

    def _read_clock_locked(self) -> datetime:
        try:
            value = self._clock()
            if (
                type(value) is not datetime
                or value.tzinfo is None
                or value.utcoffset() != timedelta(0)
            ):
                raise ValueError
            timestamp = datetime(
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
            if self._last_timestamp is not None and timestamp < self._last_timestamp:
                raise ValueError
        except BaseException:
            raise ApprovalConfigurationError("Approval clock is invalid") from None
        self._last_timestamp = timestamp
        return timestamp

    def _new_ids_locked(self, count: int) -> tuple[UUID, ...]:
        existing = {
            *self._state.reviews,
            *self._state.approvals,
            *self._state.confirmations,
            *(event.event_id for event in self._state.events),
        }
        generated: list[UUID] = []
        try:
            for _ in range(count):
                identifier = self._id_factory()
                if (
                    type(identifier) is not UUID
                    or identifier in existing
                    or identifier in generated
                ):
                    raise ValueError
                generated.append(identifier)
        except BaseException:
            raise ApprovalConfigurationError("Approval ID factory is invalid") from None
        return tuple(generated)


def _validate_metadata(
    metadata: Mapping[ToolKey, ToolMetadata],
) -> Mapping[ToolKey, ToolMetadata]:
    try:
        if not isinstance(metadata, Mapping) or not metadata:
            raise TypeError
        trusted: dict[ToolKey, ToolMetadata] = {}
        for key, value in metadata.items():
            if (
                type(key) is not tuple
                or len(key) != 2
                or any(type(part) is not str for part in key)
                or type(value) is not ToolMetadata
            ):
                raise TypeError
            rebuilt = ToolMetadata.model_validate(
                value.model_dump(mode="python", warnings="error"),
                strict=True,
            )
            if key != (rebuilt.tool_id, rebuilt.version) or key in trusted:
                raise TypeError
            trusted[key] = rebuilt
        return MappingProxyType(trusted)
    except BaseException:
        raise ApprovalConfigurationError("Approval Tool metadata is malformed") from None


def _validate_constraints(constraints: ApprovalConstraints) -> ApprovalConstraints:
    try:
        if type(constraints) is not ApprovalConstraints:
            raise TypeError
        return ApprovalConstraints.model_validate(
            constraints.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise ApprovalConfigurationError("Approval constraints are malformed") from None


def _validate_plan(plan: ExecutionPlan) -> ExecutionPlan:
    try:
        if (
            type(plan) is not ExecutionPlan
            or type(plan.task_id) is not UUID
            or type(plan.plan_id) is not UUID
            or type(plan.steps) is not tuple
            or not plan.steps
            or len(plan.steps) > 64
            or type(plan.verification_criteria) is not tuple
            or not plan.verification_criteria
            or len(plan.verification_criteria) > 128
            or any(
                type(step) is not ExecutionStep or not isinstance(step.arguments, BaseModel)
                for step in plan.steps
            )
            or any(
                type(criterion) not in VERIFICATION_CRITERION_TYPES
                for criterion in plan.verification_criteria
            )
        ):
            raise TypeError
        return ExecutionPlan.model_validate(
            plan.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise ApprovalReviewError("Approval received a malformed ExecutionPlan") from None


def _validate_decision(
    decision: PolicyDecision,
    *,
    allow_denied: bool = False,
) -> PolicyDecision:
    try:
        if (
            type(decision) is not PolicyDecision
            or type(decision.task_id) is not UUID
            or type(decision.plan_id) is not UUID
            or type(decision.target) is not TargetReference
            or type(decision.step_decisions) is not tuple
            or any(type(step) is not StepPolicyDecision for step in decision.step_decisions)
        ):
            raise TypeError
        trusted = PolicyDecision.model_validate(
            decision.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        if trusted.operator_id != "local-user":
            raise TypeError
        if not allow_denied and (
            trusted.effect is not PolicyEffect.ALLOW
            or trusted.approval_requirement is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
            or trusted.effective_risk is None
            or trusted.manual_confirmation_requirement is None
        ):
            raise TypeError
        return trusted
    except BaseException:
        raise ApprovalReviewError("Approval received an ineligible PolicyDecision") from None


def _build_snapshot(
    plan: ExecutionPlan,
    decision: PolicyDecision,
    metadata: Mapping[ToolKey, ToolMetadata],
) -> PlanApprovalSnapshot:
    if (
        plan.task_id != decision.task_id
        or plan.plan_id != decision.plan_id
        or plan.target != decision.target.target_id
        or decision.target.resource_id != plan.target
        or len(plan.steps) != len(decision.step_decisions)
    ):
        raise ApprovalReviewError("Plan and Policy Decision identities do not match")
    snapshots: list[PlanStepApprovalSnapshot] = []
    for index, (step, step_decision) in enumerate(
        zip(plan.steps, decision.step_decisions, strict=True)
    ):
        authoritative = metadata.get((step.tool_id, step.tool_version))
        if authoritative is None or type(authoritative) is not ToolMetadata:
            raise ApprovalReviewError("Approval cannot resolve exact Tool metadata")
        if (
            step_decision.step_id != step.step_id
            or step_decision.tool_id != step.tool_id
            or step_decision.tool_version != step.tool_version
            or step_decision.contract_hash != step.contract_hash
            or step_decision.implementation_hash != step.implementation_hash
            or step_decision.resolved_risk is not authoritative.risk_level
            or step.contract_hash != authoritative.contract_hash
            or step.implementation_hash != authoritative.implementation_hash
            or authoritative.target_scope.maximum_targets != 1
            or authoritative.target_scope.resource_type != decision.target.resource_type
        ):
            raise ApprovalReviewError("Approval rejected Tool integrity or scope drift")
        try:
            raw_arguments = step.arguments.model_dump(mode="json", warnings="error")
            if type(raw_arguments) is not dict or authoritative.redaction.input_fields:
                raise TypeError
            arguments = cast(dict[str, JsonValue], raw_arguments)
            selector = arguments.get(authoritative.target_scope.selector_field)
            if selector != decision.target.resource_id:
                raise TypeError
            arguments_hash = canonical_json_sha256(arguments)
            if step_decision.arguments_hash != arguments_hash:
                raise TypeError
            snapshots.append(
                PlanStepApprovalSnapshot(
                    step_index=index,
                    step_id=step.step_id,
                    role=step.role,
                    tool_id=step.tool_id,
                    tool_version=step.tool_version,
                    contract_hash=step.contract_hash,
                    implementation_hash=step.implementation_hash,
                    arguments=arguments,
                    arguments_hash=arguments_hash,
                    target=decision.target,
                    target_scope=authoritative.target_scope,
                    side_effects=authoritative.side_effects,
                    registry_risk_level=authoritative.risk_level,
                    registry_redaction=authoritative.redaction,
                    registry_verification=authoritative.verification,
                    registry_rollback=authoritative.rollback,
                    reason=step.reason,
                    impact=step.impact,
                    verification=step.verification,
                    recovery=step.recovery,
                )
            )
        except (CanonicalizationError, TypeError, ValidationError, ValueError):
            raise ApprovalReviewError("Approval rejected unsafe Tool arguments") from None
        except BaseException:
            raise ApprovalReviewError("Approval could not snapshot Tool arguments safely") from None
    try:
        return PlanApprovalSnapshot(
            plan_schema_version=plan.schema_version,
            task_id=plan.task_id,
            plan_id=plan.plan_id,
            operator_id=decision.operator_id,
            target=decision.target,
            execution_order=tuple(step.step_id for step in snapshots),
            steps=tuple(snapshots),
            verification_criteria=plan.verification_criteria,
        )
    except (ValidationError, ValueError):
        raise ApprovalReviewError("Approval could not create a canonical Plan snapshot") from None


def _snapshot_bindings(
    snapshot: PlanApprovalSnapshot,
) -> tuple[ApprovalStepBinding, ...]:
    return tuple(
        ApprovalStepBinding(
            step_index=step.step_index,
            step_id=step.step_id,
            role=step.role,
            tool_id=step.tool_id,
            tool_version=step.tool_version,
            contract_hash=step.contract_hash,
            implementation_hash=step.implementation_hash,
            arguments_hash=step.arguments_hash,
            target=step.target,
        )
        for step in snapshot.steps
    )


def _build_approval_record(
    *,
    approval_id: UUID,
    review: ApprovalReview,
    issued_at: datetime,
    expires_at: datetime,
) -> ApprovalRecord:
    bindings = _snapshot_bindings(review.snapshot)
    try:
        draft = ApprovalRecord.model_construct(
            approval_id=approval_id,
            review_id=review.review_id,
            decision="APPROVED",
            task_id=review.task_id,
            plan_id=review.plan_id,
            plan_hash=review.plan_hash,
            policy_id=review.policy_id,
            policy_version=review.policy_version,
            policy_hash=review.policy_hash,
            policy_decision_hash=review.policy_decision_hash,
            operator_id=review.operator_id,
            target=review.target,
            effective_risk=review.effective_risk,
            approval_requirement=review.approval_requirement,
            manual_confirmation_requirement=review.manual_confirmation_requirement,
            approver="local-owner",
            reviewed_at=issued_at,
            issued_at=issued_at,
            expires_at=expires_at,
            steps=bindings,
            content_hash="0" * 64,
        )
        content_hash = canonical_json_sha256(
            draft.model_dump(
                mode="json",
                exclude={"content_hash"},
                warnings="error",
            )
        )
        return ApprovalRecord(
            approval_id=approval_id,
            review_id=review.review_id,
            decision="APPROVED",
            task_id=review.task_id,
            plan_id=review.plan_id,
            plan_hash=review.plan_hash,
            policy_id=review.policy_id,
            policy_version=review.policy_version,
            policy_hash=review.policy_hash,
            policy_decision_hash=review.policy_decision_hash,
            operator_id=review.operator_id,
            target=review.target,
            effective_risk=review.effective_risk,
            approval_requirement=review.approval_requirement,
            manual_confirmation_requirement=review.manual_confirmation_requirement,
            approver="local-owner",
            reviewed_at=issued_at,
            issued_at=issued_at,
            expires_at=expires_at,
            steps=bindings,
            content_hash=content_hash,
        )
    except (CanonicalizationError, ValidationError, TypeError, ValueError):
        raise ApprovalStateError("Approval Record could not be issued safely") from None


def _build_confirmation_record(
    *,
    confirmation_id: UUID,
    approval: ApprovalRecord,
    snapshot_step: PlanStepApprovalSnapshot,
    policy_decision_hash: str,
    execution_attempt_id: UUID,
    invocation_id: UUID,
    issued_at: datetime,
    expires_at: datetime,
) -> ManualConfirmationRecord:
    try:
        draft = ManualConfirmationRecord.model_construct(
            confirmation_id=confirmation_id,
            approval_id=approval.approval_id,
            approval_record_hash=approval.content_hash,
            task_id=approval.task_id,
            plan_id=approval.plan_id,
            plan_hash=approval.plan_hash,
            policy_decision_hash=policy_decision_hash,
            execution_attempt_id=execution_attempt_id,
            invocation_id=invocation_id,
            step_index=snapshot_step.step_index,
            step_id=snapshot_step.step_id,
            role=snapshot_step.role,
            tool_id=snapshot_step.tool_id,
            tool_version=snapshot_step.tool_version,
            contract_hash=snapshot_step.contract_hash,
            implementation_hash=snapshot_step.implementation_hash,
            arguments_hash=snapshot_step.arguments_hash,
            target=snapshot_step.target,
            confirmer="local-owner",
            issued_at=issued_at,
            expires_at=expires_at,
            content_hash="0" * 64,
        )
        content_hash = canonical_json_sha256(
            draft.model_dump(
                mode="json",
                exclude={"content_hash"},
                warnings="error",
            )
        )
        return ManualConfirmationRecord(
            confirmation_id=confirmation_id,
            approval_id=approval.approval_id,
            approval_record_hash=approval.content_hash,
            task_id=approval.task_id,
            plan_id=approval.plan_id,
            plan_hash=approval.plan_hash,
            policy_decision_hash=policy_decision_hash,
            execution_attempt_id=execution_attempt_id,
            invocation_id=invocation_id,
            step_index=snapshot_step.step_index,
            step_id=snapshot_step.step_id,
            role=snapshot_step.role,
            tool_id=snapshot_step.tool_id,
            tool_version=snapshot_step.tool_version,
            contract_hash=snapshot_step.contract_hash,
            implementation_hash=snapshot_step.implementation_hash,
            arguments_hash=snapshot_step.arguments_hash,
            target=snapshot_step.target,
            confirmer="local-owner",
            issued_at=issued_at,
            expires_at=expires_at,
            content_hash=content_hash,
        )
    except (CanonicalizationError, ValidationError, TypeError, ValueError):
        raise ApprovalStateError("L3 confirmation could not be issued safely") from None


def _resolve_l3_step(
    snapshot: PlanApprovalSnapshot,
    decision: PolicyDecision,
    step_id: str,
) -> tuple[PlanStepApprovalSnapshot, StepPolicyDecision]:
    if (
        decision.effect is not PolicyEffect.ALLOW
        or decision.approval_requirement is not PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
        or decision.manual_confirmation_requirement
        is not ManualConfirmationRequirement.PER_INVOCATION
    ):
        raise ApprovalStateError("L3 confirmation requires an allowed L3 Policy Decision")
    for snapshot_step, decision_step in zip(
        snapshot.steps,
        decision.step_decisions,
        strict=True,
    ):
        if snapshot_step.step_id != step_id:
            continue
        if (
            snapshot_step.role is not StepRole.ACTION
            or decision_step.resolved_risk is not RiskLevel.L3
            or decision_step.manual_confirmation_requirement
            is not ManualConfirmationRequirement.PER_INVOCATION
        ):
            raise ApprovalStateError("Manual confirmation is only valid for an L3 ACTION step")
        return snapshot_step, decision_step
    raise ApprovalStateError("L3 confirmation Step is unknown")


def _review_event(
    *,
    event_id: UUID,
    sequence: int,
    kind: ApprovalAuditEventKind,
    occurred_at: datetime,
    review: ApprovalReview,
    actor: LocalOwner | None = None,
    reason_code: str | None = None,
) -> ApprovalAuditEvent:
    return ApprovalAuditEvent(
        event_id=event_id,
        sequence=sequence,
        kind=kind,
        occurred_at=occurred_at,
        review_id=review.review_id,
        task_id=review.task_id,
        plan_id=review.plan_id,
        plan_hash=review.plan_hash,
        policy_id=review.policy_id,
        policy_version=review.policy_version,
        policy_hash=review.policy_hash,
        policy_decision_hash=review.policy_decision_hash,
        operator_id=review.operator_id,
        target=review.target,
        effective_risk=review.effective_risk,
        approval_requirement=review.approval_requirement,
        manual_confirmation_requirement=review.manual_confirmation_requirement,
        actor=actor,
        expires_at=review.expires_at,
        step_bindings=_snapshot_bindings(review.snapshot),
        reason_code=reason_code,
    )


def _approval_event(
    *,
    event_id: UUID,
    sequence: int,
    kind: ApprovalAuditEventKind,
    occurred_at: datetime,
    record: ApprovalRecord,
    actor: LocalOwner | None = None,
    execution_attempt_id: UUID | None = None,
    reason_code: str | None = None,
) -> ApprovalAuditEvent:
    return ApprovalAuditEvent(
        event_id=event_id,
        sequence=sequence,
        kind=kind,
        occurred_at=occurred_at,
        review_id=record.review_id,
        approval_id=record.approval_id,
        task_id=record.task_id,
        plan_id=record.plan_id,
        plan_hash=record.plan_hash,
        policy_id=record.policy_id,
        policy_version=record.policy_version,
        policy_hash=record.policy_hash,
        policy_decision_hash=record.policy_decision_hash,
        operator_id=record.operator_id,
        target=record.target,
        effective_risk=record.effective_risk,
        approval_requirement=record.approval_requirement,
        manual_confirmation_requirement=record.manual_confirmation_requirement,
        actor=actor,
        approval_record_hash=record.content_hash,
        expires_at=record.expires_at,
        step_bindings=record.steps,
        execution_attempt_id=execution_attempt_id,
        reason_code=reason_code,
    )


def _confirmation_event(
    *,
    event_id: UUID,
    sequence: int,
    kind: ApprovalAuditEventKind,
    occurred_at: datetime,
    record: ManualConfirmationRecord,
    actor: LocalOwner | None = None,
    reason_code: str | None = None,
) -> ApprovalAuditEvent:
    return ApprovalAuditEvent(
        event_id=event_id,
        sequence=sequence,
        kind=kind,
        occurred_at=occurred_at,
        approval_id=record.approval_id,
        confirmation_id=record.confirmation_id,
        task_id=record.task_id,
        plan_id=record.plan_id,
        plan_hash=record.plan_hash,
        policy_decision_hash=record.policy_decision_hash,
        target=record.target,
        actor=actor,
        approval_record_hash=record.approval_record_hash,
        confirmation_record_hash=record.content_hash,
        expires_at=record.expires_at,
        step_bindings=(
            ApprovalStepBinding(
                step_index=record.step_index,
                step_id=record.step_id,
                role=record.role,
                tool_id=record.tool_id,
                tool_version=record.tool_version,
                contract_hash=record.contract_hash,
                implementation_hash=record.implementation_hash,
                arguments_hash=record.arguments_hash,
                target=record.target,
            ),
        ),
        execution_attempt_id=record.execution_attempt_id,
        invocation_id=record.invocation_id,
        step_index=record.step_index,
        step_id=record.step_id,
        tool_id=record.tool_id,
        tool_version=record.tool_version,
        contract_hash=record.contract_hash,
        implementation_hash=record.implementation_hash,
        arguments_hash=record.arguments_hash,
        reason_code=reason_code,
    )


def _invalid_result(
    *,
    reason: ApprovalValidationReason,
    checked_at: datetime | None,
    approval_id: UUID | None = None,
    confirmation_id: UUID | None = None,
    plan_hash: str | None = None,
    execution_attempt_id: UUID | None = None,
) -> ApprovalValidationResult:
    return ApprovalValidationResult(
        verdict=ApprovalValidationVerdict.INVALID,
        reason=reason,
        checked_at=checked_at,
        approval_id=approval_id,
        confirmation_id=confirmation_id,
        plan_hash=plan_hash,
        execution_attempt_id=execution_attempt_id,
    )


def _invalidation_reason(
    reason: ApprovalValidationReason,
) -> ApprovalInvalidationReason:
    if reason in {
        ApprovalValidationReason.POLICY_DENIED,
        ApprovalValidationReason.POLICY_MISMATCH,
        ApprovalValidationReason.WRONG_APPROVAL_MODE,
    }:
        return ApprovalInvalidationReason.POLICY_CHANGED
    if reason in {
        ApprovalValidationReason.PLAN_HASH_MISMATCH,
        ApprovalValidationReason.PLAN_SNAPSHOT_MISMATCH,
        ApprovalValidationReason.OPERATOR_MISMATCH,
        ApprovalValidationReason.TARGET_MISMATCH,
        ApprovalValidationReason.TOOL_INTEGRITY_MISMATCH,
        ApprovalValidationReason.ARGUMENTS_MISMATCH,
    }:
        return ApprovalInvalidationReason.PLAN_CHANGED
    return ApprovalInvalidationReason.SECURITY_CONDITION_CHANGED


def _require_uuid(value: UUID, label: str) -> None:
    if type(value) is not UUID:
        raise ApprovalStateError(f"{label} is malformed")


__all__ = ["ApprovalEngine", "Clock", "IdFactory"]
