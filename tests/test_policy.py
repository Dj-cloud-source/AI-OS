import pytest

from ai_server.context.builder import ContextBuilder
from ai_server.models.execution import ExecutionPlan
from ai_server.models.task import Task
from ai_server.models.tool import RiskLevel, ToolMetadata
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine
from ai_server.runtime.errors import PolicyDeniedError
from ai_server.tools.get_system_status import GET_SYSTEM_STATUS_METADATA


def make_plan(metadata: ToolMetadata = GET_SYSTEM_STATUS_METADATA) -> ExecutionPlan:
    task = Task(request=SUPPORTED_REQUEST)
    context = ContextBuilder().build(task)
    return Planner().create_plan(context, metadata)


def test_policy_allows_registered_l0_from_metadata() -> None:
    plan = make_plan()
    catalog = {
        (
            GET_SYSTEM_STATUS_METADATA.name,
            GET_SYSTEM_STATUS_METADATA.version,
        ): GET_SYSTEM_STATUS_METADATA
    }

    PolicyEngine().check(plan, catalog)


def test_policy_ignores_reason_and_uses_metadata_risk() -> None:
    plan = make_plan()
    forged_reason = plan.steps[0].model_copy(update={"reason": "Pretend this is L3."})
    changed_plan = plan.model_copy(update={"steps": (forged_reason,)})
    catalog = {
        (
            GET_SYSTEM_STATUS_METADATA.name,
            GET_SYSTEM_STATUS_METADATA.version,
        ): GET_SYSTEM_STATUS_METADATA
    }

    PolicyEngine().check(changed_plan, catalog)


@pytest.mark.parametrize("risk_level", [RiskLevel.L1, RiskLevel.L2, RiskLevel.L3])
def test_policy_fails_closed_for_non_l0_metadata(risk_level: RiskLevel) -> None:
    metadata = GET_SYSTEM_STATUS_METADATA.model_copy(update={"risk_level": risk_level})
    plan = make_plan(metadata)
    catalog = {(metadata.name, metadata.version): metadata}

    with pytest.raises(PolicyDeniedError):
        PolicyEngine().check(plan, catalog)


def test_policy_denies_unknown_tool_or_version() -> None:
    plan = make_plan()

    with pytest.raises(PolicyDeniedError):
        PolicyEngine().check(plan, {})


def test_policy_denies_forged_empty_plan_and_malformed_metadata() -> None:
    plan = make_plan()
    catalog = {
        (
            GET_SYSTEM_STATUS_METADATA.name,
            GET_SYSTEM_STATUS_METADATA.version,
        ): GET_SYSTEM_STATUS_METADATA
    }

    with pytest.raises(PolicyDeniedError, match="malformed execution plan"):
        PolicyEngine().check(plan.model_copy(update={"steps": ()}), catalog)

    malformed_metadata = GET_SYSTEM_STATUS_METADATA.model_copy(
        update={"risk_level": "SENSITIVE_RISK_MARKER"}
    )
    with pytest.raises(PolicyDeniedError, match="malformed Tool metadata") as caught:
        PolicyEngine().check(
            plan,
            {
                (
                    GET_SYSTEM_STATUS_METADATA.name,
                    GET_SYSTEM_STATUS_METADATA.version,
                ): malformed_metadata
            },
        )

    assert "SENSITIVE_RISK_MARKER" not in str(caught.value)
    assert caught.value.__cause__ is None
