"""Approval lifecycle endpoints; routes contain no execution logic."""

from fastapi import APIRouter, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.approval import ApprovalRequest
from app.schemas.tool import ToolResult
from app.services.approval_service import (
    ApprovalIntegrityError,
    ApprovalPolicyError,
    approval_service,
)
from app.services.approval_store import (
    ApprovalNotFoundError,
    ApprovalStateError,
)


router = APIRouter(prefix="/api/approvals", tags=["approvals"])


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


@router.get("/{approval_id}", response_model=ApprovalRequest)
async def get_approval(approval_id: str) -> ApprovalRequest:
    """Return the current state, applying lazy expiration."""
    try:
        return approval_service.get_request(approval_id)
    except ApprovalNotFoundError as exc:
        raise _not_found(exc)


@router.post("/{approval_id}/approve", response_model=ToolResult)
async def approve_approval(approval_id: str) -> ToolResult:
    """Approve and consume one exact request before executing it."""
    try:
        return approval_service.approve_request(approval_id)
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
async def reject_approval(approval_id: str) -> ApprovalRequest:
    """Reject one pending request without invoking the executor."""
    try:
        return approval_service.reject_request(approval_id)
    except ApprovalNotFoundError as exc:
        raise _not_found(exc)
    except ApprovalStateError as exc:
        raise _invalid_transition(exc)
