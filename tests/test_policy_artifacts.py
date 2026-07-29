import json
import shutil
from collections.abc import Callable, Mapping
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue

import ai_server.policy.artifact_loader as artifact_loader
from ai_server.models.policy import PolicyApprovalRequirement
from ai_server.models.tool import ToolMetadata
from ai_server.policy.artifact_loader import (
    DEFAULT_POLICY_ID,
    DEFAULT_POLICY_VERSION,
    ValidatedPolicyArtifacts,
    load_policy_artifacts,
)
from ai_server.policy.errors import PolicyConfigurationError
from ai_server.tools.bootstrap import build_default_registry
from ai_server.tools.hashing import canonical_json_sha256

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_AI_SERVER = PROJECT_ROOT / "src" / "ai_server"
SOURCE_ARTIFACT_PACKAGE = SOURCE_AI_SERVER / "policy_artifacts"
SOURCE_SCHEMA_PACKAGE = SOURCE_AI_SERVER / "schemas" / "policy"


def authoritative_metadata() -> Mapping[tuple[str, str], ToolMetadata]:
    return build_default_registry().metadata_snapshot()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert type(value) is dict
    return cast(dict[str, Any], value)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        f"{json.dumps(value, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )


def make_policy_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    artifact_package = tmp_path / "policy_artifacts"
    schema_package = tmp_path / "schemas" / "policy"
    shutil.copytree(SOURCE_ARTIFACT_PACKAGE, artifact_package)
    shutil.copytree(SOURCE_SCHEMA_PACKAGE, schema_package)
    real_files = cast(Callable[[str], Traversable], vars(artifact_loader)["files"])

    def sandboxed_files(package: str) -> Traversable:
        if package == "ai_server.policy_artifacts":
            return artifact_package
        if package == "ai_server.schemas.policy":
            return schema_package
        return real_files(package)

    monkeypatch.setattr(artifact_loader, "files", sandboxed_files)
    return artifact_package / DEFAULT_POLICY_ID / DEFAULT_POLICY_VERSION


def rebind_review_hash(artifact_root: Path) -> None:
    profile = read_json(artifact_root / "profile.json")
    review_path = artifact_root / "review-record.json"
    review = read_json(review_path)
    review["content_hash"] = canonical_json_sha256(cast(JsonValue, profile))
    write_json(review_path, review)


def load_default() -> ValidatedPolicyArtifacts:
    return load_policy_artifacts(authoritative_metadata())


def test_load_policy_artifacts_accepts_reviewed_default_profile() -> None:
    artifacts = load_default()
    rule = artifacts.profile.rules[0]

    assert artifacts.profile.policy_id == DEFAULT_POLICY_ID
    assert artifacts.profile.version == DEFAULT_POLICY_VERSION
    assert artifacts.review_record.content_hash == artifacts.profile_hash
    assert artifacts.review_record.status == "active"
    assert rule.operator_id == "local-user"
    assert rule.target.target_id == "local-mock"
    assert rule.target.resource_type == "local_system"
    assert rule.target.resource_id == "local-mock"
    assert rule.tool_id == "get_system_status"
    assert rule.tool_version == "1.0.0"
    assert rule.minimum_approval is PolicyApprovalRequirement.NOT_REQUIRED
    metadata = authoritative_metadata()[(rule.tool_id, rule.tool_version)]
    assert rule.contract_hash == metadata.contract_hash
    assert rule.implementation_hash == metadata.implementation_hash


def test_profile_formatting_and_order_do_not_change_rfc8785_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    baseline = load_default()
    profile_path = artifact_root / "profile.json"
    profile = read_json(profile_path)
    reversed_profile = {key: profile[key] for key in reversed(tuple(profile))}
    profile_path.write_text(
        json.dumps(reversed_profile, separators=(",", ":")),
        encoding="utf-8",
    )

    reordered = load_default()

    assert reordered.profile_hash == baseline.profile_hash
    assert reordered.profile == baseline.profile


@pytest.mark.parametrize(
    "relative_path",
    [
        "profile.json",
        "review-record.json",
    ],
)
def test_missing_policy_artifact_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    (artifact_root / relative_path).unlink()

    with pytest.raises(PolicyConfigurationError):
        load_default()


def test_profile_content_drift_invalidates_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    path = artifact_root / "profile.json"
    profile = read_json(path)
    profile["policy_id"] = "drifted-policy"
    write_json(path, profile)

    with pytest.raises(PolicyConfigurationError):
        load_default()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content_hash", "0" * 64),
        ("status", "inactive"),
        ("policy_id", "other-policy"),
        ("version", "1.0.1"),
        ("reviewer", "model"),
    ],
)
def test_review_record_binding_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    path = artifact_root / "review-record.json"
    review = read_json(path)
    review[field] = value
    write_json(path, review)

    with pytest.raises(PolicyConfigurationError):
        load_default()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed_at", "2026-07-29T08:00:00+08:00"),
        ("activated_at", "2026-07-28T23:59:59Z"),
    ],
)
def test_review_record_requires_ordered_utc_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    path = artifact_root / "review-record.json"
    review = read_json(path)
    review[field] = value
    write_json(path, review)

    with pytest.raises(PolicyConfigurationError):
        load_default()


def test_unknown_fields_fail_schema_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    path = artifact_root / "profile.json"
    profile = read_json(path)
    profile["dynamic_override"] = True
    write_json(path, profile)
    rebind_review_hash(artifact_root)

    with pytest.raises(PolicyConfigurationError):
        load_default()


def test_duplicate_json_object_keys_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    path = artifact_root / "profile.json"
    original = path.read_text(encoding="utf-8").lstrip()
    path.write_text(
        '{"profile_schema_version":"1",' + original[1:],
        encoding="utf-8",
    )

    with pytest.raises(PolicyConfigurationError):
        load_default()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rule: rule.__setitem__("tool_id", "unknown_tool"),
        lambda rule: rule.__setitem__("contract_hash", "0" * 64),
        lambda rule: rule.__setitem__("implementation_hash", "0" * 64),
        lambda rule: cast(dict[str, Any], rule["target"]).__setitem__(
            "resource_type", "other_system"
        ),
        lambda rule: cast(dict[str, Any], rule["target"]).__setitem__(
            "resource_id", "other-resource"
        ),
    ],
    ids=[
        "unknown-tool",
        "contract-hash",
        "implementation-hash",
        "target-scope",
        "target-identity",
    ],
)
def test_capability_must_match_authoritative_tool_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    profile_path = artifact_root / "profile.json"
    profile = read_json(profile_path)
    rules = cast(list[dict[str, Any]], profile["rules"])
    mutation(rules[0])
    write_json(profile_path, profile)
    rebind_review_hash(artifact_root)

    with pytest.raises(PolicyConfigurationError):
        load_default()


@pytest.mark.parametrize(
    ("policy_id", "version"),
    [
        ("../local-default", DEFAULT_POLICY_VERSION),
        (DEFAULT_POLICY_ID, "../1.0.0"),
        ("LOCAL", DEFAULT_POLICY_VERSION),
    ],
)
def test_artifact_identity_cannot_escape_package_scope(
    policy_id: str,
    version: str,
) -> None:
    with pytest.raises(PolicyConfigurationError):
        load_policy_artifacts(
            authoritative_metadata(),
            policy_id=policy_id,
            version=version,
        )


def test_duplicate_exact_capability_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = make_policy_sandbox(tmp_path, monkeypatch)
    profile_path = artifact_root / "profile.json"
    profile = read_json(profile_path)
    rules = cast(list[dict[str, Any]], profile["rules"])
    duplicate = dict(rules[0])
    duplicate["rule_id"] = "duplicate-rule-id"
    rules.append(duplicate)
    write_json(profile_path, profile)
    rebind_review_hash(artifact_root)

    with pytest.raises(PolicyConfigurationError):
        load_default()


def test_non_mapping_registry_input_fails_with_public_configuration_error() -> None:
    with pytest.raises(PolicyConfigurationError):
        load_policy_artifacts(cast(Any, ()))


def test_multi_target_metadata_cannot_authorize_policy_capability() -> None:
    metadata = dict(authoritative_metadata())
    key = ("get_system_status", "1.0.0")
    original = metadata[key]
    metadata[key] = original.model_copy(
        update={"target_scope": original.target_scope.model_copy(update={"maximum_targets": 2})}
    )

    with pytest.raises(PolicyConfigurationError):
        load_policy_artifacts(metadata)
