"""Chat service: conversation context -> LLM provider -> stored reply."""

import uuid
from typing import Optional

from app.schemas.chat import ChatResponse
from app.schemas.llm import LLMMessage
from app.services.conversation_store import conversation_store
from app.services.llm.factory import get_llm_provider


def get_chat_response(
    message: str,
    conversation_id: Optional[str] = None,
    *,
    owner_id: str,
    session_id: str,
) -> ChatResponse:
    """
    Store the user turn, generate a reply via the LLM provider, store it.

    The conversation is called in under the authenticated identity
    (owner_id, session_id). Turns are appended only to conversations the
    caller owns; a caller-supplied conversation created in another
    identity raises ConversationNotFoundError instead of being reused.

    Flow: obtain/create conversation -> store user turn ->
    provider(history) -> store assistant turn -> return response.

    Provider output is untrusted text: stored and returned verbatim,
    never executed or interpreted.

    Raises:
        ConversationNotFoundError: If the conversation exists but is
            bound to a different owner or session.
        LLMError: On provider configuration, API, timeout, or
            invalid-response failures (mapped to HTTP by the route).
    """
    resolved_id = conversation_id.strip() if conversation_id else None
    if not resolved_id:
        resolved_id = str(uuid.uuid4())

    conversation_store.create_conversation(
        resolved_id, owner_id=owner_id, session_id=session_id
    )
    conversation_store.add_turn(
        resolved_id, "user", message, owner_id=owner_id, session_id=session_id
    )

    history = conversation_store.get_history(
        resolved_id, owner_id=owner_id, session_id=session_id
    ) or []
    messages = [
        LLMMessage(role=turn.role, content=turn.content) for turn in history
    ]

    result = get_llm_provider().generate_response(messages)

    conversation_store.add_turn(
        resolved_id, "assistant", result.content, owner_id=owner_id, session_id=session_id
    )

    return ChatResponse(
        conversation_id=resolved_id,
        response=result.content,
        status="success",
    )
