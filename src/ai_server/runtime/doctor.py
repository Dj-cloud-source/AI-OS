"""Safe local diagnostics for the CLI doctor command."""

import sys
from importlib import import_module

from pydantic import BaseModel, ConfigDict, Field

from ai_server.models.runtime import RuntimeOutcomeStatus
from ai_server.models.task import Task
from ai_server.planner.service import SUPPORTED_REQUEST
from ai_server.runtime.engine import create_mock_runtime
from ai_server.runtime.state import RuntimeState

_REQUIRED_MODULES = ("pydantic", "rich", "sqlalchemy", "typer")


class DoctorCheck(BaseModel):
    """One structured local diagnostic check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    passed: bool
    detail: str = Field(min_length=1)


class DoctorReport(BaseModel):
    """Structured collection of safe local diagnostic checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: tuple[DoctorCheck, ...] = Field(min_length=1)

    @property
    def healthy(self) -> bool:
        """Return whether every diagnostic check passed."""
        return all(check.passed for check in self.checks)


def run_doctor() -> DoctorReport:
    """Check Python, required imports, and the local Mock Runtime only."""
    checks: list[DoctorCheck] = []
    python_supported = sys.version_info >= (3, 12)
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    checks.append(
        DoctorCheck(
            name="python",
            passed=python_supported,
            detail=f"{python_version} (requires >=3.12)",
        )
    )

    for module_name in _REQUIRED_MODULES:
        try:
            import_module(module_name)
        except Exception as error:
            checks.append(
                DoctorCheck(
                    name=f"import:{module_name}",
                    passed=False,
                    detail="missing" if isinstance(error, ImportError) else "import_failed",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name=f"import:{module_name}",
                    passed=True,
                    detail="available",
                )
            )

    try:
        outcome = create_mock_runtime().run(Task(request=SUPPORTED_REQUEST))
    except Exception as error:
        del error
        checks.append(
            DoctorCheck(
                name="mock-runtime",
                passed=False,
                detail="runtime_failed",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="mock-runtime",
                passed=(
                    outcome.status is RuntimeOutcomeStatus.COMPLETED
                    and outcome.task.state is RuntimeState.COMPLETED
                ),
                detail="simulated L0 lifecycle completed",
            )
        )

    return DoctorReport(checks=tuple(checks))


__all__ = ["DoctorCheck", "DoctorReport", "run_doctor"]
