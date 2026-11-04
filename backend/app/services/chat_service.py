"""Chat service: conversation context -> orchestrator -> stored reply.

Phase 4: the main orchestrator (``ChatOrchestrator``) sits behind this
thin service boundary. Conversation storage, ownership checks, provider
calls, structured decision validation, policy, approval, and tool
execution all live in the orchestrator and its existing collaborators;
this module only preserves the Phase 2/3 service contract for callers
``app.api.chat`` and existing tests.
"""

from typing import Optional

from app.schemas.chat import ChatResponse
from app.services.orchestrator import chat_orchestrator


def get_chat_response(
    message: str,
    conversation_id: Optional[str] = None,
    *,
    owner_id: str,
    session_id: str,
) -> ChatResponse:
    """
    Handle one authenticated user turn through the orchestrator.

    The conversation is called in under the authenticated identity
    (owner_id, session_id). Turns are appended only to conversations the
    caller owns; a caller-supplied conversation created in another
    identity raises ConversationNotFoundError instead of being reused.

    The orchestrator validates the parsed decision, and either returns
    a conversational reply, a pending
    approval proposal (mutating actions; nothing executes), the outcome
    of a policy-allowed read-only tool, or a captured task plan.
    Provider failures propagate to the route for safe structured
    mapping.

    Raises:
        ConversationNotFoundError: If the conversation exists but is
            bound to a different owner or session.
        ConversationCapacityError: When this identity's conversation
            store is exhausted (mapped to HTTP 503; nothing is stored).
        ConversationCorruptError: When persisted history cannot be
            validated (mapped to HTTP 500).
        LLMError: On provider configuration, API, timeout, or
            invalid-response failures (mapped to HTTP by the route).
        OrchestrationInvalidProposalError: When the model returns a
            decision-shaped proposal that fails validation (mapped to
            HTTP 422 by the route).
        ApprovalCapacityError: When the approval store is unavailable
            for a tool proposal (mapped to HTTP 503; nothing executes).
        ApprovalWorkflowError: When policy denies the proposed tool
            (mapped to HTTP 403).
    """
    return chat_orchestrator.handle_chat_message(
        message,
        conversation_id,
        owner_id=owner_id,
        session_id=session_id,
    )
