"""Policy decision schema (authorization only, never executes)."""

from pydantic import BaseModel, Field


MAX_POLICY_REASON_LENGTH = 256


class PolicyDecision(BaseModel):
    """Typed allow/deny decision for a requested tool action."""

    allowed: bool = Field(description="True when execution may proceed")
    reason: str = Field(
        ...,
        min_length=1,
        max_length=MAX_POLICY_REASON_LENGTH,
        description="Bounded human-readable reason",
    )
    requires_approval: bool = Field(
        description="True when the tool needs explicit approval"
    )
