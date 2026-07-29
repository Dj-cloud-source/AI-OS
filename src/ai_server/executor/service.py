"""Executor that dispatches exact planned calls through the Tool Gateway."""

from collections.abc import Callable
from uuid import UUID, uuid4

from pydantic import BaseModel

from ai_server.models.execution import ExecutionPlan, ExecutionStep, StepRole
from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.tool import TargetReference, ToolCall, ToolResult
from ai_server.runtime.errors import ToolExecutionError
from ai_server.tools.gateway import ToolGateway, ToolGatewayError
from ai_server.tools.hashing import CanonicalizationError, canonical_json_sha256

InvocationIdFactory = Callable[[], UUID]


class Executor:
    """Translate an approved immutable Plan into exact Tool Gateway calls."""

    def __init__(
        self,
        gateway: ToolGateway,
        *,
        invocation_id_factory: InvocationIdFactory = uuid4,
    ) -> None:
        """Bind the only production Tool invocation boundary."""
        if type(gateway) is not ToolGateway or not callable(invocation_id_factory):
            raise ToolExecutionError("Executor configuration is malformed")
        self._gateway = gateway
        self._invocation_id_factory = invocation_id_factory

    def execute(self, plan: ExecutionPlan) -> tuple[ToolResult[SystemStatus], ...]:
        """Dispatch supported steps once, stopping after a structured failure."""
        trusted_plan = _validate_plan(plan)
        results: list[ToolResult[SystemStatus]] = []
        for step in trusted_plan.steps:
            call = self._build_call(trusted_plan, step)
            try:
                raw_result = self._gateway.invoke(call)
            except ToolGatewayError:
                raise ToolExecutionError(
                    "Tool Gateway rejected the planned invocation safely"
                ) from None
            except BaseException:
                raise ToolExecutionError("Tool invocation failed safely") from None
            result = _validate_result(raw_result, call)
            results.append(result)
            if not result.success:
                break
        return tuple(results)

    def _build_call(
        self,
        plan: ExecutionPlan,
        step: ExecutionStep,
    ) -> ToolCall[GetSystemStatusArguments]:
        if step.role is not StepRole.OBSERVE or step.arguments.target != plan.target:
            raise ToolExecutionError("Executor received an unsupported planned Tool")
        try:
            invocation_id = self._invocation_id_factory()
            if type(invocation_id) is not UUID:
                raise TypeError
            arguments_hash = canonical_json_sha256(step.arguments)
            return ToolCall[GetSystemStatusArguments](
                invocation_id=invocation_id,
                plan_step_id=step.step_id,
                tool_id=step.tool_id,
                tool_version=step.tool_version,
                contract_hash=step.contract_hash,
                implementation_hash=step.implementation_hash,
                arguments_hash=arguments_hash,
                target=TargetReference(
                    target_id=plan.target,
                    resource_type="local_system",
                    resource_id=step.arguments.target,
                ),
                arguments=step.arguments,
            )
        except (CanonicalizationError, TypeError, ValueError):
            raise ToolExecutionError("Executor could not build a trusted ToolCall") from None
        except BaseException:
            raise ToolExecutionError("Executor could not build a trusted ToolCall") from None


def _validate_plan(plan: ExecutionPlan) -> ExecutionPlan:
    try:
        if (
            type(plan) is not ExecutionPlan
            or type(plan.steps) is not tuple
            or any(
                type(step) is not ExecutionStep
                or type(step.arguments) is not GetSystemStatusArguments
                for step in plan.steps
            )
        ):
            raise TypeError
        validated = ExecutionPlan.model_validate(
            plan.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise ToolExecutionError("Executor received a malformed plan") from None
    if not validated.steps:
        raise ToolExecutionError("Executor received an empty plan")
    return validated


def _validate_result(
    raw_result: ToolResult[BaseModel],
    call: ToolCall[GetSystemStatusArguments],
) -> ToolResult[SystemStatus]:
    try:
        if (
            not isinstance(raw_result, ToolResult)
            or (raw_result.data is not None and type(raw_result.data) is not SystemStatus)
            or (
                type(raw_result.data) is SystemStatus
                and (
                    type(raw_result.data.services) is not tuple
                    or any(
                        type(service) is not ServiceStatus for service in raw_result.data.services
                    )
                )
            )
        ):
            raise TypeError
        result = ToolResult[SystemStatus].model_validate(
            raw_result.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except BaseException:
        raise ToolExecutionError("Tool Gateway returned malformed evidence") from None
    if (
        result.invocation_id != call.invocation_id
        or result.plan_step_id != call.plan_step_id
        or result.tool_id != call.tool_id
        or result.tool_version != call.tool_version
        or result.contract_hash != call.contract_hash
        or result.arguments_hash != call.arguments_hash
        or result.target != call.target
    ):
        raise ToolExecutionError("ToolResult identity does not match the planned invocation")
    return result


__all__ = ["Executor", "InvocationIdFactory"]
