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

FORBIDDEN_PACKAGE_IMPORTS = {
    "context": {"ai_server.executor", "ai_server.tools"},
    "executor": {
        "ai_server.llm",
        "ai_server.model_adapter",
        "ai_server.model_adapters",
        "ai_server.planner",
    },
    "memory": {"ai_server.approval", "ai_server.policy"},
    "planner": {"ai_server.executor", "ai_server.tools"},
    "policy": {
        "ai_server.executor",
        "ai_server.planner",
        "ai_server.tools",
    },
    "tools": {
        "ai_server.approval",
        "ai_server.planner",
        "ai_server.policy",
    },
    "verifier": {
        "ai_server.executor",
        "ai_server.tools.bootstrap",
        "ai_server.tools.gateway",
        "ai_server.tools.get_system_status",
        "ai_server.tools.registry",
    },
}

POLICY_MODEL_ADAPTER_PREFIXES = {
    "ai_server.llm",
    "ai_server.model_adapter",
    "ai_server.model_adapters",
    "ai_server.models.adapter",
    "ai_server.models.adapters",
    "ai_server.models.llm",
    "ai_server.models.local_model",
}

CONCRETE_CAPABILITY_MODULE = "ai_server.tools.get_system_status"
CONCRETE_CAPABILITY_NAME = "GetSystemStatusTool"
CONCRETE_CAPABILITY_OWNER = Path("tools/bootstrap.py")
TOOL_GATEWAY_INVOKE_OWNER = Path("executor/service.py")
TASK_STATE_FIELDS = frozenset({"state", "state_history"})
TASK_STATE_OWNER = Path("runtime/engine.py")


def source_module(path: Path) -> str:
    relative = path.relative_to(SRC_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("ai_server", *parts))


def resolved_from_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    module_parts = source_module(path).split(".")
    if path.name != "__init__.py":
        module_parts.pop()
    keep = len(module_parts) - node.level + 1
    if keep < 1:
        return node.module or ""

    resolved_parts = module_parts[:keep]
    if node.module is not None:
        resolved_parts.extend(node.module.split("."))
    return ".".join(resolved_parts)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolved_from_module(path, node)
            if module:
                modules.add(module)
                modules.update(
                    f"{module}.{alias.name}" for alias in node.names if alias.name != "*"
                )
    return modules


def qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = qualified_name(node.value)
        if prefix is not None:
            return f"{prefix}.{node.attr}"
    return None


def has_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(f"{prefix}.")


def imports_concrete_capability(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == CONCRETE_CAPABILITY_MODULE for alias in node.names):
                return True
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        module = resolved_from_module(path, node)
        imported_names = {alias.name for alias in node.names}
        if module == CONCRETE_CAPABILITY_MODULE and (
            CONCRETE_CAPABILITY_NAME in imported_names or "*" in imported_names
        ):
            return True
        if module == "ai_server.tools" and (
            CONCRETE_CAPABILITY_NAME in imported_names or "*" in imported_names
        ):
            return True
    return False


def literal_mapping_keys(node: ast.expr) -> set[str] | None:
    if isinstance(node, ast.Dict):
        keys: set[str] = set()
        for key in node.keys:
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                return None
            keys.add(key.value)
        return keys
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and not node.args
        and all(keyword.arg is not None for keyword in node.keywords)
    ):
        return {keyword.arg for keyword in node.keywords if keyword.arg is not None}
    return None


class TaskStateMutationVisitor(ast.NodeVisitor):
    """Find writes to Task lifecycle fields without treating reads as mutations."""

    def __init__(self) -> None:
        self.mutations: list[tuple[int, str]] = []
        self._task_names: set[str] = {"task"}
        self._task_type_names: set[str] = {"Task"}

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "ai_server.models.task":
            self._task_type_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "Task"
            )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_annotated_arguments(node.args)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_annotated_arguments(node.args)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_task_producing_call(node.value):
            self._task_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        for target in node.targets:
            self._record_assignment_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and self._annotation_is_task(node.annotation):
            self._task_names.add(node.target.id)
        self._record_assignment_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_assignment_target(node.target)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._record_setattr_call(node)
        self._record_model_update_call(node)
        self._record_task_constructor_call(node)
        self._record_dictionary_update_call(node)
        self.generic_visit(node)

    def _record_annotated_arguments(self, arguments: ast.arguments) -> None:
        all_arguments = (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
        for argument in all_arguments:
            if argument.annotation is not None and self._annotation_is_task(argument.annotation):
                self._task_names.add(argument.arg)

    def _annotation_is_task(self, annotation: ast.expr) -> bool:
        return any(
            (isinstance(node, ast.Name) and node.id in self._task_type_names)
            or (isinstance(node, ast.Attribute) and node.attr in self._task_type_names)
            or (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(name in node.value for name in self._task_type_names)
            )
            for node in ast.walk(annotation)
        )

    def _is_task_reference(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self._task_names or node.id.endswith("_task")
        if isinstance(node, ast.Attribute):
            return node.attr == "task" or self._is_task_reference(node.value)
        return False

    def _is_task_producing_call(self, node: ast.expr) -> bool:
        if not isinstance(node, ast.Call):
            return False
        name = qualified_name(node.func)
        if name is None:
            return False
        return name.split(".")[-1] in self._task_type_names or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"model_copy", "model_validate"}
            and self._is_task_reference(node.func.value)
        )

    def _record_assignment_target(self, target: ast.expr) -> None:
        if (
            isinstance(target, ast.Attribute)
            and target.attr in TASK_STATE_FIELDS
            and self._is_task_reference(target.value)
        ):
            self.mutations.append((target.lineno, f"attribute write: {target.attr}"))
            return
        if not isinstance(target, ast.Subscript):
            return

        key = target.slice
        if (
            not isinstance(key, ast.Constant)
            or not isinstance(key.value, str)
            or key.value not in TASK_STATE_FIELDS
        ):
            return
        container = target.value
        if (
            isinstance(container, ast.Attribute)
            and container.attr == "__dict__"
            and self._is_task_reference(container.value)
        ) or (
            isinstance(container, ast.Call)
            and qualified_name(container.func) == "vars"
            and len(container.args) == 1
            and self._is_task_reference(container.args[0])
        ):
            self.mutations.append((target.lineno, f"mapping write: {key.value}"))

    def _record_setattr_call(self, node: ast.Call) -> None:
        name = qualified_name(node.func)
        if name not in {"builtins.setattr", "object.__setattr__", "setattr"}:
            return
        if len(node.args) < 2 or not self._is_task_reference(node.args[0]):
            return
        field = node.args[1]
        if (
            isinstance(field, ast.Constant)
            and isinstance(field.value, str)
            and field.value in TASK_STATE_FIELDS
        ):
            self.mutations.append((node.lineno, f"{name} write: {field.value}"))

    def _record_model_update_call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute):
            return
        if node.func.attr not in {"copy", "model_copy"}:
            return
        if not self._is_task_reference(node.func.value):
            return
        update = next((item.value for item in node.keywords if item.arg == "update"), None)
        if update is None:
            return
        keys = literal_mapping_keys(update)
        if keys is None:
            self.mutations.append((node.lineno, "dynamic Task model update"))
            return
        for field in sorted(keys & TASK_STATE_FIELDS):
            self.mutations.append((node.lineno, f"Task model update: {field}"))

    def _record_task_constructor_call(self, node: ast.Call) -> None:
        name = qualified_name(node.func)
        if name is None or name.split(".")[-1] not in self._task_type_names:
            return
        for keyword in node.keywords:
            if keyword.arg in TASK_STATE_FIELDS:
                self.mutations.append((node.lineno, f"Task constructor sets: {keyword.arg}"))

    def _record_dictionary_update_call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "update":
            return
        receiver = node.func.value
        is_task_dictionary = (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == "__dict__"
            and self._is_task_reference(receiver.value)
        ) or (
            isinstance(receiver, ast.Call)
            and qualified_name(receiver.func) == "vars"
            and len(receiver.args) == 1
            and self._is_task_reference(receiver.args[0])
        )
        if not is_task_dictionary or not node.args:
            return
        keys = literal_mapping_keys(node.args[0])
        if keys is None:
            self.mutations.append((node.lineno, "dynamic Task dictionary update"))
            return
        for field in sorted(keys & TASK_STATE_FIELDS):
            self.mutations.append((node.lineno, f"Task dictionary update: {field}"))


def task_state_mutations(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    visitor = TaskStateMutationVisitor()
    visitor.visit(tree)
    return visitor.mutations


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
    violations: list[tuple[Path, str]] = []
    for package, forbidden_prefixes in FORBIDDEN_PACKAGE_IMPORTS.items():
        package_root = SRC_ROOT / package
        assert package_root.is_dir()
        if package == "policy":
            forbidden_prefixes = forbidden_prefixes | POLICY_MODEL_ADAPTER_PREFIXES
        for path in package_root.rglob("*.py"):
            imports = imported_modules(path)
            violations.extend(
                (path.relative_to(SRC_ROOT), module)
                for module in imports
                if any(has_prefix(module, prefix) for prefix in forbidden_prefixes)
            )

    assert not violations


def test_bootstrap_is_only_production_importer_of_mock_tool() -> None:
    capability_importers = sorted(
        path.relative_to(SRC_ROOT)
        for path in SRC_ROOT.rglob("*.py")
        if imports_concrete_capability(path)
    )

    assert capability_importers == [CONCRETE_CAPABILITY_OWNER]


def test_runtime_engine_is_only_production_importer_of_executor() -> None:
    violations = [
        path.relative_to(SRC_ROOT)
        for path in SRC_ROOT.rglob("*.py")
        if path.relative_to(SRC_ROOT) != TASK_STATE_OWNER
        and path.relative_to(SRC_ROOT).parts[0] != "executor"
        and any(has_prefix(module, "ai_server.executor") for module in imported_modules(path))
    ]

    assert not violations


def test_executor_is_only_production_tool_gateway_invoker() -> None:
    invokers = sorted(
        path.relative_to(SRC_ROOT)
        for path in SRC_ROOT.rglob("*.py")
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "invoke"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )

    assert invokers == [TOOL_GATEWAY_INVOKE_OWNER]


def test_runtime_engine_is_only_task_state_owner() -> None:
    violations = [
        (path.relative_to(SRC_ROOT), line, mutation)
        for path in SRC_ROOT.rglob("*.py")
        if path.relative_to(SRC_ROOT) != TASK_STATE_OWNER
        for line, mutation in task_state_mutations(path)
    ]

    assert not violations
