"""Policy decision schema (authorization only, never executes)."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


MAX_POLICY_REASON_LENGTH = 256


class RiskLevel(str, Enum):
    """Small, extensible risk classification for registered tools."""

    READ_ONLY = "READ_ONLY"
    LOW_RISK = "LOW_RISK"
    HIGH_RISK = "HIGH_RISK"


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
    tool_name: Optional[str] = Field(
        default=None,
        description="Tool considered by the policy decision",
    )
    risk_level: Optional[RiskLevel] = Field(
        default=None,
        description="Risk classification used by the policy",
    )
