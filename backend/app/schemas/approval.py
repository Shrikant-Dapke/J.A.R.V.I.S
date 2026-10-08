"""Typed approval request and lifecycle schemas."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.policy import RiskLevel
from app.schemas.tool import (
    MAX_PAYLOAD_ITEMS,
    MAX_PAYLOAD_KEY_LENGTH,
    MAX_PAYLOAD_VALUE_LENGTH,
    TOOL_NAME_PATTERN,
)


MAX_ACTION_DESCRIPTION_LENGTH = 512
MAX_APPROVAL_ID_LENGTH = 64
SENSITIVE_ARGUMENT_KEY_PARTS = (
    "api_key",
    "credential",
    "password",
    "secret",
    "token",
)


class ApprovalStatus(str, Enum):
    """Approval lifecycle states, including the single-use terminal state."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"


class ApprovalRequest(BaseModel):
    """Exact, bounded action waiting for explicit user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(
        ...,
        min_length=1,
        max_length=MAX_APPROVAL_ID_LENGTH,
        description="Unique approval request identifier",
    )
    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=TOOL_NAME_PATTERN,
    )
    arguments: dict[str, str] = Field(
        default_factory=dict,
        description="Validated arguments bound to this approval",
    )
    action_description: str = Field(
        ...,
        min_length=1,
        max_length=MAX_ACTION_DESCRIPTION_LENGTH,
    )
    risk_level: RiskLevel
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    resolved_at: Optional[datetime] = None

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, str]) -> dict[str, str]:
        """Keep approval records bounded and reject secret-like fields."""
        if len(value) > MAX_PAYLOAD_ITEMS:
            raise ValueError(
                f"arguments must not exceed {MAX_PAYLOAD_ITEMS} items"
            )
        for key, argument_value in value.items():
            normalized_key = key.lower()
            if any(part in normalized_key for part in SENSITIVE_ARGUMENT_KEY_PARTS):
                raise ValueError("approval arguments must not contain secret fields")
            if len(key) > MAX_PAYLOAD_KEY_LENGTH:
                raise ValueError(
                    f"argument keys must not exceed {MAX_PAYLOAD_KEY_LENGTH} characters"
                )
            if len(argument_value) > MAX_PAYLOAD_VALUE_LENGTH:
                raise ValueError(
                    f"argument values must not exceed {MAX_PAYLOAD_VALUE_LENGTH} characters"
                )
        return dict(value)

    @model_validator(mode="after")
    def validate_expiration(self) -> "ApprovalRequest":
        """Require a usable UTC expiration after creation."""
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self
