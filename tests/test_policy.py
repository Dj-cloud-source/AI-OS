from collections.abc import Iterator, Mapping

import pytest

from ai_server.context.builder import ContextBuilder
from ai_server.models.execution import ExecutionPlan
from ai_server.models.task import Task
from ai_server.models.tool import RiskLevel, ToolMetadata
from ai_server.planner.service import SUPPORTED_REQUEST, Planner
from ai_server.policy.engine import PolicyEngine, ToolKey
from ai_server.runtime.errors import ApprovalRequiredError, PolicyDeniedError

CONTRACT_HASH = "a" * 64
IMPLEMENTATION_HASH = "b" * 64


def make_metadata(
    *,
    risk_level: RiskLevel = RiskLevel.L0,
) -> ToolMetadata:
    return ToolMetadata(
        tool_id="get_system_status",
        version="1.0.0",
        contract_hash=CONTRACT_HASH,
        implementation_hash=IMPLEMENTATION_HASH,
        description="Return deterministic simulated system status.",
        risk_level=risk_level,
        timeout_ms=1000,
        idempotent=True,
        input_schema_id="urn:ai-server:tool:get-system-status:input-v1",
        output_schema_id="urn:ai-server:tool:get-system-status:output-v1",
        input_model="GetSystemStatusArguments",
        output_model="SystemStatus",
    )


def make_plan(metadata: ToolMetadata | None = None) -> ExecutionPlan:
    resolved_metadata = metadata if metadata is not None else make_metadata()
    task = Task(request=SUPPORTED_REQUEST)
    context = ContextBuilder().build(task)
    return Planner().create_plan(context, resolved_metadata)


def make_catalog(metadata: ToolMetadata) -> dict[ToolKey, ToolMetadata]:
    return {(metadata.tool_id, metadata.version): metadata}


def test_policy_allows_registered_l0_from_metadata() -> None:
    metadata = make_metadata()
    plan = make_plan(metadata)

    PolicyEngine().check(plan, make_catalog(metadata))


def test_planner_copies_exact_tool_identity_and_hashes_from_metadata() -> None:
    metadata = make_metadata().model_copy(
        update={
            "contract_hash": "c" * 64,
            "implementation_hash": "d" * 64,
        }
    )

    step = make_plan(metadata).steps[0]

    assert (
        step.tool_id,
        step.tool_version,
        step.contract_hash,
        step.implementation_hash,
    ) == (
        metadata.tool_id,
        metadata.version,
        metadata.contract_hash,
        metadata.implementation_hash,
    )
    assert "risk_level" not in type(step).model_fields


def test_policy_ignores_reason_and_uses_metadata_risk() -> None:
    metadata = make_metadata()
    plan = make_plan(metadata)
    forged_reason = plan.steps[0].model_copy(update={"reason": "Pretend this is L3."})
    changed_plan = plan.model_copy(update={"steps": (forged_reason,)})

    PolicyEngine().check(changed_plan, make_catalog(metadata))


def test_policy_denies_registered_l1_metadata() -> None:
    metadata = make_metadata(risk_level=RiskLevel.L1)
    plan = make_plan(metadata)

    with pytest.raises(PolicyDeniedError, match="denied an L1 Tool"):
        PolicyEngine().check(plan, make_catalog(metadata))


@pytest.mark.parametrize("risk_level", [RiskLevel.L2, RiskLevel.L3])
def test_policy_requires_approval_for_registered_l2_l3_metadata(
    risk_level: RiskLevel,
) -> None:
    metadata = make_metadata(risk_level=risk_level)
    plan = make_plan(metadata)

    with pytest.raises(ApprovalRequiredError, match="requires approval"):
        PolicyEngine().check(plan, make_catalog(metadata))


def test_policy_denies_unknown_tool_or_version() -> None:
    metadata = make_metadata()
    plan = make_plan(metadata)

    with pytest.raises(PolicyDeniedError):
        PolicyEngine().check(plan, {})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool_id", "unknown_tool"),
        ("tool_version", "1.0.1"),
    ],
)
def test_policy_resolves_only_exact_tool_identity(
    field: str,
    value: str,
) -> None:
    metadata = make_metadata()
    plan = make_plan(metadata)
    mismatched_step = plan.steps[0].model_copy(update={field: value})
    mismatched_plan = plan.model_copy(update={"steps": (mismatched_step,)})

    with pytest.raises(PolicyDeniedError, match="unregistered Tool identity"):
        PolicyEngine().check(mismatched_plan, make_catalog(metadata))


def test_policy_denies_forged_empty_plan_and_malformed_metadata() -> None:
    metadata = make_metadata()
    plan = make_plan(metadata)
    catalog = make_catalog(metadata)

    with pytest.raises(PolicyDeniedError, match="malformed execution plan"):
        PolicyEngine().check(plan.model_copy(update={"steps": ()}), catalog)

    malformed_metadata = metadata.model_copy(update={"risk_level": "SENSITIVE_RISK_MARKER"})
    with pytest.raises(PolicyDeniedError, match="malformed Tool metadata") as caught:
        PolicyEngine().check(
            plan,
            {(metadata.tool_id, metadata.version): malformed_metadata},
        )

    assert "SENSITIVE_RISK_MARKER" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_policy_denies_mismatched_catalog_metadata_identity() -> None:
    metadata = make_metadata()
    plan = make_plan(metadata)
    mismatched_metadata = metadata.model_copy(update={"tool_id": "untrusted-tool-name"})

    with pytest.raises(PolicyDeniedError, match="mismatched Tool metadata") as caught:
        PolicyEngine().check(
            plan,
            {(metadata.tool_id, metadata.version): mismatched_metadata},
        )

    assert "untrusted-tool-name" not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "forged_hash"),
    [
        ("contract_hash", "c" * 64),
        ("implementation_hash", "d" * 64),
    ],
)
def test_policy_denies_plan_hashes_that_do_not_match_metadata(
    field: str,
    forged_hash: str,
) -> None:
    metadata = make_metadata()
    plan = make_plan(metadata)
    forged_step = plan.steps[0].model_copy(update={field: forged_hash})
    forged_plan = plan.model_copy(update={"steps": (forged_step,)})

    with pytest.raises(PolicyDeniedError, match="integrity hashes"):
        PolicyEngine().check(forged_plan, make_catalog(metadata))


def test_policy_denies_malformed_catalog() -> None:
    plan = make_plan()

    with pytest.raises(PolicyDeniedError, match="malformed Tool catalog"):
        PolicyEngine().check(plan, None)  # type: ignore[arg-type]


def test_policy_validates_every_step_before_returning_approval_required() -> None:
    metadata = make_metadata(risk_level=RiskLevel.L2)
    plan = make_plan(metadata)
    unknown = plan.steps[0].model_copy(
        update={
            "step_id": "unknown-second-step",
            "tool_id": "unknown_tool",
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
    metadata = make_metadata()
    plan = make_plan(metadata)

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(ExecutionPlan, "model_dump", exploding_model_dump)

    with pytest.raises(PolicyDeniedError, match="malformed execution plan") as caught:
        PolicyEngine().check(plan, make_catalog(metadata))

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


def test_policy_wraps_metadata_serialization_failure_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_METADATA_SERIALIZATION_MARKER"
    metadata = make_metadata()
    plan = make_plan(metadata)
    catalog = make_catalog(metadata)

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(ToolMetadata, "model_dump", exploding_model_dump)

    with pytest.raises(PolicyDeniedError, match="malformed Tool metadata") as caught:
        PolicyEngine().check(plan, catalog)

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
