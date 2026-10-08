"""Production tool catalog registration."""

from app.services.create_directory_tool import CREATE_DIRECTORY_TOOL
from app.services.open_application_tool import OPEN_APPLICATION_TOOL
from app.services.system_info_tool import SYSTEM_INFO_TOOL
from app.services.tool_base import Tool
from app.services.tool_registry import ToolRegistry


BUILTIN_TOOLS: tuple[Tool, ...] = (
    SYSTEM_INFO_TOOL,
    OPEN_APPLICATION_TOOL,
    CREATE_DIRECTORY_TOOL,
)


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register each built-in exactly once in the supplied registry."""
    for tool in BUILTIN_TOOLS:
        if not registry.exists(tool.name):
            registry.register(tool)
