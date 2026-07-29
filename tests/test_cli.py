import os
import socket
import subprocess
from importlib import import_module
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest
import sqlalchemy
import typer
from typer.testing import CliRunner

from ai_server import __version__
from ai_server.cli.app import app
from ai_server.models.execution import ExecutionPlan
from ai_server.models.policy import (
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEvaluationContext,
)
from ai_server.models.task import Task
from ai_server.planner.service import SUPPORTED_REQUEST
from ai_server.runtime.doctor import DoctorCheck, DoctorReport, run_doctor
from ai_server.runtime.engine import RuntimeEngine

runner = CliRunner()


def fail_external_call(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError("doctor attempted a forbidden external operation")


def human_approval_runtime() -> RuntimeEngine:
    """Build the local Runtime with a test-only stricter human approval decision."""
    runtime = RuntimeEngine()
    trusted_evaluate = runtime._policy.evaluate

    def evaluate(
        plan: ExecutionPlan,
        context: PolicyEvaluationContext,
    ) -> PolicyDecision:
        decision = trusted_evaluate(plan, context)
        steps = tuple(
            step.model_copy(
                update={"approval_requirement": (PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL)}
            )
            for step in decision.step_decisions
        )
        return decision.model_copy(
            update={
                "approval_requirement": (PolicyApprovalRequirement.HUMAN_PLAN_APPROVAL),
                "step_decisions": steps,
            }
        )

    runtime._policy.evaluate = evaluate  # type: ignore[method-assign]
    return runtime


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout == f"ai-server {__version__}\n"
    assert result.stderr == ""
    assert distribution_version("ai-server-runtime") == __version__


def test_run_command_executes_only_the_registered_l0_mock() -> None:
    result = runner.invoke(app, ["run", SUPPORTED_REQUEST])

    assert result.exit_code == 0
    assert "COMPLETED" in result.stdout
    assert "local-mock" in result.stdout
    assert "tool_results" in result.stdout
    assert "all_successful" in result.stdout


def test_run_command_commit_requires_exact_hash_and_stops_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_module = import_module("ai_server.cli.app")
    runtime = human_approval_runtime()
    monkeypatch.setattr(cli_module, "_create_runtime", lambda: runtime)
    monkeypatch.setattr(cli_module, "_is_interactive_review", lambda: True)

    def exact_commit(prompt: str, **kwargs: object) -> str:
        del kwargs
        plan_hash = prompt.split()[2]
        return f"COMMIT {plan_hash}"

    monkeypatch.setattr(cli_module.typer, "prompt", exact_commit)

    result = runner.invoke(app, ["run"], terminal_width=240)

    assert result.exit_code == 0
    assert "WAITING_FOR_APPROVAL" in result.stdout
    assert "Exact Plan Review" in result.stdout
    assert "Plan Approval Recorded" in result.stdout
    assert "manual_confirmation" in result.stdout
    assert "policy_decision_hash" in result.stdout
    assert "registry_risk" in result.stdout
    assert "target_scope" in result.stdout
    assert "side_effects" in result.stdout
    assert "registry_redaction" in result.stdout
    assert "registry_verification" in result.stdout
    assert "registry_rollback" in result.stdout
    assert "execution remains paused until Phase 5" in result.stdout
    approval_output = result.stdout.split("Plan Approval Recorded", maxsplit=1)[1]
    assert "approver" not in approval_output
    assert "content_hash" not in approval_output
    assert "password" not in result.stdout.lower()
    assert "private key" not in result.stdout.lower()
    assert all(
        event.kind.value not in {"PLAN_APPROVAL_CONSUMED", "L3_CONFIRMATION_CONSUMED"}
        for event in runtime._approval.events
    )


def test_run_command_rejects_non_tty_human_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_module = import_module("ai_server.cli.app")
    runtime = human_approval_runtime()
    monkeypatch.setattr(cli_module, "_create_runtime", lambda: runtime)
    monkeypatch.setattr(cli_module, "_is_interactive_review", lambda: False)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 2
    assert "interactive local TTY" in result.stderr
    assert "nothing was authorized" in result.stderr
    assert all(event.kind.value != "PLAN_APPROVAL_ISSUED" for event in runtime._approval.events)


@pytest.mark.parametrize(
    ("response", "exit_code", "expected"),
    [
        ("REJECT", 1, "Plan rejected"),
        ("COMMIT wrong-hash", 2, "did not match the exact Plan Hash"),
    ],
)
def test_run_command_reject_and_mismatched_commit_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    exit_code: int,
    expected: str,
) -> None:
    cli_module = import_module("ai_server.cli.app")
    runtime = human_approval_runtime()
    monkeypatch.setattr(cli_module, "_create_runtime", lambda: runtime)
    monkeypatch.setattr(cli_module, "_is_interactive_review", lambda: True)
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda prompt, **kwargs: response,
    )

    result = runner.invoke(app, ["run"])

    assert result.exit_code == exit_code
    assert expected in result.output
    assert all(event.kind.value != "PLAN_APPROVAL_ISSUED" for event in runtime._approval.events)


def test_run_command_has_no_automatic_yes_option() -> None:
    result = runner.invoke(app, ["run", "--yes"])

    assert result.exit_code != 0


@pytest.mark.parametrize(
    "interruption",
    [EOFError(), KeyboardInterrupt(), typer.Abort()],
)
def test_run_command_prompt_interruptions_authorize_nothing(
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    cli_module = import_module("ai_server.cli.app")
    runtime = human_approval_runtime()
    monkeypatch.setattr(cli_module, "_create_runtime", lambda: runtime)
    monkeypatch.setattr(cli_module, "_is_interactive_review", lambda: True)

    def interrupt(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        raise interruption

    monkeypatch.setattr(cli_module.typer, "prompt", interrupt)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 2
    assert "nothing was authorized" in result.stderr
    assert all(event.kind.value != "PLAN_APPROVAL_ISSUED" for event in runtime._approval.events)


def test_run_command_redacts_approval_review_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_REVIEW_FAILURE_MARKER"
    cli_module = import_module("ai_server.cli.app")
    runtime = human_approval_runtime()

    def explode(outcome: object) -> None:
        del outcome
        raise RuntimeError(marker)

    monkeypatch.setattr(runtime, "prepare_approval_review", explode)
    monkeypatch.setattr(cli_module, "_create_runtime", lambda: runtime)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 2
    assert "Approval Review could not be prepared safely" in result.stderr
    assert marker not in result.output
    assert all(event.kind.value != "PLAN_APPROVAL_ISSUED" for event in runtime._approval.events)


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
