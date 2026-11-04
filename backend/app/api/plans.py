"""Task-plan endpoints (thin routes, logic lives in services).

Plan data is owner/session scoped exactly like conversations and
approvals: a foreign identity observes 404, never the plan's state.

Execution is deliberately single-step per call: every step goes through
the existing policy and approval workflow. There is no endpoint that
advances a sequence automatically. A step is requested at most once:
COMPLETED, EXECUTED, and FAILED are final states. A failed, rejected,
expired, or unavailable step cannot be re-requested; address the cause
and capture a new plan via a new chat turn instead.

Phase 5.2-a adds a read-only plan inventory (GET /api/plans) that
derives advisory attention flags from persisted state; the listing
never reconciles approvals or mutates step status.
"""

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.security import (
    AuthenticatedSession,
    require_authenticated_session,
    require_safe_origin,
)
from app.schemas.approval import ApprovalRequest
from app.schemas.plan import PlanDetail, PlanListResponse, PlanSummary, TaskPlan
from app.schemas.tool import ToolResult
from app.services.approval_service import ApprovalArgumentsError, ApprovalWorkflowError
from app.services.approval_store import ApprovalCapacityError
from app.services.plan_service import (
    PlanCorruptError,
    PlanNotFoundError,
    PlanStepNotFoundError,
    PlanStepStateError,
    derive_plan_status,
    plan_service,
    summarize_plan_steps,
)
from app.services.tool_registry import UnknownToolError


router = APIRouter(tags=["plans"])

# Plan inventory pagination policy (Phase 5.2-a): newest creation first
# with a deterministic plan_id tiebreak, mirroring the conversation
# listing policy approved in Phase 5.1.
DEFAULT_PLAN_LIST_LIMIT = 50
MAX_PLAN_LIST_LIMIT = 200


@router.get("/api/plans", response_model=PlanListResponse, status_code=200)
async def list_plans(
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    limit: int = Query(
        default=DEFAULT_PLAN_LIST_LIMIT,
        ge=1,
        le=MAX_PLAN_LIST_LIMIT,
        description="Page size (1..200)",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Page offset into the deterministic ordering",
    ),
) -> PlanListResponse:
    """List the authenticated identity's plans (metadata only).

    Strictly read-only: statuses and attention flags describe plans
    exactly as persisted — no reconcile, no state transition, no
    approval side effect. Ordering is ``created_at DESC, plan_id DESC``
    and page rows plus ``total`` come from ONE consistent storage
    transaction. A corrupted persisted plan fails closed with a
    structured 500 instead of being served or skipped.
    """
    try:
        views, total = plan_service.list_plans_page(
            owner_id=principal.owner_id,
            session_id=principal.session_id,
            limit=limit,
            offset=offset,
        )
    except PlanCorruptError:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Plan state could not be validated.",
        )
    return PlanListResponse(
        plans=[
            PlanSummary(
                plan_id=view.plan_id,
                goal=view.plan.goal,
                created_at=view.created_at,
                updated_at=view.updated_at,
                step_count=len(view.plan.steps),
                **summarize_plan_steps(view.plan),
            )
            for view in views
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/plans/{plan_id}", response_model=PlanDetail, status_code=200)
async def get_plan(
    plan_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
) -> PlanDetail:
    """Return the session's plan after approval reconciliation.

    The authoritative read: approval-driven states are reconciled on
    read and deterministic derived status is added (overall_status,
    ready_step_ids, attention lists — Phase 5.2-c). A corrupted
    persisted plan fails closed with a structured 500; unknown and
    foreign plans observe 404, never the record's state.
    """
    try:
        plan = plan_service.get_plan(
            plan_id, principal.owner_id, principal.session_id
        )
    except PlanNotFoundError as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except PlanCorruptError:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Plan state could not be validated.",
        )
    return PlanDetail(
        **plan.model_dump(),
        **derive_plan_status(plan),
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


@router.post(
    "/api/plans/{plan_id}/steps/{step_id}/resolve-recovery",
    response_model=TaskPlan,
    status_code=200,
)
async def resolve_plan_step_recovery(
    plan_id: str,
    step_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    _origin: None = Depends(require_safe_origin),
) -> TaskPlan:
    """Explicitly reconcile a RECOVERY_REQUIRED step.

    Approval-driven automatic execution never resolves uncertain steps:
    whether the side effect occurred cannot be proven by the backend, so
    a human acknowledges the uncertainty here. The step resolves FAILED
    with an ``execution_uncertain`` classification and the standing
    mutation boundary attempt is finalized RECONCILED. The user must
    verify any external effect manually; dependents stay blocked either
    way until a new plan captures the follow-up work.
    """
    try:
        return plan_service.resolve_step_recovery(
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
