"""Tool invocation endpoint (policy gate, thin route, no tool logic)."""

from typing import Optional

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.invocation import ToolInvokeRequest
from app.schemas.approval import ApprovalRequest
from app.schemas.tool import ToolResult
from app.services.approval_service import (
    ApprovalArgumentsError,
    ApprovalWorkflowError,
    approval_service,
)
from app.services.tool_registry import UnknownToolError, tool_registry


router = APIRouter(tags=["tools"])


@router.post(
    "/api/tools/{name}/invoke",
    response_model=ToolResult | ApprovalRequest,
    status_code=200,
    responses={202: {"model": ApprovalRequest}},
)
async def invoke_tool(
    name: str, request: Optional[ToolInvokeRequest] = None
) -> ToolResult | JSONResponse:
    """
    Validate -> registry lookup -> policy decision -> execution boundary.

    Only policy-allowed tools reach the executor. Arguments are accepted as
    bounded structured data and validated by the selected tool.
    """
    if tool_registry.get(name) is None:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown tool '{name}'",
        )

    arguments = request.arguments if request is not None else None
    try:
        result = approval_service.request_tool(name, arguments)
    except UnknownToolError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ApprovalArgumentsError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except ApprovalWorkflowError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )

    if isinstance(result, ApprovalRequest):
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=result.model_dump(mode="json"),
        )
    return result
