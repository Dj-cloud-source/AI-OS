"""Task lifecycle model."""

from itertools import pairwise
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_server.runtime.state import RuntimeState, RuntimeStateMachine


class Task(BaseModel):
    """An immutable user task and its Runtime-managed state history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID = Field(default_factory=uuid4)
    request: str = Field(min_length=1)
    user: Literal["local-user"] = "local-user"
    target: Literal["local-mock"] = "local-mock"
    state: RuntimeState = RuntimeState.RECEIVED
    state_history: tuple[RuntimeState, ...] = (RuntimeState.RECEIVED,)

    @model_validator(mode="after")
    def validate_state_history(self) -> Self:
        """Ensure current state and history cannot contradict each other."""
        if not self.state_history:
            raise ValueError("state_history must not be empty")
        if self.state_history[0] is not RuntimeState.RECEIVED:
            raise ValueError("state_history must start with RECEIVED")
        if self.state_history[-1] is not self.state:
            raise ValueError("state must match the last state_history entry")
        for current, target in pairwise(self.state_history):
            if not RuntimeStateMachine.can_transition(current, target):
                raise ValueError(
                    f"state_history contains invalid transition: {current.value} -> {target.value}"
                )
        return self


__all__ = ["Task"]
