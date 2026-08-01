"""Execution plan models produced by Planner."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_server.models.system_status import GetSystemStatusArguments
from ai_server.models.tool import HashDigest, SemanticVersion, ToolId
from ai_server.models.verification import VerificationCriterion

_STRICT_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


class StepRole(StrEnum):
    """The lifecycle role of an execution step."""

    OBSERVE = "OBSERVE"
    ACTION = "ACTION"
    VERIFY = "VERIFY"


class ExecutionStep(BaseModel):
    """One explained, typed Tool call in an execution plan."""

    model_config = _STRICT_FROZEN_CONFIG

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

    model_config = _STRICT_FROZEN_CONFIG

    schema_version: Literal["2"] = "2"
    plan_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    target: Literal["local-mock"]
    steps: tuple[ExecutionStep, ...] = Field(min_length=1, max_length=64)
    verification_criteria: tuple[VerificationCriterion, ...] = Field(
        min_length=1,
        max_length=128,
    )

    @model_validator(mode="after")
    def validate_step_ids(self) -> Self:
        """Require ordered Steps and uniquely bound mandatory verification criteria."""
        step_ids = tuple(step.step_id for step in self.steps)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("ExecutionPlan step IDs must be unique")
        verification_started = False
        for step in self.steps:
            if step.role is StepRole.VERIFY:
                verification_started = True
            elif verification_started:
                raise ValueError("OBSERVE and ACTION steps cannot follow a VERIFY step")
        criterion_ids = tuple(criterion.criterion_id for criterion in self.verification_criteria)
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("ExecutionPlan verification criterion IDs must be unique")
        known_steps = set(step_ids)
        if any(
            criterion.evidence_step_id not in known_steps
            for criterion in self.verification_criteria
        ):
            raise ValueError("Verification criteria must reference planned evidence Steps")
        return self


__all__ = ["ExecutionPlan", "ExecutionStep", "StepRole"]
