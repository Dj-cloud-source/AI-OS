"""Typed Tool metadata, result, and error contracts."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(StrEnum):
    """Static Tool risk levels."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class ToolMetadata(BaseModel):
    """Immutable metadata that is the sole authority for Tool risk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    risk_level: RiskLevel
    timeout_seconds: float = Field(gt=0)
    idempotent: bool
    input_model: str = Field(min_length=1)
    output_model: str = Field(min_length=1)


class ToolResult[PayloadT: BaseModel](BaseModel):
    """A structured Tool outcome with typed payload data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    success: Literal[True] = True
    data: PayloadT
    duration_ms: int = Field(ge=0)


__all__ = ["RiskLevel", "ToolMetadata", "ToolResult"]
