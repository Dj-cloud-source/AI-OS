"""Deterministic mock system-status Tool."""

from typing import ClassVar

from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)


class InvalidSystemStatusArgumentsError(ValueError):
    """Raised when the Mock Tool receives malformed typed arguments."""

    code: ClassVar[str] = "invalid_system_status_arguments"


class GetSystemStatusTool:
    """Return deterministic simulated status without inspecting any system."""

    def invoke(self, arguments: GetSystemStatusArguments) -> SystemStatus:
        """Validate typed arguments and return payload data only."""
        try:
            if type(arguments) is not GetSystemStatusArguments:
                raise TypeError
            validated_arguments = GetSystemStatusArguments.model_validate(
                arguments.model_dump(mode="python", warnings="error"),
                strict=True,
            )
        except BaseException:
            raise InvalidSystemStatusArgumentsError(
                "Mock Tool rejected malformed arguments"
            ) from None

        return SystemStatus(
            target=validated_arguments.target,
            cpu_percent=12.5,
            memory_percent=34.0,
            disk_percent=45.5,
            services=(ServiceStatus(name="mock-api", state="running"),),
        )


__all__ = [
    "GetSystemStatusTool",
    "InvalidSystemStatusArgumentsError",
]
