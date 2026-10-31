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


class ConversationNotFoundError(LookupError):
    """Raised when a conversation is unknown or owned by another identity."""


class InMemoryConversationStore:
    """Thread-safe in-memory store for owner-scoped conversation turns."""

    def __init__(self) -> None:
        self._conversations: dict[str, list[ConversationTurn]] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def create_conversation(
        self,
        conversation_id: Optional[str] = None,
        *,
        owner_id: str,
        session_id: str,
    ) -> str:
        """
        Create a conversation (no-op if it already exists).

        An existing conversation keeps its original owner and session.

        Returns:
            The conversation identifier (generated when not supplied).
        """
        resolved = conversation_id.strip() if conversation_id else None
        if not resolved:
            resolved = str(uuid.uuid4())
        identity = (owner_id, session_id)
        with self._lock:
            self._conversations.setdefault(resolved, [])
            self._owners.setdefault(resolved, identity)
        return resolved

    def exists(self, conversation_id: str) -> bool:
        """Check whether a conversation exists (existence, not ownership)."""
        with self._lock:
            return conversation_id in self._conversations

    def add_turn(
        self,
        conversation_id: str,
        role: Literal["user", "assistant"],
        content: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> ConversationTurn:
        """
        Append a turn, creating the conversation if missing.

        A conversation created in another owner/session is treated as not
        found and is never modified.

        Raises:
            ConversationNotFoundError: If the conversation exists but is
                bound to a different owner or session.
        """
        turn = ConversationTurn(role=role, content=content)
        identity = (owner_id, session_id)
        with self._lock:
            existing = self._owners.get(conversation_id)
            if existing is not None and existing != identity:
                raise ConversationNotFoundError("Conversation was not found")
            self._conversations.setdefault(conversation_id, []).append(turn)
            self._owners.setdefault(conversation_id, identity)
        return turn

    def get_history(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> Optional[list[ConversationTurn]]:
        """
        Retrieve the requesting identity's chronological turns, or None if
        the conversation is unknown or owned by another owner/session.

        Returns a copy so callers cannot mutate stored state.
        """
        with self._lock:
            if self._owners.get(conversation_id) != (owner_id, session_id):
                return None
            turns = self._conversations.get(conversation_id)
            return list(turns) if turns is not None else None

    def clear_conversation(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> bool:
        """
        Delete a conversation owned by the requesting identity.

        Returns:
            True if deleted, False if unknown or owned by another identity.
        """
        with self._lock:
            if self._owners.get(conversation_id) != (owner_id, session_id):
                return False
            if self._conversations.pop(conversation_id, None) is None:
                return False
            self._owners.pop(conversation_id, None)
            return True


conversation_store = InMemoryConversationStore()
