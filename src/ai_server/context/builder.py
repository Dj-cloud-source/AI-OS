"""Pure local-only context builder for the Phase 1 Runtime."""

from uuid import UUID

from ai_server.models.context import RuntimeContext
from ai_server.models.task import Task
from ai_server.runtime.errors import InvalidTaskError
from ai_server.runtime.state import RuntimeState


class ContextBuilder:
    """Build local context from Task data without performing I/O."""

    def build(self, task: Task) -> RuntimeContext:
        """Build deterministic local-only context for Planner."""
        try:
            if (
                type(task) is not Task
                or type(task.task_id) is not UUID
                or type(task.state_history) is not tuple
                or any(type(state) is not RuntimeState for state in task.state_history)
            ):
                raise TypeError
            validated = Task.model_validate(
                task.model_dump(mode="python", warnings="none"),
                strict=True,
            )
            if type(validated.task_id) is not UUID:
                raise TypeError
            return RuntimeContext(
                task_id=validated.task_id,
                request=validated.request,
                user=validated.user,
                target=validated.target,
            )
        except Exception:
            raise InvalidTaskError("Context Builder rejected malformed Task input") from None


__all__ = ["ContextBuilder"]
