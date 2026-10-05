"""Policy service: decides authorization, never executes tools."""

from typing import Optional

from app.schemas.policy import PolicyDecision
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
        )

    if definition.requires_approval:
        return PolicyDecision(
            allowed=False,
            reason=f"Tool '{tool_name}' requires explicit approval.",
            requires_approval=True,
        )

    if not definition.read_only:
        return PolicyDecision(
            allowed=False,
            reason=f"Tool '{tool_name}' is not read-only.",
            requires_approval=False,
        )

    return PolicyDecision(
        allowed=True,
        reason=f"Tool '{tool_name}' is registered read-only without approval.",
        requires_approval=False,
    )
