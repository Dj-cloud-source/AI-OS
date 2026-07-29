import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from importlib.resources import files
from typing import Any, cast
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, ValidationError
from referencing import Registry, Resource

from ai_server.models.tool import (
    ApprovalBinding,
    ApprovalImplication,
    ApprovalRequirement,
    RedactionRequirement,
    ReplayFixtureReference,
    RiskLevel,
    RollbackRequirement,
    RollbackStrategy,
    SideEffectKind,
    TargetReference,
    ToolCall,
    ToolContract,
    ToolError,
    ToolErrorCategory,
    ToolErrorDefinition,
    ToolMetadata,
    ToolRegistryRecord,
    ToolRegistryStatus,
    ToolResult,
    ToolSideEffects,
    ToolTargetScope,
    VerificationRequirement,
)
from ai_server.tools.protocol import (
    NORMATIVE_TOOL_SCHEMA_FILES,
    TOOL_CONTRACT_SCHEMA_ID,
    TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID,
    TOOL_REGISTRY_RECORD_SCHEMA_ID,
    TOOL_REPLAY_FIXTURE_SCHEMA_ID,
    TOOL_RESULT_SCHEMA_ID,
    TOOL_SCHEMA_DIALECT,
)

HASH = "0" * 64
OTHER_HASH = "1" * 64
REVIEWED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target: str


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str


def make_contract(
    *,
    risk_level: RiskLevel = RiskLevel.L0,
    implication: ApprovalImplication = ApprovalImplication.AUTOMATIC_EXECUTION,
    bindings: tuple[ApprovalBinding, ...] = (),
    idempotent: bool = True,
    automatic_retry: bool = False,
) -> ToolContract:
    output_schema = deepcopy(load_schemas()[TOOL_RESULT_SCHEMA_ID])
    output_schema["$id"] = "urn:ai-server:tool:get-system-status:1.0.0:result"
    return ToolContract(
        contract_schema_version="1",
        schema_dialect=TOOL_SCHEMA_DIALECT,
        tool_id="get_system_status",
        version="1.0.0",
        implementation_hash=OTHER_HASH,
        description="Return deterministic simulated system status.",
        risk_level=risk_level,
        approval=ApprovalRequirement(
            derived_from_risk_level=True,
            implication=implication,
            binds=bindings,
        ),
        side_effects=ToolSideEffects(
            mutates_remote_state=False,
            kind=SideEffectKind.READ_ONLY,
        ),
        target_scope=ToolTargetScope(
            resource_type="local_system",
            maximum_targets=1,
            selector_field="target",
            allow_dynamic_expansion=False,
        ),
        input_schema={
            "$schema": TOOL_SCHEMA_DIALECT,
            "$id": "urn:ai-server:tool:get-system-status:1.0.0:input",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target": {
                    "const": "local-mock",
                    "type": "string",
                }
            },
            "required": ["target"],
        },
        output_schema=output_schema,
        redaction=RedactionRequirement(
            profile_id="local-default",
            profile_version="1.0.0",
            input_fields=(),
            output_fields=(),
            safe_evidence_fields=("source",),
            max_retained_payload_bytes=4096,
        ),
        errors=(
            ToolErrorDefinition(
                code="invalid_arguments",
                category=ToolErrorCategory.VALIDATION,
                message="Tool arguments are invalid.",
                retryable=False,
            ),
        ),
        timeout_ms=1000,
        idempotent=idempotent,
        automatic_retry=automatic_retry,
        verification=VerificationRequirement(
            required=True,
            evidence_fields=("source",),
            tools=(),
        ),
        rollback=RollbackRequirement(
            required=False,
            available=False,
            strategy=RollbackStrategy.NOT_REQUIRED,
        ),
        replay_fixtures=(
            ReplayFixtureReference(
                fixture_id="get-system-status-success",
                path="fixtures/get-system-status-success.json",
            ),
        ),
    )


def make_target() -> TargetReference:
    return TargetReference(
        target_id="local-mock",
        resource_type="local_system",
        resource_id="local-mock",
    )


def make_success_result() -> ToolResult[Payload]:
    return ToolResult[Payload](
        invocation_id=uuid4(),
        plan_step_id="status",
        tool_id="get_system_status",
        tool_version="1.0.0",
        contract_hash=HASH,
        arguments_hash=OTHER_HASH,
        target=make_target(),
        success=True,
        data=Payload(source="mock"),
        evidence={"source": "mock"},
        error=None,
        duration_ms=0,
    )


def load_schemas() -> dict[str, dict[str, Any]]:
    package = files("ai_server.schemas.tool")
    loaded: dict[str, dict[str, Any]] = {}
    for filename in NORMATIVE_TOOL_SCHEMA_FILES:
        document = json.loads(package.joinpath(filename).read_text(encoding="utf-8"))
        loaded[document["$id"]] = document
    return loaded


def make_schema_registry(
    schemas: dict[str, dict[str, Any]],
) -> Registry[Any]:
    resources = (
        (schema_id, Resource.from_contents(schema)) for schema_id, schema in schemas.items()
    )
    return Registry().with_resources(resources)


def make_schema_samples() -> dict[str, dict[str, Any]]:
    result = make_success_result().model_dump(mode="json")
    contract = make_contract().model_dump(mode="json")
    record = ToolRegistryRecord(
        tool_id="get_system_status",
        version="1.0.0",
        contract_hash=HASH,
        implementation_hash=OTHER_HASH,
        status=ToolRegistryStatus.REGISTERED,
        reviewer="local-owner",
        reviewed_at=REVIEWED_AT,
        registered_at=REVIEWED_AT,
    ).model_dump(mode="json")
    implementation_bundle = {
        "artifact_format": "tool-implementation-bundle-v1",
        "tool_id": "get_system_status",
        "version": "1.0.0",
        "runtime_abi": "python-source-v1.requires-python-ge-3.12",
        "handler_entry_point": ("ai_server.tools.get_system_status:GetSystemStatusTool.invoke"),
        "input_model_entry_point": ("ai_server.models.system_status:GetSystemStatusArguments"),
        "output_model_entry_point": "ai_server.models.system_status:SystemStatus",
        "dependency_lock_sha256": f"sha256:{HASH}",
        "files": [
            {
                "path": "ai_server/tools/get_system_status.py",
                "size_bytes": 1,
                "sha256": f"sha256:{OTHER_HASH}",
            }
        ],
    }
    replay_fixture = {
        "fixture_schema_version": "1",
        "fixture_id": "get-system-status-success",
        "content_hash": HASH,
        "tool_id": "get_system_status",
        "version": "1.0.0",
        "provenance": "mock",
        "sequence_position": 0,
        "input": {"target": "local-mock"},
        "arguments_hash": OTHER_HASH,
        "result": result,
        "verification_result": {
            "success": True,
            "criteria": ["source is mock"],
            "evidence": {"source": "mock"},
            "failure_reason": None,
        },
        "expected_outcome": "success",
        "expected_error_code": None,
        "redaction": {
            "profile_version": "1.0.0",
            "sanitized": True,
            "removed_data_classes": [],
        },
    }
    return {
        TOOL_CONTRACT_SCHEMA_ID: contract,
        TOOL_RESULT_SCHEMA_ID: result,
        TOOL_REPLAY_FIXTURE_SCHEMA_ID: replay_fixture,
        TOOL_REGISTRY_RECORD_SCHEMA_ID: record,
        TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID: implementation_bundle,
    }


def test_tool_contract_and_projection_are_strict_frozen_models() -> None:
    contract = make_contract()
    metadata = ToolMetadata(
        tool_id=contract.tool_id,
        version=contract.version,
        contract_hash=HASH,
        implementation_hash=OTHER_HASH,
        description=contract.description,
        risk_level=contract.risk_level,
        timeout_ms=contract.timeout_ms,
        idempotent=contract.idempotent,
        input_schema_id="urn:ai-server:tool:get_system_status:1.0.0:input",
        output_schema_id="urn:ai-server:tool:get_system_status:1.0.0:output",
        input_model="GetSystemStatusArguments",
        output_model="SystemStatus",
    )

    assert ToolContract.model_validate_json(contract.model_dump_json()) == contract
    assert ToolMetadata.model_validate_json(metadata.model_dump_json()) == metadata
    with pytest.raises(ValidationError):
        metadata.timeout_ms = 2
    with pytest.raises(ValidationError):
        ToolMetadata.model_validate(
            {
                **metadata.model_dump(mode="python"),
                "timeout_ms": "1000",
            }
        )
    with pytest.raises(ValidationError):
        ToolMetadata.model_validate(
            {
                **metadata.model_dump(mode="python"),
                "risk_override": "L3",
            }
        )


def test_contract_rejects_approval_and_retry_mismatches() -> None:
    with pytest.raises(ValidationError, match="Approval implication"):
        make_contract(implication=ApprovalImplication.EXPLICIT_HUMAN_APPROVAL)
    with pytest.raises(ValidationError, match="Approval bindings"):
        make_contract(bindings=(ApprovalBinding.PLAN_HASH,))
    with pytest.raises(ValidationError, match="Automatic retry"):
        make_contract(idempotent=False, automatic_retry=True)

    l2_contract = make_contract(
        risk_level=RiskLevel.L2,
        implication=ApprovalImplication.EXPLICIT_HUMAN_APPROVAL,
        bindings=(
            ApprovalBinding.PLAN_HASH,
            ApprovalBinding.ARGUMENTS,
            ApprovalBinding.EXPIRATION,
        ),
    )
    assert l2_contract.risk_level is RiskLevel.L2


def test_contract_rejects_non_strict_schemas_and_duplicate_ids() -> None:
    valid = make_contract()
    invalid_schema = valid.model_dump(mode="python")
    invalid_schema["input_schema"] = {"type": "object", "properties": {}}
    with pytest.raises(ValidationError, match="strict object JSON Schema"):
        ToolContract.model_validate(invalid_schema)

    duplicate_errors = valid.model_dump(mode="python")
    duplicate_errors["errors"] = valid.errors * 2
    with pytest.raises(ValidationError, match="error codes must be unique"):
        ToolContract.model_validate(duplicate_errors)
    duplicate_fixtures = valid.model_dump(mode="python")
    duplicate_fixtures["replay_fixtures"] = valid.replay_fixtures * 2
    with pytest.raises(ValidationError, match="fixture IDs must be unique"):
        ToolContract.model_validate(duplicate_fixtures)


def test_registry_record_requires_utc_and_complete_registered_binding() -> None:
    with pytest.raises(ValidationError, match="require implementation hash"):
        ToolRegistryRecord(
            tool_id="get_system_status",
            version="1.0.0",
            contract_hash=HASH,
            implementation_hash=None,
            status=ToolRegistryStatus.REGISTERED,
            reviewer="local-owner",
            reviewed_at=REVIEWED_AT,
            registered_at=None,
        )
    with pytest.raises(ValidationError, match="must use UTC"):
        ToolRegistryRecord(
            tool_id="get_system_status",
            version="1.0.0",
            contract_hash=HASH,
            implementation_hash=None,
            status=ToolRegistryStatus.DESIGN_ONLY,
            reviewer="local-owner",
            reviewed_at=REVIEWED_AT.astimezone(timezone(timedelta(hours=8))),
            registered_at=None,
        )

    design_only = ToolRegistryRecord(
        tool_id="get_system_status",
        version="1.0.0",
        contract_hash=HASH,
        implementation_hash=None,
        status=ToolRegistryStatus.DESIGN_ONLY,
        reviewer="local-owner",
        reviewed_at=REVIEWED_AT,
        registered_at=None,
    )
    assert design_only.registered_at is None


def test_tool_call_is_exact_hash_bound_and_typed() -> None:
    call = ToolCall[Arguments](
        invocation_id=uuid4(),
        plan_step_id="status",
        tool_id="get_system_status",
        tool_version="1.0.0",
        contract_hash=HASH,
        implementation_hash=OTHER_HASH,
        arguments_hash=HASH,
        target=make_target(),
        arguments=Arguments(target="local-mock"),
    )

    assert ToolCall[Arguments].model_validate_json(call.model_dump_json()) == call
    invalid = call.model_dump(mode="json")
    invalid["arguments"] = {"target": 1}
    with pytest.raises(ValidationError):
        ToolCall[Arguments].model_validate(invalid)
    invalid = call.model_dump(mode="python")
    invalid["command"] = "forbidden"
    with pytest.raises(ValidationError):
        ToolCall[Arguments].model_validate(invalid)


def test_tool_result_enforces_success_data_error_exclusivity() -> None:
    success = make_success_result()
    failure = ToolResult[Payload](
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
            code="operation_failed",
            category=ToolErrorCategory.EXECUTION,
            message="Tool invocation failed safely.",
            retryable=False,
        ),
        duration_ms=1,
    )

    assert ToolResult[Payload].model_validate_json(success.model_dump_json()) == success
    assert ToolResult[Payload].model_validate_json(failure.model_dump_json()) == failure
    with pytest.raises(ValidationError, match="Successful ToolResult"):
        ToolResult[Payload](**{**success.model_dump(mode="python"), "error": failure.error})
    with pytest.raises(ValidationError, match="Failed ToolResult"):
        ToolResult[Payload](
            **{
                **failure.model_dump(mode="python"),
                "data": Payload(source="unsafe"),
            }
        )
    with pytest.raises(ValidationError, match="Failed ToolResult"):
        ToolResult[Payload](**{**failure.model_dump(mode="python"), "error": None})


def test_erased_tool_result_retains_concrete_structured_payload() -> None:
    concrete = make_success_result()
    erased = ToolResult[BaseModel](
        invocation_id=concrete.invocation_id,
        plan_step_id=concrete.plan_step_id,
        tool_id=concrete.tool_id,
        tool_version=concrete.tool_version,
        contract_hash=concrete.contract_hash,
        arguments_hash=concrete.arguments_hash,
        target=concrete.target,
        success=True,
        data=concrete.data,
        evidence=concrete.evidence,
        error=None,
        duration_ms=concrete.duration_ms,
    )

    assert erased.model_dump(mode="json")["data"] == {"source": "mock"}


def test_all_normative_schemas_are_draft_2020_12_with_stable_ids() -> None:
    schemas = load_schemas()

    assert set(schemas) == {
        TOOL_CONTRACT_SCHEMA_ID,
        TOOL_RESULT_SCHEMA_ID,
        TOOL_REPLAY_FIXTURE_SCHEMA_ID,
        TOOL_REGISTRY_RECORD_SCHEMA_ID,
        TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID,
    }
    for schema_id, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"] == schema_id
        assert schema["$schema"] == TOOL_SCHEMA_DIALECT
        assert schema["additionalProperties"] is False


def test_normative_schema_samples_validate_together() -> None:
    schemas = load_schemas()
    registry = make_schema_registry(schemas)

    for schema_id, sample in make_schema_samples().items():
        validator = Draft202012Validator(
            schemas[schema_id],
            registry=registry,
            format_checker=FormatChecker(),
        )
        assert list(validator.iter_errors(sample)) == []


@pytest.mark.parametrize(
    "schema_id",
    [
        TOOL_CONTRACT_SCHEMA_ID,
        TOOL_RESULT_SCHEMA_ID,
        TOOL_REPLAY_FIXTURE_SCHEMA_ID,
        TOOL_REGISTRY_RECORD_SCHEMA_ID,
        TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID,
    ],
)
def test_normative_schemas_reject_unknown_root_fields(schema_id: str) -> None:
    schemas = load_schemas()
    registry = make_schema_registry(schemas)
    sample = deepcopy(make_schema_samples()[schema_id])
    sample["unexpected"] = True

    validator = Draft202012Validator(
        schemas[schema_id],
        registry=registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(sample))


def test_normative_schemas_reject_unknown_nested_security_fields() -> None:
    schemas = load_schemas()
    registry = make_schema_registry(schemas)
    samples = make_schema_samples()
    invalid_samples = (
        (
            TOOL_CONTRACT_SCHEMA_ID,
            samples[TOOL_CONTRACT_SCHEMA_ID],
            ("approval",),
        ),
        (
            TOOL_RESULT_SCHEMA_ID,
            samples[TOOL_RESULT_SCHEMA_ID],
            ("target",),
        ),
        (
            TOOL_REPLAY_FIXTURE_SCHEMA_ID,
            samples[TOOL_REPLAY_FIXTURE_SCHEMA_ID],
            ("redaction",),
        ),
        (
            TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID,
            samples[TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID],
            ("files", "0"),
        ),
    )

    for schema_id, original, path in invalid_samples:
        sample = deepcopy(original)
        nested: dict[str, Any] = sample
        for part in path:
            value = nested[part]
            if isinstance(value, list):
                nested = value[int(path[-1])]
                break
            nested = value
        nested["secret"] = "must-not-cross-boundary"
        validator = Draft202012Validator(
            schemas[schema_id],
            registry=registry,
            format_checker=FormatChecker(),
        )
        assert list(validator.iter_errors(sample))


def set_contract_invalid_risk_enum(sample: dict[str, Any]) -> None:
    sample["risk_level"] = "L4"


def set_contract_invalid_nested_side_effect_enum(sample: dict[str, Any]) -> None:
    side_effects = cast(dict[str, Any], sample["side_effects"])
    side_effects["kind"] = "arbitrary_command"


def set_result_invalid_nested_error_enum(sample: dict[str, Any]) -> None:
    sample["success"] = False
    sample["data"] = None
    sample["evidence"] = {}
    sample["error"] = {
        "code": "operation_failed",
        "category": "arbitrary",
        "message": "Sanitized failure",
        "retryable": False,
    }


def set_replay_invalid_provenance_enum(sample: dict[str, Any]) -> None:
    sample["provenance"] = "production_live"


def set_registry_invalid_status_enum(sample: dict[str, Any]) -> None:
    sample["status"] = "active"


def set_implementation_invalid_artifact_format(sample: dict[str, Any]) -> None:
    sample["artifact_format"] = "tool-implementation-bundle-v2"


@pytest.mark.parametrize(
    ("schema_id", "mutation"),
    [
        (TOOL_CONTRACT_SCHEMA_ID, set_contract_invalid_risk_enum),
        (TOOL_CONTRACT_SCHEMA_ID, set_contract_invalid_nested_side_effect_enum),
        (TOOL_RESULT_SCHEMA_ID, set_result_invalid_nested_error_enum),
        (TOOL_REPLAY_FIXTURE_SCHEMA_ID, set_replay_invalid_provenance_enum),
        (TOOL_REGISTRY_RECORD_SCHEMA_ID, set_registry_invalid_status_enum),
        (
            TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID,
            set_implementation_invalid_artifact_format,
        ),
    ],
    ids=[
        "contract-risk",
        "contract-side-effect",
        "result-error-category",
        "replay-provenance",
        "registry-status",
        "implementation-format",
    ],
)
def test_normative_schemas_reject_invalid_enums_and_constants(
    schema_id: str,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    schemas = load_schemas()
    registry = make_schema_registry(schemas)
    sample = deepcopy(make_schema_samples()[schema_id])
    mutation(sample)
    validator = Draft202012Validator(
        schemas[schema_id],
        registry=registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(sample))


def set_contract_approval_cross_field_mismatch(sample: dict[str, Any]) -> None:
    sample["risk_level"] = "L2"


def set_result_success_cross_field_mismatch(sample: dict[str, Any]) -> None:
    sample["data"] = None


def set_replay_outcome_cross_field_mismatch(sample: dict[str, Any]) -> None:
    sample["expected_error_code"] = "operation_failed"


def set_registry_missing_registered_implementation(sample: dict[str, Any]) -> None:
    sample["implementation_hash"] = None


def set_registry_missing_registered_timestamp(sample: dict[str, Any]) -> None:
    sample["registered_at"] = None


def set_implementation_duplicate_file_entry(sample: dict[str, Any]) -> None:
    entries = cast(list[dict[str, Any]], sample["files"])
    entries.append(deepcopy(entries[0]))


@pytest.mark.parametrize(
    ("schema_id", "mutation"),
    [
        (TOOL_CONTRACT_SCHEMA_ID, set_contract_approval_cross_field_mismatch),
        (TOOL_RESULT_SCHEMA_ID, set_result_success_cross_field_mismatch),
        (TOOL_REPLAY_FIXTURE_SCHEMA_ID, set_replay_outcome_cross_field_mismatch),
        (
            TOOL_REGISTRY_RECORD_SCHEMA_ID,
            set_registry_missing_registered_implementation,
        ),
        (
            TOOL_REGISTRY_RECORD_SCHEMA_ID,
            set_registry_missing_registered_timestamp,
        ),
        (
            TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID,
            set_implementation_duplicate_file_entry,
        ),
    ],
    ids=[
        "contract-risk-approval",
        "result-success-data",
        "replay-outcome-error",
        "registry-implementation",
        "registry-timestamp",
        "implementation-duplicate-file",
    ],
)
def test_normative_schemas_reject_cross_field_and_uniqueness_mismatches(
    schema_id: str,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    schemas = load_schemas()
    registry = make_schema_registry(schemas)
    sample = deepcopy(make_schema_samples()[schema_id])
    mutation(sample)
    validator = Draft202012Validator(
        schemas[schema_id],
        registry=registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(sample))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewer", ""),
        ("reviewed_at", "not-a-date-time"),
        ("registered_at", "not-a-date-time"),
    ],
    ids=["reviewer", "reviewed-at", "registered-at"],
)
def test_registry_record_schema_rejects_invalid_review_evidence(
    field: str,
    value: str,
) -> None:
    schemas = load_schemas()
    registry = make_schema_registry(schemas)
    sample = deepcopy(make_schema_samples()[TOOL_REGISTRY_RECORD_SCHEMA_ID])
    sample[field] = value
    validator = Draft202012Validator(
        schemas[TOOL_REGISTRY_RECORD_SCHEMA_ID],
        registry=registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(sample))


def test_implementation_bundle_schema_rejects_path_traversal() -> None:
    schemas = load_schemas()
    registry = make_schema_registry(schemas)
    sample = deepcopy(make_schema_samples()[TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID])
    sample["files"][0]["path"] = "../outside.py"
    validator = Draft202012Validator(
        schemas[TOOL_IMPLEMENTATION_BUNDLE_SCHEMA_ID],
        registry=registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(sample))
