"""Deterministic Phase 0 Planner."""

from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.system_status import GetSystemStatusArguments
from ai_server.models.tool import ToolMetadata
from ai_server.runtime.errors import UnsupportedTaskError

SUPPORTED_REQUEST = "get_system_status"


class Planner:
    """Create an explained plan without invoking operational capabilities."""

    def create_plan(
        self,
        context: RuntimeContext,
        metadata: ToolMetadata,
    ) -> ExecutionPlan:
        """Create the one supported Phase 0 mock execution plan."""
        if context.request != SUPPORTED_REQUEST:
            raise UnsupportedTaskError("Unsupported Phase 0 task request")

        step = ExecutionStep(
            step_id="get-system-status",
            role=StepRole.OBSERVE,
            tool_name=metadata.name,
            tool_version=metadata.version,
            arguments=GetSystemStatusArguments(),
            reason="Collect simulated system status for the local Runtime check.",
            impact="No external impact; the Tool returns deterministic mock data.",
            verification=(
                "Confirm Tool identity, version, target, and explicitly simulated "
                "structured mock evidence."
            ),
            recovery="No rollback is required for a read-only mock operation.",
        )
        return ExecutionPlan(
            task_id=context.task_id,
            target=context.target,
            steps=(step,),
        )


__all__ = ["Planner", "SUPPORTED_REQUEST"]
