"""Structured context supplied to Planner."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RuntimeContext(BaseModel):
    """Pure local context built without operational I/O."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    request: str = Field(min_length=1)
    user: Literal["local-user"]
    target: Literal["local-mock"]
    source: Literal["task"] = "task"
    simulated: Literal[True] = True


__all__ = ["RuntimeContext"]
