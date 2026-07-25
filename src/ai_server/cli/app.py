"""Thin Typer adapter for the local Runtime."""

import typer
from rich.console import Console
from rich.table import Table

from ai_server import __version__
from ai_server.runtime.doctor import run_doctor

app = typer.Typer(
    add_completion=False,
    help="Local-first AIOps Agent Runtime.",
    no_args_is_help=True,
)


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
    Console().print(table)
    if not report.healthy:
        raise typer.Exit(code=1)


__all__ = ["app", "doctor", "version"]
