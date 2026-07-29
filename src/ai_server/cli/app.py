"""Thin Typer adapter for the local Runtime."""

import json
import sys

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ai_server import __version__
from ai_server.models.approval import ApprovalRecord, ApprovalReview
from ai_server.models.runtime import RuntimeOutcome, RuntimeOutcomeStatus
from ai_server.models.task import Task
from ai_server.planner.service import SUPPORTED_REQUEST
from ai_server.runtime.doctor import run_doctor
from ai_server.runtime.engine import RuntimeEngine

app = typer.Typer(
    add_completion=False,
    help="Local-first AIOps Agent Runtime.",
    no_args_is_help=True,
)
console = Console()


def _create_runtime() -> RuntimeEngine:
    """Construct the reviewed local Runtime used by the run command."""
    return RuntimeEngine()


def _is_interactive_review() -> bool:
    """Return whether both CLI input and output are attached to a TTY."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except BaseException:
        return False


def _render_outcome(outcome: RuntimeOutcome) -> None:
    """Render a bounded Runtime outcome without exposing raw exception data."""
    table = Table(title="ai-server run")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("status", outcome.status.value)
    table.add_row("task_id", str(outcome.task.task_id))
    table.add_row("target", outcome.task.target)
    if outcome.plan is not None:
        table.add_row("plan_id", str(outcome.plan.plan_id))
    if outcome.policy_decision is not None:
        table.add_row(
            "policy",
            f"{outcome.policy_decision.policy_id}@{outcome.policy_decision.policy_version}",
        )
        table.add_row("policy_effect", outcome.policy_decision.effect.value)
    if outcome.failure is not None:
        table.add_row("failure_code", outcome.failure.code)
        table.add_row("failure", outcome.failure.message)
    if outcome.results:
        table.add_row("tool_results", str(len(outcome.results)))
        table.add_row("all_successful", str(all(result.success for result in outcome.results)))
    console.print(table)


def _render_review(review: ApprovalReview) -> None:
    """Display the exact safe Review content and full Plan Hash."""
    summary = Table(title="Exact Plan Review")
    summary.add_column("Field")
    summary.add_column("Value")
    summary.add_row("review_id", str(review.review_id))
    summary.add_row("plan_hash", review.plan_hash)
    summary.add_row("operator", review.operator_id)
    summary.add_row("target", review.target.model_dump_json())
    summary.add_row("risk", review.effective_risk.value)
    summary.add_row("approval", review.approval_requirement.value)
    summary.add_row(
        "manual_confirmation",
        review.manual_confirmation_requirement.value,
    )
    summary.add_row("policy", f"{review.policy_id}@{review.policy_version}")
    summary.add_row("policy_hash", review.policy_hash)
    summary.add_row("policy_decision_hash", review.policy_decision_hash)
    summary.add_row("expires_at", review.expires_at.isoformat())
    console.print(summary)
    for step in review.snapshot.steps:
        table = Table(title=f"Step {step.step_index}: {step.step_id}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("role", step.role.value)
        table.add_row("tool", f"{step.tool_id}@{step.tool_version}")
        table.add_row("registry_risk", step.registry_risk_level.value)
        table.add_row("contract_hash", step.contract_hash)
        table.add_row("implementation_hash", step.implementation_hash)
        table.add_row(
            "arguments",
            Text(
                json.dumps(
                    step.model_dump(mode="json", warnings="error")["arguments"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
        )
        table.add_row("arguments_hash", step.arguments_hash)
        table.add_row("target", step.target.model_dump_json())
        table.add_row("target_scope", step.target_scope.model_dump_json())
        table.add_row("side_effects", step.side_effects.model_dump_json())
        table.add_row("registry_redaction", step.registry_redaction.model_dump_json())
        table.add_row(
            "registry_verification",
            step.registry_verification.model_dump_json(),
        )
        table.add_row("registry_rollback", step.registry_rollback.model_dump_json())
        table.add_row("why", Text(step.reason))
        table.add_row("impact", Text(step.impact))
        table.add_row("verification", Text(step.verification))
        table.add_row("recovery", Text(step.recovery))
        table.add_row("skill_provenance", str(step.skill_provenance))
        table.add_row(
            "limitations",
            Text(json.dumps(step.limitations, ensure_ascii=False)),
        )
        console.print(table)


def _render_approval(record: ApprovalRecord) -> None:
    """Display non-secret evidence that authorization was recorded."""
    table = Table(title="Plan Approval Recorded")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("approval_id", str(record.approval_id))
    table.add_row("plan_hash", record.plan_hash)
    table.add_row("expires_at", record.expires_at.isoformat())
    console.print(table)


@app.command()
def version() -> None:
    """Print the ai-server package version."""
    typer.echo(f"ai-server {__version__}")


@app.command()
def doctor() -> None:
    """Run safe local dependency and Mock Runtime checks."""
    report = run_doctor()
    table = Table(title="ai-server doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for check in report.checks:
        status = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
        table.add_row(check.name, status, check.detail)
    console.print(table)
    if not report.healthy:
        raise typer.Exit(code=1)


@app.command("run")
def run_task(
    request: str = typer.Argument(
        SUPPORTED_REQUEST,
        help="The exact local Runtime request.",
    ),
) -> None:
    """Run the local Mock Runtime or review one human-approval pause."""
    try:
        runtime = _create_runtime()
        outcome = runtime.run(Task(request=request))
    except BaseException:
        typer.echo("Runtime could not start safely.", err=True)
        raise typer.Exit(code=2) from None

    _render_outcome(outcome)
    if outcome.status is RuntimeOutcomeStatus.COMPLETED:
        return
    if outcome.status is RuntimeOutcomeStatus.FAILED:
        raise typer.Exit(code=1)

    try:
        review = runtime.prepare_approval_review(outcome)
    except BaseException:
        typer.echo("Approval Review could not be prepared safely.", err=True)
        raise typer.Exit(code=2) from None
    _render_review(review)
    if not _is_interactive_review():
        typer.echo(
            "Human approval requires an interactive local TTY; nothing was authorized.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        response = typer.prompt(
            f"Type COMMIT {review.plan_hash} or REJECT",
            default="",
            show_default=False,
        )
    except (EOFError, KeyboardInterrupt, typer.Abort):
        typer.echo("Approval cancelled; nothing was authorized.", err=True)
        raise typer.Exit(code=2) from None

    if response == f"COMMIT {review.plan_hash}":
        try:
            record = runtime.commit_approval(outcome, review.review_id)
        except BaseException:
            typer.echo("Approval Commit failed safely.", err=True)
            raise typer.Exit(code=2) from None
        _render_approval(record)
        typer.echo(
            "Authorization recorded in this process; execution remains paused until Phase 5."
        )
        return
    if response == "REJECT":
        try:
            runtime.reject_approval(outcome, review.review_id)
        except BaseException:
            typer.echo("Approval rejection failed safely.", err=True)
            raise typer.Exit(code=2) from None
        typer.echo("Plan rejected; no Tool was dispatched.")
        raise typer.Exit(code=1)

    typer.echo(
        "Approval input did not match the exact Plan Hash; nothing was authorized.", err=True
    )
    raise typer.Exit(code=2)


__all__ = ["app", "doctor", "run_task", "version"]
