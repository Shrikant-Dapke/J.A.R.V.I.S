"""Tool invocation request schema (no executable content allowed)."""

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.tool import (
    MAX_PAYLOAD_ITEMS,
    MAX_PAYLOAD_KEY_LENGTH,
    MAX_PAYLOAD_VALUE_LENGTH,
)


class ToolInvokeRequest(BaseModel):
    """Typed invocation request (structured arguments only)."""

    arguments: Optional[dict[str, str]] = Field(
        default=None,
        description="Optional bounded string arguments (no code or commands)",
    )

    @field_validator("arguments")
    @classmethod
    def validate_arguments(
        cls, v: Optional[dict[str, str]]
    ) -> Optional[dict[str, str]]:
        """Enforce the same bounds as tool payloads."""
        if v is None:
            return None
        if len(v) > MAX_PAYLOAD_ITEMS:
            raise ValueError(
                f"arguments must not exceed {MAX_PAYLOAD_ITEMS} items"
            )
        for key, value in v.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("argument keys and values must be strings")
            if len(key) > MAX_PAYLOAD_KEY_LENGTH:
                raise ValueError(
                    f"argument keys must not exceed {MAX_PAYLOAD_KEY_LENGTH} characters"
                )
            if len(value) > MAX_PAYLOAD_VALUE_LENGTH:
                raise ValueError(
                    f"argument values must not exceed {MAX_PAYLOAD_VALUE_LENGTH} characters"
                )
        return v
