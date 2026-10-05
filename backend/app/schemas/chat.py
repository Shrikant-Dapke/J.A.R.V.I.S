"""Chat request/response schemas."""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


MAX_MESSAGE_LENGTH = 4000
MAX_CONVERSATION_ID_LENGTH = 128


class ChatRequest(BaseModel):
    """Chat request contract."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=MAX_MESSAGE_LENGTH,
        description="User message",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        max_length=MAX_CONVERSATION_ID_LENGTH,
        description="Optional conversation identifier",
    )

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        """Reject empty, whitespace-only, and oversized messages."""
        if not v or not v.strip():
            raise ValueError("message must not be empty")
        stripped = v.strip()
        if len(stripped) == 0:
            raise ValueError("message must not be empty")
        if len(v) > MAX_MESSAGE_LENGTH:
            raise ValueError(
                f"message must not exceed {MAX_MESSAGE_LENGTH} characters"
            )
        return v

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, v: Optional[str]) -> Optional[str]:
        """Normalize empty conversation_id to None."""
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if len(v) > MAX_CONVERSATION_ID_LENGTH:
            raise ValueError(
                f"conversation_id must not exceed {MAX_CONVERSATION_ID_LENGTH} characters"
            )
        return v


class ChatResponse(BaseModel):
    """Chat response contract."""

    conversation_id: str = Field(description="Conversation identifier")
    response: str = Field(description="Assistant response")
    status: Literal["success"] = Field(
        default="success", description="Response status"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Response timestamp (UTC)",
    )
