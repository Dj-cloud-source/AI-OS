"""Pure deterministic verification of hash-bound structured Tool evidence."""

from datetime import timedelta

from ai_server.models.execution import ExecutionPlan, ExecutionStep
from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.tool import TargetReference, ToolError, ToolResult
from ai_server.models.verification import (
    VERIFICATION_CRITERION_TYPES,
    EqualityCriterion,
    ExpectedStateCriterion,
    HealthStatusCriterion,
    NumericBoundsCriterion,
    VerificationCheckResult,
    VerificationCheckStatus,
    VerificationContext,
    VerificationCriterion,
    VerificationEffectDisposition,
    VerificationEvidenceReference,
    VerificationFailureReason,
    VerificationResult,
    VerificationStatus,
)
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.verifier.errors import VerificationInputError


def _trusted_context(context: VerificationContext) -> VerificationContext:
    """Revalidate an exact Context without reading external state."""
    try:
        if type(context) is not VerificationContext:
            raise TypeError
        return VerificationContext.model_validate(
            context.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise VerificationInputError("Verification Context is not trustworthy") from None


def _criteria_from_plan(plan: ExecutionPlan) -> tuple[VerificationCriterion, ...] | None:
    """Return exact criteria when the Plan has a trustworthy bounded shape."""
    raw = plan.verification_criteria
    if type(raw) is not tuple or not raw:
        return None
    if len(raw) > 128 or any(type(item) not in VERIFICATION_CRITERION_TYPES for item in raw):
        return None
    try:
        criteria: tuple[VerificationCriterion, ...] = tuple(
            _trusted_criterion(item) for item in raw
        )
    except BaseException:
        return None
    criterion_ids = tuple(item.criterion_id for item in criteria)
    if len(criterion_ids) != len(set(criterion_ids)):
        return None
    return criteria


def _trusted_criterion(criterion: VerificationCriterion) -> VerificationCriterion:
    """Strictly rebuild one exact allowlisted criterion class."""
    document = criterion.model_dump(mode="python", warnings="error")
    if type(criterion) is EqualityCriterion:
        return EqualityCriterion.model_validate(document, strict=True)
    if type(criterion) is NumericBoundsCriterion:
        return NumericBoundsCriterion.model_validate(document, strict=True)
    if type(criterion) is ExpectedStateCriterion:
        return ExpectedStateCriterion.model_validate(document, strict=True)
    if type(criterion) is HealthStatusCriterion:
        return HealthStatusCriterion.model_validate(document, strict=True)
    raise TypeError


def _trusted_plan(
    plan: ExecutionPlan,
    context: VerificationContext,
) -> tuple[
    ExecutionPlan | None, tuple[VerificationCriterion, ...], VerificationFailureReason | None
]:
    """Establish exact Plan bindings and return a stable failure when possible."""
    if type(plan) is not ExecutionPlan:
        raise VerificationInputError("Verification Plan is not trustworthy")
    try:
        task_id = plan.task_id
        plan_id = plan.plan_id
    except BaseException:
        raise VerificationInputError("Verification Plan binding is unavailable") from None
    if task_id != context.task_id or plan_id != context.plan_id:
        return None, (), VerificationFailureReason.PLAN_BINDING_MISMATCH
    try:
        plan_digest = canonical_json_sha256(plan)
    except BaseException:
        return None, (), VerificationFailureReason.MALFORMED_PLAN
    if plan_digest != context.plan_digest:
        return None, (), VerificationFailureReason.PLAN_BINDING_MISMATCH

    criteria = _criteria_from_plan(plan)
    try:
        if (
            criteria is None
            or type(plan.steps) is not tuple
            or any(
                type(step) is not ExecutionStep
                or type(step.arguments) is not GetSystemStatusArguments
                for step in plan.steps
            )
        ):
            raise TypeError
        trusted = ExecutionPlan.model_validate(
            plan.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        trusted_criteria = _criteria_from_plan(trusted)
        if trusted_criteria is None:
            raise TypeError
    except BaseException:
        return None, criteria or (), VerificationFailureReason.MALFORMED_PLAN
    return trusted, trusted_criteria, None


def _trusted_result(raw_result: ToolResult[SystemStatus]) -> ToolResult[SystemStatus] | None:
    """Revalidate one exact result and every typed nested payload object."""
    try:
        if type(raw_result) is not ToolResult[SystemStatus]:
            raise TypeError
        if raw_result.success:
            if (
                type(raw_result.data) is not SystemStatus
                or type(raw_result.data.services) is not tuple
                or any(type(service) is not ServiceStatus for service in raw_result.data.services)
                or raw_result.error is not None
            ):
                raise TypeError
        elif raw_result.data is not None or type(raw_result.error) is not ToolError:
            raise TypeError
        return ToolResult[SystemStatus].model_validate(
            raw_result.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        return None


def _expected_target(plan: ExecutionPlan, step: ExecutionStep) -> TargetReference:
    """Derive the only bounded target identity permitted by the current Plan model."""
    return TargetReference(
        target_id=plan.target,
        resource_type="local_system",
        resource_id=step.arguments.target,
    )


def _evidence_reference(
    step_index: int,
    step: ExecutionStep,
    result: ToolResult[SystemStatus],
    context: VerificationContext,
) -> VerificationEvidenceReference:
    """Create hash-only provenance without copying observed values."""
    return VerificationEvidenceReference(
        step_index=step_index,
        step_id=step.step_id,
        invocation_id=result.invocation_id,
        tool_id=result.tool_id,
        tool_version=result.tool_version,
        contract_hash=result.contract_hash,
        implementation_hash=step.implementation_hash,
        arguments_hash=result.arguments_hash,
        target=result.target,
        result_hash=canonical_json_sha256(result),
        accepted_at=context.evidence_accepted_at,
    )


def _matching_references(
    plan: ExecutionPlan | None,
    results: tuple[ToolResult[SystemStatus], ...],
    context: VerificationContext,
) -> tuple[VerificationEvidenceReference, ...]:
    """Retain references only for exact, ordered, identity-bound evidence."""
    if plan is None or type(results) is not tuple:
        return ()
    references: list[VerificationEvidenceReference] = []
    for index, (step, raw_result) in enumerate(zip(plan.steps, results, strict=False)):
        result = _trusted_result(raw_result)
        if result is None:
            continue
        if (
            result.plan_step_id != step.step_id
            or result.tool_id != step.tool_id
            or result.tool_version != step.tool_version
            or result.contract_hash != step.contract_hash
            or result.arguments_hash != canonical_json_sha256(step.arguments)
            or result.target != _expected_target(plan, step)
        ):
            continue
        try:
            references.append(_evidence_reference(index, step, result, context))
        except BaseException:
            continue
    return tuple(references)


def _failed_checks(
    criteria: tuple[VerificationCriterion, ...],
    reason: VerificationFailureReason,
) -> tuple[VerificationCheckResult, ...]:
    """Build ordered fail-closed decisions for all safely readable criteria."""
    return tuple(
        VerificationCheckResult(
            criterion_id=criterion.criterion_id,
            evidence_step_id=criterion.evidence_step_id,
            evaluator_version=criterion.evaluator_version,
            status=VerificationCheckStatus.FAILED,
            failure_reason=reason,
        )
        for criterion in criteria
    )


def _result(
    context: VerificationContext,
    checks: tuple[VerificationCheckResult, ...],
    references: tuple[VerificationEvidenceReference, ...],
    reasons: tuple[VerificationFailureReason, ...],
) -> VerificationResult:
    """Construct one canonical hash-bound terminal VerificationResult."""
    passed = bool(checks) and all(
        check.status is VerificationCheckStatus.PASSED for check in checks
    )
    status = VerificationStatus.PASSED if passed else VerificationStatus.FAILED
    if passed:
        effect_disposition = (
            VerificationEffectDisposition.VERIFIED
            if context.mutating_effect_pending
            else VerificationEffectDisposition.NONE
        )
        human_intervention_required = False
        unique_reasons: tuple[VerificationFailureReason, ...] = ()
    else:
        effect_disposition = (
            VerificationEffectDisposition.UNKNOWN
            if context.mutating_effect_pending
            else VerificationEffectDisposition.NONE
        )
        human_intervention_required = context.mutating_effect_pending
        unique_reasons = tuple(dict.fromkeys(reasons))
        if not unique_reasons:
            unique_reasons = (VerificationFailureReason.VERIFIER_FAILED,)
    draft = VerificationResult.model_construct(
        result_schema_version="1",
        task_id=context.task_id,
        plan_id=context.plan_id,
        plan_digest=context.plan_digest,
        execution_attempt_id=context.execution_attempt_id,
        execution_report_hash=context.execution_report_hash,
        evaluated_at=context.evaluated_at,
        status=status,
        checks=checks,
        evidence_references=references,
        failure_reasons=unique_reasons,
        effect_disposition=effect_disposition,
        human_intervention_required=human_intervention_required,
        content_hash="0" * 64,
    )
    content_hash = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"content_hash"}, warnings="error")
    )
    return VerificationResult(
        task_id=context.task_id,
        plan_id=context.plan_id,
        plan_digest=context.plan_digest,
        execution_attempt_id=context.execution_attempt_id,
        execution_report_hash=context.execution_report_hash,
        evaluated_at=context.evaluated_at,
        status=status,
        checks=checks,
        evidence_references=references,
        failure_reasons=unique_reasons,
        effect_disposition=effect_disposition,
        human_intervention_required=human_intervention_required,
        content_hash=content_hash,
    )


def _structural_failure(
    context: VerificationContext,
    criteria: tuple[VerificationCriterion, ...],
    references: tuple[VerificationEvidenceReference, ...],
    reason: VerificationFailureReason,
) -> VerificationResult:
    """Return a deterministic structural failure bound to a trusted Context."""
    return _result(context, _failed_checks(criteria, reason), references, (reason,))


def _identity_failure(
    plan: ExecutionPlan,
    results: tuple[ToolResult[SystemStatus], ...],
) -> VerificationFailureReason | None:
    """Return the first stable result-envelope failure in Plan order."""
    result_step_ids = tuple(result.plan_step_id for result in results)
    expected_step_ids = tuple(step.step_id for step in plan.steps)
    if result_step_ids != expected_step_ids:
        if set(result_step_ids) == set(expected_step_ids):
            return VerificationFailureReason.EVIDENCE_ORDER_MISMATCH
        return VerificationFailureReason.EVIDENCE_IDENTITY_MISMATCH
    invocation_ids = tuple(result.invocation_id for result in results)
    if len(invocation_ids) != len(set(invocation_ids)):
        return VerificationFailureReason.DUPLICATE_INVOCATION_ID
    for step, result in zip(plan.steps, results, strict=True):
        if result.tool_version != step.tool_version:
            return VerificationFailureReason.TOOL_VERSION_MISMATCH
        if result.target != _expected_target(plan, step):
            return VerificationFailureReason.TARGET_MISMATCH
        if (
            result.tool_id != step.tool_id
            or result.contract_hash != step.contract_hash
            or result.arguments_hash != canonical_json_sha256(step.arguments)
        ):
            return VerificationFailureReason.EVIDENCE_IDENTITY_MISMATCH
        if not result.success:
            return VerificationFailureReason.UNSUCCESSFUL_TOOL_RESULT
        if result.data is None or result.data.target != plan.target:
            return VerificationFailureReason.TARGET_MISMATCH
    return None


def _is_contradictory(result: ToolResult[SystemStatus]) -> bool:
    """Detect impossible duplicate identities or conflicting scalar representations."""
    if result.data is None:
        return False
    service_names = tuple(service.name for service in result.data.services)
    if len(service_names) != len(set(service_names)):
        return True
    scalar_data: tuple[tuple[str, str | bool], ...] = (
        ("source", result.data.source),
        ("simulated", result.data.simulated),
        ("target", result.data.target),
        ("hostname", result.data.hostname),
    )
    for field, data_value in scalar_data:
        if field not in result.evidence:
            continue
        evidence_value = result.evidence[field]
        if type(evidence_value) is not type(data_value) or evidence_value != data_value:
            return True
    return False


def _data_scalar(
    data: SystemStatus,
    field: str,
) -> str | bool:
    """Resolve one statically allowlisted scalar field without dynamic paths."""
    if field == "source":
        return data.source
    if field == "simulated":
        return data.simulated
    if field == "target":
        return data.target
    return data.hostname


def _numeric_value(data: SystemStatus, field: str) -> float:
    """Resolve one statically allowlisted utilization field without expressions."""
    if field == "cpu_percent":
        return data.cpu_percent
    if field == "memory_percent":
        return data.memory_percent
    return data.disk_percent


def _freshness_failure(
    criterion: VerificationCriterion,
    context: VerificationContext,
) -> VerificationFailureReason | None:
    """Evaluate evidence age from collection start with an inclusive upper bound."""
    if context.evidence_accepted_at is None:
        return VerificationFailureReason.CLOCK_UNAVAILABLE
    age = (
        context.evaluated_at
        - context.evidence_accepted_at
        + timedelta(milliseconds=context.collection_duration_ms)
    )
    if age > timedelta(milliseconds=criterion.maximum_age_ms):
        return VerificationFailureReason.STALE_EVIDENCE
    return None


def _evaluate_criterion(
    criterion: VerificationCriterion,
    result: ToolResult[SystemStatus],
    context: VerificationContext,
) -> VerificationCheckResult:
    """Evaluate one bounded criterion without dynamic paths or expressions."""
    failure = _freshness_failure(criterion, context)
    data = result.data
    if failure is not None or data is None:
        return VerificationCheckResult(
            criterion_id=criterion.criterion_id,
            evidence_step_id=criterion.evidence_step_id,
            evaluator_version=criterion.evaluator_version,
            status=VerificationCheckStatus.FAILED,
            failure_reason=(failure or VerificationFailureReason.CRITERION_EVIDENCE_MISSING),
        )
    if isinstance(criterion, EqualityCriterion):
        observed: object
        if criterion.source == "data":
            observed = _data_scalar(data, criterion.field)
        elif criterion.field not in result.evidence:
            failure = VerificationFailureReason.CRITERION_EVIDENCE_MISSING
            observed = None
        else:
            observed = result.evidence[criterion.field]
        if failure is None and (
            type(observed) is not type(criterion.expected) or observed != criterion.expected
        ):
            failure = VerificationFailureReason.CRITERION_MISMATCH
    elif isinstance(criterion, NumericBoundsCriterion):
        observed_number = _numeric_value(data, criterion.field)
        if observed_number < criterion.minimum or observed_number > criterion.maximum:
            failure = VerificationFailureReason.CRITERION_MISMATCH
    elif isinstance(criterion, ExpectedStateCriterion):
        states = tuple(
            service.state for service in data.services if service.name == criterion.service_name
        )
        if not states:
            failure = VerificationFailureReason.CRITERION_EVIDENCE_MISSING
        elif states[0] != criterion.expected_state:
            failure = VerificationFailureReason.CRITERION_MISMATCH
    elif isinstance(criterion, HealthStatusCriterion):
        healthy = bool(data.services) and all(
            service.state == "running" for service in data.services
        )
        healthy = healthy and all(
            value <= criterion.maximum_utilization_percent
            for value in (data.cpu_percent, data.memory_percent, data.disk_percent)
        )
        expected_healthy = criterion.expected_status == "healthy"
        if healthy is not expected_healthy:
            failure = VerificationFailureReason.CRITERION_MISMATCH

    return VerificationCheckResult(
        criterion_id=criterion.criterion_id,
        evidence_step_id=criterion.evidence_step_id,
        evaluator_version=criterion.evaluator_version,
        status=(
            VerificationCheckStatus.PASSED if failure is None else VerificationCheckStatus.FAILED
        ),
        failure_reason=failure,
    )


def build_verification_failure(
    plan: ExecutionPlan,
    results: tuple[ToolResult[SystemStatus], ...],
    context: VerificationContext,
    reason: VerificationFailureReason,
) -> VerificationResult:
    """Build a safe failure result for a Verifier boundary or clock failure."""
    trusted_context = _trusted_context(context)
    if type(reason) is not VerificationFailureReason:
        raise VerificationInputError("Verification failure reason is not trustworthy")
    trusted_plan, criteria, plan_failure = _trusted_plan(plan, trusted_context)
    effective_reason = plan_failure or reason
    references = _matching_references(trusted_plan, results, trusted_context)
    return _structural_failure(trusted_context, criteria, references, effective_reason)


def evaluate_verification(
    plan: ExecutionPlan,
    results: tuple[ToolResult[SystemStatus], ...],
    context: VerificationContext,
) -> VerificationResult:
    """Purely evaluate exact structured evidence against immutable Plan criteria."""
    trusted_context = _trusted_context(context)
    trusted_plan, criteria, plan_failure = _trusted_plan(plan, trusted_context)
    if plan_failure is not None or trusted_plan is None:
        return _structural_failure(
            trusted_context,
            criteria,
            (),
            plan_failure or VerificationFailureReason.MALFORMED_PLAN,
        )
    if type(results) is not tuple:
        return _structural_failure(
            trusted_context,
            criteria,
            (),
            VerificationFailureReason.MALFORMED_EVIDENCE,
        )
    if len(results) < len(trusted_plan.steps):
        return _structural_failure(
            trusted_context,
            criteria,
            _matching_references(trusted_plan, results, trusted_context),
            VerificationFailureReason.MISSING_EVIDENCE,
        )
    if len(results) > len(trusted_plan.steps):
        return _structural_failure(
            trusted_context,
            criteria,
            _matching_references(trusted_plan, results, trusted_context),
            VerificationFailureReason.EXTRA_EVIDENCE,
        )

    validated_results: list[ToolResult[SystemStatus]] = []
    for raw_result in results:
        result = _trusted_result(raw_result)
        if result is None:
            return _structural_failure(
                trusted_context,
                criteria,
                (),
                VerificationFailureReason.MALFORMED_EVIDENCE,
            )
        validated_results.append(result)
    evidence = tuple(validated_results)
    references = _matching_references(trusted_plan, evidence, trusted_context)
    identity_failure = _identity_failure(trusted_plan, evidence)
    if identity_failure is not None:
        return _structural_failure(
            trusted_context,
            criteria,
            references,
            identity_failure,
        )
    if any(_is_contradictory(result) for result in evidence):
        return _structural_failure(
            trusted_context,
            criteria,
            references,
            VerificationFailureReason.CONTRADICTORY_EVIDENCE,
        )

    by_step_id = {result.plan_step_id: result for result in evidence}
    checks: list[VerificationCheckResult] = []
    for criterion in criteria:
        result = by_step_id.get(criterion.evidence_step_id)
        if result is None:
            checks.append(
                VerificationCheckResult(
                    criterion_id=criterion.criterion_id,
                    evidence_step_id=criterion.evidence_step_id,
                    evaluator_version=criterion.evaluator_version,
                    status=VerificationCheckStatus.FAILED,
                    failure_reason=VerificationFailureReason.CRITERION_EVIDENCE_MISSING,
                )
            )
        else:
            checks.append(_evaluate_criterion(criterion, result, trusted_context))
    reasons = tuple(check.failure_reason for check in checks if check.failure_reason is not None)
    return _result(trusted_context, tuple(checks), references, reasons)


class Verifier:
    """Delegate verification to the pure deterministic evaluator."""

    def verify(
        self,
        plan: ExecutionPlan,
        results: tuple[ToolResult[SystemStatus], ...],
        context: VerificationContext,
    ) -> VerificationResult:
        """Return a structured result without invoking Tools, clocks, or gateways."""
        return evaluate_verification(plan, results, context)


__all__ = ["Verifier", "build_verification_failure", "evaluate_verification"]
