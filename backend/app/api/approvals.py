"""Approval lifecycle endpoints; routes contain no execution logic.

Phase 5.2-b adds a strictly read-only inventory (GET /api/approvals)
that serves persisted approval metadata without arguments, digests, or
retention flags. The exact request, including its arguments, stays on
the authenticated detail endpoint (GET /api/approvals/{approval_id}),
which is also where lazy expiration applies: the listing reports
persisted status verbatim and mutates nothing.
"""

from fastapi import APIRouter, Depends, Query, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.security import (
    AuthenticatedSession,
    require_authenticated_session,
    require_safe_origin,
)
from app.schemas.approval import (
    ApprovalListResponse,
    ApprovalRequest,
    ApprovalSummary,
)
from app.schemas.tool import ToolResult
from app.services.approval_service import (
    ApprovalIntegrityError,
    ApprovalPolicyError,
    approval_service,
)
from app.services.approval_store import (
    ApprovalCorruptError,
    ApprovalNotFoundError,
    ApprovalStateError,
)


router = APIRouter(prefix="/api/approvals", tags=["approvals"])

# Approval inventory pagination policy (Phase 5.2-b): newest creation
# first with a deterministic approval_id tiebreak, mirroring the plan
# listing policy approved in Phase 5.2-a.
DEFAULT_APPROVAL_LIST_LIMIT = 50
MAX_APPROVAL_LIST_LIMIT = 200


def _not_found(exc: ApprovalNotFoundError) -> StarletteHTTPException:
    return StarletteHTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=str(exc),
    )


def _invalid_transition(exc: ApprovalStateError) -> StarletteHTTPException:
    return StarletteHTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(exc),
    )


@router.get("", response_model=ApprovalListResponse, status_code=200)
async def list_approvals(
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    limit: int = Query(
        default=DEFAULT_APPROVAL_LIST_LIMIT,
        ge=1,
        le=MAX_APPROVAL_LIST_LIMIT,
        description="Page size (1..200)",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Page offset into the deterministic ordering",
    ),
) -> ApprovalListResponse:
    """List the authenticated identity's approval records (metadata only).

    Strictly read-only: no cleanup, no lazy expiration, no claim, no
    state transition. Statuses are persisted verbatim (advisory), the
    ordering is ``created_at DESC, approval_id DESC``, and page rows
    plus ``total`` come from ONE consistent storage transaction. Raw
    arguments, digests, and retention flags are never included. A
    corrupted persisted record fails closed with a structured 500.
    """
    try:
        views, total = approval_service.list_approvals_page(
            principal.owner_id,
            principal.session_id,
            limit=limit,
            offset=offset,
        )
    except ApprovalCorruptError:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Approval state could not be validated.",
        )
    return ApprovalListResponse(
        approvals=[
            ApprovalSummary(
                approval_id=view.approval_id,
                tool_name=view.tool_name,
                action_description=view.action_description,
                risk_level=view.risk_level,
                status=view.status,
                created_at=view.created_at,
                expires_at=view.expires_at,
                resolved_at=view.resolved_at,
            )
            for view in views
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{approval_id}", response_model=ApprovalRequest)
async def get_approval(
    approval_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
) -> ApprovalRequest:
    """Return the current state, applying lazy expiration.

    A corrupted persisted record fails closed with a structured 500
    carrying a fixed, non-sensitive message (Phase 5.2-c): unknown and
    foreign approvals still observe 404 and never the record's state.
    """
    try:
        return approval_service.get_request(
            approval_id, principal.owner_id, principal.session_id
        )
    except ApprovalNotFoundError as exc:
        raise _not_found(exc)
    except ApprovalCorruptError:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Approval state could not be validated.",
        )


@router.post("/{approval_id}/approve", response_model=ToolResult)
async def approve_approval(
    approval_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    _origin: None = Depends(require_safe_origin),
) -> ToolResult:
    """Approve and consume one exact request before executing it."""
    try:
        return approval_service.approve_request(
            approval_id, principal.owner_id, principal.session_id
        )
    except ApprovalNotFoundError as exc:
        raise _not_found(exc)
    except ApprovalStateError as exc:
        raise _invalid_transition(exc)
    except (ApprovalPolicyError, ApprovalIntegrityError) as exc:
        raise StarletteHTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
@router.post("/{approval_id}/reject", response_model=ApprovalRequest)
async def reject_approval(
    approval_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    _origin: None = Depends(require_safe_origin),
) -> ApprovalRequest:
    """Reject one pending request without invoking the executor."""
    try:
        return approval_service.reject_request(
            approval_id, principal.owner_id, principal.session_id
        )
    except ApprovalNotFoundError as exc:
        raise _not_found(exc)
    except ApprovalStateError as exc:
        raise _invalid_transition(exc)
