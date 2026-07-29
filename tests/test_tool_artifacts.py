import json
import shutil
import tomllib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue

import ai_server.tools.artifact_loader as artifact_loader
from ai_server.tools.artifact_loader import (
    DEPENDENCY_LOCK_FORMAT,
    ToolArtifactValidationError,
    ValidatedToolArtifacts,
    load_tool_artifacts,
)
from ai_server.tools.hashing import canonical_json_sha256

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_AI_SERVER = PROJECT_ROOT / "src" / "ai_server"
SOURCE_ARTIFACT_ROOT = SOURCE_AI_SERVER / "tool_artifacts" / "get_system_status" / "1.0.0"
TOOL_ID = "get_system_status"
TOOL_VERSION = "1.0.0"
HANDLER_ENTRY_POINT = "ai_server.tools.get_system_status:GetSystemStatusTool.invoke"
INPUT_MODEL_ENTRY_POINT = "ai_server.models.system_status:GetSystemStatusArguments"
OUTPUT_MODEL_ENTRY_POINT = "ai_server.models.system_status:SystemStatus"


@dataclass(frozen=True, slots=True)
class ArtifactSandbox:
    ai_server_root: Path
    artifact_root: Path


def make_artifact_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ArtifactSandbox:
    ai_server_root = tmp_path / "ai_server"
    artifact_package = ai_server_root / "tool_artifacts"
    shutil.copytree(
        SOURCE_AI_SERVER / "tool_artifacts",
        artifact_package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for relative_path in (
        Path("models/system_status.py"),
        Path("tools/get_system_status.py"),
    ):
        destination = ai_server_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE_AI_SERVER / relative_path, destination)

    real_files = cast(
        Callable[[str], Traversable],
        vars(artifact_loader)["files"],
    )

    def sandboxed_files(package: str) -> Traversable:
        if package == "ai_server":
            return ai_server_root
        if package == "ai_server.tool_artifacts":
            return artifact_package
        return real_files(package)

    monkeypatch.setattr(artifact_loader, "files", sandboxed_files)
    return ArtifactSandbox(
        ai_server_root=ai_server_root,
        artifact_root=artifact_package / TOOL_ID / TOOL_VERSION,
    )


def load_sandboxed_artifacts() -> ValidatedToolArtifacts:
    return load_tool_artifacts(
        TOOL_ID,
        TOOL_VERSION,
        handler_entry_point=HANDLER_ENTRY_POINT,
        input_model_entry_point=INPUT_MODEL_ENTRY_POINT,
        output_model_entry_point=OUTPUT_MODEL_ENTRY_POINT,
    )


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert type(value) is dict
    return cast(dict[str, Any], value)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        f"{json.dumps(value, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )


def rewrite_fixture(
    path: Path,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    fixture = read_json(path)
    mutation(fixture)
    set_fixture_content_hash(fixture)
    write_json(path, fixture)


def set_fixture_content_hash(fixture: dict[str, Any]) -> None:
    hash_input = dict(fixture)
    hash_input.pop("content_hash", None)
    fixture["content_hash"] = canonical_json_sha256(cast(JsonValue, hash_input))


def rewrite_dependency_lock_and_manifest(
    sandbox: ArtifactSandbox,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    lock_path = sandbox.artifact_root / "dependency-lock.json"
    lock = read_json(lock_path)
    mutation(lock)
    lock_bytes = (
        json.dumps(
            lock,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    lock_path.write_bytes(lock_bytes)

    manifest_path = sandbox.artifact_root / "implementation-bundle.json"
    manifest = read_json(manifest_path)
    digest = f"sha256:{sha256(lock_bytes).hexdigest()}"
    manifest["dependency_lock_sha256"] = digest
    files = cast(list[dict[str, Any]], manifest["files"])
    lock_entry = next(
        entry for entry in files if cast(str, entry["path"]).endswith("/dependency-lock.json")
    )
    lock_entry["size_bytes"] = len(lock_bytes)
    lock_entry["sha256"] = digest
    write_json(manifest_path, manifest)


def test_load_tool_artifacts_accepts_the_real_reviewed_bundle() -> None:
    artifacts = load_sandboxed_artifacts()

    assert artifacts.contract.tool_id == TOOL_ID
    assert artifacts.contract.version == TOOL_VERSION
    assert artifacts.contract_hash == artifacts.record.contract_hash
    assert artifacts.implementation_hash == artifacts.record.implementation_hash
    assert artifacts.handler_entry_point == HANDLER_ENTRY_POINT
    assert artifacts.input_model_entry_point == INPUT_MODEL_ENTRY_POINT
    assert artifacts.output_model_entry_point == OUTPUT_MODEL_ENTRY_POINT
    assert artifacts.fixture_ids == ("get-system-status-success",)


def test_contract_formatting_and_key_order_do_not_change_canonical_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    baseline = load_sandboxed_artifacts()
    contract_path = sandbox.artifact_root / "contract.json"
    contract = read_json(contract_path)
    reversed_contract = {key: contract[key] for key in reversed(tuple(contract))}
    contract_path.write_text(
        json.dumps(
            reversed_contract,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    reordered = load_sandboxed_artifacts()

    assert reordered.contract_hash == baseline.contract_hash
    assert reordered.contract == baseline.contract


@pytest.mark.parametrize(
    "relative_path",
    [
        "contract.json",
        "registry-record.json",
        "implementation-bundle.json",
        "dependency-lock.json",
        "fixtures/success.mock.json",
    ],
)
def test_missing_required_artifact_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    (sandbox.artifact_root / relative_path).unlink()

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def test_contract_content_drift_fails_record_and_fixture_hash_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "contract.json"
    contract = read_json(path)
    contract["description"] = "Changed but otherwise schema-valid description."
    write_json(path, contract)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


@pytest.mark.parametrize(
    "field",
    [
        "contract_hash",
        "implementation_hash",
    ],
)
def test_registry_record_hash_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "registry-record.json"
    record = read_json(path)
    record[field] = "0" * 64
    write_json(path, record)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "disabled"),
        ("reviewer", "different-reviewer"),
    ],
    ids=["status", "reviewer"],
)
def test_registry_status_or_reviewer_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "registry-record.json"
    record = read_json(path)
    record[field] = value
    write_json(path, record)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_abi", "python-source-v2.requires-python-ge-3.12"),
        (
            "handler_entry_point",
            "ai_server.tools.get_system_status:GetSystemStatusTool.other_invoke",
        ),
        (
            "input_model_entry_point",
            "ai_server.models.system_status:OtherArguments",
        ),
        (
            "output_model_entry_point",
            "ai_server.models.system_status:OtherStatus",
        ),
    ],
    ids=["abi", "handler", "input-model", "output-model"],
)
def test_manifest_abi_or_entry_point_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "implementation-bundle.json"
    manifest = read_json(path)
    manifest[field] = value
    write_json(path, manifest)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def test_reviewed_implementation_file_content_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    implementation = sandbox.ai_server_root / "tools" / "get_system_status.py"
    implementation.write_text(
        f"{implementation.read_text(encoding='utf-8')}\n# drift\n",
        encoding="utf-8",
    )

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def test_reviewed_implementation_manifest_hash_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "implementation-bundle.json"
    manifest = read_json(path)
    files = cast(list[dict[str, Any]], manifest["files"])
    files[0]["sha256"] = f"sha256:{'0' * 64}"
    write_json(path, manifest)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def remove_input_model_from_manifest(manifest: dict[str, Any]) -> None:
    files = cast(list[dict[str, Any]], manifest["files"])
    manifest["files"] = [
        entry for entry in files if entry["path"] != "ai_server/models/system_status.py"
    ]


def duplicate_manifest_path(manifest: dict[str, Any]) -> None:
    files = cast(list[dict[str, Any]], manifest["files"])
    files.append(dict(files[0]))


def set_manifest_path_traversal(manifest: dict[str, Any]) -> None:
    files = cast(list[dict[str, Any]], manifest["files"])
    files[0]["path"] = "../outside.py"


@pytest.mark.parametrize(
    "mutation",
    [
        remove_input_model_from_manifest,
        duplicate_manifest_path,
        set_manifest_path_traversal,
    ],
    ids=["undeclared-entrypoint", "duplicate-path", "path-traversal"],
)
def test_manifest_path_boundary_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "implementation-bundle.json"
    manifest = read_json(path)
    mutation(manifest)
    write_json(path, manifest)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def test_manifest_rejects_symlinked_reviewed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    implementation = sandbox.ai_server_root / "tools" / "get_system_status.py"
    target = sandbox.ai_server_root / "symlink-target.py"
    target.write_bytes(implementation.read_bytes())
    implementation.unlink()
    try:
        implementation.symlink_to(target)
    except OSError:
        pytest.skip("The test environment does not permit local symlinks")

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def test_dependency_lock_byte_hash_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "dependency-lock.json"
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def set_invalid_dependency_lock_format(lock: dict[str, Any]) -> None:
    lock["format"] = "uv-tool-lock-v2"


def remove_required_dependency_from_closure(lock: dict[str, Any]) -> None:
    packages = cast(list[dict[str, Any]], lock["packages"])
    lock["packages"] = [package for package in packages if package["name"] != "typing-inspection"]


@pytest.mark.parametrize(
    "mutation",
    [
        set_invalid_dependency_lock_format,
        remove_required_dependency_from_closure,
    ],
    ids=["invalid-content", "incomplete-closure"],
)
def test_dependency_lock_content_or_closure_drift_fails_closed_after_rebinding_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    rewrite_dependency_lock_and_manifest(sandbox, mutation)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def test_duplicate_fixture_reference_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "contract.json"
    contract = read_json(path)
    fixtures = cast(list[dict[str, Any]], contract["replay_fixtures"])
    fixtures.append(dict(fixtures[0]))
    write_json(path, contract)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def test_duplicate_replay_sequence_position_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    contract_path = sandbox.artifact_root / "contract.json"
    contract = read_json(contract_path)
    references = cast(list[dict[str, Any]], contract["replay_fixtures"])
    references.append(
        {
            "fixture_id": "get-system-status-second",
            "path": "fixtures/second.mock.json",
        }
    )
    contract_hash = canonical_json_sha256(cast(JsonValue, contract))
    write_json(contract_path, contract)

    record_path = sandbox.artifact_root / "registry-record.json"
    record = read_json(record_path)
    record["contract_hash"] = contract_hash
    write_json(record_path, record)

    fixture_path = sandbox.artifact_root / "fixtures" / "success.mock.json"
    fixture = read_json(fixture_path)
    result = cast(dict[str, Any], fixture["result"])
    result["contract_hash"] = contract_hash
    set_fixture_content_hash(fixture)
    write_json(fixture_path, fixture)

    second_fixture = deepcopy(fixture)
    second_fixture["fixture_id"] = "get-system-status-second"
    set_fixture_content_hash(second_fixture)
    write_json(sandbox.artifact_root / "fixtures" / "second.mock.json", second_fixture)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def test_replay_content_hash_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    path = sandbox.artifact_root / "fixtures" / "success.mock.json"
    fixture = read_json(path)
    verification = cast(dict[str, Any], fixture["verification_result"])
    verification["criteria"] = ["Changed without updating content hash."]
    write_json(path, fixture)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def set_replay_secret(fixture: dict[str, Any]) -> None:
    result = cast(dict[str, Any], fixture["result"])
    data = cast(dict[str, Any], result["data"])
    services = cast(list[dict[str, Any]], data["services"])
    services[0]["name"] = "-----BEGIN PRIVATE KEY-----"


def set_replay_executable_marker(fixture: dict[str, Any]) -> None:
    verification = cast(dict[str, Any], fixture["verification_result"])
    verification["criteria"] = ["bash -c forbidden"]


def set_replay_output_drift(fixture: dict[str, Any]) -> None:
    result = cast(dict[str, Any], fixture["result"])
    data = cast(dict[str, Any], result["data"])
    data["source"] = "not-mock"


def set_replay_target_drift(fixture: dict[str, Any]) -> None:
    result = cast(dict[str, Any], fixture["result"])
    target = cast(dict[str, Any], result["target"])
    target["resource_id"] = "other-mock"


def set_replay_fixture_identity_drift(fixture: dict[str, Any]) -> None:
    fixture["fixture_id"] = "other-fixture"


def set_replay_version_drift(fixture: dict[str, Any]) -> None:
    fixture["version"] = "1.0.1"


def set_replay_arguments_hash_drift(fixture: dict[str, Any]) -> None:
    fixture["arguments_hash"] = "0" * 64
    result = cast(dict[str, Any], fixture["result"])
    result["arguments_hash"] = "0" * 64


def set_replay_invalid_sequence(fixture: dict[str, Any]) -> None:
    fixture["sequence_position"] = -1


def set_replay_expected_outcome_mismatch(fixture: dict[str, Any]) -> None:
    fixture["expected_outcome"] = "failure"
    fixture["expected_error_code"] = "tool_execution_failed"


def set_replay_expected_error_mismatch(fixture: dict[str, Any]) -> None:
    result = cast(dict[str, Any], fixture["result"])
    result["success"] = False
    result["data"] = None
    result["evidence"] = {}
    result["error"] = {
        "code": "tool_execution_failed",
        "category": "execution",
        "message": "Tool execution failed safely",
        "retryable": False,
    }
    fixture["expected_outcome"] = "failure"
    fixture["expected_error_code"] = "tool_timeout"


def set_replay_undeclared_error(fixture: dict[str, Any]) -> None:
    result = cast(dict[str, Any], fixture["result"])
    result["success"] = False
    result["data"] = None
    result["evidence"] = {}
    result["error"] = {
        "code": "undeclared_error",
        "category": "execution",
        "message": "Sanitized undeclared error",
        "retryable": False,
    }
    fixture["expected_outcome"] = "failure"
    fixture["expected_error_code"] = "undeclared_error"


def set_replay_redaction_profile_drift(fixture: dict[str, Any]) -> None:
    redaction = cast(dict[str, Any], fixture["redaction"])
    redaction["profile_version"] = "1.0.1"


def set_replay_invalid_redaction_report(fixture: dict[str, Any]) -> None:
    redaction = cast(dict[str, Any], fixture["redaction"])
    redaction["sanitized"] = False


def set_replay_failed_verification(fixture: dict[str, Any]) -> None:
    verification = cast(dict[str, Any], fixture["verification_result"])
    verification["success"] = False
    verification["failure_reason"] = "Required Mock evidence did not verify."


@pytest.mark.parametrize(
    "mutation",
    [
        set_replay_secret,
        set_replay_executable_marker,
        set_replay_output_drift,
        set_replay_target_drift,
    ],
    ids=[
        "secret",
        "executable",
        "output",
        "target",
    ],
)
def test_replay_unsafe_or_unbound_content_fails_closed_after_rehash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    fixture = sandbox.artifact_root / "fixtures" / "success.mock.json"
    rewrite_fixture(fixture, mutation)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


@pytest.mark.parametrize(
    "mutation",
    [
        set_replay_fixture_identity_drift,
        set_replay_version_drift,
        set_replay_arguments_hash_drift,
        set_replay_invalid_sequence,
        set_replay_expected_outcome_mismatch,
        set_replay_expected_error_mismatch,
        set_replay_undeclared_error,
        set_replay_redaction_profile_drift,
        set_replay_invalid_redaction_report,
        set_replay_failed_verification,
    ],
    ids=[
        "fixture-identity",
        "version",
        "arguments-hash",
        "sequence",
        "expected-outcome",
        "expected-error",
        "undeclared-error",
        "redaction-profile",
        "redaction-report",
        "verification",
    ],
)
def test_replay_binding_and_report_drift_fails_closed_after_rehash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    sandbox = make_artifact_sandbox(tmp_path, monkeypatch)
    fixture = sandbox.artifact_root / "fixtures" / "success.mock.json"
    rewrite_fixture(fixture, mutation)

    with pytest.raises(ToolArtifactValidationError):
        load_sandboxed_artifacts()


def artifact_projection(artifact: dict[str, Any]) -> dict[str, Any]:
    url = cast(str, artifact["url"])
    return {
        "filename": url.rsplit("/", maxsplit=1)[-1],
        "url": url,
        "sha256": artifact["hash"],
        "size_bytes": artifact["size"],
    }


def dependency_names(package: dict[str, Any]) -> list[str]:
    dependencies = cast(list[dict[str, Any]], package.get("dependencies", []))
    return sorted({cast(str, dependency["name"]) for dependency in dependencies})


def pydantic_closure(
    packages_by_name: dict[str, dict[str, Any]],
) -> set[str]:
    closure: set[str] = set()
    pending = ["pydantic"]
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        pending.extend(dependency_names(packages_by_name[name]))
    return closure


def package_projection(package: dict[str, Any]) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    sdist = package.get("sdist")
    if type(sdist) is dict:
        artifacts.append(artifact_projection(cast(dict[str, Any], sdist)))
    artifacts.extend(
        artifact_projection(artifact)
        for artifact in cast(list[dict[str, Any]], package.get("wheels", []))
    )
    return {
        "name": package["name"],
        "version": package["version"],
        "source": package["source"],
        "dependencies": dependency_names(package),
        "artifacts": sorted(artifacts, key=lambda item: cast(str, item["filename"])),
    }


def test_dependency_lock_is_exact_sorted_pydantic_closure_projection() -> None:
    uv_lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    uv_packages = cast(list[dict[str, Any]], uv_lock["package"])
    packages_by_name = {
        cast(str, package["name"]): package
        for package in uv_packages
        if package.get("source") == {"registry": "https://pypi.org/simple"}
    }
    closure = pydantic_closure(packages_by_name)
    expected_packages = sorted(
        (package_projection(packages_by_name[name]) for name in closure),
        key=lambda package: (
            cast(str, package["name"]),
            cast(str, package["version"]),
        ),
    )
    expected = {
        "format": DEPENDENCY_LOCK_FORMAT,
        "source_lock": {
            "format_version": uv_lock["version"],
            "revision": uv_lock["revision"],
        },
        "requires_python": uv_lock["requires-python"],
        "roots": ["pydantic"],
        "packages": expected_packages,
    }
    reviewed = read_json(SOURCE_ARTIFACT_ROOT / "dependency-lock.json")

    assert closure == {
        "annotated-types",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
        "typing-inspection",
    }
    assert reviewed == expected
    assert reviewed["roots"] == sorted(set(reviewed["roots"]))
    package_identities = [
        (package["name"], package["version"])
        for package in cast(list[dict[str, Any]], reviewed["packages"])
    ]
    assert package_identities == sorted(set(package_identities))
    for package in cast(list[dict[str, Any]], reviewed["packages"]):
        assert package["dependencies"] == sorted(set(package["dependencies"]))
        filenames = [
            artifact["filename"] for artifact in cast(list[dict[str, Any]], package["artifacts"])
        ]
        assert filenames == sorted(set(filenames))
