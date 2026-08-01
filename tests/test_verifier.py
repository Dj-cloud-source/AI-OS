from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from ai_server.context.builder import ContextBuilder
from ai_server.models.execution import ExecutionPlan
from ai_server.models.system_status import ServiceStatus, SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import (
    TargetReference,
    ToolError,
    ToolErrorCategory,
    ToolResult,
)
from ai_server.models.verification import (
    EqualityCriterion,
    ExpectedStateCriterion,
    HealthStatusCriterion,
    NumericBoundsCriterion,
    VerificationCheckStatus,
    VerificationContext,
    VerificationEffectDisposition,
    VerificationFailureReason,
    VerificationResult,
    VerificationStatus,
)
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.gateway import ToolGateway
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.verifier.service import (
    Verifier,
    build_verification_failure,
    evaluate_verification,
)

ACCEPTED_AT = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def make_plan() -> ExecutionPlan:
    """Build the reviewed local Mock Plan with authoritative metadata."""
    metadata = next(iter(build_default_registry().metadata_snapshot().values()))
    task = Task(request=SUPPORTED_REQUEST)
    return Planner().create_plan(ContextBuilder().build(task), metadata)


def with_criteria(plan: ExecutionPlan, criteria: tuple[object, ...]) -> ExecutionPlan:
    """Rebuild a Plan with exact test criteria."""
    document = plan.model_dump(mode="python", warnings="error")
    document["verification_criteria"] = criteria
    return ExecutionPlan.model_validate(document, strict=True)


def make_context(
    plan: ExecutionPlan,
    *,
    accepted_at: datetime | None = ACCEPTED_AT,
    evaluated_at: datetime = ACCEPTED_AT,
    collection_duration_ms: int = 0,
    mutating_effect_pending: bool = False,
) -> VerificationContext:
    """Build exact deterministic execution and time bindings."""
    return VerificationContext(
        task_id=plan.task_id,
        plan_id=plan.plan_id,
        plan_digest=canonical_json_sha256(plan),
        execution_attempt_id=UUID("00000000-0000-4000-8000-000000000010"),
        execution_report_hash="e" * 64,
        evidence_accepted_at=accepted_at,
        evaluated_at=evaluated_at,
        collection_duration_ms=collection_duration_ms,
        mutating_effect_pending=mutating_effect_pending,
    )


def make_result(
    plan: ExecutionPlan,
    *,
    step_index: int = 0,
    invocation_number: int = 1,
    data: SystemStatus | None = None,
) -> ToolResult[SystemStatus]:
    """Build one matching successful structured evidence result."""
    step = plan.steps[step_index]
    return ToolResult[SystemStatus](
        invocation_id=UUID(f"00000000-0000-4000-8000-{invocation_number:012d}"),
        plan_step_id=step.step_id,
        tool_id=step.tool_id,
        tool_version=step.tool_version,
        contract_hash=step.contract_hash,
        arguments_hash=canonical_json_sha256(step.arguments),
        target=TargetReference(
            target_id=plan.target,
            resource_type="local_system",
            resource_id=step.arguments.target,
        ),
        success=True,
        data=data
        or SystemStatus(
            cpu_percent=12.5,
            memory_percent=34.0,
            disk_percent=45.5,
            services=(ServiceStatus(name="mock-api", state="running"),),
        ),
        evidence={
            "source": "mock",
            "simulated": True,
            "target": "local-mock",
            "hostname": "mock-server",
        },
        error=None,
        duration_ms=0,
    )


def make_two_step_plan() -> ExecutionPlan:
    """Build two ordered observation Steps for envelope-order tests."""
    base = make_plan()
    second = base.steps[0].model_copy(update={"step_id": "get-system-status-second"})
    return ExecutionPlan(
        plan_id=base.plan_id,
        task_id=base.task_id,
        target=base.target,
        steps=(base.steps[0], second),
        verification_criteria=(
            EqualityCriterion(
                criterion_id="first-source",
                evidence_step_id=base.steps[0].step_id,
                source="data",
                field="source",
                expected="mock",
            ),
            EqualityCriterion(
                criterion_id="second-source",
                evidence_step_id=second.step_id,
                source="data",
                field="source",
                expected="mock",
            ),
        ),
    )


def failure_reason(result: VerificationResult) -> VerificationFailureReason:
    """Return the first stable reason from a failed result."""
    assert result.status is VerificationStatus.FAILED
    return result.failure_reasons[0]


def test_all_four_evaluators_pass_without_raw_values_in_result() -> None:
    plan = make_plan()
    step_id = plan.steps[0].step_id
    plan = with_criteria(
        plan,
        (
            EqualityCriterion(
                criterion_id="source-is-mock",
                evidence_step_id=step_id,
                source="evidence",
                field="source",
                expected="mock",
            ),
            NumericBoundsCriterion(
                criterion_id="cpu-in-range",
                evidence_step_id=step_id,
                field="cpu_percent",
                minimum=12.5,
                maximum=12.5,
            ),
            ExpectedStateCriterion(
                criterion_id="api-running",
                evidence_step_id=step_id,
                service_name="mock-api",
                expected_state="running",
            ),
            HealthStatusCriterion(
                criterion_id="system-healthy",
                evidence_step_id=step_id,
                expected_status="healthy",
                maximum_utilization_percent=50.0,
            ),
        ),
    )

    result = evaluate_verification(plan, (make_result(plan),), make_context(plan))

    assert result.status is VerificationStatus.PASSED
    assert tuple(check.status for check in result.checks) == (VerificationCheckStatus.PASSED,) * 4
    assert result.failure_reasons == ()
    assert result.effect_disposition is VerificationEffectDisposition.NONE
    serialized = result.model_dump_json()
    assert "12.5" not in serialized
    assert "mock-api" not in serialized


@pytest.mark.parametrize(
    "criterion",
    [
        EqualityCriterion(
            criterion_id="wrong-source",
            evidence_step_id="get-system-status",
            source="data",
            field="source",
            expected="real",
        ),
        NumericBoundsCriterion(
            criterion_id="cpu-too-low",
            evidence_step_id="get-system-status",
            field="cpu_percent",
            minimum=0.0,
            maximum=10.0,
        ),
        ExpectedStateCriterion(
            criterion_id="api-stopped",
            evidence_step_id="get-system-status",
            service_name="mock-api",
            expected_state="stopped",
        ),
        HealthStatusCriterion(
            criterion_id="system-unhealthy",
            evidence_step_id="get-system-status",
            expected_status="unhealthy",
            maximum_utilization_percent=100.0,
        ),
    ],
)
def test_each_evaluator_fails_closed_on_mismatch(criterion: object) -> None:
    plan = with_criteria(make_plan(), (criterion,))

    result = evaluate_verification(plan, (make_result(plan),), make_context(plan))

    assert failure_reason(result) is VerificationFailureReason.CRITERION_MISMATCH
    assert result.checks[0].status is VerificationCheckStatus.FAILED


def test_equality_evaluator_requires_exact_str_or_bool_type() -> None:
    class StringSubclass(str):
        pass

    with pytest.raises(ValidationError):
        EqualityCriterion.model_validate(
            {
                "criterion_id": "strict-bool",
                "evidence_step_id": "get-system-status",
                "source": "evidence",
                "field": "simulated",
                "expected": 1,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        EqualityCriterion(
            criterion_id="strict-string",
            evidence_step_id="get-system-status",
            source="data",
            field="source",
            expected=StringSubclass("mock"),
        )


@pytest.mark.parametrize(
    "update",
    [
        {"kind": "expression"},
        {"mandatory": False},
        {"evaluator_version": "2"},
    ],
)
def test_criterion_protocol_rejects_unknown_or_weakened_controls(
    update: dict[str, object],
) -> None:
    document: dict[str, object] = {
        "criterion_id": "strict-protocol",
        "evidence_step_id": "get-system-status",
        "source": "data",
        "field": "source",
        "expected": "mock",
    }
    document.update(update)

    with pytest.raises(ValidationError):
        EqualityCriterion.model_validate(document, strict=True)


def test_numeric_criterion_requires_a_complete_inclusive_interval() -> None:
    with pytest.raises(ValidationError):
        NumericBoundsCriterion.model_validate(
            {
                "criterion_id": "missing-max",
                "evidence_step_id": "get-system-status",
                "field": "cpu_percent",
                "minimum": 0.0,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        NumericBoundsCriterion(
            criterion_id="inverted",
            evidence_step_id="get-system-status",
            field="cpu_percent",
            minimum=20.0,
            maximum=10.0,
        )


def test_freshness_upper_bound_is_inclusive() -> None:
    plan = make_plan()
    criterion = EqualityCriterion(
        criterion_id="fresh-source",
        evidence_step_id=plan.steps[0].step_id,
        source="data",
        field="source",
        expected="mock",
        maximum_age_ms=30,
    )
    plan = with_criteria(plan, (criterion,))
    at_boundary = make_context(
        plan,
        evaluated_at=ACCEPTED_AT + timedelta(milliseconds=20),
        collection_duration_ms=10,
    )
    beyond_boundary = make_context(
        plan,
        evaluated_at=ACCEPTED_AT + timedelta(milliseconds=20, microseconds=1),
        collection_duration_ms=10,
    )

    assert evaluate_verification(plan, (make_result(plan),), at_boundary).status is (
        VerificationStatus.PASSED
    )
    assert (
        failure_reason(evaluate_verification(plan, (make_result(plan),), beyond_boundary))
        is VerificationFailureReason.STALE_EVIDENCE
    )


def test_missing_evidence_clock_never_passes() -> None:
    plan = make_plan()

    result = evaluate_verification(
        plan,
        (make_result(plan),),
        make_context(plan, accepted_at=None),
    )

    assert failure_reason(result) is VerificationFailureReason.CLOCK_UNAVAILABLE
    assert result.evidence_references[0].accepted_at is None


def test_verification_context_rejects_naive_or_future_timestamps() -> None:
    plan = make_plan()
    document = make_context(plan).model_dump(mode="python")
    document["evaluated_at"] = ACCEPTED_AT.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        VerificationContext.model_validate(document, strict=True)

    document = make_context(plan).model_dump(mode="python")
    document["evidence_accepted_at"] = ACCEPTED_AT + timedelta(microseconds=1)
    with pytest.raises(ValidationError, match="later"):
        VerificationContext.model_validate(document, strict=True)


@pytest.mark.parametrize(
    ("evidence", "expected_reason"),
    [
        ((), VerificationFailureReason.MISSING_EVIDENCE),
        ("extra", VerificationFailureReason.EXTRA_EVIDENCE),
    ],
)
def test_missing_and_extra_evidence_fail_closed(
    evidence: tuple[object, ...] | str,
    expected_reason: VerificationFailureReason,
) -> None:
    plan = make_plan()
    results = () if evidence == () else (make_result(plan), make_result(plan, invocation_number=2))

    result = evaluate_verification(plan, results, make_context(plan))

    assert failure_reason(result) is expected_reason


def test_non_tuple_and_malformed_payload_fail_closed() -> None:
    plan = make_plan()
    valid = make_result(plan)
    assert valid.data is not None
    malformed = valid.model_copy(
        update={"data": valid.data.model_copy(update={"cpu_percent": "not-a-number"})}
    )

    non_tuple = evaluate_verification(plan, [valid], make_context(plan))  # type: ignore[arg-type]
    malformed_result = evaluate_verification(plan, (malformed,), make_context(plan))

    assert failure_reason(non_tuple) is VerificationFailureReason.MALFORMED_EVIDENCE
    assert failure_reason(malformed_result) is VerificationFailureReason.MALFORMED_EVIDENCE


def test_malformed_nested_criterion_returns_bound_failure() -> None:
    plan = make_plan()
    criterion = plan.verification_criteria[0].model_copy(update={"criterion_id": ""})
    malformed_plan = plan.model_copy(update={"verification_criteria": (criterion,)})

    result = evaluate_verification(
        malformed_plan,
        (make_result(malformed_plan),),
        make_context(malformed_plan),
    )

    assert failure_reason(result) is VerificationFailureReason.MALFORMED_PLAN


def test_reordered_evidence_fails_closed() -> None:
    plan = make_two_step_plan()
    first = make_result(plan, step_index=0, invocation_number=1)
    second = make_result(plan, step_index=1, invocation_number=2)

    result = evaluate_verification(plan, (second, first), make_context(plan))

    assert failure_reason(result) is VerificationFailureReason.EVIDENCE_ORDER_MISMATCH


def test_wrong_tool_version_fails_closed() -> None:
    plan = make_plan()
    result = make_result(plan).model_copy(update={"tool_version": "1.0.1"})

    verification = evaluate_verification(plan, (result,), make_context(plan))

    assert failure_reason(verification) is VerificationFailureReason.TOOL_VERSION_MISMATCH


def test_wrong_target_fails_closed() -> None:
    plan = make_plan()
    wrong_target = TargetReference(
        target_id="other-target",
        resource_type="local_system",
        resource_id="local-mock",
    )
    result = make_result(plan).model_copy(update={"target": wrong_target})

    verification = evaluate_verification(plan, (result,), make_context(plan))

    assert failure_reason(verification) is VerificationFailureReason.TARGET_MISMATCH


def test_duplicate_invocation_id_fails_closed() -> None:
    plan = make_two_step_plan()
    first = make_result(plan, step_index=0, invocation_number=1)
    second = make_result(plan, step_index=1, invocation_number=1)

    result = evaluate_verification(plan, (first, second), make_context(plan))

    assert failure_reason(result) is VerificationFailureReason.DUPLICATE_INVOCATION_ID


def test_duplicate_service_names_are_contradictory() -> None:
    plan = make_plan()
    data = SystemStatus(
        cpu_percent=12.5,
        memory_percent=34.0,
        disk_percent=45.5,
        services=(
            ServiceStatus(name="mock-api", state="running"),
            ServiceStatus(name="mock-api", state="stopped"),
        ),
    )

    result = evaluate_verification(
        plan,
        (make_result(plan, data=data),),
        make_context(plan),
    )

    assert failure_reason(result) is VerificationFailureReason.CONTRADICTORY_EVIDENCE


def test_retained_evidence_type_conflict_is_contradictory() -> None:
    plan = make_plan()
    result = make_result(plan).model_copy(
        update={
            "evidence": {
                "source": "mock",
                "simulated": 1,
                "target": "local-mock",
                "hostname": "mock-server",
            }
        }
    )

    verification = evaluate_verification(plan, (result,), make_context(plan))

    assert failure_reason(verification) is (VerificationFailureReason.CONTRADICTORY_EVIDENCE)


def test_structured_tool_failure_has_stable_reason() -> None:
    plan = make_plan()
    success = make_result(plan)
    failed = ToolResult[SystemStatus](
        invocation_id=success.invocation_id,
        plan_step_id=success.plan_step_id,
        tool_id=success.tool_id,
        tool_version=success.tool_version,
        contract_hash=success.contract_hash,
        arguments_hash=success.arguments_hash,
        target=success.target,
        success=False,
        data=None,
        evidence={},
        error=ToolError(
            code="tool_execution_failed",
            category=ToolErrorCategory.EXECUTION,
            message="Tool execution failed safely",
            retryable=False,
        ),
        duration_ms=0,
    )

    result = evaluate_verification(plan, (failed,), make_context(plan))

    assert failure_reason(result) is VerificationFailureReason.UNSUCCESSFUL_TOOL_RESULT


def test_result_hash_round_trip_and_tampering_detection() -> None:
    plan = make_plan()
    result = evaluate_verification(plan, (make_result(plan),), make_context(plan))

    round_tripped = VerificationResult.model_validate_json(result.model_dump_json())

    assert round_tripped == result
    assert result.content_hash == canonical_json_sha256(
        result.model_dump(mode="json", exclude={"content_hash"}, warnings="error")
    )
    document = result.model_dump(mode="python", warnings="error")
    document["execution_report_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="content hash"):
        VerificationResult.model_validate(document)


def test_external_failure_builder_marks_pending_effect_unknown() -> None:
    plan = make_plan()
    context = make_context(plan, mutating_effect_pending=True)

    result = build_verification_failure(
        plan,
        (make_result(plan),),
        context,
        VerificationFailureReason.VERIFIER_RESULT_INVALID,
    )

    assert result.status is VerificationStatus.FAILED
    assert result.effect_disposition is VerificationEffectDisposition.UNKNOWN
    assert result.human_intervention_required is True
    assert result.failure_reasons == (VerificationFailureReason.VERIFIER_RESULT_INVALID,)


def test_verifier_delegates_without_invoking_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = make_plan()

    def forbidden_gateway_call(*args: object, **kwargs: object) -> object:
        raise AssertionError("Verifier must not invoke Tool Gateway")

    monkeypatch.setattr(ToolGateway, "invoke", forbidden_gateway_call)

    result = Verifier().verify(plan, (make_result(plan),), make_context(plan))

    assert result.status is VerificationStatus.PASSED
