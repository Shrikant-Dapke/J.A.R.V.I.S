"""Tool contract schemas (typed boundary, no arbitrary execution)."""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


MAX_TOOL_NAME_LENGTH = 64
MAX_TOOL_DESCRIPTION_LENGTH = 512
MAX_PAYLOAD_ITEMS = 32
MAX_PAYLOAD_KEY_LENGTH = 64
MAX_PAYLOAD_VALUE_LENGTH = 512
MAX_ERROR_LENGTH = 512

TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_]+$"


class ToolResult(BaseModel):
    """Typed result returned by a tool invocation."""

    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TOOL_NAME_LENGTH,
        pattern=TOOL_NAME_PATTERN,
        description="Registered tool name",
    )
    status: Literal["success", "failure"] = Field(description="Execution status")
    payload: Optional[dict[str, str]] = Field(
        default=None,
        description="Bounded string payload (keys/values length-limited)",
    )
    error: Optional[str] = Field(
        default=None,
        max_length=MAX_ERROR_LENGTH,
        description="Safe error message (no internals)",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Result timestamp (UTC)",
    )

    @field_validator("payload")
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


class ToolDefinition(BaseModel):
    """Static description of a registered tool."""

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
