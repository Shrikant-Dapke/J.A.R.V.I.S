"""Chat API router (thin route, logic lives in service layer)."""

from fastapi import APIRouter, Depends, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.security import (
    AuthenticatedSession,
    require_authenticated_session,
    require_safe_origin,
)
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.approval_service import ApprovalArgumentsError, ApprovalWorkflowError
from app.services.approval_store import ApprovalCapacityError
from app.services.chat_service import get_chat_response
from app.services.conversation_store import ConversationNotFoundError
from app.services.orchestrator import (
    OrchestrationInstructionBudgetError,
    OrchestrationInvalidProposalError,
)
from app.services.plan_service import PlanCapacityError
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
    Accept a chat message and return the orchestrator-driven response.

    Flow: authentication -> origin validation -> validation -> route ->
    orchestrator -> structured response. Conversations are called in
    under the authenticated owner/session; provider failures map to the
    existing structured errors without leaking internals.

    Orchestration additions:

    - A rejected model proposal (unknown tool, malformed decision or
      arguments, invalid plan) maps to 422; nothing executes.
    - A message too large for the orchestrator instruction budget also
      maps to 422 and is never silently truncated.
    - A policy-denied tool proposal maps to 403 (fail closed).
    - An unavailable approval store maps to 503 (fail closed, nothing
      executes).
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
    except OrchestrationInvalidProposalError:
        raise StarletteHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The assistant proposed an action that is not available.",
        )
    except OrchestrationInstructionBudgetError:
        raise StarletteHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The message exceeds the orchestrator instruction budget.",
        )
    except ApprovalArgumentsError:
        raise StarletteHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The assistant proposed invalid tool arguments.",
        )
    except ApprovalCapacityError:
        raise StarletteHTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The approval service is temporarily unavailable.",
        )
    except PlanCapacityError:
        raise StarletteHTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The plan store is temporarily unavailable.",
        )
    except ApprovalWorkflowError:
        raise StarletteHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The proposed action was denied by policy.",
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
