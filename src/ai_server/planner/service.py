"""Deterministic local-only Planner for the Phase 1 Runtime."""

from uuid import UUID

from ai_server.models.context import RuntimeContext
from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.system_status import GetSystemStatusArguments
from ai_server.models.tool import ToolMetadata
from ai_server.runtime.errors import InvalidTaskError, UnsupportedTaskError

SUPPORTED_REQUEST = "get_system_status"
GET_SYSTEM_STATUS_STEP = ExecutionStep(
    step_id="get-system-status",
    role=StepRole.OBSERVE,
    tool_name="get_system_status",
    tool_version="1.0.0",
    arguments=GetSystemStatusArguments(),
    reason="Collect simulated system status for the local Runtime check.",
    impact="No external impact; the Tool returns deterministic mock data.",
    verification=(
        "Confirm Tool identity, version, target, and explicitly simulated structured mock evidence."
    ),
    recovery="No rollback is required for a read-only mock operation.",
)


class Planner:
    """Create an explained plan without invoking operational capabilities."""

    def create_plan(
        self,
        context: RuntimeContext,
        metadata: ToolMetadata,
    ) -> ExecutionPlan:
        """Create the one supported Phase 1 mock execution plan."""
        try:
            if type(context) is not RuntimeContext or type(context.task_id) is not UUID:
                raise TypeError
            context = RuntimeContext.model_validate(
                context.model_dump(mode="python", warnings="none"),
                strict=True,
            )
            if type(context.task_id) is not UUID:
                raise TypeError
        except Exception:
            raise InvalidTaskError("Planner rejected malformed Runtime context") from None

        if context.request != SUPPORTED_REQUEST:
            raise UnsupportedTaskError("Unsupported Phase 1 task request")

        try:
            if type(metadata) is not ToolMetadata:
                raise TypeError
            metadata = ToolMetadata.model_validate(
                metadata.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except Exception:
            raise UnsupportedTaskError("Planner rejected malformed Tool metadata") from None

        if (
            metadata.name != GET_SYSTEM_STATUS_STEP.tool_name
            or metadata.version != GET_SYSTEM_STATUS_STEP.tool_version
        ):
            raise UnsupportedTaskError("Unsupported Phase 1 Tool metadata")
        try:
            return ExecutionPlan(
                task_id=context.task_id,
                target=context.target,
                steps=(GET_SYSTEM_STATUS_STEP,),
            )
        except Exception:
            raise UnsupportedTaskError("Planner could not create a valid execution plan") from None


__all__ = ["GET_SYSTEM_STATUS_STEP", "Planner", "SUPPORTED_REQUEST"]
