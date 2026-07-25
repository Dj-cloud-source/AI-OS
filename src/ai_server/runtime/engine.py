"""Phase 0 Runtime orchestration."""

import json
import logging
from types import MappingProxyType
from typing import Literal

from pydantic import ValidationError

from ai_server.context.builder import ContextBuilder
from ai_server.executor.service import Executor
from ai_server.models.execution import ExecutionPlan
from ai_server.models.system_status import SystemStatus
from ai_server.models.task import Task
from ai_server.models.tool import ToolMetadata, ToolResult
from ai_server.planner.service import Planner
from ai_server.policy.engine import PolicyEngine, ToolKey
from ai_server.runtime.errors import (
    InvalidTaskError,
    PlanMismatchError,
    PolicyDeniedError,
    ToolExecutionError,
    VerificationError,
)
from ai_server.runtime.state import RuntimeState, RuntimeStateMachine
from ai_server.tools.get_system_status import GET_SYSTEM_STATUS_METADATA
from ai_server.verifier.service import Verifier

logger = logging.getLogger(__name__)


class RuntimeEngine:
    """Own Task state and orchestrate the five concrete Phase 0 components."""

    def __init__(
        self,
        *,
        context_builder: ContextBuilder | None = None,
        planner: Planner | None = None,
        policy: PolicyEngine | None = None,
        executor: Executor | None = None,
        verifier: Verifier | None = None,
        tool_metadata: ToolMetadata = GET_SYSTEM_STATUS_METADATA,
    ) -> None:
        """Compose the concrete local-only Phase 0 Runtime."""
        self._context_builder = context_builder or ContextBuilder()
        self._planner = planner or Planner()
        self._policy = policy or PolicyEngine()
        self._executor = executor or Executor()
        self._verifier = verifier or Verifier()
        try:
            self._tool_metadata = ToolMetadata.model_validate(
                tool_metadata.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except (AttributeError, TypeError, ValidationError):
            raise PolicyDeniedError("Runtime rejected malformed Tool metadata") from None
        catalog: dict[ToolKey, ToolMetadata] = {
            (
                self._tool_metadata.name,
                self._tool_metadata.version,
            ): self._tool_metadata
        }
        self._catalog = MappingProxyType(catalog)

    def run(self, task: Task) -> Task:
        """Run the safe L0 mock lifecycle and return the completed immutable Task."""
        task = self._validate_task(task)
        task = self._transition(task, RuntimeState.CONTEXT_BUILDING)
        context = self._context_builder.build(task)

        task = self._transition(task, RuntimeState.PLANNING)
        raw_plan = self._planner.create_plan(context, self._tool_metadata)
        plan = self._validate_plan(raw_plan)
        if plan.task_id != task.task_id or plan.target != task.target:
            raise PlanMismatchError("Planner returned a plan for a different Task or target")

        task = self._transition(task, RuntimeState.POLICY_CHECK)
        self._policy.check(plan, self._catalog)

        task = self._transition(task, RuntimeState.EXECUTING)
        try:
            results = self._executor.execute(plan)
        except ToolExecutionError:
            self._log_execution_audit(
                task,
                plan,
                (),
                result_override="execution_failed",
                verification="not_run",
            )
            raise

        task = self._transition(task, RuntimeState.VERIFYING)
        try:
            self._verifier.verify(plan, results)
        except VerificationError:
            self._log_execution_audit(
                task,
                plan,
                results,
                verification="failed",
            )
            raise
        self._log_execution_audit(
            task,
            plan,
            results,
            verification="passed",
        )

        return self._transition(task, RuntimeState.COMPLETED)

    @staticmethod
    def _validate_task(task: Task) -> Task:
        try:
            if not isinstance(task, Task):
                raise TypeError
            return Task.model_validate(
                task.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except (TypeError, ValidationError):
            raise InvalidTaskError("Runtime rejected malformed Task input") from None

    @staticmethod
    def _validate_plan(plan: ExecutionPlan) -> ExecutionPlan:
        try:
            if not isinstance(plan, ExecutionPlan):
                raise TypeError
            return ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except (TypeError, ValidationError):
            raise PlanMismatchError("Planner returned a malformed execution plan") from None

    @staticmethod
    def _log_execution_audit(
        task: Task,
        plan: ExecutionPlan,
        results: tuple[ToolResult[SystemStatus], ...],
        *,
        verification: Literal["passed", "failed", "not_run"],
        result_override: Literal["execution_failed"] | None = None,
    ) -> None:
        for index, step in enumerate(plan.steps):
            result = results[index] if index < len(results) else None
            result_status: Literal["success", "invalid", "execution_failed"]
            duration_ms: int | None
            if result_override is not None:
                result_status = result_override
                duration_ms = None
            elif isinstance(result, ToolResult):
                result_status = "success"
                duration_ms = result.duration_ms
            else:
                result_status = "invalid"
                duration_ms = None
            logger.info(
                json.dumps(
                    {
                        "event": "execution_audit",
                        "task_id": str(task.task_id),
                        "plan_id": str(plan.plan_id),
                        "approval_id": None,
                        "operator": task.user,
                        "user": task.user,
                        "target": task.target,
                        "tool": step.tool_name,
                        "tool_version": step.tool_version,
                        "arguments": step.arguments.model_dump(mode="json"),
                        "result": result_status,
                        "duration_ms": duration_ms,
                        "verification": verification,
                    },
                    sort_keys=True,
                )
            )

    @staticmethod
    def _transition(task: Task, target: RuntimeState) -> Task:
        current = task.state
        next_state = RuntimeStateMachine.transition(current, target)
        updated = task.model_copy(
            update={
                "state": next_state,
                "state_history": (*task.state_history, next_state),
            }
        )
        logger.info(
            json.dumps(
                {
                    "event": "runtime_state_transition",
                    "task_id": str(task.task_id),
                    "user": task.user,
                    "target": task.target,
                    "from_state": current.value,
                    "to_state": next_state.value,
                },
                sort_keys=True,
            )
        )
        return updated


def create_mock_runtime() -> RuntimeEngine:
    """Create the concrete local-only Phase 0 Runtime."""
    return RuntimeEngine()


__all__ = ["RuntimeEngine", "create_mock_runtime"]
