"""Typed models for the simulated system-status Tool."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GetSystemStatusArguments(BaseModel):
    """Validated arguments constrained to the fixed local mock target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Literal["local-mock"] = "local-mock"


class ServiceStatus(BaseModel):
    """Simulated service state returned by the Mock Tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    state: Literal["running", "stopped"]


class SystemStatus(BaseModel):
    """Typed and explicitly simulated system status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["mock"] = "mock"
    simulated: Literal[True] = True
    target: Literal["local-mock"] = "local-mock"
    hostname: Literal["mock-server"] = "mock-server"
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    disk_percent: float = Field(ge=0, le=100)
    services: tuple[ServiceStatus, ...]


__all__ = ["GetSystemStatusArguments", "ServiceStatus", "SystemStatus"]
