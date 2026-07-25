"""Pure Phase 0 context builder."""

from ai_server.models.context import RuntimeContext
from ai_server.models.task import Task


class ContextBuilder:
    """Build local context from Task data without performing I/O."""

    def build(self, task: Task) -> RuntimeContext:
        """Build deterministic local-only context for Planner."""
        return RuntimeContext(
            task_id=task.task_id,
            request=task.request,
            user=task.user,
            target=task.target,
        )


__all__ = ["ContextBuilder"]
