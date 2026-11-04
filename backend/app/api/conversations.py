"""Conversation context endpoints (thin routes, logic lives in store).

Phase 5.1: conversations persist in the shared execution-state database
and the API exposes metadata listing with offset pagination plus
bounded history retrieval. All routes are read-only unless explicitly
state-changing (DELETE), every route authenticates the caller first,
and every served conversation is identity-scoped to the authenticated
owner/session. Foreign conversations stay indistinguishable from
unknown ones and metadata never leaks cross-identity.
"""

from fastapi import APIRouter, Depends, Query, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.security import (
    AuthenticatedSession,
    require_authenticated_session,
    require_safe_origin,
)
from app.schemas.conversation import (
    ConversationClearResponse,
    ConversationHistoryResponse,
    ConversationListResponse,
    ConversationSummary,
)
from app.services.conversation_store import (
    ConversationCorruptError,
    conversation_store,
)


router = APIRouter(tags=["conversations"])

# Listing pagination policy (Phase 5.1): newest activity first with a
# deterministic conversation_id tiebreak from the storage façade, so an
# (limit, offset) page is reproducible while rows are unchanged.
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

# History retrieval policy: newest complete turns, returned chronologically.
DEFAULT_HISTORY_LIMIT = 100
MAX_HISTORY_LIMIT = 400


@router.get(
    "/api/conversations",
    response_model=ConversationListResponse,
    status_code=200,
)
async def list_conversations(
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    limit: int = Query(
        default=DEFAULT_LIST_LIMIT,
        ge=1,
        le=MAX_LIST_LIMIT,
        description="Page size (1..200)",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Page offset into the deterministic ordering",
    ),
) -> ConversationListResponse:
    """List the authenticated identity's conversations (metadata only).

    Ordering is ``updated_at DESC, conversation_id DESC``: paginating
    again returns the same rows in the same order unless state changed,
    and the most recently active conversation is always the first entry
    of page one. The page and ``total`` come from ONE consistent
    storage transaction.
    """
    try:
        records, total = conversation_store.list_conversations_page(
            owner_id=principal.owner_id,
            session_id=principal.session_id,
            limit=limit,
            offset=offset,
        )
    except ConversationCorruptError:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Conversation history could not be validated.",
        )
    return ConversationListResponse(
        conversations=[
            ConversationSummary(
                conversation_id=record.conversation_id,
                created_at=record.created_at,
                updated_at=record.updated_at,
                turn_count=record.turn_count,
            )
            for record in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/conversations/{conversation_id}",
    response_model=ConversationHistoryResponse,
    status_code=200,
)
async def get_conversation(
    conversation_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    limit: int = Query(
        default=DEFAULT_HISTORY_LIMIT,
        ge=1,
        le=MAX_HISTORY_LIMIT,
        description="Most recent turns to return (1..400); still in "
        "chronological order",
    ),
) -> ConversationHistoryResponse:
    """Return the session's most recent turns and the persisted total.

    ``limit`` windows the view to the most recent turns (backward
    compatible: the default of 100 behaves exactly like the previous
    full-history endpoint for conversations with at most 100 turns).
    Stored history itself is never modified by reading.
    """
    try:
        turns = conversation_store.get_history(
            conversation_id,
            owner_id=principal.owner_id,
            session_id=principal.session_id,
            limit=limit,
        )
        total_turn_count = conversation_store.get_turn_count(
            conversation_id,
            owner_id=principal.owner_id,
            session_id=principal.session_id,
        )
    except ConversationCorruptError:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Conversation history could not be validated.",
        )
    if turns is None or total_turn_count is None:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' not found",
        )
    return ConversationHistoryResponse(
        conversation_id=conversation_id,
        turns=turns,
        turn_count=len(turns),
        total_turn_count=total_turn_count,
    )


@router.delete(
    "/api/conversations/{conversation_id}",
    response_model=ConversationClearResponse,
    status_code=200,
)
async def delete_conversation(
    conversation_id: str,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    _origin: None = Depends(require_safe_origin),
) -> ConversationClearResponse:
    """Delete the authenticated session's conversation from the store."""
    deleted = conversation_store.clear_conversation(
        conversation_id,
        owner_id=principal.owner_id,
        session_id=principal.session_id,
    )
    if not deleted:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' not found",
        )
    return ConversationClearResponse(conversation_id=conversation_id)
