"""Conversation context endpoints (thin routes, logic lives in store)."""

from fastapi import APIRouter, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.conversation import (
    ConversationClearResponse,
    ConversationHistoryResponse,
)
from app.services.conversation_store import conversation_store


router = APIRouter(tags=["conversations"])


@router.get(
    "/api/conversations/{conversation_id}",
    response_model=ConversationHistoryResponse,
    status_code=200,
)
async def get_conversation(conversation_id: str) -> ConversationHistoryResponse:
    """Return ordered turns and total turn count for a conversation."""
    turns = conversation_store.get_history(conversation_id)
    if turns is None:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' not found",
        )
    return ConversationHistoryResponse(
        conversation_id=conversation_id,
        turns=turns,
        turn_count=len(turns),
    )


@router.delete(
    "/api/conversations/{conversation_id}",
    response_model=ConversationClearResponse,
    status_code=200,
)
async def delete_conversation(conversation_id: str) -> ConversationClearResponse:
    """Delete a conversation from the in-memory store."""
    deleted = conversation_store.clear_conversation(conversation_id)
    if not deleted:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' not found",
        )
    return ConversationClearResponse(conversation_id=conversation_id)
