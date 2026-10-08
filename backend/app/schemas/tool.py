"""Shared schemas for the controlled tool boundary."""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.policy import RiskLevel


MAX_TOOL_NAME_LENGTH = 64
MAX_TOOL_DESCRIPTION_LENGTH = 512
MAX_PAYLOAD_ITEMS = 32
MAX_PAYLOAD_KEY_LENGTH = 64
MAX_PAYLOAD_VALUE_LENGTH = 512
MAX_ERROR_LENGTH = 512
MAX_MESSAGE_LENGTH = 512

TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_]+$"


class ToolResult(BaseModel):
    """Bounded, user-safe result returned by a tool invocation.

    ``payload`` is retained for the Phase 1 API contract. New code should use
    ``data`` as the structured output field; tool factories populate both with
    the same bounded values while the contract settles.
    """

    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TOOL_NAME_LENGTH,
        pattern=TOOL_NAME_PATTERN,
        description="Registered tool name",
    )
    status: Literal["success", "already_exists", "failure"] = Field(
        description="Execution status"
    )
    success: bool = Field(
        default=True, description="Whether the requested operation completed"
    )
    message: str = Field(
        default="Tool execution completed.",
        min_length=1,
        max_length=MAX_MESSAGE_LENGTH,
        description="Safe human-readable result message",
    )
    data: Optional[dict[str, str]] = Field(
        default=None,
        description="Bounded structured output from the tool",
    )
    payload: Optional[dict[str, str]] = Field(
        default=None,
        description="Bounded string payload (keys/values length-limited)",
    )
    error: Optional[str] = Field(
        default=None,
        max_length=MAX_ERROR_LENGTH,
        description="Safe error message (no internals)",
    )
    error_code: Optional[str] = Field(
        default=None,
        max_length=64,
        pattern=TOOL_NAME_PATTERN,
        description="Stable, non-sensitive error classification",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Result timestamp (UTC)",
    )

    @field_validator("data", "payload")
    @classmethod
    def validate_payload(
        cls, v: Optional[dict[str, str]]
    ) -> Optional[dict[str, str]]:
        """Enforce bounds on payload size, keys, and values."""
        if v is None:
            return None
        if len(v) > MAX_PAYLOAD_ITEMS:
            raise ValueError(
                f"payload must not exceed {MAX_PAYLOAD_ITEMS} items"
            )
        for key, value in v.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("payload keys and values must be strings")
            if len(key) > MAX_PAYLOAD_KEY_LENGTH:
                raise ValueError(
                    f"payload keys must not exceed {MAX_PAYLOAD_KEY_LENGTH} characters"
                )
            if len(value) > MAX_PAYLOAD_VALUE_LENGTH:
                raise ValueError(
                    f"payload values must not exceed {MAX_PAYLOAD_VALUE_LENGTH} characters"
                )
        return v

    @model_validator(mode="after")
    def normalize_success(self) -> "ToolResult":
        """Keep the boolean success flag consistent with the status."""
        expected_success = self.status in {"success", "already_exists"}
        if self.success != expected_success:
            self.success = expected_success
        return self

    @classmethod
    def success_result(
        cls,
        tool_name: str,
        message: str,
        data: Optional[dict[str, str]] = None,
    ) -> "ToolResult":
        """Build a successful result and preserve the legacy payload field."""
        return cls(
            tool_name=tool_name,
            status="success",
            success=True,
            message=message,
            data=data,
            payload=data,
            error=None,
            error_code=None,
        )

    @classmethod
    def already_exists_result(
        cls,
        tool_name: str,
        message: str,
        data: Optional[dict[str, str]] = None,
    ) -> "ToolResult":
        """Build the distinct, non-error existing-resource result."""
        return cls(
            tool_name=tool_name,
            status="already_exists",
            success=True,
            message=message,
            data=data,
            payload=data,
            error=None,
            error_code=None,
        )

    @classmethod
    def failure_result(
        cls,
        tool_name: str,
        error_code: str,
        message: str,
    ) -> "ToolResult":
        """Build a safe failure without exposing exception details."""
        return cls(
            tool_name=tool_name,
            status="failure",
            success=False,
            message=message,
            data=None,
            payload=None,
            error=message,
            error_code=error_code,
        )


class ToolDefinition(BaseModel):
    """Static description of a registered tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TOOL_NAME_LENGTH,
        pattern=TOOL_NAME_PATTERN,
        description="Unique tool identifier",
    )
    description: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TOOL_DESCRIPTION_LENGTH,
        description="Human-readable tool description",
    )
    read_only: bool = Field(description="True when the tool never mutates state")
    requires_approval: bool = Field(
        description="True when execution needs explicit approval"
    )
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON schema for the typed input model",
    )
    risk_level: Optional[RiskLevel] = Field(
        default=None,
        description="Policy risk classification for this tool",
    )
