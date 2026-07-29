"""Explicit local-only bootstrap for the reviewed Phase 2 Tool catalog."""

from ai_server.models.system_status import GetSystemStatusArguments, SystemStatus
from ai_server.tools.get_system_status import GetSystemStatusTool
from ai_server.tools.registry import ToolDefinition, ToolRegistry

GET_SYSTEM_STATUS_TOOL_ID = "get_system_status"
GET_SYSTEM_STATUS_TOOL_VERSION = "1.0.0"


def build_default_registry() -> ToolRegistry:
    """Register the reviewed deterministic Mock Tool and freeze the catalog."""
    tool = GetSystemStatusTool()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            tool_id=GET_SYSTEM_STATUS_TOOL_ID,
            version=GET_SYSTEM_STATUS_TOOL_VERSION,
            input_model=GetSystemStatusArguments,
            output_model=SystemStatus,
            handler=tool.invoke,
        )
    )
    registry.freeze()
    return registry


__all__ = [
    "GET_SYSTEM_STATUS_TOOL_ID",
    "GET_SYSTEM_STATUS_TOOL_VERSION",
    "build_default_registry",
]
