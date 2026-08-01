import os
import socket
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import import_module
from importlib.metadata import version as distribution_version
from io import StringIO
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
import sqlalchemy
import typer
from rich.console import Console
from typer.testing import CliRunner

from ai_server import __version__
from ai_server.cli.app import app
from ai_server.executor.service import Executor
from ai_server.models.execution import ExecutionPlan, StepRole
from ai_server.models.executor import (
    ExecutionAttemptAuthorization,
    ExecutionReport,
    ManualConfirmationChallenge,
)
from ai_server.models.policy import (
    PolicyApprovalRequirement,
    PolicyDecision,
    PolicyEvaluationContext,
)
from ai_server.models.task import Task
from ai_server.models.tool import TargetReference
from ai_server.planner.service import SUPPORTED_REQUEST
from ai_server.runtime.doctor import DoctorCheck, DoctorReport, run_doctor
from ai_server.runtime.engine import RuntimeEngine
from ai_server.tools.hashing import canonical_json_sha256

runner = CliRunner()


def manual_confirmation_challenge() -> ManualConfirmationChallenge:
    """Build one valid non-secret L3 CLI fixture."""
    draft = ManualConfirmationChallenge.model_construct(
        challenge_schema_version="1",
        authorization_hash="a" * 64,
        approval_id=UUID("00000000-0000-4000-8000-000000000001"),
        approval_plan_hash="b" * 64,
        approval_record_hash="c" * 64,
        approval_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        execution_attempt_id=UUID("00000000-0000-4000-8000-000000000002"),
        invocation_id=UUID("00000000-0000-4000-8000-000000000003"),
        step_index=0,
        step_id="restart-service",
        role=StepRole.ACTION,
        tool_id="test_l3_tool",
        tool_version="1.0.0",
        contract_hash="d" * 64,
        implementation_hash="e" * 64,
        arguments_hash="f" * 64,
        target=TargetReference(
            target_id="local-mock",
            resource_type="local_system",
            resource_id="local-mock",
        ),
        challenge_hash="0" * 64,
    )
    payload = draft.model_dump(
        mode="json",
        exclude={"challenge_hash"},
        warnings="error",
    )
    document = draft.model_dump(mode="python", warnings="error")
    document["challenge_hash"] = canonical_json_sha256(payload)
    return ManualConfirmationChallenge.model_validate(
        document,
        strict=True,
    )


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
    assert "verification_status" in result.stdout
    assert "PASSED" in result.stdout
    assert "verification_hash" in result.stdout
    assert "final_effect_disposition" in result.stdout
    assert "NONE" in result.stdout
    assert "human_intervention_required" in result.stdout
    assert "False" in result.stdout


def test_run_command_displays_attempt_closure_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_CLI_EXECUTION_UNCERTAINTY"
    cli_module = import_module("ai_server.cli.app")
    runtime = RuntimeEngine()

    class AbortUncertainExecutor:
        def __init__(self, delegate: Executor) -> None:
            self._delegate = delegate

        def begin_attempt(
            self,
            plan: ExecutionPlan,
            policy_decision: PolicyDecision,
            approval_id: UUID | None,
        ) -> ExecutionAttemptAuthorization:
            return self._delegate.begin_attempt(plan, policy_decision, approval_id)

        def execute_actions(
            self,
            authorization: ExecutionAttemptAuthorization,
            confirmation_reader: Callable[[ManualConfirmationChallenge], str] | None = None,
        ) -> ExecutionReport:
            del authorization, confirmation_reader
            raise RuntimeError(marker)

        def execute_verification(
            self,
            authorization: ExecutionAttemptAuthorization,
        ) -> ExecutionReport:
            del authorization
            raise AssertionError("verification must not run")

        def abort_attempt(
            self,
            authorization: ExecutionAttemptAuthorization,
            *,
            reason_code: str = "attempt_aborted",
        ) -> ExecutionReport:
            del authorization, reason_code
            raise RuntimeError(marker)

    runtime._executor = cast(Executor, AbortUncertainExecutor(runtime._executor))
    monkeypatch.setattr(cli_module, "_create_runtime", lambda: runtime)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == 1
    assert "execution_uncertainty" in result.stdout
    assert "ATTEMPT_CLOSURE_UNCONFIRMED" in result.stdout
    assert "dispatch_status" in result.stdout
    assert "UNKNOWN" in result.stdout
    assert "effect_disposition" in result.stdout
    assert "human_intervention_required" in result.stdout
    assert "True" in result.stdout
    assert marker not in result.output


def test_run_command_commit_requires_exact_hash_and_resumes_same_process(
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
    assert "Mandatory Verification Criteria" in result.stdout
    assert '"expected"' in result.stdout
    assert '"field"' in result.stdout
    assert '"source"' in result.stdout
    assert "COMPLETED" in result.stdout
    assert "execution_attempt_id" in result.stdout
    assert "Governed Tool Invocations" in result.stdout
    approval_output = result.stdout.split("Plan Approval Recorded", maxsplit=1)[1]
    assert "approver" not in approval_output
    assert "content_hash" not in approval_output
    assert "password" not in result.stdout.lower()
    assert "private key" not in result.stdout.lower()
    assert (
        sum(event.kind.value == "PLAN_APPROVAL_CONSUMED" for event in runtime._approval.events) == 1
    )
    assert all(event.kind.value != "L3_CONFIRMATION_CONSUMED" for event in runtime._approval.events)


def test_l3_cli_challenge_displays_every_binding_and_requests_full_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_module = import_module("ai_server.cli.app")
    challenge = manual_confirmation_challenge()
    prompts: list[str] = []
    output_buffer = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output_buffer, width=240, color_system=None),
    )
    monkeypatch.setattr(cli_module, "_is_interactive_review", lambda: True)

    def exact_confirmation(prompt: str, **kwargs: object) -> str:
        del kwargs
        prompts.append(prompt)
        return f"CONFIRM {challenge.challenge_hash}"

    monkeypatch.setattr(cli_module.typer, "prompt", exact_confirmation)

    response = cli_module._prompt_l3_confirmation(challenge)
    output = output_buffer.getvalue()

    assert response == f"CONFIRM {challenge.challenge_hash}"
    assert prompts == [f"Type CONFIRM {challenge.challenge_hash}"]
    for expected in (
        challenge.challenge_hash,
        challenge.authorization_hash,
        str(challenge.approval_id),
        challenge.approval_plan_hash,
        challenge.approval_record_hash,
        str(challenge.execution_attempt_id),
        str(challenge.invocation_id),
        challenge.step_id,
        challenge.tool_id,
        challenge.contract_hash,
        challenge.implementation_hash,
        challenge.arguments_hash,
        challenge.target.target_id,
    ):
        assert expected in output


def test_l3_cli_challenge_cannot_read_from_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_module = import_module("ai_server.cli.app")
    monkeypatch.setattr(cli_module, "_is_interactive_review", lambda: False)

    def fail_prompt(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("non-TTY confirmation attempted to read input")

    monkeypatch.setattr(cli_module.typer, "prompt", fail_prompt)

    assert cli_module._prompt_l3_confirmation(manual_confirmation_challenge()) == ""


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
