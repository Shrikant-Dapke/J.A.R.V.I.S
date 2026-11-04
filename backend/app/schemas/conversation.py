"""Conversation context schemas (persistent history, Phase 5.1)."""

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
    turn_count: int = Field(
        description="Number of turns in THIS response (windowed views "
        "may be smaller than the stored total)"
    )
    total_turn_count: int = Field(
        description="Total turns persisted for the conversation"
    )


class ConversationSummary(BaseModel):
    """One conversation's metadata (never message content)."""

    conversation_id: str = Field(description="Conversation identifier")
    created_at: datetime = Field(description="Creation timestamp (UTC)")
    updated_at: datetime = Field(
        description="Timestamp of the last stored turn (UTC)"
    )
    turn_count: int = Field(description="Total turns stored")


class ConversationListResponse(BaseModel):
    """Metadata page for the authenticated identity's conversations."""

    conversations: list[ConversationSummary] = Field(
        description="Conversations ordered by most recent activity"
    )
    total: int = Field(
        description="Total conversations for the authenticated identity"
    )
    limit: int = Field(description="Page size actually applied")
    offset: int = Field(description="Offset actually applied")


class ConversationClearResponse(BaseModel):
    """Conversation deletion confirmation."""

    conversation_id: str = Field(description="Deleted conversation identifier")
    status: Literal["deleted"] = Field(
        default="deleted", description="Deletion status"
    )
