"""Execution boundary: runs already-authorized tools, decides nothing.

Callers MUST evaluate policy first. This module performs no
authorization, no registry lookup, and no dynamic dispatch:
every executable tool has an explicit branch below.
"""

from app.schemas.tool import ToolResult
from app.services.system_info_tool import TOOL_NAME as SYSTEM_INFO_TOOL
from app.services.system_info_tool import get_system_info


def execute_authorized_tool(tool_name: str) -> ToolResult:
    """
    Execute an already-authorized registered tool.

    Raises:
        ValueError: If the tool has no registered implementation.
    """
    if tool_name == SYSTEM_INFO_TOOL:
        return get_system_info()

    raise ValueError(f"Tool '{tool_name}' has no registered implementation")
