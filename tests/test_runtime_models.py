from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_server.models.execution import ExecutionPlan
from ai_server.models.runtime import (
    LifecycleEvent,
    LifecycleEventKind,
    RuntimeComponent,
    RuntimeFailure,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
)
from ai_server.models.task import Task
from ai_server.models.tool import ToolMetadata
from ai_server.planner.service import SUPPORTED_REQUEST
from ai_server.policy.engine import PolicyEngine, ToolKey
from ai_server.runtime.engine import RuntimeEngine
from ai_server.runtime.errors import ApprovalRequiredError
from ai_server.runtime.state import RuntimeState


def assign_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


def completed_outcome() -> RuntimeOutcome:
    timestamp = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
    return RuntimeEngine(clock=lambda: timestamp).run(Task(request=SUPPORTED_REQUEST))


def waiting_outcome() -> RuntimeOutcome:
    class ApprovalPolicy(PolicyEngine):
        def check(
            self,
            plan: ExecutionPlan,
            catalog: Mapping[ToolKey, ToolMetadata],
        ) -> None:
            del plan, catalog
            raise ApprovalRequiredError("approval required")

    return RuntimeEngine(policy=ApprovalPolicy()).run(Task(request=SUPPORTED_REQUEST))


def test_runtime_lifecycle_models_round_trip_and_are_frozen() -> None:
    completed = completed_outcome()
    failed = RuntimeEngine().run(Task(request="unsupported"))

    assert RuntimeOutcome.model_validate_json(completed.model_dump_json()) == completed
    assert RuntimeOutcome.model_validate_json(failed.model_dump_json()) == failed
    assert isinstance(completed.events, tuple)
    assert isinstance(completed.results, tuple)

    models_and_fields = (
        (completed, "status", RuntimeOutcomeStatus.FAILED),
        (completed.events[0], "sequence", 99),
        (
            RuntimeFailure(
                code="safe_failure",
                component=RuntimeComponent.RUNTIME,
                message="Safe failure.",
            ),
            "code",
            "changed",
        ),
    )
    for model, name, value in models_and_fields:
        with pytest.raises(ValidationError):
            assign_attribute(model, name, value)


def test_lifecycle_event_requires_aware_utc_timestamp() -> None:
    task = Task(request=SUPPORTED_REQUEST)

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        LifecycleEvent(
            task_id=task.task_id,
            sequence=0,
            occurred_at=datetime(2026, 7, 25, 8, 0),
            kind=LifecycleEventKind.STATE_ENTERED,
            state=RuntimeState.RECEIVED,
        )


def test_lifecycle_event_datetime_hooks_cannot_raise_baseexception() -> None:
    class ExitingTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta:
            del value
            raise SystemExit("SENSITIVE_EVENT_TIMEZONE_MARKER")

        def dst(self, value: datetime | None) -> timedelta:
            del value
            return timedelta(0)

        def tzname(self, value: datetime | None) -> str:
            del value
            return "untrusted-test"

    with pytest.raises(ValidationError) as caught:
        LifecycleEvent(
            task_id=uuid4(),
            sequence=0,
            occurred_at=datetime(2026, 7, 25, 8, 0, tzinfo=ExitingTimezone()),
            kind=LifecycleEventKind.STATE_ENTERED,
            state=RuntimeState.RECEIVED,
        )

    assert "SENSITIVE_EVENT_TIMEZONE_MARKER" not in str(caught.value)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("sequence", "sequences"),
        ("task_id", "belong"),
        ("timestamp", "backwards"),
        ("history", "state-entry events"),
        ("arguments_hash", "execution result identity"),
        ("target_scope", "execution result identity"),
        ("status", "FAILED outcome"),
        ("missing_failure", "FAILED outcome"),
    ],
)
def test_runtime_outcome_rejects_contradictory_lifecycle_data(
    mutation: str,
    message: str,
) -> None:
    completed = completed_outcome()
    payload = completed.model_dump(mode="python")

    if mutation == "sequence":
        payload["events"][1]["sequence"] = 7
    elif mutation == "task_id":
        payload["events"][1]["task_id"] = uuid4()
    elif mutation == "timestamp":
        payload["events"][1]["occurred_at"] = datetime(2025, 1, 1, tzinfo=UTC)
    elif mutation == "history":
        payload["task"]["state_history"] = [
            RuntimeState.RECEIVED,
            RuntimeState.CONTEXT_BUILDING,
            RuntimeState.PLANNING,
            RuntimeState.POLICY_CHECK,
            RuntimeState.WAITING_FOR_APPROVAL,
            RuntimeState.EXECUTING,
            RuntimeState.FAILED,
        ]
        payload["task"]["state"] = RuntimeState.FAILED
    elif mutation == "arguments_hash":
        payload["results"][0]["arguments_hash"] = "d" * 64
    elif mutation == "target_scope":
        payload["results"][0]["target"]["resource_type"] = "other_resource"
    elif mutation == "status":
        payload["status"] = RuntimeOutcomeStatus.FAILED
    else:
        failed = RuntimeEngine().run(Task(request="unsupported"))
        payload = failed.model_dump(mode="python")
        payload["failure"] = None

    with pytest.raises(ValidationError, match=message):
        RuntimeOutcome.model_validate(payload)


def test_event_shapes_reject_contradictory_fields() -> None:
    task = Task(request=SUPPORTED_REQUEST)
    timestamp = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)

    invalid_payloads = (
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.STATE_ENTERED,
            "state": RuntimeState.RECEIVED,
            "component": RuntimeComponent.RUNTIME,
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.COMPONENT_COMPLETED,
            "state": RuntimeState.PLANNING,
            "component": RuntimeComponent.EXECUTOR,
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.APPROVAL_DECISION_RECORDED,
            "state": RuntimeState.EXECUTING,
            "reason_code": "not_required",
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.APPROVAL_DECISION_RECORDED,
            "state": RuntimeState.WAITING_FOR_APPROVAL,
            "reason_code": "approval_required",
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.PAUSED,
            "state": RuntimeState.POLICY_CHECK,
            "reason_code": "approval_required",
        },
        {
            "task_id": task.task_id,
            "sequence": 0,
            "occurred_at": timestamp,
            "kind": LifecycleEventKind.FAILED,
            "state": RuntimeState.FAILED,
        },
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            LifecycleEvent.model_validate(payload)


def test_outcome_requires_exact_component_completion_evidence() -> None:
    completed = completed_outcome()
    payload = completed.model_dump(mode="python")
    payload["events"] = [
        event for event in payload["events"] if event["component"] != RuntimeComponent.PLANNER
    ]
    for sequence, event in enumerate(payload["events"]):
        event["sequence"] = sequence

    with pytest.raises(ValidationError, match="component-completion events"):
        RuntimeOutcome.model_validate(payload)


def test_failure_component_must_match_failed_stage() -> None:
    failed = RuntimeEngine().run(Task(request="unsupported"))
    payload = failed.model_dump(mode="python")
    payload["failure"]["component"] = RuntimeComponent.CONTEXT_BUILDER
    payload["events"][-1]["component"] = RuntimeComponent.CONTEXT_BUILDER

    with pytest.raises(ValidationError, match="failed component|failure component"):
        RuntimeOutcome.model_validate(payload)


def test_plan_and_results_require_completed_producer_stages() -> None:
    waiting = waiting_outcome()
    completed = completed_outcome()

    missing_plan = waiting.model_dump(mode="python")
    missing_plan["plan"] = None
    with pytest.raises(ValidationError, match="require an execution plan"):
        RuntimeOutcome.model_validate(missing_plan)

    premature_result = waiting.model_dump(mode="python")
    premature_result["results"] = completed.model_dump(mode="python")["results"]
    with pytest.raises(ValidationError, match="Incomplete Executor results"):
        RuntimeOutcome.model_validate(premature_result)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "paused_too"])
def test_automatic_approval_gate_requires_one_not_required_decision(
    mutation: str,
) -> None:
    completed = completed_outcome()
    payload = completed.model_dump(mode="python")
    payload["events"] = list(payload["events"])
    decision_index = next(
        index
        for index, event in enumerate(payload["events"])
        if event["kind"] is LifecycleEventKind.APPROVAL_DECISION_RECORDED
    )

    if mutation == "missing":
        del payload["events"][decision_index]
    else:
        extra = payload["events"][decision_index].copy()
        if mutation == "paused_too":
            extra.update(
                {
                    "kind": LifecycleEventKind.PAUSED,
                    "reason_code": "approval_required",
                }
            )
        payload["events"].insert(decision_index + 1, extra)
    for sequence, event in enumerate(payload["events"]):
        event["sequence"] = sequence

    with pytest.raises(ValidationError, match="exactly one approval decision"):
        RuntimeOutcome.model_validate(payload)


def test_waiting_history_requires_pause_and_defined_terminal_reason() -> None:
    waiting = waiting_outcome()
    without_pause = waiting.model_dump(mode="python")
    without_pause["events"] = [
        event for event in without_pause["events"] if event["kind"] != LifecycleEventKind.PAUSED
    ]
    for sequence, event in enumerate(without_pause["events"]):
        event["sequence"] = sequence

    with pytest.raises(ValidationError, match="approval decision"):
        RuntimeOutcome.model_validate(without_pause)

    rejected = RuntimeEngine().reject(waiting)
    unsupported_failure = rejected.model_dump(mode="python")
    unsupported_failure["events"][-1].update(
        {
            "kind": LifecycleEventKind.FAILED,
            "reason_code": "runtime_failure",
        }
    )
    unsupported_failure["failure"].update(
        {
            "code": "runtime_failure",
            "message": "Safe.",
        }
    )
    with pytest.raises(ValidationError, match="unsupported Phase 1 reason"):
        RuntimeOutcome.model_validate(unsupported_failure)


def test_runtime_outcome_public_enums_are_exact_and_stable() -> None:
    assert tuple(RuntimeOutcomeStatus) == (
        RuntimeOutcomeStatus.COMPLETED,
        RuntimeOutcomeStatus.FAILED,
        RuntimeOutcomeStatus.WAITING_FOR_APPROVAL,
    )
    assert tuple(LifecycleEventKind) == (
        LifecycleEventKind.STATE_ENTERED,
        LifecycleEventKind.COMPONENT_COMPLETED,
        LifecycleEventKind.APPROVAL_DECISION_RECORDED,
        LifecycleEventKind.PAUSED,
        LifecycleEventKind.REJECTED,
        LifecycleEventKind.FAILED,
    )
