"""Governed Executor that binds authorization to exact Tool dispatch."""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from hmac import compare_digest
from threading import RLock
from time import monotonic_ns
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel

from ai_server.approval.engine import ApprovalEngine
from ai_server.approval.errors import ApprovalError
from ai_server.executor.errors import (
    ExecutionAttemptError,
    ExecutionAuthorizationError,
    ExecutorConfigurationError,
)
from ai_server.models.approval import (
    ApprovalRecord,
    ApprovalValidationVerdict,
    ManualConfirmationRecord,
)
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.executor import (
    DispatchStatus,
    EffectDisposition,
    ExecutionAttemptAuthorization,
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionNextState,
    ExecutionReport,
    ExecutionReportStatus,
    ManualConfirmationChallenge,
    StepExecutionRecord,
)
from ai_server.models.policy import (
    ManualConfirmationRequirement,
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEffect,
    PolicyEvaluationContext,
    StepPolicyDecision,
)
from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.tool import (
    RiskLevel,
    TargetReference,
    ToolCall,
    ToolMetadata,
    ToolResult,
)
from ai_server.models.verification import VERIFICATION_CRITERION_TYPES
from ai_server.policy.engine import PolicyEngine
from ai_server.policy.errors import PolicyConfigurationError, PolicyEvaluationError
from ai_server.tools.gateway import (
    GatewayDispatchReceipt,
    GatewayDispatchStatus,
    InvalidGatewayConfigurationError,
    InvalidToolCallError,
    PostDispatchToolIntegrityError,
    ToolGateway,
    ToolGatewayError,
    ToolIntegrityError,
    ToolResolutionError,
)
from ai_server.tools.hashing import CanonicalizationError, canonical_json_sha256

type MonotonicClock = Callable[[], int]
type IdFactory = Callable[[], UUID]
type ManualConfirmationReader = Callable[[ManualConfirmationChallenge], str]


class _AttemptPhase(StrEnum):
    ACTIONS = "ACTIONS"
    VERIFICATION = "VERIFICATION"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class _Attempt:
    authorization: ExecutionAttemptAuthorization
    plan: ExecutionPlan
    decision: PolicyDecision
    phase: _AttemptPhase
    next_step_index: int
    records: tuple[StepExecutionRecord, ...]
    events: tuple[ExecutionEvent, ...]
    started_ns: int


class Executor:
    """Execute one immutable Plan through bound Policy, Approval, and Gateway."""

    def __init__(
        self,
        gateway: ToolGateway,
        policy: PolicyEngine,
        approval: ApprovalEngine,
        *,
        clock: MonotonicClock = monotonic_ns,
        attempt_id_factory: IdFactory = uuid4,
        invocation_id_factory: IdFactory = uuid4,
    ) -> None:
        """Bind the only production Tool dispatch boundary and its authorities."""
        if (
            type(gateway) is not ToolGateway
            or type(policy) is not PolicyEngine
            or type(approval) is not ApprovalEngine
            or not callable(clock)
            or not callable(attempt_id_factory)
            or not callable(invocation_id_factory)
        ):
            raise ExecutorConfigurationError("Executor configuration is malformed")
        self._gateway = gateway
        self._policy = policy
        self._approval = approval
        self._clock = clock
        self._attempt_id_factory = attempt_id_factory
        self._invocation_id_factory = invocation_id_factory
        self._attempts: dict[UUID, _Attempt] = {}
        self._invocation_ids: set[UUID] = set()
        self._active_attempt_calls: set[UUID] = set()
        self._lock = RLock()

    def begin_attempt(
        self,
        plan: ExecutionPlan,
        policy_decision: PolicyDecision,
        approval_id: UUID | None,
    ) -> ExecutionAttemptAuthorization:
        """Revalidate authority and open one process-local single-use attempt."""
        trusted_plan = _validate_plan(plan)
        trusted_decision = _validate_decision(policy_decision, trusted_plan)
        started_ns = _read_clock(self._clock)
        if started_ns is None:
            raise ExecutionAuthorizationError(
                "Executor clock failed before authorization",
                reason_code="executor_clock_failed",
            )
        try:
            plan_digest = canonical_json_sha256(trusted_plan)
            policy_decision_hash = canonical_json_sha256(trusted_decision)
        except (CanonicalizationError, TypeError, ValueError):
            raise ExecutionAuthorizationError(
                "Execution authorization inputs are not canonicalizable",
                reason_code="authorization_hash_failed",
            ) from None

        with self._lock:
            fresh_decision = self._fresh_policy_decision(trusted_plan, trusted_decision)
            _validate_l3_roles(trusted_plan, fresh_decision)
            self._validate_mutating_roles(trusted_plan)
            attempt_id = self._new_attempt_id()
            approval_record = None
            if fresh_decision.approval_requirement is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL:
                if type(approval_id) is not UUID:
                    raise ExecutionAuthorizationError(
                        "Human execution Approval is missing",
                        reason_code="approval_missing",
                    )
                validation = self._approval.consume_for_attempt(
                    approval_id,
                    trusted_plan,
                    fresh_decision,
                    attempt_id,
                )
                if validation.verdict is not ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT:
                    raise ExecutionAuthorizationError(
                        "Execution Approval could not authorize this attempt",
                        reason_code=validation.reason.value,
                    )
                try:
                    approval_record = self._approval.record_for_attempt(
                        approval_id,
                        attempt_id,
                    )
                except ApprovalError:
                    _close_consumed_approval(self._approval, approval_id, attempt_id)
                    raise ExecutionAuthorizationError(
                        "Execution Approval evidence became unavailable",
                        reason_code="approval_record_unavailable",
                    ) from None
            elif approval_id is not None:
                raise ExecutionAuthorizationError(
                    "NOT_REQUIRED execution cannot consume a human Approval",
                    reason_code="unexpected_approval",
                )

            try:
                authorization = _build_authorization(
                    attempt_id=attempt_id,
                    plan=trusted_plan,
                    plan_digest=plan_digest,
                    decision=fresh_decision,
                    policy_decision_hash=policy_decision_hash,
                    approval_record=approval_record,
                )
            except BaseException:
                if approval_record is not None:
                    _close_consumed_approval(
                        self._approval,
                        approval_record.approval_id,
                        attempt_id,
                    )
                raise ExecutionAuthorizationError(
                    "Executor could not create trusted authorization evidence",
                    reason_code="authorization_evidence_failed",
                ) from None

            attempt = _Attempt(
                authorization=authorization,
                plan=trusted_plan,
                decision=fresh_decision,
                phase=_AttemptPhase.ACTIONS,
                next_step_index=0,
                records=(),
                events=(
                    ExecutionEvent(
                        sequence=0,
                        kind=ExecutionEventKind.ATTEMPT_AUTHORIZED,
                        execution_attempt_id=attempt_id,
                    ),
                ),
                started_ns=started_ns,
            )
            self._attempts[attempt_id] = attempt
            return authorization

    def execute_actions(
        self,
        authorization: ExecutionAttemptAuthorization,
        confirmation_reader: ManualConfirmationReader | None = None,
    ) -> ExecutionReport:
        """Execute the exact OBSERVE/ACTION prefix once and stop before VERIFY."""
        with self._lock:
            attempt = self._resolve_attempt(authorization, _AttemptPhase.ACTIONS)
            attempt_id = attempt.authorization.execution_attempt_id
            if attempt_id in self._active_attempt_calls:
                raise ExecutionAttemptError("Execution attempt already has an active call")
            self._active_attempt_calls.add(attempt_id)
            try:
                while (
                    attempt.next_step_index < len(attempt.plan.steps)
                    and attempt.plan.steps[attempt.next_step_index].role is not StepRole.VERIFY
                ):
                    attempt, failure_code = self._execute_next_step(
                        attempt,
                        confirmation_reader=confirmation_reader,
                    )
                    self._attempts[attempt_id] = attempt
                    if failure_code is not None:
                        return self._finish_failed(attempt, failure_code)

                if attempt.next_step_index < len(attempt.plan.steps):
                    attempt = replace(
                        attempt,
                        phase=_AttemptPhase.VERIFICATION,
                        events=(
                            *attempt.events,
                            _attempt_event(
                                attempt,
                                ExecutionEventKind.PHASE_READY,
                            ),
                        ),
                    )
                    self._attempts[attempt_id] = attempt
                    return self._success_report(
                        attempt,
                        status=ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH,
                        close=False,
                    )
                return self._success_report(
                    attempt,
                    status=ExecutionReportStatus.READY_FOR_VERIFIER,
                    close=True,
                )
            finally:
                self._active_attempt_calls.discard(attempt_id)

    def execute_verification(
        self,
        authorization: ExecutionAttemptAuthorization,
    ) -> ExecutionReport:
        """Execute the exact VERIFY suffix once for Runtime's Verifier."""
        with self._lock:
            attempt = self._resolve_attempt(authorization, _AttemptPhase.VERIFICATION)
            attempt_id = attempt.authorization.execution_attempt_id
            if attempt_id in self._active_attempt_calls:
                raise ExecutionAttemptError("Execution attempt already has an active call")
            self._active_attempt_calls.add(attempt_id)
            try:
                while attempt.next_step_index < len(attempt.plan.steps):
                    if attempt.plan.steps[attempt.next_step_index].role is not StepRole.VERIFY:
                        return self._finish_failed(attempt, "verification_order_invalid")
                    attempt, failure_code = self._execute_next_step(
                        attempt,
                        confirmation_reader=None,
                    )
                    self._attempts[attempt_id] = attempt
                    if failure_code is not None:
                        return self._finish_failed(attempt, failure_code)
                return self._success_report(
                    attempt,
                    status=ExecutionReportStatus.READY_FOR_VERIFIER,
                    close=True,
                )
            finally:
                self._active_attempt_calls.discard(attempt_id)

    def abort_attempt(
        self,
        authorization: ExecutionAttemptAuthorization,
        *,
        reason_code: str = "attempt_aborted",
    ) -> ExecutionReport:
        """Close one open attempt without dispatching any remaining Step."""
        with self._lock:
            attempt = self._resolve_attempt(
                authorization,
                (_AttemptPhase.ACTIONS, _AttemptPhase.VERIFICATION),
            )
            attempt_id = attempt.authorization.execution_attempt_id
            if attempt_id in self._active_attempt_calls:
                raise ExecutionAttemptError("Execution attempt already has an active call")
            self._active_attempt_calls.add(attempt_id)
            try:
                if (
                    type(reason_code) is not str
                    or not reason_code
                    or not reason_code[0].islower()
                    or any(
                        not (character.islower() or character.isdigit() or character == "_")
                        for character in reason_code
                    )
                ):
                    raise ExecutionAttemptError("Attempt abort reason is malformed")
                return self._finish_failed(attempt, reason_code)
            finally:
                self._active_attempt_calls.discard(attempt_id)

    def _execute_next_step(
        self,
        attempt: _Attempt,
        *,
        confirmation_reader: ManualConfirmationReader | None,
    ) -> tuple[_Attempt, str | None]:
        step_index = attempt.next_step_index
        step = attempt.plan.steps[step_index]
        try:
            fresh_decision = self._fresh_policy_decision(attempt.plan, attempt.decision)
            step_decision = fresh_decision.step_decisions[step_index]
            invocation_id = self._new_invocation_id()
            call = _build_call(attempt.plan, step, fresh_decision, invocation_id)
        except ExecutionAuthorizationError as error:
            return attempt, error.reason_code
        except BaseException:
            return attempt, "tool_call_build_failed"

        if self._approval_must_be_revalidated(attempt):
            approval_id = attempt.authorization.approval_id
            if approval_id is None:
                return self._append_failed_record(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    failure_code="approval_missing",
                )
            validation = self._approval.validate_for_attempt(
                approval_id,
                attempt.plan,
                fresh_decision,
                attempt.authorization.execution_attempt_id,
            )
            if validation.verdict is not ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT:
                return self._append_failed_record(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    failure_code=validation.reason.value,
                )

        confirmation: ManualConfirmationRecord | None = None
        if (
            step_decision.manual_confirmation_requirement
            is ManualConfirmationRequirement.PER_INVOCATION
        ):
            challenge = _build_confirmation_challenge(
                attempt.authorization,
                step_index,
                step,
                call,
            )
            if confirmation_reader is None:
                return self._append_failed_record(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    failure_code="l3_confirmation_unavailable",
                )
            try:
                response = confirmation_reader(challenge)
            except BaseException:
                response = None
            expected = f"CONFIRM {challenge.challenge_hash}"
            if type(response) is not str or not compare_digest(response, expected):
                return self._append_failed_record(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    failure_code="l3_confirmation_rejected",
                )
            approval_id = cast(UUID, attempt.authorization.approval_id)
            try:
                confirmation = self._approval.issue_l3_confirmation(
                    approval_id,
                    attempt.plan,
                    fresh_decision,
                    execution_attempt_id=attempt.authorization.execution_attempt_id,
                    invocation_id=invocation_id,
                    step_id=step.step_id,
                )
            except ApprovalError:
                return self._append_failed_record(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    failure_code="l3_confirmation_issue_failed",
                )
            confirmation_validation = self._approval.consume_l3_confirmation(
                confirmation.confirmation_id,
                approval_id,
                attempt.plan,
                fresh_decision,
                execution_attempt_id=attempt.authorization.execution_attempt_id,
                invocation_id=invocation_id,
                step_id=step.step_id,
            )
            if (
                confirmation_validation.verdict
                is not ApprovalValidationVerdict.VALID_FOR_BOUND_ATTEMPT
            ):
                return self._append_failed_record(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    failure_code=confirmation_validation.reason.value,
                )
            try:
                receipt = self._gateway._invoke_with_receipt(call)
            except ToolGatewayError as error:
                return self._append_gateway_exception(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    error=error,
                    confirmation=confirmation,
                )
            except BaseException:
                return self._append_unknown_dispatch(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    confirmation=confirmation,
                )
        else:
            try:
                receipt = self._gateway._invoke_with_receipt(call)
            except ToolGatewayError as error:
                return self._append_gateway_exception(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    error=error,
                    confirmation=None,
                )
            except BaseException:
                return self._append_unknown_dispatch(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    confirmation=None,
                )

        return self._append_receipt(
            attempt,
            step_index=step_index,
            step=step,
            call=call,
            receipt=receipt,
            confirmation=confirmation,
        )

    def _fresh_policy_decision(
        self,
        plan: ExecutionPlan,
        expected: PolicyDecision,
    ) -> PolicyDecision:
        try:
            raw = self._policy.evaluate(
                plan,
                PolicyEvaluationContext(
                    operator_id=expected.operator_id,
                    target=expected.target,
                ),
            )
            fresh = _validate_decision(raw, plan)
        except (
            PolicyConfigurationError,
            PolicyEvaluationError,
            ExecutionAuthorizationError,
        ):
            raise ExecutionAuthorizationError(
                "Policy revalidation failed safely",
                reason_code="policy_revalidation_failed",
            ) from None
        except BaseException:
            raise ExecutionAuthorizationError(
                "Policy revalidation failed safely",
                reason_code="policy_revalidation_failed",
            ) from None
        if fresh != expected or fresh.effect is not PolicyEffect.ALLOW:
            raise ExecutionAuthorizationError(
                "Policy authority changed before execution",
                reason_code="policy_revalidation_failed",
            )
        return fresh

    def _new_attempt_id(self) -> UUID:
        try:
            identifier = self._attempt_id_factory()
        except BaseException:
            identifier = None
        if type(identifier) is not UUID or identifier in self._attempts:
            raise ExecutionAuthorizationError(
                "Executor produced an invalid Attempt identity",
                reason_code="attempt_id_invalid",
            )
        return identifier

    def _new_invocation_id(self) -> UUID:
        try:
            identifier = self._invocation_id_factory()
        except BaseException:
            identifier = None
        if type(identifier) is not UUID or identifier in self._invocation_ids:
            raise ExecutionAuthorizationError(
                "Executor produced an invalid Invocation identity",
                reason_code="invocation_id_invalid",
            )
        self._invocation_ids.add(identifier)
        return identifier

    def _resolve_attempt(
        self,
        authorization: ExecutionAttemptAuthorization,
        expected_phase: _AttemptPhase | tuple[_AttemptPhase, ...],
    ) -> _Attempt:
        trusted = _validate_authorization(authorization)
        attempt = self._attempts.get(trusted.execution_attempt_id)
        if attempt is None or attempt.authorization != trusted:
            raise ExecutionAttemptError("Execution authorization is unknown or forged")
        phases = expected_phase if type(expected_phase) is tuple else (expected_phase,)
        if attempt.phase not in phases:
            raise ExecutionAttemptError("Execution attempt is closed, replayed, or out of order")
        return attempt

    def _approval_must_be_revalidated(
        self,
        attempt: _Attempt,
    ) -> bool:
        return (
            attempt.authorization.approval_requirement
            is PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL
        )

    def _append_receipt(
        self,
        attempt: _Attempt,
        *,
        step_index: int,
        step: ExecutionStep,
        call: ToolCall[GetSystemStatusArguments],
        receipt: GatewayDispatchReceipt,
        confirmation: ManualConfirmationRecord | None,
    ) -> tuple[_Attempt, str | None]:
        expected_mutation = self._authoritative_mutation_flag(step)
        if (
            type(receipt) is not GatewayDispatchReceipt
            or type(receipt.dispatch_status) is not GatewayDispatchStatus
            or type(receipt.mutates_remote_state) is not bool
            or expected_mutation is None
            or receipt.mutates_remote_state is not expected_mutation
        ):
            return self._append_failed_record(
                attempt,
                step_index=step_index,
                step=step,
                call=call,
                failure_code="malformed_gateway_receipt",
                confirmation=confirmation,
                dispatch_status=DispatchStatus.UNKNOWN,
                effect_disposition=EffectDisposition.UNKNOWN,
            )
        try:
            result = _validate_result(receipt.result, call)
            if self._gateway.validate_result(call, result) is not True:
                raise ExecutionAttemptError("Gateway result failed its registered Tool Contract")
        except BaseException:
            return self._append_failed_record(
                attempt,
                step_index=step_index,
                step=step,
                call=call,
                failure_code="malformed_gateway_receipt",
                confirmation=confirmation,
                dispatch_status=DispatchStatus.UNKNOWN,
                effect_disposition=EffectDisposition.UNKNOWN,
            )
        if (
            result.success
            and receipt.dispatch_status is not GatewayDispatchStatus.HANDLER_DISPATCHED
        ):
            return self._append_failed_record(
                attempt,
                step_index=step_index,
                step=step,
                call=call,
                failure_code="malformed_gateway_receipt",
                confirmation=confirmation,
                dispatch_status=DispatchStatus.UNKNOWN,
                effect_disposition=EffectDisposition.UNKNOWN,
            )
        dispatch_status = _dispatch_status(receipt.dispatch_status)
        effect = _effect_disposition(
            dispatch_status=dispatch_status,
            mutates_remote_state=receipt.mutates_remote_state,
            success=result.success,
        )
        failure_code = None
        if not result.success:
            if result.error is None:
                return self._append_failed_record(
                    attempt,
                    step_index=step_index,
                    step=step,
                    call=call,
                    failure_code="malformed_gateway_receipt",
                    confirmation=confirmation,
                    dispatch_status=dispatch_status,
                    effect_disposition=effect,
                )
            failure_code = result.error.code
        record = _step_record(
            step_index=step_index,
            step=step,
            call=call,
            dispatch_status=dispatch_status,
            effect_disposition=effect,
            result=result,
            failure_code=failure_code,
            confirmation=confirmation,
        )
        updated = _append_record(attempt, record)
        return updated, failure_code

    def _authoritative_mutation_flag(self, step: ExecutionStep) -> bool | None:
        """Resolve reviewed side-effect authority for one exact planned Tool."""
        try:
            metadata = self._policy.metadata_for(step.tool_id, step.tool_version)
            if (
                type(metadata) is not ToolMetadata
                or metadata.contract_hash != step.contract_hash
                or metadata.implementation_hash != step.implementation_hash
                or type(metadata.side_effects.mutates_remote_state) is not bool
            ):
                return None
            return metadata.side_effects.mutates_remote_state
        except BaseException:
            return None

    def _validate_mutating_roles(self, plan: ExecutionPlan) -> None:
        """Restrict every metadata-declared mutating Tool to an ACTION Step."""
        for step in plan.steps:
            mutates_remote_state = self._authoritative_mutation_flag(step)
            if mutates_remote_state is None:
                raise ExecutionAuthorizationError(
                    "Executor could not resolve authoritative Tool side effects",
                    reason_code="mutation_role_invalid",
                )
            if mutates_remote_state and step.role is not StepRole.ACTION:
                raise ExecutionAuthorizationError(
                    "Mutating execution is restricted to ACTION steps",
                    reason_code="mutation_role_invalid",
                )

    def _append_gateway_exception(
        self,
        attempt: _Attempt,
        *,
        step_index: int,
        step: ExecutionStep,
        call: ToolCall[GetSystemStatusArguments],
        error: ToolGatewayError,
        confirmation: ManualConfirmationRecord | None,
    ) -> tuple[_Attempt, str]:
        expected_mutation = self._authoritative_mutation_flag(step)
        error_type = type(error)
        pre_dispatch_types = (
            InvalidToolCallError,
            InvalidGatewayConfigurationError,
            ToolResolutionError,
            ToolIntegrityError,
        )
        if error_type in pre_dispatch_types:
            status = DispatchStatus.NOT_DISPATCHED
            effect = EffectDisposition.NONE
            failure_code = error_type.code
        elif (
            error_type is PostDispatchToolIntegrityError
            and expected_mutation is not None
            and type(error.mutates_remote_state) is bool
            and error.mutates_remote_state is expected_mutation
        ):
            status = DispatchStatus.HANDLER_DISPATCHED
            effect = EffectDisposition.UNKNOWN if expected_mutation else EffectDisposition.NONE
            failure_code = PostDispatchToolIntegrityError.code
        else:
            return self._append_unknown_dispatch(
                attempt,
                step_index=step_index,
                step=step,
                call=call,
                confirmation=confirmation,
            )
        return self._append_failed_record(
            attempt,
            step_index=step_index,
            step=step,
            call=call,
            failure_code=failure_code,
            confirmation=confirmation,
            dispatch_status=status,
            effect_disposition=effect,
        )

    def _append_unknown_dispatch(
        self,
        attempt: _Attempt,
        *,
        step_index: int,
        step: ExecutionStep,
        call: ToolCall[GetSystemStatusArguments],
        confirmation: ManualConfirmationRecord | None,
    ) -> tuple[_Attempt, str]:
        return self._append_failed_record(
            attempt,
            step_index=step_index,
            step=step,
            call=call,
            failure_code="gateway_dispatch_unknown",
            confirmation=confirmation,
            dispatch_status=DispatchStatus.UNKNOWN,
            effect_disposition=EffectDisposition.UNKNOWN,
        )

    def _append_failed_record(
        self,
        attempt: _Attempt,
        *,
        step_index: int,
        step: ExecutionStep,
        call: ToolCall[GetSystemStatusArguments],
        failure_code: str,
        confirmation: ManualConfirmationRecord | None = None,
        dispatch_status: DispatchStatus = DispatchStatus.NOT_DISPATCHED,
        effect_disposition: EffectDisposition = EffectDisposition.NONE,
    ) -> tuple[_Attempt, str]:
        record = _step_record(
            step_index=step_index,
            step=step,
            call=call,
            dispatch_status=dispatch_status,
            effect_disposition=effect_disposition,
            result=None,
            failure_code=failure_code,
            confirmation=confirmation,
        )
        return _append_record(attempt, record), failure_code

    def _success_report(
        self,
        attempt: _Attempt,
        *,
        status: ExecutionReportStatus,
        close: bool,
    ) -> ExecutionReport:
        if close:
            closed, close_error = self._close_attempt(attempt)
            if close_error is not None:
                return self._finish_failed(closed, close_error)
            attempt = closed
        duration = _elapsed_ms(attempt.started_ns, self._clock)
        if duration is None:
            return self._finish_failed(attempt, "executor_clock_failed")
        if close:
            attempt = replace(
                attempt,
                events=(
                    *attempt.events,
                    _attempt_event(attempt, ExecutionEventKind.ATTEMPT_CLOSED),
                ),
            )
        self._attempts[attempt.authorization.execution_attempt_id] = attempt
        return _build_report(
            attempt,
            status=status,
            next_state=ExecutionNextState.VERIFYING,
            total_duration_ms=duration,
            failure_code=None,
            failed_step_index=None,
        )

    def _finish_failed(self, attempt: _Attempt, failure_code: str) -> ExecutionReport:
        closed, close_error = self._close_attempt(attempt)
        final_code = close_error or failure_code
        if not closed.events or closed.events[-1].kind is not ExecutionEventKind.ATTEMPT_FAILED:
            closed = replace(
                closed,
                events=(
                    *closed.events,
                    _attempt_event(
                        closed,
                        ExecutionEventKind.ATTEMPT_FAILED,
                        reason_code=final_code,
                    ),
                ),
            )
        closed = replace(
            closed,
            events=(
                *closed.events,
                _attempt_event(closed, ExecutionEventKind.ATTEMPT_CLOSED),
            ),
        )
        self._attempts[closed.authorization.execution_attempt_id] = closed
        if closed.records and closed.records[-1].failure_code is not None:
            failed_step_index = closed.records[-1].step_index
        elif closed.next_step_index < len(closed.plan.steps):
            failed_step_index = closed.next_step_index
        else:
            failed_step_index = None
        return _build_report(
            closed,
            status=ExecutionReportStatus.FAILED,
            next_state=ExecutionNextState.FAILED,
            total_duration_ms=_elapsed_ms(closed.started_ns, self._clock),
            failure_code=final_code,
            failed_step_index=failed_step_index,
        )

    def _close_attempt(self, attempt: _Attempt) -> tuple[_Attempt, str | None]:
        if attempt.phase is _AttemptPhase.CLOSED:
            return attempt, None
        error_code = None
        approval_id = attempt.authorization.approval_id
        if approval_id is not None:
            try:
                self._approval.close_attempt(
                    approval_id,
                    attempt.authorization.execution_attempt_id,
                )
            except ApprovalError:
                error_code = "attempt_close_failed"
        return replace(attempt, phase=_AttemptPhase.CLOSED), error_code


def _validate_plan(plan: ExecutionPlan) -> ExecutionPlan:
    try:
        if (
            type(plan) is not ExecutionPlan
            or type(plan.steps) is not tuple
            or type(plan.verification_criteria) is not tuple
            or any(
                type(criterion) not in VERIFICATION_CRITERION_TYPES
                for criterion in plan.verification_criteria
            )
            or any(
                type(step) is not ExecutionStep
                or type(step.role) is not StepRole
                or type(step.arguments) is not GetSystemStatusArguments
                for step in plan.steps
            )
        ):
            raise TypeError
        return ExecutionPlan.model_validate(
            plan.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise ExecutionAuthorizationError(
            "Executor received a malformed Plan",
            reason_code="plan_malformed",
        ) from None


def _validate_decision(
    decision: PolicyDecision,
    plan: ExecutionPlan,
) -> PolicyDecision:
    try:
        if (
            type(decision) is not PolicyDecision
            or type(decision.step_decisions) is not tuple
            or any(type(step) is not StepPolicyDecision for step in decision.step_decisions)
        ):
            raise TypeError
        validated = PolicyDecision.model_validate(
            decision.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise ExecutionAuthorizationError(
            "Executor received a malformed Policy decision",
            reason_code="policy_decision_malformed",
        ) from None
    if (
        validated.task_id != plan.task_id
        or validated.plan_id != plan.plan_id
        or validated.target.target_id != plan.target
        or validated.target.resource_id != plan.target
        or len(validated.step_decisions) != len(plan.steps)
        or validated.effect is not PolicyEffect.ALLOW
    ):
        raise ExecutionAuthorizationError(
            "Policy decision does not authorize the exact Plan",
            reason_code="policy_decision_mismatch",
        )
    for step, step_decision in zip(plan.steps, validated.step_decisions, strict=True):
        try:
            arguments_hash = canonical_json_sha256(step.arguments)
        except BaseException:
            raise ExecutionAuthorizationError(
                "Plan arguments are not canonicalizable",
                reason_code="arguments_hash_failed",
            ) from None
        if (
            step_decision.step_id != step.step_id
            or step_decision.tool_id != step.tool_id
            or step_decision.tool_version != step.tool_version
            or step_decision.contract_hash != step.contract_hash
            or step_decision.implementation_hash != step.implementation_hash
            or step_decision.arguments_hash != arguments_hash
            or step_decision.effect is not PolicyEffect.ALLOW
        ):
            raise ExecutionAuthorizationError(
                "Policy Step decision does not match the exact Plan",
                reason_code="policy_step_mismatch",
            )
    return validated


def _validate_l3_roles(plan: ExecutionPlan, decision: PolicyDecision) -> None:
    for step, step_decision in zip(plan.steps, decision.step_decisions, strict=True):
        if step_decision.resolved_risk is RiskLevel.L3 and step.role is not StepRole.ACTION:
            raise ExecutionAuthorizationError(
                "L3 execution is restricted to ACTION steps",
                reason_code="l3_role_invalid",
            )


def _validate_authorization(
    authorization: ExecutionAttemptAuthorization,
) -> ExecutionAttemptAuthorization:
    try:
        if type(authorization) is not ExecutionAttemptAuthorization:
            raise TypeError
        return ExecutionAttemptAuthorization.model_validate(
            authorization.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise ExecutionAttemptError("Execution authorization is malformed") from None


def _build_authorization(
    *,
    attempt_id: UUID,
    plan: ExecutionPlan,
    plan_digest: str,
    decision: PolicyDecision,
    policy_decision_hash: str,
    approval_record: ApprovalRecord | None,
) -> ExecutionAttemptAuthorization:
    approval_id = approval_record.approval_id if approval_record is not None else None
    approval_plan_hash = approval_record.plan_hash if approval_record is not None else None
    approval_record_hash = approval_record.content_hash if approval_record is not None else None
    approval_expires_at = approval_record.expires_at if approval_record is not None else None
    draft = ExecutionAttemptAuthorization.model_construct(
        authorization_schema_version="1",
        execution_attempt_id=attempt_id,
        task_id=plan.task_id,
        plan_id=plan.plan_id,
        plan_digest=plan_digest,
        policy_decision_hash=policy_decision_hash,
        approval_requirement=cast(
            PolicyApprovalRequirement,
            decision.approval_requirement,
        ),
        approval_id=approval_id,
        approval_plan_hash=approval_plan_hash,
        approval_record_hash=approval_record_hash,
        approval_expires_at=approval_expires_at,
        content_hash="0" * 64,
    )
    content_hash = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"content_hash"}, warnings="error")
    )
    return ExecutionAttemptAuthorization(
        execution_attempt_id=attempt_id,
        task_id=plan.task_id,
        plan_id=plan.plan_id,
        plan_digest=plan_digest,
        policy_decision_hash=policy_decision_hash,
        approval_requirement=cast(
            PolicyApprovalRequirement,
            decision.approval_requirement,
        ),
        approval_id=approval_id,
        approval_plan_hash=approval_plan_hash,
        approval_record_hash=approval_record_hash,
        approval_expires_at=approval_expires_at,
        content_hash=content_hash,
    )


def _build_call(
    plan: ExecutionPlan,
    step: ExecutionStep,
    decision: PolicyDecision,
    invocation_id: UUID,
) -> ToolCall[GetSystemStatusArguments]:
    if step.arguments.target != plan.target:
        raise ExecutionAuthorizationError(
            "Planned target selector changed before dispatch",
            reason_code="target_mismatch",
        )
    try:
        return ToolCall[GetSystemStatusArguments](
            invocation_id=invocation_id,
            plan_step_id=step.step_id,
            tool_id=step.tool_id,
            tool_version=step.tool_version,
            contract_hash=step.contract_hash,
            implementation_hash=step.implementation_hash,
            arguments_hash=canonical_json_sha256(step.arguments),
            target=TargetReference(
                target_id=plan.target,
                resource_type=decision.target.resource_type,
                resource_id=step.arguments.target,
            ),
            arguments=step.arguments,
        )
    except BaseException:
        raise ExecutionAuthorizationError(
            "Executor could not build the exact ToolCall",
            reason_code="tool_call_build_failed",
        ) from None


def _build_confirmation_challenge(
    authorization: ExecutionAttemptAuthorization,
    step_index: int,
    step: ExecutionStep,
    call: ToolCall[GetSystemStatusArguments],
) -> ManualConfirmationChallenge:
    if (
        authorization.approval_id is None
        or authorization.approval_plan_hash is None
        or authorization.approval_record_hash is None
        or authorization.approval_expires_at is None
        or step.role is not StepRole.ACTION
    ):
        raise ExecutionAuthorizationError(
            "L3 Challenge lacks exact Approval evidence",
            reason_code="l3_challenge_invalid",
        )
    draft = ManualConfirmationChallenge.model_construct(
        challenge_schema_version="1",
        authorization_hash=authorization.content_hash,
        approval_id=authorization.approval_id,
        approval_plan_hash=authorization.approval_plan_hash,
        approval_record_hash=authorization.approval_record_hash,
        approval_expires_at=authorization.approval_expires_at,
        execution_attempt_id=authorization.execution_attempt_id,
        invocation_id=call.invocation_id,
        step_index=step_index,
        step_id=step.step_id,
        role=StepRole.ACTION,
        tool_id=step.tool_id,
        tool_version=step.tool_version,
        contract_hash=step.contract_hash,
        implementation_hash=step.implementation_hash,
        arguments_hash=call.arguments_hash,
        target=call.target,
        challenge_hash="0" * 64,
    )
    challenge_hash = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"challenge_hash"}, warnings="error")
    )
    return ManualConfirmationChallenge(
        authorization_hash=authorization.content_hash,
        approval_id=authorization.approval_id,
        approval_plan_hash=authorization.approval_plan_hash,
        approval_record_hash=authorization.approval_record_hash,
        approval_expires_at=authorization.approval_expires_at,
        execution_attempt_id=authorization.execution_attempt_id,
        invocation_id=call.invocation_id,
        step_index=step_index,
        step_id=step.step_id,
        role=StepRole.ACTION,
        tool_id=step.tool_id,
        tool_version=step.tool_version,
        contract_hash=step.contract_hash,
        implementation_hash=step.implementation_hash,
        arguments_hash=call.arguments_hash,
        target=call.target,
        challenge_hash=challenge_hash,
    )


def _validate_result(
    raw_result: ToolResult[BaseModel],
    call: ToolCall[GetSystemStatusArguments],
) -> ToolResult[SystemStatus]:
    try:
        if (
            not isinstance(raw_result, ToolResult)
            or (raw_result.data is not None and type(raw_result.data) is not SystemStatus)
            or (
                type(raw_result.data) is SystemStatus
                and (
                    type(raw_result.data.services) is not tuple
                    or any(
                        type(service) is not ServiceStatus for service in raw_result.data.services
                    )
                )
            )
        ):
            raise TypeError
        result = ToolResult[SystemStatus].model_validate(
            raw_result.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise ExecutionAttemptError("Gateway returned malformed Tool evidence") from None
    if (
        result.invocation_id != call.invocation_id
        or result.plan_step_id != call.plan_step_id
        or result.tool_id != call.tool_id
        or result.tool_version != call.tool_version
        or result.contract_hash != call.contract_hash
        or result.arguments_hash != call.arguments_hash
        or result.target != call.target
    ):
        raise ExecutionAttemptError("ToolResult identity differs from the exact ToolCall")
    return result


def _step_record(
    *,
    step_index: int,
    step: ExecutionStep,
    call: ToolCall[GetSystemStatusArguments],
    dispatch_status: DispatchStatus,
    effect_disposition: EffectDisposition,
    result: ToolResult[SystemStatus] | None,
    failure_code: str | None,
    confirmation: ManualConfirmationRecord | None,
) -> StepExecutionRecord:
    return StepExecutionRecord(
        step_index=step_index,
        step_id=step.step_id,
        role=step.role,
        tool_id=step.tool_id,
        tool_version=step.tool_version,
        contract_hash=step.contract_hash,
        implementation_hash=step.implementation_hash,
        arguments_hash=call.arguments_hash,
        target=call.target,
        invocation_id=call.invocation_id,
        dispatch_status=dispatch_status,
        effect_disposition=effect_disposition,
        confirmation_id=confirmation.confirmation_id if confirmation is not None else None,
        confirmation_record_hash=confirmation.content_hash if confirmation is not None else None,
        result=result,
        failure_code=failure_code,
    )


def _append_record(attempt: _Attempt, record: StepExecutionRecord) -> _Attempt:
    event = ExecutionEvent(
        sequence=len(attempt.events),
        kind=ExecutionEventKind.STEP_FINISHED,
        execution_attempt_id=attempt.authorization.execution_attempt_id,
        step_index=record.step_index,
        step_id=record.step_id,
        invocation_id=record.invocation_id,
        dispatch_status=record.dispatch_status,
        effect_disposition=record.effect_disposition,
    )
    return replace(
        attempt,
        next_step_index=record.step_index + 1,
        records=(*attempt.records, record),
        events=(*attempt.events, event),
    )


def _attempt_event(
    attempt: _Attempt,
    kind: ExecutionEventKind,
    *,
    reason_code: str | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        sequence=len(attempt.events),
        kind=kind,
        execution_attempt_id=attempt.authorization.execution_attempt_id,
        reason_code=reason_code,
    )


def _build_report(
    attempt: _Attempt,
    *,
    status: ExecutionReportStatus,
    next_state: ExecutionNextState,
    total_duration_ms: int | None,
    failure_code: str | None,
    failed_step_index: int | None,
) -> ExecutionReport:
    human_intervention_required = any(
        record.effect_disposition is EffectDisposition.UNKNOWN for record in attempt.records
    )
    draft = ExecutionReport.model_construct(
        report_schema_version="1",
        execution_attempt_id=attempt.authorization.execution_attempt_id,
        authorization_hash=attempt.authorization.content_hash,
        task_id=attempt.authorization.task_id,
        plan_id=attempt.authorization.plan_id,
        plan_digest=attempt.authorization.plan_digest,
        policy_decision_hash=attempt.authorization.policy_decision_hash,
        approval_id=attempt.authorization.approval_id,
        status=status,
        next_state=next_state,
        records=attempt.records,
        events=attempt.events,
        total_duration_ms=total_duration_ms,
        failed_step_index=failed_step_index,
        failure_code=failure_code,
        human_intervention_required=human_intervention_required,
        content_hash="0" * 64,
    )
    content_hash = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"content_hash"}, warnings="error")
    )
    return ExecutionReport(
        execution_attempt_id=attempt.authorization.execution_attempt_id,
        authorization_hash=attempt.authorization.content_hash,
        task_id=attempt.authorization.task_id,
        plan_id=attempt.authorization.plan_id,
        plan_digest=attempt.authorization.plan_digest,
        policy_decision_hash=attempt.authorization.policy_decision_hash,
        approval_id=attempt.authorization.approval_id,
        status=status,
        next_state=next_state,
        records=attempt.records,
        events=attempt.events,
        total_duration_ms=total_duration_ms,
        failed_step_index=failed_step_index,
        failure_code=failure_code,
        human_intervention_required=human_intervention_required,
        content_hash=content_hash,
    )


def _dispatch_status(value: GatewayDispatchStatus) -> DispatchStatus:
    return {
        GatewayDispatchStatus.NOT_DISPATCHED: DispatchStatus.NOT_DISPATCHED,
        GatewayDispatchStatus.HANDLER_DISPATCHED: DispatchStatus.HANDLER_DISPATCHED,
    }[value]


def _effect_disposition(
    *,
    dispatch_status: DispatchStatus,
    mutates_remote_state: bool,
    success: bool,
) -> EffectDisposition:
    if dispatch_status is DispatchStatus.NOT_DISPATCHED or not mutates_remote_state:
        return EffectDisposition.NONE
    return EffectDisposition.PENDING_VERIFICATION if success else EffectDisposition.UNKNOWN


def _read_clock(clock: MonotonicClock) -> int | None:
    try:
        value = clock()
    except BaseException:
        return None
    return value if type(value) is int and value >= 0 else None


def _elapsed_ms(started_ns: int, clock: MonotonicClock) -> int | None:
    ended_ns = _read_clock(clock)
    if ended_ns is None or ended_ns < started_ns:
        return None
    elapsed = ended_ns - started_ns
    return 0 if elapsed == 0 else (elapsed + 999_999) // 1_000_000


def _close_consumed_approval(
    approval: ApprovalEngine,
    approval_id: UUID,
    attempt_id: UUID,
) -> None:
    with suppress(ApprovalError):
        approval.close_attempt(approval_id, attempt_id)


__all__ = [
    "Executor",
    "IdFactory",
    "ManualConfirmationReader",
    "MonotonicClock",
]
