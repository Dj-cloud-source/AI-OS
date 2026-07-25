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
from ai_server.runtime.doctor import DoctorCheck, DoctorReport

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
        "open",
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
