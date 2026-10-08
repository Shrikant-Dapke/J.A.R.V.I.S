"""System-info endpoint (thin route, logic lives in tool/service layer)."""

from fastapi import APIRouter, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.tool import ToolResult
from app.services.system_info_tool import TOOL_NAME
from app.services.tool_executor import execute_authorized_tool
from app.services.tool_registry import tool_registry


router = APIRouter(tags=["tools"])


@router.get("/api/system-info", response_model=ToolResult, status_code=200)
async def system_info() -> ToolResult:
    """
    Invoke the read-only get_system_info stub through the tool boundary.

    The route contains no tool implementation logic.
    """
    definition = tool_registry.get(TOOL_NAME)
    if definition is None:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Tool '{TOOL_NAME}' is not registered",
        )
    if not definition.read_only:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Tool '{TOOL_NAME}' is not read-only",
        )
    return execute_authorized_tool(TOOL_NAME)
