"""Chat service: conversation context -> LLM provider -> stored reply."""

import uuid
from typing import Optional

from app.schemas.chat import ChatResponse
from app.schemas.llm import LLMMessage
from app.services.conversation_store import conversation_store
from app.services.llm.factory import get_llm_provider


def get_chat_response(
    message: str, conversation_id: Optional[str] = None
) -> ChatResponse:
    """
    Store the user turn, generate a reply via the LLM provider, store it.

    Flow: obtain/create conversation -> store user turn ->
    provider(history) -> store assistant turn -> return response.

    Provider output is untrusted text: stored and returned verbatim,
    never executed or interpreted.

    Raises:
        LLMError: On provider configuration, API, timeout, or
            invalid-response failures (mapped to HTTP by the route).
    """
    resolved_id = conversation_id.strip() if conversation_id else None
    if not resolved_id:
        resolved_id = str(uuid.uuid4())

    conversation_store.create_conversation(resolved_id)
    conversation_store.add_turn(resolved_id, "user", message)

    history = conversation_store.get_history(resolved_id) or []
    messages = [
        LLMMessage(role=turn.role, content=turn.content) for turn in history
    ]

    result = get_llm_provider().generate_response(messages)

    conversation_store.add_turn(resolved_id, "assistant", result.content)

    return ChatResponse(
        conversation_id=resolved_id,
        response=result.content,
        status="success",
    )
