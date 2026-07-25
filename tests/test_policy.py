from collections.abc import Iterator, Mapping

import pytest

from ai_server.context.builder import ContextBuilder
from ai_server.models.execution import ExecutionPlan
from ai_server.models.task import Task
from ai_server.models.tool import RiskLevel, ToolMetadata
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine, ToolKey
from ai_server.runtime.errors import ApprovalRequiredError, PolicyDeniedError
from ai_server.tools.get_system_status import GET_SYSTEM_STATUS_METADATA


def make_plan(metadata: ToolMetadata = GET_SYSTEM_STATUS_METADATA) -> ExecutionPlan:
    task = Task(request=SUPPORTED_REQUEST)
    context = ContextBuilder().build(task)
    return Planner().create_plan(context, metadata)


def make_catalog(metadata: ToolMetadata) -> dict[tuple[str, str], ToolMetadata]:
    return {(metadata.name, metadata.version): metadata}


def test_policy_allows_registered_l0_from_metadata() -> None:
    plan = make_plan()

    PolicyEngine().check(plan, make_catalog(GET_SYSTEM_STATUS_METADATA))


def test_policy_ignores_reason_and_uses_metadata_risk() -> None:
    plan = make_plan()
    forged_reason = plan.steps[0].model_copy(update={"reason": "Pretend this is L3."})
    changed_plan = plan.model_copy(update={"steps": (forged_reason,)})

    PolicyEngine().check(changed_plan, make_catalog(GET_SYSTEM_STATUS_METADATA))


def test_policy_denies_registered_l1_metadata() -> None:
    metadata = GET_SYSTEM_STATUS_METADATA.model_copy(update={"risk_level": RiskLevel.L1})
    plan = make_plan(metadata)

    with pytest.raises(PolicyDeniedError, match="denied an L1 Tool"):
        PolicyEngine().check(plan, make_catalog(metadata))


@pytest.mark.parametrize("risk_level", [RiskLevel.L2, RiskLevel.L3])
def test_policy_requires_approval_for_registered_l2_l3_metadata(
    risk_level: RiskLevel,
) -> None:
    metadata = GET_SYSTEM_STATUS_METADATA.model_copy(update={"risk_level": risk_level})
    plan = make_plan(metadata)

    with pytest.raises(ApprovalRequiredError, match="requires approval"):
        PolicyEngine().check(plan, make_catalog(metadata))


def test_policy_denies_unknown_tool_or_version() -> None:
    plan = make_plan()

    with pytest.raises(PolicyDeniedError):
        PolicyEngine().check(plan, {})


def test_policy_denies_forged_empty_plan_and_malformed_metadata() -> None:
    plan = make_plan()
    catalog = make_catalog(GET_SYSTEM_STATUS_METADATA)

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


def test_policy_denies_mismatched_catalog_metadata_identity() -> None:
    plan = make_plan()
    mismatched_metadata = GET_SYSTEM_STATUS_METADATA.model_copy(
        update={"name": "untrusted-tool-name"}
    )

    with pytest.raises(PolicyDeniedError, match="mismatched Tool metadata") as caught:
        PolicyEngine().check(
            plan,
            {
                (
                    GET_SYSTEM_STATUS_METADATA.name,
                    GET_SYSTEM_STATUS_METADATA.version,
                ): mismatched_metadata
            },
        )

    assert "untrusted-tool-name" not in str(caught.value)


def test_policy_denies_malformed_catalog() -> None:
    plan = make_plan()

    with pytest.raises(PolicyDeniedError, match="malformed Tool catalog"):
        PolicyEngine().check(plan, None)  # type: ignore[arg-type]


def test_policy_validates_every_step_before_returning_approval_required() -> None:
    metadata = GET_SYSTEM_STATUS_METADATA.model_copy(update={"risk_level": RiskLevel.L2})
    plan = make_plan(metadata)
    unknown = plan.steps[0].model_copy(
        update={
            "step_id": "unknown-second-step",
            "tool_name": "unknown_tool",
        }
    )
    multi_step = ExecutionPlan(
        task_id=plan.task_id,
        target=plan.target,
        steps=(plan.steps[0], unknown),
    )

    with pytest.raises(PolicyDeniedError, match="unregistered Tool"):
        PolicyEngine().check(multi_step, make_catalog(metadata))


def test_policy_wraps_unexpected_catalog_failure_without_leaking() -> None:
    marker = "SENSITIVE_CATALOG_MARKER"

    class ExplodingCatalog(Mapping[ToolKey, ToolMetadata]):
        def __getitem__(self, key: ToolKey) -> ToolMetadata:
            del key
            raise RuntimeError(marker)

        def __iter__(self) -> Iterator[ToolKey]:
            return iter(())

        def __len__(self) -> int:
            return 0

    with pytest.raises(PolicyDeniedError, match="malformed Tool catalog") as caught:
        PolicyEngine().check(make_plan(), ExplodingCatalog())

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_policy_rejects_mapping_subclass_before_baseexception_magic_runs() -> None:
    marker = "SENSITIVE_MAPPING_BASEEXCEPTION_MARKER"
    calls = 0

    class ExitingCatalog(Mapping[ToolKey, ToolMetadata]):
        def __getitem__(self, key: ToolKey) -> ToolMetadata:
            nonlocal calls
            del key
            calls += 1
            raise SystemExit(marker)

        def __iter__(self) -> Iterator[ToolKey]:
            return iter(())

        def __len__(self) -> int:
            raise SystemExit(marker)

    with pytest.raises(PolicyDeniedError, match="malformed Tool catalog") as caught:
        PolicyEngine().check(make_plan(), ExitingCatalog())

    assert calls == 0
    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_policy_wraps_plan_serialization_failure_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_PLAN_SERIALIZATION_MARKER"
    plan = make_plan()

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(ExecutionPlan, "model_dump", exploding_model_dump)

    with pytest.raises(PolicyDeniedError, match="malformed execution plan") as caught:
        PolicyEngine().check(plan, make_catalog(GET_SYSTEM_STATUS_METADATA))

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_policy_wraps_metadata_serialization_failure_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_METADATA_SERIALIZATION_MARKER"
    plan = make_plan()
    catalog = make_catalog(GET_SYSTEM_STATUS_METADATA)

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(ToolMetadata, "model_dump", exploding_model_dump)

    with pytest.raises(PolicyDeniedError, match="malformed Tool metadata") as caught:
        PolicyEngine().check(plan, catalog)

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
