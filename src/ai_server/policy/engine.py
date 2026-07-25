"""Fail-closed Phase 0 Policy."""

from collections.abc import Mapping

from pydantic import ValidationError

from ai_server.models.execution import ExecutionPlan
from ai_server.models.tool import RiskLevel, ToolMetadata
from ai_server.runtime.errors import PolicyDeniedError

ToolKey = tuple[str, str]


class PolicyEngine:
    """Allow only registered L0 Tools with metadata-owned risk."""

    def check(
        self,
        plan: ExecutionPlan,
        catalog: Mapping[ToolKey, ToolMetadata],
    ) -> None:
        """Raise an explicit denial unless every planned Tool is registered L0."""
        try:
            if not isinstance(plan, ExecutionPlan):
                raise TypeError
            plan = ExecutionPlan.model_validate(
                plan.model_dump(mode="python", warnings="none"),
                strict=True,
            )
        except (TypeError, ValidationError):
            raise PolicyDeniedError("Policy denied a malformed execution plan") from None

        if not plan.steps:
            raise PolicyDeniedError("Policy denied an empty execution plan")

        for step in plan.steps:
            key = (step.tool_name, step.tool_version)
            metadata = catalog.get(key)
            if metadata is None:
                raise PolicyDeniedError("Policy denied an unregistered Tool identity")
            try:
                metadata = ToolMetadata.model_validate(
                    metadata.model_dump(mode="python", warnings="none"),
                    strict=True,
                )
            except (AttributeError, TypeError, ValidationError):
                raise PolicyDeniedError("Policy denied malformed Tool metadata") from None
            if metadata.risk_level is not RiskLevel.L0:
                raise PolicyDeniedError("Phase 0 Policy allows only registered L0 Tools")


__all__ = ["PolicyEngine", "ToolKey"]
