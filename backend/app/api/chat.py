"""Chat API router (thin route, logic lives in service layer)."""

from fastapi import APIRouter, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import get_chat_response
from app.services.llm.errors import (
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMTimeoutError,
)


router = APIRouter(tags=["chat"])


@router.post("/api/chat", response_model=ChatResponse, status_code=200)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Accept a chat message and return the provider-generated response.

    Flow: request -> validation -> route -> service -> structured response.
    Provider failures map to structured errors without leaking internals.
    """
    try:
        return get_chat_response(
            message=request.message,
            conversation_id=request.conversation_id,
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
