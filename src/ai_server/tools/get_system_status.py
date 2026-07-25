"""Deterministic mock system-status Tool."""

from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.tool import RiskLevel, ToolMetadata, ToolResult

GET_SYSTEM_STATUS_METADATA = ToolMetadata(
    name="get_system_status",
    version="1.0.0",
    description="Return deterministic simulated system status.",
    risk_level=RiskLevel.L0,
    timeout_seconds=1.0,
    idempotent=True,
    input_model="GetSystemStatusArguments",
    output_model="SystemStatus",
)


def get_system_status(arguments: GetSystemStatusArguments) -> ToolResult[SystemStatus]:
    """Return deterministic mock data without inspecting the local machine."""
    status = SystemStatus(
        target=arguments.target,
        cpu_percent=12.5,
        memory_percent=34.0,
        disk_percent=45.5,
        services=(ServiceStatus(name="mock-api", state="running"),),
    )
    return ToolResult[SystemStatus](
        tool_name=GET_SYSTEM_STATUS_METADATA.name,
        tool_version=GET_SYSTEM_STATUS_METADATA.version,
        success=True,
        data=status,
        duration_ms=0,
    )


__all__ = [
    "GET_SYSTEM_STATUS_METADATA",
    "GetSystemStatusArguments",
    "ServiceStatus",
    "SystemStatus",
    "get_system_status",
]
