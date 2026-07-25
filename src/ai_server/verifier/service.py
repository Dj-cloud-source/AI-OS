"""Deterministic Phase 0 Verifier."""

from pydantic import ValidationError

from ai_server.models.execution import ExecutionPlan
from ai_server.models.system_status import SystemStatus
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
            if not isinstance(plan, ExecutionPlan):
                raise TypeError
            plan = ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except (TypeError, ValidationError):
            raise VerificationError("Verification received a malformed plan") from None

        if not plan.steps:
            raise VerificationError("Verification cannot accept an empty plan")
        if len(results) != len(plan.steps):
            raise VerificationError("Verification evidence count does not match the plan")

        for step, raw_result in zip(plan.steps, results, strict=True):
            try:
                if not isinstance(raw_result, ToolResult):
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
