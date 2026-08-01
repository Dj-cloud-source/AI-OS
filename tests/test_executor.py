from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from ai_server.approval.engine import ApprovalEngine
from ai_server.context.builder import ContextBuilder
from ai_server.executor.errors import ExecutionAttemptError, ExecutionAuthorizationError
from ai_server.executor.service import Executor
from ai_server.models.execution import ExecutionPlan, StepRole
from ai_server.models.executor import (
    DispatchStatus,
    EffectDisposition,
    ExecutionAttemptAuthorization,
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionNextState,
    ExecutionReport,
    ExecutionReportStatus,
    StepExecutionRecord,
)
from ai_server.models.policy import (
    PolicyDecision,
    PolicyEffect,
    PolicyEvaluationContext,
    PolicyReasonCode,
)
from ai_server.models.system_status import GetSystemStatusArguments, ServiceStatus, SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import (
    TargetReference,
    ToolCall,
    ToolError,
    ToolErrorCategory,
    ToolReference,
    ToolResult,
)
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.gateway import (
    GatewayDispatchReceipt,
    GatewayDispatchStatus,
    PostDispatchToolIntegrityError,
    ToolGateway,
    ToolGatewayError,
    ToolIntegrityError,
)
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.tools.registry import ToolRegistry

TASK_ID = UUID("00000000-0000-4000-8000-000000000001")
ATTEMPT_ID = UUID("00000000-0000-4000-8000-000000000002")
SECOND_ATTEMPT_ID = UUID("00000000-0000-4000-8000-000000000003")
INVOCATION_IDS = tuple(UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(10, 20))
FORGED_TASK_ID = UUID("00000000-0000-4000-8000-000000000099")


@dataclass(frozen=True, slots=True)
class ExecutorHarness:
    registry: ToolRegistry
    policy: PolicyEngine
    approval: ApprovalEngine
    gateway: ToolGateway
    executor: Executor
    plan: ExecutionPlan
    decision: PolicyDecision


def constant_clock() -> int:
    return 0


def sequence_clock(*values: int) -> Callable[[], int]:
    readings = iter(values)
    return lambda: next(readings)


def id_factory(*identifiers: UUID) -> Callable[[], UUID]:
    values = iter(identifiers)
    return lambda: next(values)


def make_plan(roles: tuple[StepRole, ...]) -> ExecutionPlan:
    registry = build_default_registry()
    metadata = registry.metadata_snapshot()[("get_system_status", "1.0.0")]
    task = Task(task_id=TASK_ID, request=SUPPORTED_REQUEST)
    base = Planner().create_plan(ContextBuilder().build(task), metadata)
    steps = tuple(
        base.steps[0].model_copy(
            update={
                "step_id": f"status-{index}",
                "role": role,
            }
        )
        for index, role in enumerate(roles)
    )
    return ExecutionPlan(
        plan_id=base.plan_id,
        task_id=base.task_id,
        target=base.target,
        steps=steps,
        verification_criteria=tuple(
            criterion.model_copy(
                update={
                    "criterion_id": f"{criterion.criterion_id}-{step_index}",
                    "evidence_step_id": step.step_id,
                }
            )
            for step_index, step in enumerate(steps)
            for criterion in base.verification_criteria
        ),
    )


def declare_mock_verification_tool(policy: PolicyEngine) -> None:
    """Install a test-only self-reference for explicit VERIFY Step fixtures."""
    key = ("get_system_status", "1.0.0")
    metadata = policy.metadata_for(*key)
    assert metadata is not None
    verification = metadata.verification.model_copy(
        update={"tools": (ToolReference(tool_id=key[0], version=key[1]),)}
    )
    policy._metadata = MappingProxyType(
        {key: metadata.model_copy(update={"verification": verification})}
    )


def make_harness(
    *,
    roles: tuple[StepRole, ...] = (StepRole.OBSERVE,),
    clock: Callable[[], int] = constant_clock,
    attempt_ids: tuple[UUID, ...] = (ATTEMPT_ID, SECOND_ATTEMPT_ID),
    invocation_ids: tuple[UUID, ...] = INVOCATION_IDS,
) -> ExecutorHarness:
    registry = build_default_registry()
    policy = PolicyEngine(registry)
    plan = make_plan(roles)
    if StepRole.VERIFY in roles:
        declare_mock_verification_tool(policy)
    decision = policy.evaluate(
        plan,
        PolicyEvaluationContext(
            operator_id="local-user",
            target=TargetReference(
                target_id=plan.target,
                resource_type="local_system",
                resource_id=plan.target,
            ),
        ),
    )
    approval = ApprovalEngine(
        registry.metadata_snapshot(),
        policy.approval_constraints,
    )
    gateway = ToolGateway(registry)
    executor = Executor(
        gateway,
        policy,
        approval,
        clock=clock,
        attempt_id_factory=id_factory(*attempt_ids),
        invocation_id_factory=id_factory(*invocation_ids),
    )
    return ExecutorHarness(
        registry=registry,
        policy=policy,
        approval=approval,
        gateway=gateway,
        executor=executor,
        plan=plan,
        decision=decision,
    )


def success_result(
    call: ToolCall[GetSystemStatusArguments],
) -> ToolResult[BaseModel]:
    return ToolResult[BaseModel](
        invocation_id=call.invocation_id,
        plan_step_id=call.plan_step_id,
        tool_id=call.tool_id,
        tool_version=call.tool_version,
        contract_hash=call.contract_hash,
        arguments_hash=call.arguments_hash,
        target=call.target,
        success=True,
        data=SystemStatus(
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


def failure_result(
    call: ToolCall[GetSystemStatusArguments],
) -> ToolResult[BaseModel]:
    return ToolResult[BaseModel](
        invocation_id=call.invocation_id,
        plan_step_id=call.plan_step_id,
        tool_id=call.tool_id,
        tool_version=call.tool_version,
        contract_hash=call.contract_hash,
        arguments_hash=call.arguments_hash,
        target=call.target,
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


def receipt(
    result: ToolResult[BaseModel],
    *,
    status: GatewayDispatchStatus = GatewayDispatchStatus.HANDLER_DISPATCHED,
) -> GatewayDispatchReceipt:
    return GatewayDispatchReceipt(
        result=result,
        dispatch_status=status,
        mutates_remote_state=False,
    )


def authorize(harness: ExecutorHarness) -> ExecutionAttemptAuthorization:
    return harness.executor.begin_attempt(
        harness.plan,
        harness.decision,
        approval_id=None,
    )


def denied_decision(decision: PolicyDecision) -> PolicyDecision:
    denied_step = decision.step_decisions[0].model_copy(
        update={
            "effect": PolicyEffect.DENY,
            "reason_code": PolicyReasonCode.TOOL_NOT_ALLOWED,
        }
    )
    draft = decision.model_copy(
        update={
            "effect": PolicyEffect.DENY,
            "reason_code": PolicyReasonCode.TOOL_NOT_ALLOWED,
            "step_decisions": (denied_step, *decision.step_decisions[1:]),
        }
    )
    return PolicyDecision.model_validate(
        draft.model_dump(mode="python", warnings="error"),
        strict=True,
    )


def forge_authorization(
    authorization: ExecutionAttemptAuthorization,
) -> ExecutionAttemptAuthorization:
    document = authorization.model_dump(mode="python", warnings="error")
    document["task_id"] = FORGED_TASK_ID
    json_document = authorization.model_dump(mode="json", warnings="error")
    json_document["task_id"] = str(FORGED_TASK_ID)
    document["content_hash"] = canonical_json_sha256(
        {key: value for key, value in json_document.items() if key != "content_hash"}
    )
    return ExecutionAttemptAuthorization.model_validate(document, strict=True)


def assign_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


def all_keys(value: object) -> set[str]:
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        return {
            key
            for raw_key, item in mapping.items()
            for key in (({raw_key} if type(raw_key) is str else set()) | all_keys(item))
        }
    if type(value) in {list, tuple}:
        return {
            key for item in cast(list[object] | tuple[object, ...], value) for key in all_keys(item)
        }
    return set()


def rehash_report(
    report: ExecutionReport,
    **updates: object,
) -> ExecutionReport:
    """Recompute an untrusted report Hash after applying hostile field changes."""
    draft = report.model_copy(
        update={
            **updates,
            "content_hash": "0" * 64,
        }
    )
    content_hash = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    document = draft.model_dump(mode="python", warnings="error")
    document["content_hash"] = content_hash
    return ExecutionReport.model_validate(document, strict=True)


def test_executor_builds_exact_calls_and_preserves_plan_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(
        roles=(StepRole.OBSERVE, StepRole.ACTION, StepRole.ACTION),
    )
    calls: list[ToolCall[GetSystemStatusArguments]] = []
    original = harness.gateway._invoke_with_receipt

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        calls.append(call)
        return original(call)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    authorization = authorize(harness)
    report = harness.executor.execute_actions(authorization)

    assert tuple(call.plan_step_id for call in calls) == tuple(
        step.step_id for step in harness.plan.steps
    )
    assert tuple(call.invocation_id for call in calls) == INVOCATION_IDS[:3]
    for call, step in zip(calls, harness.plan.steps, strict=True):
        assert call.tool_id == step.tool_id
        assert call.tool_version == step.tool_version
        assert call.contract_hash == step.contract_hash
        assert call.implementation_hash == step.implementation_hash
        assert call.arguments_hash == canonical_json_sha256(step.arguments)
        assert call.arguments == step.arguments
        assert call.target.target_id == harness.plan.target
        assert call.target.resource_type == harness.decision.target.resource_type
        assert call.target.resource_id == step.arguments.target
    assert report.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert report.next_state is ExecutionNextState.VERIFYING
    assert len(report.records) == 3
    assert all(
        record.dispatch_status is DispatchStatus.HANDLER_DISPATCHED
        and record.effect_disposition is EffectDisposition.NONE
        and record.result is not None
        and record.result.success
        for record in report.records
    )


def test_executor_separates_action_prefix_from_verification_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(
        roles=(
            StepRole.OBSERVE,
            StepRole.ACTION,
            StepRole.VERIFY,
            StepRole.VERIFY,
        ),
    )
    calls: list[str] = []
    original = harness.gateway._invoke_with_receipt

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        calls.append(call.plan_step_id)
        return original(call)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)
    authorization = authorize(harness)

    action_report = harness.executor.execute_actions(authorization)

    assert calls == ["status-0", "status-1"]
    assert action_report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
    assert action_report.next_state is ExecutionNextState.VERIFYING
    assert tuple(record.role for record in action_report.records) == (
        StepRole.OBSERVE,
        StepRole.ACTION,
    )
    with pytest.raises(ExecutionAttemptError, match="out of order"):
        harness.executor.execute_actions(authorization)

    final_report = harness.executor.execute_verification(authorization)

    assert calls == ["status-0", "status-1", "status-2", "status-3"]
    assert final_report.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert final_report.next_state is ExecutionNextState.VERIFYING
    assert tuple(record.role for record in final_report.records) == (
        StepRole.OBSERVE,
        StepRole.ACTION,
        StepRole.VERIFY,
        StepRole.VERIFY,
    )
    with pytest.raises(ExecutionAttemptError, match="closed"):
        harness.executor.execute_verification(authorization)


def test_executor_reports_total_monotonic_duration_across_both_phases() -> None:
    harness = make_harness(
        roles=(StepRole.OBSERVE, StepRole.VERIFY),
        clock=sequence_clock(2_000_000, 5_000_000, 11_000_000),
    )
    authorization = authorize(harness)

    action_report = harness.executor.execute_actions(authorization)
    final_report = harness.executor.execute_verification(authorization)

    assert action_report.status is ExecutionReportStatus.AWAITING_VERIFICATION_DISPATCH
    assert action_report.total_duration_ms == 3
    assert final_report.status is ExecutionReportStatus.READY_FOR_VERIFIER
    assert final_report.total_duration_ms == 9


def test_executor_clock_failure_before_attempt_authorization_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(clock=sequence_clock())
    gateway_calls = 0

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal gateway_calls
        gateway_calls += 1
        return receipt(success_result(call))

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    with pytest.raises(ExecutionAuthorizationError) as caught:
        authorize(harness)

    assert caught.value.reason_code == "executor_clock_failed"
    assert gateway_calls == 0


def test_executor_final_clock_failure_returns_closed_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(clock=sequence_clock(2_000_000))
    gateway_calls = 0
    original = harness.gateway._invoke_with_receipt

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal gateway_calls
        gateway_calls += 1
        return original(call)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)
    authorization = authorize(harness)

    report = harness.executor.execute_actions(authorization)

    assert gateway_calls == 1
    assert report.status is ExecutionReportStatus.FAILED
    assert report.next_state is ExecutionNextState.FAILED
    assert report.failure_code == "executor_clock_failed"
    assert report.total_duration_ms is None
    assert report.failed_step_index is None
    assert len(report.records) == 1
    assert report.records[0].result is not None
    assert report.records[0].result.success is True
    assert report.events[-2].kind is ExecutionEventKind.ATTEMPT_FAILED
    assert report.events[-1].kind is ExecutionEventKind.ATTEMPT_CLOSED
    with pytest.raises(ExecutionAttemptError, match="closed"):
        harness.executor.execute_actions(authorization)
    assert gateway_calls == 1


def test_executor_rejects_verification_before_action_phase() -> None:
    harness = make_harness(roles=(StepRole.OBSERVE, StepRole.VERIFY))
    authorization = authorize(harness)

    with pytest.raises(ExecutionAttemptError, match="out of order"):
        harness.executor.execute_verification(authorization)


def test_executor_stops_on_structured_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(
        roles=(StepRole.OBSERVE, StepRole.ACTION, StepRole.ACTION),
    )
    calls: list[ToolCall[GetSystemStatusArguments]] = []

    def failing_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        calls.append(call)
        return receipt(failure_result(call))

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", failing_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert len(calls) == 1
    assert report.status is ExecutionReportStatus.FAILED
    assert report.next_state is ExecutionNextState.FAILED
    assert report.failure_code == "tool_execution_failed"
    assert report.failed_step_index == 0
    assert len(report.records) == 1
    assert report.records[0].result is not None
    assert report.records[0].result.error is not None
    assert report.records[0].result.error.retryable is False
    assert report.events[-2].kind is ExecutionEventKind.ATTEMPT_FAILED
    assert report.events[-1].kind is ExecutionEventKind.ATTEMPT_CLOSED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invocation_id", INVOCATION_IDS[5]),
        ("plan_step_id", "other-step"),
        ("tool_id", "other_tool"),
        ("tool_version", "1.0.1"),
        ("contract_hash", "c" * 64),
        ("arguments_hash", "d" * 64),
        (
            "target",
            TargetReference(
                target_id="other-target",
                resource_type="local_system",
                resource_id="other-target",
            ),
        ),
    ],
)
def test_executor_fails_closed_on_receipt_result_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    harness = make_harness()
    calls = 0

    def mismatched_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        result = success_result(call).model_copy(update={field: value})
        return receipt(result)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", mismatched_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert calls == 1
    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == "malformed_gateway_receipt"
    assert report.records[0].result is None
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN
    assert report.human_intervention_required is True


@pytest.mark.parametrize(
    "hostile_variant",
    [
        "secret_evidence",
        "contract_invalid_evidence",
        "contract_timeout",
        "secret_error",
    ],
)
def test_executor_revalidates_identity_valid_results_against_registered_contract(
    monkeypatch: pytest.MonkeyPatch,
    hostile_variant: str,
) -> None:
    harness = make_harness()
    calls = 0
    secret_marker = "bash -c EXECUTOR_SECRET_MARKER"

    def hostile_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        if hostile_variant == "secret_error":
            base = failure_result(call)
            assert base.error is not None
            result = base.model_copy(
                update={"error": base.error.model_copy(update={"message": secret_marker})}
            )
        else:
            base = success_result(call)
            if hostile_variant == "secret_evidence":
                document = base.model_dump(mode="python", warnings="error")
                document["evidence"] = {"password": secret_marker}
                result = ToolResult[BaseModel].model_validate(document, strict=True)
            elif hostile_variant == "contract_invalid_evidence":
                document = base.model_dump(mode="python", warnings="error")
                document["evidence"] = {"source": "mock"}
                result = ToolResult[BaseModel].model_validate(document, strict=True)
            else:
                result = base.model_copy(update={"duration_ms": 1_001})
        return receipt(result)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", hostile_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert calls == 1
    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == "malformed_gateway_receipt"
    assert report.records[0].result is None
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN
    assert report.human_intervention_required is True
    assert secret_marker not in report.model_dump_json()


def test_executor_fails_closed_on_malformed_receipt_result_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(roles=(StepRole.OBSERVE, StepRole.ACTION))
    calls = 0

    def malformed_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        del call
        return GatewayDispatchReceipt(
            result=cast(ToolResult[BaseModel], object()),
            dispatch_status=GatewayDispatchStatus.HANDLER_DISPATCHED,
            mutates_remote_state=False,
        )

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", malformed_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert calls == 1
    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == "malformed_gateway_receipt"
    assert len(report.records) == 1
    assert report.records[0].result is None
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN


@pytest.mark.parametrize(
    "malformed_receipt",
    [
        object(),
        GatewayDispatchReceipt(
            result=cast(ToolResult[BaseModel], object()),
            dispatch_status=cast(GatewayDispatchStatus, "handler_dispatched"),
            mutates_remote_state=False,
        ),
        GatewayDispatchReceipt(
            result=cast(ToolResult[BaseModel], object()),
            dispatch_status=GatewayDispatchStatus.HANDLER_DISPATCHED,
            mutates_remote_state=cast(bool, 1),
        ),
    ],
)
def test_executor_converts_untrusted_receipt_shape_to_unknown_evidence(
    monkeypatch: pytest.MonkeyPatch,
    malformed_receipt: object,
) -> None:
    harness = make_harness()

    def malformed_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        del call
        return cast(GatewayDispatchReceipt, malformed_receipt)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", malformed_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == "malformed_gateway_receipt"
    assert report.human_intervention_required is True
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN


def test_executor_rejects_receipt_mutation_flag_different_from_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()

    def mismatched_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        return GatewayDispatchReceipt(
            result=success_result(call),
            dispatch_status=GatewayDispatchStatus.HANDLER_DISPATCHED,
            mutates_remote_state=True,
        )

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", mismatched_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == "malformed_gateway_receipt"
    assert report.human_intervention_required is True
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN


def test_executor_rejects_success_receipt_that_claims_no_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()

    def contradictory_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        return GatewayDispatchReceipt(
            result=success_result(call),
            dispatch_status=GatewayDispatchStatus.NOT_DISPATCHED,
            mutates_remote_state=False,
        )

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", contradictory_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert report.failure_code == "malformed_gateway_receipt"
    assert report.records[0].result is None
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN
    assert report.human_intervention_required is True


def test_executor_converts_gateway_exception_without_retry_or_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(roles=(StepRole.OBSERVE, StepRole.ACTION))
    marker = "SENSITIVE_GATEWAY_MARKER"
    calls = 0

    def failing_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        del call
        raise ToolGatewayError(marker)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", failing_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert calls == 1
    assert report.failure_code == "gateway_dispatch_unknown"
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN
    assert report.human_intervention_required is True
    assert marker not in str(report.model_dump(mode="json", warnings="error"))


def test_executor_accepts_only_exact_known_pre_dispatch_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()

    def failing_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        del call
        raise ToolIntegrityError("SENSITIVE_PRE_DISPATCH_EXCEPTION")

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", failing_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert report.failure_code == ToolIntegrityError.code
    assert report.records[0].dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert report.records[0].effect_disposition is EffectDisposition.NONE
    assert report.human_intervention_required is False
    assert "SENSITIVE_PRE_DISPATCH_EXCEPTION" not in report.model_dump_json()


def test_executor_accepts_exact_post_dispatch_error_with_authoritative_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()

    def failing_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        del call
        raise PostDispatchToolIntegrityError(
            "SENSITIVE_POST_DISPATCH_EXCEPTION",
            mutates_remote_state=False,
        )

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", failing_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert report.failure_code == PostDispatchToolIntegrityError.code
    assert report.records[0].dispatch_status is DispatchStatus.HANDLER_DISPATCHED
    assert report.records[0].effect_disposition is EffectDisposition.NONE
    assert report.human_intervention_required is False
    assert "SENSITIVE_POST_DISPATCH_EXCEPTION" not in report.model_dump_json()


def test_executor_rejects_post_dispatch_error_with_wrong_mutation_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()

    def failing_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        del call
        raise PostDispatchToolIntegrityError(
            "SENSITIVE_POST_DISPATCH_MISMATCH",
            mutates_remote_state=True,
        )

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", failing_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert report.failure_code == "gateway_dispatch_unknown"
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN
    assert report.human_intervention_required is True


def test_executor_rejects_unregistered_gateway_error_subclass_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()

    class UnknownEffectAfterDispatchError(ToolGatewayError):
        code = "unknown_effect_after_dispatch"
        dispatch_status = GatewayDispatchStatus.HANDLER_DISPATCHED

    def failing_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        del call
        raise UnknownEffectAfterDispatchError("SENSITIVE_TYPED_EXCEPTION")

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", failing_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert report.failure_code == "gateway_dispatch_unknown"
    assert report.human_intervention_required is True
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN
    assert "SENSITIVE_TYPED_EXCEPTION" not in str(report.model_dump(mode="json", warnings="error"))


def test_executor_marks_untyped_gateway_exception_dispatch_and_effect_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()

    def exploding_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        del call
        raise RuntimeError("SENSITIVE_RAW_EXCEPTION")

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", exploding_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert report.failure_code == "gateway_dispatch_unknown"
    assert report.human_intervention_required is True
    assert report.records[0].dispatch_status is DispatchStatus.UNKNOWN
    assert report.records[0].effect_disposition is EffectDisposition.UNKNOWN
    assert "SENSITIVE_RAW_EXCEPTION" not in str(report.model_dump(mode="json", warnings="error"))


@pytest.mark.parametrize("replacement", ["drift", "deny"])
def test_executor_stops_before_dispatch_when_policy_changes(
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    harness = make_harness()
    authorization = authorize(harness)
    if replacement == "drift":
        changed = harness.decision.model_copy(update={"policy_hash": "f" * 64})
    else:
        changed = denied_decision(harness.decision)
    gateway_calls = 0

    def changed_evaluate(
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        del plan, context
        return changed

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal gateway_calls
        gateway_calls += 1
        return receipt(success_result(call))

    monkeypatch.setattr(harness.policy, "evaluate", changed_evaluate)
    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    report = harness.executor.execute_actions(authorization)

    assert gateway_calls == 0
    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == "policy_revalidation_failed"
    assert report.records == ()


@pytest.mark.parametrize("failure_source", ["policy", "invocation_id"])
@pytest.mark.parametrize("successful_dispatches", [0, 1])
def test_executor_pre_dispatch_failure_identifies_next_step_without_extra_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    failure_source: str,
    successful_dispatches: int,
) -> None:
    invocation_ids = INVOCATION_IDS[:successful_dispatches]
    harness = make_harness(
        roles=(StepRole.OBSERVE,) * (successful_dispatches + 1),
        invocation_ids=invocation_ids if failure_source == "invocation_id" else INVOCATION_IDS,
    )
    authorization = authorize(harness)
    if failure_source == "policy":
        policy_reads = 0

        def failing_evaluate(
            plan: ExecutionPlan,
            context: PolicyEvaluationContext,
        ) -> PolicyDecision:
            nonlocal policy_reads
            del plan, context
            if policy_reads == successful_dispatches:
                raise RuntimeError("SENSITIVE_POLICY_FAILURE")
            policy_reads += 1
            return harness.decision

        monkeypatch.setattr(harness.policy, "evaluate", failing_evaluate)

    gateway_calls = 0
    original = harness.gateway._invoke_with_receipt

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal gateway_calls
        gateway_calls += 1
        return original(call)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    report = harness.executor.execute_actions(authorization)

    expected_code = (
        "policy_revalidation_failed" if failure_source == "policy" else "invocation_id_invalid"
    )
    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == expected_code
    assert report.failed_step_index == successful_dispatches
    assert len(report.records) == successful_dispatches
    assert gateway_calls == successful_dispatches
    assert all(record.result is not None and record.result.success for record in report.records)
    with pytest.raises(ExecutionAttemptError, match="closed"):
        harness.executor.execute_actions(authorization)
    assert gateway_calls == successful_dispatches


def test_executor_rejects_denied_decision_before_opening_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    calls = 0

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        return receipt(success_result(call))

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    with pytest.raises(ExecutionAuthorizationError) as caught:
        harness.executor.begin_attempt(
            harness.plan,
            denied_decision(harness.decision),
            approval_id=None,
        )

    assert caught.value.reason_code == "policy_decision_mismatch"
    assert calls == 0


def test_executor_rejects_structurally_valid_forged_authorization_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    authorization = authorize(harness)
    forged = forge_authorization(authorization)
    calls = 0

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        return receipt(success_result(call))

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    with pytest.raises(ExecutionAttemptError, match="forged"):
        harness.executor.execute_actions(forged)

    assert calls == 0


def test_executor_rejects_closed_authorization_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    authorization = authorize(harness)
    calls = 0
    original = harness.gateway._invoke_with_receipt

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        return original(call)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    report = harness.executor.execute_actions(authorization)

    assert report.status is ExecutionReportStatus.READY_FOR_VERIFIER
    with pytest.raises(ExecutionAttemptError, match="closed"):
        harness.executor.execute_actions(authorization)
    assert calls == 1


def test_executor_rejects_duplicate_invocation_id_without_second_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(
        roles=(StepRole.OBSERVE, StepRole.ACTION),
        invocation_ids=(INVOCATION_IDS[0], INVOCATION_IDS[0]),
    )
    calls = 0
    original = harness.gateway._invoke_with_receipt

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        return original(call)

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    report = harness.executor.execute_actions(authorize(harness))

    assert calls == 1
    assert report.status is ExecutionReportStatus.FAILED
    assert report.failure_code == "invocation_id_invalid"
    assert len(report.records) == 1
    assert report.records[0].result is not None
    assert report.records[0].result.success is True


def test_authorization_and_report_are_hash_bound_frozen_and_omit_raw_arguments() -> None:
    harness = make_harness()
    authorization = authorize(harness)
    report = harness.executor.execute_actions(authorization)

    assert authorization.plan_digest == canonical_json_sha256(harness.plan)
    assert authorization.policy_decision_hash == canonical_json_sha256(harness.decision)
    assert authorization.content_hash == canonical_json_sha256(
        authorization.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    assert report.content_hash == canonical_json_sha256(
        report.model_dump(
            mode="json",
            exclude={"content_hash"},
            warnings="error",
        )
    )
    assert report.authorization_hash == authorization.content_hash
    assert "arguments" not in all_keys(authorization.model_dump(mode="json", warnings="error"))
    assert "arguments" not in all_keys(report.model_dump(mode="json", warnings="error"))
    with pytest.raises(ValidationError, match="frozen"):
        assign_attribute(authorization, "task_id", FORGED_TASK_ID)
    with pytest.raises(ValidationError, match="frozen"):
        assign_attribute(report, "failure_code", "forged")


@pytest.mark.parametrize(
    ("dispatch_status", "effect_disposition"),
    [
        (DispatchStatus.NOT_DISPATCHED, EffectDisposition.NONE),
        (DispatchStatus.UNKNOWN, EffectDisposition.UNKNOWN),
    ],
)
def test_success_record_requires_definite_handler_dispatch(
    dispatch_status: DispatchStatus,
    effect_disposition: EffectDisposition,
) -> None:
    harness = make_harness()
    report = harness.executor.execute_actions(authorize(harness))
    record = report.records[0]
    document = record.model_dump(mode="python", warnings="error")
    document.update(
        {
            "dispatch_status": dispatch_status,
            "effect_disposition": effect_disposition,
        }
    )

    with pytest.raises(ValidationError, match="definite handler dispatch"):
        StepExecutionRecord.model_validate(document, strict=True)


def test_report_rejects_missing_step_event_even_with_recomputed_hash() -> None:
    harness = make_harness()
    report = harness.executor.execute_actions(authorize(harness))
    retained_events = tuple(
        event for event in report.events if event.kind is not ExecutionEventKind.STEP_FINISHED
    )
    events = tuple(
        event.model_copy(update={"sequence": sequence})
        for sequence, event in enumerate(retained_events)
    )

    with pytest.raises(ValidationError, match="exactly mirror"):
        rehash_report(report, events=events)


def test_report_status_requires_exact_final_attempt_event_with_recomputed_hash() -> None:
    harness = make_harness()
    report = harness.executor.execute_actions(authorize(harness))
    forged_final = ExecutionEvent(
        sequence=report.events[-1].sequence,
        kind=ExecutionEventKind.PHASE_READY,
        execution_attempt_id=report.execution_attempt_id,
    )

    with pytest.raises(ValidationError, match="successfully closed"):
        rehash_report(report, events=(*report.events[:-1], forged_final))


def test_success_report_requires_a_measured_total_duration() -> None:
    harness = make_harness()
    report = harness.executor.execute_actions(authorize(harness))

    with pytest.raises(ValidationError, match="explicit clock failure"):
        rehash_report(report, total_duration_ms=None)


def test_abort_attempt_closes_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    authorization = authorize(harness)
    calls = 0

    def recording_invoke(
        call: ToolCall[GetSystemStatusArguments],
    ) -> GatewayDispatchReceipt:
        nonlocal calls
        calls += 1
        return receipt(success_result(call))

    monkeypatch.setattr(harness.gateway, "_invoke_with_receipt", recording_invoke)

    report = harness.executor.abort_attempt(authorization)

    assert calls == 0
    assert report.status is ExecutionReportStatus.FAILED
    assert report.next_state is ExecutionNextState.FAILED
    assert report.failure_code == "attempt_aborted"
    assert report.records == ()
    with pytest.raises(ExecutionAttemptError, match="closed"):
        harness.executor.execute_actions(authorization)
