"""Project one Tool's reviewed dependency closure from the repository uv.lock."""

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any, cast


def project_dependency_lock(
    source_path: Path,
    destination_path: Path,
    roots: tuple[str, ...],
) -> None:
    """Write a deterministic Tool dependency projection from one uv lockfile."""
    with source_path.open("rb") as source:
        lock = tomllib.load(source)
    packages = _index_packages(lock)
    selected = _dependency_closure(packages, roots)
    document = {
        "format": "uv-tool-lock-v1",
        "source_lock": {
            "format_version": _required_int(lock, "version"),
            "revision": _required_int(lock, "revision"),
        },
        "requires_python": _required_string(lock, "requires-python"),
        "roots": sorted(set(roots)),
        "packages": [_project_package(packages[name]) for name in sorted(selected)],
    }
    destination_path.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _index_packages(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_packages = lock.get("package")
    if type(raw_packages) is not list:
        raise ValueError("uv.lock does not contain a package list")
    packages: dict[str, dict[str, Any]] = {}
    for value in raw_packages:
        if type(value) is not dict:
            raise ValueError("uv.lock contains a malformed package")
        package = cast(dict[str, Any], value)
        source = package.get("source")
        name = package.get("name")
        if (
            type(name) is str
            and type(source) is dict
            and source.get("registry") == "https://pypi.org/simple"
        ):
            if name in packages:
                raise ValueError(f"uv.lock contains ambiguous versions for {name}")
            packages[name] = package
    return packages


def _dependency_closure(
    packages: dict[str, dict[str, Any]],
    roots: tuple[str, ...],
) -> set[str]:
    if not roots or len(roots) != len(set(roots)):
        raise ValueError("Tool dependency roots must be non-empty and unique")
    selected: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        try:
            package = packages[name]
        except KeyError:
            raise ValueError(f"uv.lock does not contain dependency {name}") from None
        selected.add(name)
        for dependency in _dependency_names(package):
            if dependency not in selected:
                pending.append(dependency)
    return selected


def _dependency_names(package: dict[str, Any]) -> tuple[str, ...]:
    raw_dependencies = package.get("dependencies", [])
    if type(raw_dependencies) is not list:
        raise ValueError("uv.lock contains malformed dependencies")
    names: list[str] = []
    for value in raw_dependencies:
        if type(value) is not dict or type(value.get("name")) is not str:
            raise ValueError("uv.lock contains a malformed dependency")
        names.append(cast(str, value["name"]))
    return tuple(sorted(set(names)))


def _project_package(package: dict[str, Any]) -> dict[str, object]:
    source = package.get("source")
    if type(source) is not dict or source != {"registry": "https://pypi.org/simple"}:
        raise ValueError("Tool dependencies must come from the reviewed PyPI registry")
    artifacts: list[dict[str, object]] = []
    raw_sdist = package.get("sdist")
    if raw_sdist is not None:
        artifacts.append(_project_artifact(raw_sdist))
    raw_wheels = package.get("wheels", [])
    if type(raw_wheels) is not list:
        raise ValueError("uv.lock contains a malformed wheel list")
    artifacts.extend(_project_artifact(value) for value in raw_wheels)
    artifacts.sort(key=lambda artifact: cast(str, artifact["filename"]))
    return {
        "name": _required_string(package, "name"),
        "version": _required_string(package, "version"),
        "source": {"registry": "https://pypi.org/simple"},
        "dependencies": list(_dependency_names(package)),
        "artifacts": artifacts,
    }


def _project_artifact(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("uv.lock contains a malformed distribution artifact")
    artifact = cast(dict[str, Any], value)
    url = _required_string(artifact, "url")
    return {
        "filename": url.rsplit("/", maxsplit=1)[-1],
        "url": url,
        "sha256": _required_string(artifact, "hash"),
        "size_bytes": _required_int(artifact, "size"),
    }


def _required_string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if type(value) is not str or not value:
        raise ValueError(f"uv.lock field {key} must be a non-empty string")
    return value


def _required_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if type(value) is not int or value < 1:
        raise ValueError(f"uv.lock field {key} must be a positive integer")
    return value


def main() -> None:
    """Parse arguments and generate one deterministic Tool dependency projection."""
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("roots", nargs="+")
    arguments = parser.parse_args()
    project_dependency_lock(
        arguments.source,
        arguments.destination,
        tuple(arguments.roots),
    )


if __name__ == "__main__":
    main()
