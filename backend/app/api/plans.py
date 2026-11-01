"""Task-plan endpoints (thin routes, logic lives in services).

Plan data is owner/session scoped exactly like conversations and
approvals: a foreign identity observes 404, never the plan's state.

Execution is deliberately single-step per call: every step goes through
the existing policy and approval workflow. There is no endpoint that
advances a sequence automatically. A step is requested at most once:
COMPLETED, EXECUTED, and FAILED are final states. A failed, rejected,
expired, or unavailable step cannot be re-requested; address the cause
and capture a new plan via a new chat turn instead.
"""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.security import (
    AuthenticatedSession,
    require_authenticated_session,
    require_safe_origin,
)
from app.schemas.approval import ApprovalRequest
from app.schemas.plan import TaskPlan
from app.schemas.tool import ToolResult
from app.services.approval_service import ApprovalArgumentsError, ApprovalWorkflowError
from app.services.approval_store import ApprovalCapacityError
from app.services.plan_service import (
    PlanNotFoundError,
    PlanStepNotFoundError,
    PlanStepStateError,
    plan_service,
)
from app.services.tool_registry import UnknownToolError


router = APIRouter(tags=["plans"])


@router.get("/api/plans/{plan_id}", response_model=TaskPlan, status_code=200)
async def get_plan(
    plan_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
) -> TaskPlan:
    """Return the authenticated session's plan after approval reconciliation."""
    try:
        return plan_service.get_plan(
            plan_id, principal.owner_id, principal.session_id
        )
    except PlanNotFoundError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )


@router.post(
    "/api/plans/{plan_id}/steps/{step_id}/request",
    response_model=ToolResult | ApprovalRequest,
    status_code=200,
    responses={202: {"model": ApprovalRequest}},
)
async def request_plan_step(
    plan_id: str,
    step_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    _origin: None = Depends(require_safe_origin),
) -> ToolResult | JSONResponse:
    """Request one ready plan step through policy and the approval workflow.

    Read-only tools execute once and resolve the step. Mutating tools
    return a pending exact-argument approval (202) and execute nothing
    here. No automatic advancement happens for other steps.
    """
    try:
        outcome = plan_service.request_step_execution(
            plan_id, step_id, principal.owner_id, principal.session_id
        )
    except (PlanNotFoundError, PlanStepNotFoundError) as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except PlanStepStateError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except ApprovalArgumentsError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except UnknownToolError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except ApprovalCapacityError:
        raise StarletteHTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The approval service is temporarily unavailable.",
        )
    except ApprovalWorkflowError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )

    if isinstance(outcome, ApprovalRequest):
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=outcome.model_dump(mode="json"),
        )
    return outcome
