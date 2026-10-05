"""In-memory conversation context store.

Temporary development-only state. NOT the permanent JARVIS memory system.

LIMITATION: state lives in a single process. Running multiple workers
(e.g. uvicorn --workers N) gives each worker its own isolated store.
A persistent implementation must replace this class without changing
the API contract.
"""

import threading
import uuid
from typing import Literal, Optional

from app.schemas.conversation import ConversationTurn


class InMemoryConversationStore:
    """Thread-safe in-memory store for conversation turns."""

    def __init__(self) -> None:
        self._conversations: dict[str, list[ConversationTurn]] = {}
        self._lock = threading.Lock()

    def create_conversation(
        self, conversation_id: Optional[str] = None
    ) -> str:
        """
        Create a conversation (no-op if it already exists).

        Returns:
            The conversation identifier (generated when not supplied).
        """
        resolved = conversation_id.strip() if conversation_id else None
        if not resolved:
            resolved = str(uuid.uuid4())
        with self._lock:
            self._conversations.setdefault(resolved, [])
        return resolved

    def exists(self, conversation_id: str) -> bool:
        """Check whether a conversation exists."""
        with self._lock:
            return conversation_id in self._conversations

    def add_turn(
        self,
        conversation_id: str,
        role: Literal["user", "assistant"],
        content: str,
    ) -> ConversationTurn:
        """Append a turn, creating the conversation if missing."""
        turn = ConversationTurn(role=role, content=content)
        with self._lock:
            self._conversations.setdefault(conversation_id, []).append(turn)
        return turn

    def get_history(self, conversation_id: str) -> Optional[list[ConversationTurn]]:
        """
        Retrieve chronological turns, or None if unknown.

        Returns a copy so callers cannot mutate stored state.
        """
        with self._lock:
            turns = self._conversations.get(conversation_id)
            return list(turns) if turns is not None else None

    def clear_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation.

        Returns:
            True if deleted, False if it did not exist.
        """
        with self._lock:
            return self._conversations.pop(conversation_id, None) is not None


conversation_store = InMemoryConversationStore()
