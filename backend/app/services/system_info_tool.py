"""Deterministic read-only get_system_info stub (no machine inspection).

Returns fixed placeholder values only. Real system inspection is
explicitly out of scope for this phase.
"""

from app.schemas.tool import ToolDefinition, ToolResult
from app.services.tool_registry import tool_registry


TOOL_NAME = "get_system_info"

TOOL_DEFINITION = ToolDefinition(
    name=TOOL_NAME,
    description="Return bounded placeholder system information (stub).",
    read_only=True,
    requires_approval=False,
)

PLACEHOLDER_PAYLOAD: dict[str, str] = {
    "os": "Windows",
    "platform": "placeholder",
    "hostname": "placeholder",
    "architecture": "x64",
}


def get_system_info() -> ToolResult:
    """Return the fixed placeholder system-information result."""
    return ToolResult(
        tool_name=TOOL_NAME,
        status="success",
        payload=dict(PLACEHOLDER_PAYLOAD),
        error=None,
    )


if not tool_registry.exists(TOOL_NAME):
    tool_registry.register(TOOL_DEFINITION)
