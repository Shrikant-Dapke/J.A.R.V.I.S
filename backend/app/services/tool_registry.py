"""Central registry for controlled tool implementations and metadata."""

import threading
from typing import Optional, Union

from app.schemas.tool import ToolDefinition
from app.services.tool_base import Tool


RegisteredTool = Union[Tool, ToolDefinition]


class UnknownToolError(ValueError):
    """Raised when a caller requests a tool that is not registered."""


class ToolRegistry:
    """Thread-safe registry with duplicate and unknown-tool protection."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._lock = threading.Lock()

    def register(self, tool: RegisteredTool) -> None:
        """
        Register a tool implementation or metadata-only definition.

        Metadata-only definitions remain supported for policy-only test doubles
        and future tools that have not yet been implemented.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        if not isinstance(tool, (Tool, ToolDefinition)):
            raise TypeError("tool must be a Tool or ToolDefinition")

        name = tool.name
        with self._lock:
            if name in self._tools:
                raise ValueError(f"tool '{name}' is already registered")
            self._tools[name] = tool

    def get(self, name: str) -> Optional[RegisteredTool]:
        """Retrieve a registered tool or definition, or None if unknown."""
        with self._lock:
            return self._tools.get(name)

    def require(self, name: str) -> RegisteredTool:
        """Retrieve a registered item or reject an unknown name."""
        tool = self.get(name)
        if tool is None:
            raise UnknownToolError(f"Tool '{name}' is not registered")
        return tool

    def get_definition(self, name: str) -> Optional[ToolDefinition]:
        """Return normalized metadata for a registered item."""
        tool = self.get(name)
        if tool is None:
            return None
        return tool.definition if isinstance(tool, Tool) else tool

    def list_tools(self) -> list[RegisteredTool]:
        """List all registered implementations and metadata definitions."""
        with self._lock:
            return list(self._tools.values())

    def exists(self, name: str) -> bool:
        """Check whether a tool is registered."""
        with self._lock:
            return name in self._tools


tool_registry = ToolRegistry()

# Importing the registry makes the production catalog available to policy and
# executor callers without coupling either one to a specific tool module.
from app.services.builtin_tools import register_builtin_tools  # noqa: E402

register_builtin_tools(tool_registry)
