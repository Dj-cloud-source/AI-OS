"""Deterministic, fail-closed policy checks for the Phase 1 Runtime."""

from collections.abc import Mapping
from types import MappingProxyType

from ai_server.models.execution import ExecutionPlan, ExecutionStep
from ai_server.models.system_status import GetSystemStatusArguments
from ai_server.models.tool import RiskLevel, ToolMetadata
from ai_server.runtime.errors import ApprovalRequiredError, PolicyDeniedError

ToolKey = tuple[str, str]


class PolicyEngine:
    """Apply Phase 1 risk behavior using registered Tool metadata only."""

    def check(
        self,
        plan: ExecutionPlan,
        catalog: Mapping[ToolKey, ToolMetadata],
    ) -> None:
        """Allow L0, deny L1, and pause L2/L3 plans for future approval."""
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
            raise PolicyDeniedError("Policy denied a malformed execution plan") from None

        if not plan.steps:
            raise PolicyDeniedError("Policy denied an empty execution plan")

        if type(catalog) not in {dict, MappingProxyType}:
            raise PolicyDeniedError("Policy denied a malformed Tool catalog")

        resolved_risks: list[RiskLevel] = []
        for step in plan.steps:
            key = (step.tool_id, step.tool_version)
            try:
                metadata = catalog.get(key)
            except Exception:
                raise PolicyDeniedError("Policy denied a malformed Tool catalog") from None
            if metadata is None:
                raise PolicyDeniedError("Policy denied an unregistered Tool identity")
            try:
                if type(metadata) is not ToolMetadata:
                    raise TypeError
                metadata = ToolMetadata.model_validate(
                    metadata.model_dump(mode="python", warnings="none"),
                    strict=True,
                )
            except Exception:
                raise PolicyDeniedError("Policy denied malformed Tool metadata") from None

            if (metadata.tool_id, metadata.version) != key:
                raise PolicyDeniedError("Policy denied mismatched Tool metadata")
            if (
                step.contract_hash != metadata.contract_hash
                or step.implementation_hash != metadata.implementation_hash
            ):
                raise PolicyDeniedError("Policy denied mismatched Tool integrity hashes")

            resolved_risks.append(metadata.risk_level)

        if RiskLevel.L1 in resolved_risks:
            raise PolicyDeniedError("Phase 1 Policy denied an L1 Tool")
        if any(risk in (RiskLevel.L2, RiskLevel.L3) for risk in resolved_risks):
            raise ApprovalRequiredError("Phase 1 Policy requires approval for an L2/L3 Tool")


__all__ = ["PolicyEngine", "ToolKey"]
