"""Chat API router (thin route, logic lives in service layer)."""

from fastapi import APIRouter, Depends, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.security import (
    AuthenticatedSession,
    require_authenticated_session,
    require_safe_origin,
)
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import get_chat_response
from app.services.conversation_store import ConversationNotFoundError
from app.services.llm.errors import (
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMTimeoutError,
)


router = APIRouter(tags=["chat"])


@router.post("/api/chat", response_model=ChatResponse, status_code=200)
async def chat(
    request: ChatRequest,
    principal: AuthenticatedSession = Depends(require_authenticated_session),
    _origin: None = Depends(require_safe_origin),
) -> ChatResponse:
    """
    Accept a chat message and return the provider-generated response.

    Flow: authentication -> origin validation -> validation -> route ->
    service -> structured response. Conversations are called in under
    the authenticated owner/session; provider failures map to structured
    errors without leaking internals.
    """
    try:
        return get_chat_response(
            message=request.message,
            conversation_id=request.conversation_id,
            owner_id=principal.owner_id,
            session_id=principal.session_id,
        )
    except ConversationNotFoundError:
        raise StarletteHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation was not found",
        )
    except LLMTimeoutError:
        raise StarletteHTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Language model request timed out",
        )
    except LLMConfigurationError:
        raise StarletteHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Language model is not configured",
        )
    except (LLMProviderError, LLMInvalidResponseError):
        raise StarletteHTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Language model request failed",
        )
