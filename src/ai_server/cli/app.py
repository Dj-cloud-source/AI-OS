"""Thin Typer adapter for the local Runtime."""

import json
import sys

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ai_server import __version__
from ai_server.models.approval import ApprovalRecord, ApprovalReview
from ai_server.models.executor import ManualConfirmationChallenge
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
    if outcome.execution_authorization is not None:
        table.add_row(
            "execution_attempt_id",
            str(outcome.execution_authorization.execution_attempt_id),
        )
    if outcome.execution_report is not None:
        table.add_row("execution_report", outcome.execution_report.status.value)
        if outcome.execution_uncertainty is None:
            table.add_row(
                "execution_report_human_intervention_required",
                str(outcome.execution_report.human_intervention_required),
            )
    if outcome.execution_uncertainty is not None:
        uncertainty = outcome.execution_uncertainty
        table.add_row("execution_uncertainty", uncertainty.uncertainty_kind)
        table.add_row("uncertainty_hash", uncertainty.content_hash)
        table.add_row("dispatch_status", uncertainty.dispatch_status.value)
        table.add_row("effect_disposition", uncertainty.effect_disposition.value)
        table.add_row(
            "human_intervention_required",
            str(uncertainty.human_intervention_required),
        )
    if outcome.verification_result is not None:
        verification = outcome.verification_result
        table.add_row("verification_status", verification.status.value)
        table.add_row("verification_hash", verification.content_hash)
        table.add_row(
            "verification_failure_reasons",
            ",".join(reason.value for reason in verification.failure_reasons) or "none",
        )
    table.add_row("final_effect_disposition", outcome.final_effect_disposition.value)
    table.add_row(
        "human_intervention_required",
        str(outcome.human_intervention_required),
    )
    if outcome.results:
        table.add_row("tool_results", str(len(outcome.results)))
        table.add_row("all_successful", str(all(result.success for result in outcome.results)))
    console.print(table)
    if outcome.execution_report is not None:
        records = Table(title="Governed Tool Invocations")
        records.add_column("Step")
        records.add_column("Role")
        records.add_column("Tool")
        records.add_column("Dispatch")
        records.add_column("Effect")
        records.add_column("Result")
        for record in outcome.execution_report.records:
            records.add_row(
                f"{record.step_index}: {record.step_id}",
                record.role.value,
                f"{record.tool_id}@{record.tool_version}",
                record.dispatch_status.value,
                record.effect_disposition.value,
                "success"
                if record.result is not None and record.result.success
                else (record.failure_code or "unavailable"),
            )
        console.print(records)


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
    criteria = Table(title="Mandatory Verification Criteria")
    criteria.add_column("Criterion", no_wrap=True)
    criteria.add_column("Exact Definition", overflow="fold")
    for criterion in review.snapshot.verification_criteria:
        criteria.add_row(
            criterion.criterion_id,
            Text(
                json.dumps(
                    criterion.model_dump(mode="json", warnings="error"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
        )
    console.print(criteria)


def _render_approval(record: ApprovalRecord) -> None:
    """Display non-secret evidence that authorization was recorded."""
    table = Table(title="Plan Approval Recorded")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("approval_id", str(record.approval_id))
    table.add_row("plan_hash", record.plan_hash)
    table.add_row("expires_at", record.expires_at.isoformat())
    console.print(table)


def _render_l3_challenge(challenge: ManualConfirmationChallenge) -> None:
    """Display every non-secret fact bound by an L3 Challenge Hash."""
    table = Table(title="Immediate L3 Invocation Confirmation")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("challenge_hash", challenge.challenge_hash)
    table.add_row("authorization_hash", challenge.authorization_hash)
    table.add_row("approval_id", str(challenge.approval_id))
    table.add_row("approval_plan_hash", challenge.approval_plan_hash)
    table.add_row("approval_record_hash", challenge.approval_record_hash)
    table.add_row("approval_expires_at", challenge.approval_expires_at.isoformat())
    table.add_row("execution_attempt_id", str(challenge.execution_attempt_id))
    table.add_row("invocation_id", str(challenge.invocation_id))
    table.add_row("step", f"{challenge.step_index}: {challenge.step_id}")
    table.add_row("role", challenge.role.value)
    table.add_row("tool", f"{challenge.tool_id}@{challenge.tool_version}")
    table.add_row("contract_hash", challenge.contract_hash)
    table.add_row("implementation_hash", challenge.implementation_hash)
    table.add_row("arguments_hash", challenge.arguments_hash)
    table.add_row("target", challenge.target.model_dump_json())
    console.print(table)


def _prompt_l3_confirmation(challenge: ManualConfirmationChallenge) -> str:
    """Read one exact L3 confirmation only from the interactive local TTY."""
    if not _is_interactive_review():
        return ""
    _render_l3_challenge(challenge)
    try:
        response = typer.prompt(
            f"Type CONFIRM {challenge.challenge_hash}",
            default="",
            show_default=False,
        )
        return response if type(response) is str else ""
    except (EOFError, KeyboardInterrupt, typer.Abort):
        return ""


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
        try:
            resumed = runtime.resume_approved(
                outcome,
                record.approval_id,
                confirmation_reader=_prompt_l3_confirmation,
            )
        except BaseException:
            typer.echo("Approved execution could not resume safely.", err=True)
            raise typer.Exit(code=2) from None
        _render_outcome(resumed)
        if resumed.status is RuntimeOutcomeStatus.COMPLETED:
            return
        if resumed.status is RuntimeOutcomeStatus.FAILED:
            raise typer.Exit(code=1)
        typer.echo("Approved execution remains paused; no Tool was dispatched.", err=True)
        raise typer.Exit(code=2)
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
