"""Conversation context schemas (temporary in-memory state, not permanent memory)."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class ConversationTurn(BaseModel):
    """A single conversation turn."""

    role: Literal["user", "assistant"] = Field(description="Turn role")
    content: str = Field(description="Turn content")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Turn timestamp (UTC)",
    )


class ConversationHistoryResponse(BaseModel):
    """Ordered conversation history contract."""

    conversation_id: str = Field(description="Conversation identifier")
    turns: list[ConversationTurn] = Field(description="Chronological turns")
    turn_count: int = Field(description="Total number of turns")


class ConversationClearResponse(BaseModel):
    """Conversation deletion confirmation."""

    conversation_id: str = Field(description="Deleted conversation identifier")
    status: Literal["deleted"] = Field(
        default="deleted", description="Deletion status"
    )
