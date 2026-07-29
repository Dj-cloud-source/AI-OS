"""Execution plan models produced by Planner."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_server.models.system_status import GetSystemStatusArguments
from ai_server.models.tool import HashDigest, SemanticVersion, ToolId


class StepRole(StrEnum):
    """The lifecycle role of an execution step."""

    OBSERVE = "OBSERVE"
    ACTION = "ACTION"
    VERIFY = "VERIFY"


class ExecutionStep(BaseModel):
    """One explained, typed Tool call in an execution plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    role: StepRole
    tool_id: ToolId
    tool_version: SemanticVersion
    contract_hash: HashDigest
    implementation_hash: HashDigest
    arguments: GetSystemStatusArguments
    reason: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    verification: str = Field(min_length=1)
    recovery: str = Field(min_length=1)


class ExecutionPlan(BaseModel):
    """An ordered immutable collection of explained execution steps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    plan_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    target: Literal["local-mock"]
    steps: tuple[ExecutionStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_step_ids(self) -> Self:
        """Require unique step IDs in their declared order."""
        step_ids = tuple(step.step_id for step in self.steps)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("ExecutionPlan step IDs must be unique")
        return self


__all__ = ["ExecutionPlan", "ExecutionStep", "StepRole"]
