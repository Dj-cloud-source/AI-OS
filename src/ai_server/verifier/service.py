"""Deterministic Phase 0 Verifier."""

from ai_server.models.execution import ExecutionPlan, ExecutionStep
from ai_server.models.system_status import (
    GetSystemStatusArguments,
    ServiceStatus,
    SystemStatus,
)
from ai_server.models.tool import TargetReference, ToolResult
from ai_server.runtime.errors import VerificationError
from ai_server.tools.hashing import canonical_json_sha256


class Verifier:
    """Verify existing ToolResults without invoking a Tool."""

    def verify(
        self,
        plan: ExecutionPlan,
        results: tuple[ToolResult[SystemStatus], ...],
    ) -> None:
        """Raise an explicit error unless all mock evidence matches the plan."""
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
            plan = ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except Exception:
            raise VerificationError("Verification received a malformed plan") from None

        if not plan.steps:
            raise VerificationError("Verification cannot accept an empty plan")
        if type(results) is not tuple:
            raise VerificationError("Verification evidence container is malformed")
        if len(results) != len(plan.steps):
            raise VerificationError("Verification evidence count does not match the plan")

        for step, raw_result in zip(plan.steps, results, strict=True):
            try:
                if (
                    not isinstance(raw_result, ToolResult)
                    or not raw_result.success
                    or type(raw_result.data) is not SystemStatus
                    or type(raw_result.data.services) is not tuple
                    or any(
                        type(service) is not ServiceStatus for service in raw_result.data.services
                    )
                ):
                    raise VerificationError("Verification evidence is malformed")
                result = ToolResult[SystemStatus].model_validate(
                    raw_result.model_dump(mode="python", warnings="none"),
                    strict=True,
                )
            except VerificationError:
                raise
            except Exception:
                raise VerificationError("Verification evidence is malformed") from None

            if (
                result.plan_step_id != step.step_id
                or result.tool_id != step.tool_id
                or result.tool_version != step.tool_version
                or result.contract_hash != step.contract_hash
                or result.arguments_hash != canonical_json_sha256(step.arguments)
            ):
                raise VerificationError("Verification evidence identity does not match the plan")
            expected_target = TargetReference(
                target_id=plan.target,
                resource_type="local_system",
                resource_id=step.arguments.target,
            )
            if result.target != expected_target:
                raise VerificationError("Verification evidence target does not match the plan")
            if result.data is None:
                raise VerificationError("Verification evidence is missing payload data")
            if result.data.target != plan.target:
                raise VerificationError("Verification evidence target does not match the plan")
            if not result.data.simulated or result.data.source != "mock":
                raise VerificationError("Verification evidence is not explicitly simulated")


__all__ = ["Verifier"]
