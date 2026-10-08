"""Execution boundary for tools that have already been authorized."""

from typing import Any, Mapping, Optional

from app.schemas.tool import ToolResult
from app.services.tool_base import Tool
from app.services.tool_registry import ToolRegistry, UnknownToolError, tool_registry


class ToolNotImplementedError(ValueError):
    """Raised when metadata exists but no executable implementation is bound."""


class ToolExecutor:
    """Invoke registered implementations without making policy decisions."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def execute(
        self,
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
    ) -> ToolResult:
        """Execute one registered tool; callers must perform authorization first."""
        registered = self._registry.require(tool_name)
        if not isinstance(registered, Tool):
            raise ToolNotImplementedError(
                f"Tool '{tool_name}' has no registered implementation"
            )
        return registered.invoke(arguments)

default_tool_executor = ToolExecutor(tool_registry)


def execute_authorized_tool(
    tool_name: str,
    arguments: Optional[Mapping[str, Any]] = None,
    registry: Optional[ToolRegistry] = None,
) -> ToolResult:
    """Compatibility function for the internal authorized execution boundary."""
    executor = default_tool_executor if registry is None else ToolExecutor(registry)
    return executor.execute(tool_name, arguments)


__all__ = [
    "ToolExecutor",
    "ToolNotImplementedError",
    "UnknownToolError",
    "execute_authorized_tool",
]
