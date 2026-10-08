"""Policy service: decides authorization, never executes tools."""

from typing import Optional

from app.schemas.policy import PolicyDecision, RiskLevel
from app.services.tool_registry import ToolRegistry, tool_registry


def evaluate_tool_policy(
    tool_name: str, registry: Optional[ToolRegistry] = None
) -> PolicyDecision:
    """
    Decide whether a requested tool may execute (fail-closed).

    1. Unknown tool -> DENY.
    2. Tool requiring approval -> DENY (no approval workflow yet).
    3. Non-read-only tool -> DENY.
    4. Registered read-only tool without approval -> ALLOW.

    This function performs no execution and no I/O beyond registry lookup.
    """
    lookup = registry if registry is not None else tool_registry
    definition = lookup.get(tool_name)

    if definition is None:
        return PolicyDecision(
            allowed=False,
            reason=f"Unknown tool '{tool_name}'.",
            requires_approval=False,
            tool_name=tool_name,
        )

    risk_level = definition.risk_level
    if risk_level is None:
        risk_level = RiskLevel.READ_ONLY if definition.read_only else RiskLevel.LOW_RISK

    if definition.requires_approval or risk_level in {
        RiskLevel.LOW_RISK,
        RiskLevel.HIGH_RISK,
    }:
        return PolicyDecision(
            allowed=False,
            reason=f"Tool '{tool_name}' requires explicit approval.",
            requires_approval=True,
            tool_name=tool_name,
            risk_level=risk_level,
        )

    if risk_level is not RiskLevel.READ_ONLY or not definition.read_only:
        return PolicyDecision(
            allowed=False,
            reason=f"Tool '{tool_name}' has an unsafe policy classification.",
            requires_approval=False,
            tool_name=tool_name,
            risk_level=risk_level,
        )

    return PolicyDecision(
        allowed=True,
        reason=f"Tool '{tool_name}' is registered read-only without approval.",
        requires_approval=False,
        tool_name=tool_name,
        risk_level=risk_level,
    )
