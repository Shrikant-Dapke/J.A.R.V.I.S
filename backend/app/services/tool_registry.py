"""Tool registry: definitions only, never executes tools."""

import threading
from typing import Optional

from app.schemas.tool import ToolDefinition


class ToolRegistry:
    """Small registry for tool definitions (lookup only, no execution)."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._lock = threading.Lock()

    def register(self, definition: ToolDefinition) -> None:
        """
        Register a tool definition.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        with self._lock:
            if definition.name in self._tools:
                raise ValueError(
                    f"tool '{definition.name}' is already registered"
                )
            self._tools[definition.name] = definition

    def get(self, name: str) -> Optional[ToolDefinition]:
        """Retrieve a tool definition, or None if unknown."""
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """List all registered tool definitions."""
        with self._lock:
            return list(self._tools.values())

    def exists(self, name: str) -> bool:
        """Check whether a tool is registered."""
        with self._lock:
            return name in self._tools


tool_registry = ToolRegistry()
