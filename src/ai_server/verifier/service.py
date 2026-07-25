"""Deterministic Phase 0 Verifier."""

from ai_server.models.execution import ExecutionPlan, ExecutionStep
from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.tool import ToolResult
from ai_server.runtime.errors import VerificationError


class Verifier:
    """Verify existing ToolResults without invoking a Tool."""

    def verify(
        self,
        plan: ExecutionPlan,
        results: tuple[ToolResult[SystemStatus], ...],
    ) -> None:
        """Raise an explicit error unless all mock evidence matches the plan."""
        try:
            if (
                type(plan) is not ExecutionPlan
                or type(plan.steps) is not tuple
                or any(
                    type(step) is not ExecutionStep
                    or type(step.arguments) is not GetSystemStatusArguments
                    for step in plan.steps
                )
            ):
                raise TypeError
            plan = ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except Exception:
            raise VerificationError("Verification received a malformed plan") from None

        if not plan.steps:
            raise VerificationError("Verification cannot accept an empty plan")
        if type(results) is not tuple:
            raise VerificationError("Verification evidence container is malformed")
        if len(results) != len(plan.steps):
            raise VerificationError("Verification evidence count does not match the plan")

        for step, raw_result in zip(plan.steps, results, strict=True):
            try:
                if (
                    type(raw_result) is not ToolResult[SystemStatus]
                    or type(raw_result.data) is not SystemStatus
                    or type(raw_result.data.services) is not tuple
                    or any(
                        type(service) is not ServiceStatus for service in raw_result.data.services
                    )
                ):
                    raise VerificationError("Verification evidence is malformed")
                result = ToolResult[SystemStatus].model_validate(
                    raw_result.model_dump(mode="python", warnings="none"),
                    strict=True,
                )
            except VerificationError:
                raise
            except Exception:
                raise VerificationError("Verification evidence is malformed") from None

            if result.tool_name != step.tool_name or result.tool_version != step.tool_version:
                raise VerificationError("Verification evidence identity does not match the plan")
            if result.data.target != plan.target:
                raise VerificationError("Verification evidence target does not match the plan")
            if not result.data.simulated or result.data.source != "mock":
                raise VerificationError("Verification evidence is not explicitly simulated")


__all__ = ["Verifier"]
