import ast
from pathlib import Path

SRC_ROOT = Path(__file__).parents[1] / "src" / "ai_server"

FORBIDDEN_GLOBAL_IMPORTS = {
    "aiohttp",
    "anthropic",
    "asyncssh",
    "autogen",
    "crewai",
    "docker",
    "http",
    "httpx",
    "kubernetes",
    "langchain",
    "langgraph",
    "llama_cpp",
    "ollama",
    "openai",
    "os",
    "paramiko",
    "pathlib",
    "pty",
    "requests",
    "shutil",
    "socket",
    "sqlite3",
    "subprocess",
    "urllib",
}

FORBIDDEN_CALLS = {
    "builtins.open",
    "eval",
    "exec",
    "open",
    "os.exec",
    "os.popen",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.spawn",
    "os.system",
    "shutil.rmtree",
}

FORBIDDEN_BOUNDARY_IMPORTS = {
    "context/builder.py": {"ai_server.executor", "ai_server.tools"},
    "planner/service.py": {"ai_server.executor", "ai_server.tools"},
    "policy/engine.py": {
        "ai_server.executor",
        "ai_server.planner",
        "ai_server.tools",
    },
    "verifier/service.py": {"ai_server.executor", "ai_server.tools"},
    "tools/get_system_status.py": {
        "ai_server.approval",
        "ai_server.planner",
        "ai_server.policy",
    },
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = qualified_name(node.value)
        if prefix is not None:
            return f"{prefix}.{node.attr}"
    return None


def test_source_has_no_forbidden_external_capability_imports() -> None:
    imported: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        imported.update(imported_modules(path))

    roots = {module.split(".", maxsplit=1)[0] for module in imported}
    assert roots.isdisjoint(FORBIDDEN_GLOBAL_IMPORTS)
    assert "sqlalchemy" not in roots


def test_source_has_no_forbidden_shell_or_file_mutation_calls() -> None:
    calls: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls.update(
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            if (name := qualified_name(node.func)) is not None
        )

    assert calls.isdisjoint(FORBIDDEN_CALLS)


def test_architecture_boundaries_have_no_forbidden_imports() -> None:
    for relative_path, forbidden_prefixes in FORBIDDEN_BOUNDARY_IMPORTS.items():
        imports = imported_modules(SRC_ROOT / relative_path)
        assert not any(
            module == prefix or module.startswith(f"{prefix}.")
            for module in imports
            for prefix in forbidden_prefixes
        )


def test_executor_is_only_production_caller_of_mock_tool() -> None:
    capability_importers: list[Path] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "ai_server.tools.get_system_status"
            and any(alias.name == "get_system_status" for alias in node.names)
            for node in ast.walk(tree)
        ):
            capability_importers.append(path.relative_to(SRC_ROOT))

    assert capability_importers == [Path("executor/service.py")]
