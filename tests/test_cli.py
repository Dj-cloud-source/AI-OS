import os
import socket
import subprocess
from importlib import import_module
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest
import sqlalchemy
from typer.testing import CliRunner

from ai_server import __version__
from ai_server.cli.app import app
from ai_server.models.task import Task
from ai_server.runtime.doctor import DoctorCheck, DoctorReport, run_doctor

runner = CliRunner()


def fail_external_call(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError("doctor attempted a forbidden external operation")


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout == f"ai-server {__version__}\n"
    assert result.stderr == ""
    assert distribution_version("ai-server-runtime") == __version__


def test_doctor_command_is_safe_and_uses_mock_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", fail_external_call)
    monkeypatch.setattr(subprocess, "run", fail_external_call)
    monkeypatch.setattr(subprocess, "Popen", fail_external_call)
    monkeypatch.setattr(os, "system", fail_external_call)
    monkeypatch.setattr(sqlalchemy, "create_engine", fail_external_call)
    for function_name in (
        "makedirs",
        "mkdir",
        "remove",
        "removedirs",
        "rename",
        "replace",
        "rmdir",
        "unlink",
    ):
        monkeypatch.setattr(os, function_name, fail_external_call)
    for method_name in (
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    ):
        monkeypatch.setattr(Path, method_name, fail_external_call)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "ai-server doctor" in result.stdout
    assert "python" in result.stdout
    assert "import:pydantic" in result.stdout
    assert "import:sqlalchemy" in result.stdout
    assert "mock-runtime" in result.stdout
    assert "simulated L0 lifecycle completed" in result.stdout
    assert "PASS" in result.stdout


def test_doctor_command_returns_nonzero_for_failed_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_module = import_module("ai_server.cli.app")
    failed_report = DoctorReport(
        checks=(DoctorCheck(name="mock-runtime", passed=False, detail="failed"),)
    )
    monkeypatch.setattr(cli_module, "run_doctor", lambda: failed_report)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert result.exception is not None


def test_doctor_redacts_unexpected_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_DOCTOR_IMPORT_MARKER"
    doctor_module = import_module("ai_server.runtime.doctor")

    def exploding_import(module_name: str) -> None:
        del module_name
        raise RuntimeError(marker)

    monkeypatch.setattr(doctor_module, "import_module", exploding_import)

    report = run_doctor()
    import_checks = [check for check in report.checks if check.name.startswith("import:")]

    assert import_checks
    assert all(not check.passed and check.detail == "import_failed" for check in import_checks)
    assert marker not in report.model_dump_json()


def test_doctor_redacts_unexpected_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_DOCTOR_RUNTIME_MARKER"
    doctor_module = import_module("ai_server.runtime.doctor")

    class ExplodingRuntime:
        def run(self, task: Task) -> None:
            del task
            raise RuntimeError(marker)

    monkeypatch.setattr(doctor_module, "create_mock_runtime", ExplodingRuntime)

    report = run_doctor()
    runtime_check = next(check for check in report.checks if check.name == "mock-runtime")

    assert not runtime_check.passed
    assert runtime_check.detail == "runtime_failed"
    assert marker not in report.model_dump_json()


def test_doctor_does_not_reflect_untrusted_exception_class_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_EXCEPTION_CLASS_MARKER"
    doctor_module = import_module("ai_server.runtime.doctor")

    class ExplodingName(type):
        def __getattribute__(cls, name: str) -> object:
            if name == "__name__":
                raise RuntimeError(marker)
            return super().__getattribute__(name)

    class UntrustedError(Exception, metaclass=ExplodingName):
        pass

    def exploding_import(module_name: str) -> None:
        del module_name
        raise UntrustedError(marker)

    class ExplodingRuntime:
        def run(self, task: Task) -> None:
            del task
            raise UntrustedError(marker)

    monkeypatch.setattr(doctor_module, "import_module", exploding_import)
    monkeypatch.setattr(doctor_module, "create_mock_runtime", ExplodingRuntime)

    report = run_doctor()

    assert all(
        check.detail == "import_failed"
        for check in report.checks
        if check.name.startswith("import:")
    )
    runtime_check = next(check for check in report.checks if check.name == "mock-runtime")
    assert runtime_check.detail == "runtime_failed"
    assert marker not in report.model_dump_json()
