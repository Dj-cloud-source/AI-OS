from uuid import UUID

import pytest

from ai_server.context.builder import ContextBuilder
from ai_server.models.execution import ExecutionPlan
from ai_server.models.system_status import ServiceStatus, SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import (
    RedactionRequirement,
    RiskLevel,
    RollbackRequirement,
    RollbackStrategy,
    SideEffectKind,
    TargetReference,
    ToolError,
    ToolErrorCategory,
    ToolMetadata,
    ToolResult,
    ToolSideEffects,
    ToolTargetScope,
    VerificationRequirement,
)
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.runtime.errors import VerificationError
from ai_server.tools.hashing import canonical_json_sha256
from ai_server.verifier.service import Verifier


def make_success() -> tuple[ExecutionPlan, ToolResult[SystemStatus]]:
    """Build a matching plan and trusted structured success evidence."""
    metadata = ToolMetadata(
        tool_id="get_system_status",
        version="1.0.0",
        contract_hash="a" * 64,
        implementation_hash="b" * 64,
        description="Return deterministic simulated system status.",
        risk_level=RiskLevel.L0,
        side_effects=ToolSideEffects(
            mutates_remote_state=False,
            kind=SideEffectKind.NONE,
        ),
        target_scope=ToolTargetScope(
            resource_type="local_system",
            maximum_targets=1,
            selector_field="target",
            allow_dynamic_expansion=False,
        ),
        redaction=RedactionRequirement(
            profile_id="local-default",
            profile_version="1.0.0",
            safe_evidence_fields=("source",),
            max_retained_payload_bytes=4096,
        ),
        verification=VerificationRequirement(
            required=True,
            evidence_fields=("source",),
        ),
        rollback=RollbackRequirement(
            required=False,
            available=False,
            strategy=RollbackStrategy.NOT_REQUIRED,
        ),
        timeout_ms=1000,
        idempotent=True,
        input_schema_id="urn:ai-server:tool:get-system-status:input-v1",
        output_schema_id="urn:ai-server:tool:get-system-status:output-v1",
        input_model="GetSystemStatusArguments",
        output_model="SystemStatus",
    )
    task = Task(request=SUPPORTED_REQUEST)
    plan = Planner().create_plan(ContextBuilder().build(task), metadata)
    step = plan.steps[0]
    result = ToolResult[SystemStatus](
        invocation_id=UUID("00000000-0000-4000-8000-000000000001"),
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
        data=SystemStatus(
            cpu_percent=12.5,
            memory_percent=34.0,
            disk_percent=45.5,
            services=(ServiceStatus(name="mock-api", state="running"),),
        ),
        evidence={"source": "mock"},
        error=None,
        duration_ms=0,
    )
    return plan, result


def failed_result(result: ToolResult[SystemStatus]) -> ToolResult[SystemStatus]:
    """Convert trusted success evidence into a valid structured failure."""
    return ToolResult[SystemStatus](
        invocation_id=result.invocation_id,
        plan_step_id=result.plan_step_id,
        tool_id=result.tool_id,
        tool_version=result.tool_version,
        contract_hash=result.contract_hash,
        arguments_hash=result.arguments_hash,
        target=result.target,
        success=False,
        data=None,
        evidence={},
        error=ToolError(
            code="tool_execution_failed",
            category=ToolErrorCategory.EXECUTION,
            message="Tool execution failed safely",
            retryable=False,
        ),
        duration_ms=result.duration_ms,
    )


def test_verifier_accepts_exact_success_evidence() -> None:
    plan, result = make_success()

    Verifier().verify(plan, (result,))


def test_verifier_rejects_structured_failure() -> None:
    plan, result = make_success()

    with pytest.raises(VerificationError, match="malformed"):
        Verifier().verify(plan, (failed_result(result),))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plan_step_id", "other-step"),
        ("tool_id", "other_tool"),
        ("tool_version", "1.0.1"),
        ("contract_hash", "c" * 64),
        ("arguments_hash", "d" * 64),
    ],
)
def test_verifier_rejects_result_identity_or_hash_mismatch(
    field: str,
    value: str,
) -> None:
    plan, result = make_success()
    mismatched = result.model_copy(update={field: value})

    with pytest.raises(VerificationError, match="identity"):
        Verifier().verify(plan, (mismatched,))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_id", "other-target"),
        ("resource_type", "other_resource"),
        ("resource_id", "other-resource"),
    ],
)
def test_verifier_rejects_any_target_mismatch(
    field: str,
    value: str,
) -> None:
    plan, result = make_success()
    target = result.target.model_copy(update={field: value})
    mismatched = result.model_copy(update={"target": target})

    with pytest.raises(VerificationError, match="target"):
        Verifier().verify(plan, (mismatched,))


def test_verifier_accepts_stopped_service_as_observed_status() -> None:
    plan, result = make_success()
    assert result.data is not None
    stopped_data = result.data.model_copy(
        update={"services": (result.data.services[0].model_copy(update={"state": "stopped"}),)}
    )
    stopped_result = result.model_copy(update={"data": stopped_data})

    Verifier().verify(plan, (stopped_result,))


def test_verifier_rejects_payload_target_mismatch() -> None:
    plan, result = make_success()
    assert result.data is not None
    mismatched_data = result.data.model_copy(update={"target": "other-target"})
    mismatched = result.model_copy(update={"data": mismatched_data})

    with pytest.raises(VerificationError, match="malformed|target"):
        Verifier().verify(plan, (mismatched,))


def test_verifier_rejects_missing_or_extra_evidence() -> None:
    plan, result = make_success()

    with pytest.raises(VerificationError, match="count"):
        Verifier().verify(plan, ())
    with pytest.raises(VerificationError, match="count"):
        Verifier().verify(plan, (result, result))


def test_verifier_rejects_empty_plan() -> None:
    plan, result = make_success()
    empty_plan = plan.model_copy(update={"steps": ()})

    with pytest.raises(VerificationError, match="malformed plan"):
        Verifier().verify(empty_plan, (result,))
