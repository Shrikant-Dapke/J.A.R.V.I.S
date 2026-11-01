"""Chat request/response schemas."""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.orchestration import ExecutedAction, ProposedAction
from app.schemas.plan import TaskPlan


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
    """Chat response contract.

    Backward compatible with the Phase 2/3 contract:

    - ``status="success"`` — a conversational reply (optionally with the
      structured outcome of an auto-executed read-only tool in
      ``tool_result``).
    - ``status="action_proposed"`` — a mutating action was proposed;
      ``action`` carries the exact pending approval. Nothing executed.
    - ``status="plan_proposed"`` — a validated multi-step plan was
      captured; ``plan`` carries the steps. Nothing executed.
    """

    conversation_id: str = Field(description="Conversation identifier")
    response: str = Field(description="Assistant response")
    status: Literal["success", "action_proposed", "plan_proposed"] = Field(
        default="success", description="Response status"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Response timestamp (UTC)",
    )
    action: Optional[ProposedAction] = Field(
        default=None,
        description="Pending approval payload when status is action_proposed",
    )
    tool_result: Optional[ExecutedAction] = Field(
        default=None,
        description="Structured outcome when a read-only tool was executed",
    )
    plan: Optional[TaskPlan] = Field(
        default=None,
        description="Captured plan payload when status is plan_proposed",
    )

    @model_validator(mode="after")
    def validate_payload_consistency(self) -> "ChatResponse":
        """Keep status and optional payloads mutually consistent."""
        if self.status == "action_proposed" and self.action is None:
            raise ValueError("action_proposed requires an action payload")
        if self.status == "plan_proposed" and self.plan is None:
            raise ValueError("plan_proposed requires a plan payload")
        if self.action is not None and self.status != "action_proposed":
            raise ValueError("action payload requires status action_proposed")
        if self.plan is not None and self.status != "plan_proposed":
            raise ValueError("plan payload requires status plan_proposed")
        if self.tool_result is not None and self.status != "success":
            raise ValueError("tool_result payload requires status success")
        return self
