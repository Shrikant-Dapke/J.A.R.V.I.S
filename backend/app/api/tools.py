"""Tool invocation endpoint (policy gate, thin route, no tool logic)."""

from typing import Optional

from fastapi import APIRouter, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.invocation import ToolInvokeRequest
from app.schemas.tool import ToolResult
from app.services.policy_service import evaluate_tool_policy
from app.services.tool_executor import execute_authorized_tool
from app.services.tool_registry import tool_registry


router = APIRouter(tags=["tools"])


@router.post(
    "/api/tools/{name}/invoke",
    response_model=ToolResult,
    status_code=200,
)
async def invoke_tool(
    name: str, request: Optional[ToolInvokeRequest] = None
) -> ToolResult:
    """
    Validate -> registry lookup -> policy decision -> execution boundary.

    Only policy-allowed tools reach the executor. Arguments are accepted
    as bounded structured data; get_system_info ignores them (takes none).
    """
    if tool_registry.get(name) is None:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown tool '{name}'",
        )

    decision = evaluate_tool_policy(name)
    if not decision.allowed:
        if decision.requires_approval:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Tool '{name}' requires explicit approval",
            )
        raise StarletteHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tool '{name}' is not permitted: {decision.reason}",
        )

    arguments = request.arguments if request is not None else None
    return execute_authorized_tool(name, arguments)
