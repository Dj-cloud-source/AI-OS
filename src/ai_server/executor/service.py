"""Concrete Phase 0 Executor and its one Mock Tool capability."""

from collections.abc import Callable

from pydantic import ValidationError

from ai_server.models.execution import ExecutionPlan, StepRole
from ai_server.models.system_status import GetSystemStatusArguments, SystemStatus
from ai_server.models.tool import ToolResult
from ai_server.runtime.errors import ToolExecutionError
from ai_server.tools.get_system_status import GET_SYSTEM_STATUS_METADATA, get_system_status

SystemStatusTool = Callable[[GetSystemStatusArguments], ToolResult[SystemStatus]]


class Executor:
    """Invoke the exact Phase 0 Mock Tool without changing the plan."""

    def __init__(self, system_status_tool: SystemStatusTool = get_system_status) -> None:
        """Bind the concrete Mock Tool capability to Executor alone."""
        self._system_status_tool = system_status_tool

    def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
        """Execute each supported step exactly once in declared order."""
        try:
            if not isinstance(plan, ExecutionPlan):
                raise TypeError
            plan = ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except (TypeError, ValidationError):
            raise ToolExecutionError("Executor received a malformed plan") from None

        if not plan.steps:
            raise ToolExecutionError("Executor received an empty plan")

        results: list[ToolResult[SystemStatus]] = []
        for step in plan.steps:
            if (
                step.tool_name != GET_SYSTEM_STATUS_METADATA.name
                or step.tool_version != GET_SYSTEM_STATUS_METADATA.version
                or step.role is not StepRole.OBSERVE
            ):
                raise ToolExecutionError("Executor received an unsupported planned Tool")
            if step.arguments.target != plan.target:
                raise ToolExecutionError("Executor received mismatched Tool arguments")

            try:
                raw_result = self._system_status_tool(step.arguments)
                if not isinstance(raw_result, ToolResult):
                    raise ToolExecutionError("Tool returned an invalid structured result")
                result = ToolResult[SystemStatus].model_validate(
                    raw_result.model_dump(mode="python", warnings="none"),
                    strict=True,
                )
            except ToolExecutionError:
                raise
            except Exception:
                raise ToolExecutionError("Tool invocation failed safely") from None

            if result.tool_name != step.tool_name or result.tool_version != step.tool_version:
                raise ToolExecutionError("ToolResult identity does not match the plan")
            results.append(result)
        return tuple(results)


__all__ = ["Executor", "SystemStatusTool"]
