from collections.abc import MutableMapping, MutableSequence
from typing import Any, cast
from uuid import UUID

import pytest

from ai_server.models.execution import StepRole
from ai_server.models.executor import (
    DispatchStatus,
    EffectDisposition,
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionNextState,
    ExecutionReport,
    ExecutionReportStatus,
    StepExecutionRecord,
)
from ai_server.models.system_status import ServiceStatus, SystemStatus
from ai_server.models.tool import TargetReference, ToolResult
from ai_server.tools.hashing import canonical_json_sha256

CONTRACT_HASH = "a" * 64
ARGUMENTS_HASH = "b" * 64
IMPLEMENTATION_HASH = "c" * 64
POLICY_HASH = "d" * 64
AUTHORIZATION_HASH = "e" * 64
INVOCATION_ID = UUID("00000000-0000-4000-8000-000000000001")
ATTEMPT_ID = UUID("00000000-0000-4000-8000-000000000002")
TASK_ID = UUID("00000000-0000-4000-8000-000000000003")
PLAN_ID = UUID("00000000-0000-4000-8000-000000000004")


def make_nested_result() -> ToolResult[SystemStatus]:
    return ToolResult[SystemStatus](
        invocation_id=INVOCATION_ID,
        plan_step_id="status",
        tool_id="get_system_status",
        tool_version="1.0.0",
        contract_hash=CONTRACT_HASH,
        arguments_hash=ARGUMENTS_HASH,
        target=TargetReference(
            target_id="local-mock",
            resource_type="local_system",
            resource_id="local-mock",
        ),
        success=True,
        data=SystemStatus(
            cpu_percent=12.5,
            memory_percent=34.0,
            disk_percent=45.5,
            services=(ServiceStatus(name="mock-api", state="running"),),
        ),
        evidence={
            "source": "mock",
            "diagnostics": {
                "labels": ["stable", {"origin": "fixture"}],
            },
        },
        error=None,
        duration_ms=0,
    )


def make_report(result: ToolResult[SystemStatus]) -> ExecutionReport:
    record = StepExecutionRecord(
        step_index=0,
        step_id="status",
        role=StepRole.OBSERVE,
        tool_id=result.tool_id,
        tool_version=result.tool_version,
        contract_hash=result.contract_hash,
        implementation_hash=IMPLEMENTATION_HASH,
        arguments_hash=result.arguments_hash,
        target=result.target,
        invocation_id=result.invocation_id,
        dispatch_status=DispatchStatus.HANDLER_DISPATCHED,
        effect_disposition=EffectDisposition.NONE,
        result=result,
        failure_code=None,
    )
    events = (
        ExecutionEvent(
            sequence=0,
            kind=ExecutionEventKind.ATTEMPT_AUTHORIZED,
            execution_attempt_id=ATTEMPT_ID,
        ),
        ExecutionEvent(
            sequence=1,
            kind=ExecutionEventKind.STEP_FINISHED,
            execution_attempt_id=ATTEMPT_ID,
            step_index=0,
            step_id="status",
            invocation_id=INVOCATION_ID,
            dispatch_status=DispatchStatus.HANDLER_DISPATCHED,
            effect_disposition=EffectDisposition.NONE,
        ),
        ExecutionEvent(
            sequence=2,
            kind=ExecutionEventKind.ATTEMPT_CLOSED,
            execution_attempt_id=ATTEMPT_ID,
        ),
    )
    draft = ExecutionReport.model_construct(
        execution_attempt_id=ATTEMPT_ID,
        authorization_hash=AUTHORIZATION_HASH,
        task_id=TASK_ID,
        plan_id=PLAN_ID,
        plan_digest=CONTRACT_HASH,
        policy_decision_hash=POLICY_HASH,
        approval_id=None,
        status=ExecutionReportStatus.READY_FOR_VERIFIER,
        next_state=ExecutionNextState.VERIFYING,
        records=(record,),
        events=events,
        total_duration_ms=0,
        failed_step_index=None,
        failure_code=None,
        human_intervention_required=False,
        content_hash="0" * 64,
    )
    content_hash = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"content_hash"}, warnings="error")
    )
    document = draft.model_dump(mode="python", warnings="error")
    document["content_hash"] = content_hash
    return ExecutionReport.model_validate(document, strict=True)


def test_tool_result_evidence_is_deeply_immutable_and_round_trips() -> None:
    result = make_nested_result()
    top_level = cast(MutableMapping[str, object], result.evidence)
    diagnostics = cast(MutableMapping[str, object], result.evidence["diagnostics"])
    labels = cast(MutableSequence[object], diagnostics["labels"])
    nested_label = cast(MutableMapping[str, object], labels[1])

    with pytest.raises(TypeError):
        top_level["source"] = "forged"
    with pytest.raises(TypeError):
        diagnostics["extra"] = True
    with pytest.raises(AttributeError):
        labels.append("forged")
    with pytest.raises(TypeError):
        nested_label["origin"] = "forged"

    for mode in ("python", "json"):
        document = result.model_dump(mode=mode, warnings="error")
        evidence = cast(dict[str, Any], document["evidence"])
        dumped_diagnostics = cast(dict[str, Any], evidence["diagnostics"])
        assert type(evidence) is dict
        assert type(dumped_diagnostics) is dict
        assert type(dumped_diagnostics["labels"]) is list

    python_document = result.model_dump(mode="python", warnings="error")
    assert ToolResult[SystemStatus].model_validate(python_document, strict=True) == result
    assert ToolResult[SystemStatus].model_validate_json(result.model_dump_json()) == result


def test_execution_report_hash_cannot_be_invalidated_through_exposed_result() -> None:
    report = make_report(make_nested_result())
    original_hash = report.content_hash
    original_document = report.model_dump(mode="json", warnings="error")
    exposed_result = report.results[0]

    with pytest.raises(TypeError):
        cast(MutableMapping[str, object], exposed_result.evidence)["source"] = "forged"

    dumped = report.model_dump(mode="python", warnings="error")
    records = cast(tuple[dict[str, Any], ...], dumped["records"])
    dumped_result = cast(dict[str, Any], records[0]["result"])
    dumped_evidence = cast(dict[str, Any], dumped_result["evidence"])
    dumped_evidence["source"] = "changed-copy"

    assert report.content_hash == original_hash
    assert report.model_dump(mode="json", warnings="error") == original_document
    assert exposed_result.evidence["source"] == "mock"
    assert (
        ExecutionReport.model_validate(
            report.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        == report
    )
