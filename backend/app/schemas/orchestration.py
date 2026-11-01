"""Orchestration decision and response schemas.

The model's structured output is UNTRUSTED: every branch below is a
strict Pydantic model with fixed bounds. Anything that carries an
``action`` discriminator must validate exactly; text that does not
parse into such an object stays plain conversational text and is never
interpreted by the orchestrator.

Arguments are bounded **string maps** — the same contract the existing
``/api/tools/{name}/invoke`` endpoint uses — so tool implementations
keep one validation path and arbitrary nested model output can never
wash through.
"""

import json
import re

from datetime import datetime
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

from app.schemas.approval import ApprovalStatus
from app.schemas.policy import RiskLevel
from app.schemas.tool import (
    MAX_PAYLOAD_ITEMS,
    MAX_PAYLOAD_KEY_LENGTH,
    MAX_PAYLOAD_VALUE_LENGTH,
    TOOL_NAME_PATTERN,
)

# Assistant-visible text bounds (kept aligned with the LLM message
# contract so stored turns can always be replayed into provider calls).
MAX_DECISION_TEXT_LENGTH = 4000
MAX_EXPLANATION_LENGTH = 512

DECISION_ACTION_PATTERN = TOOL_NAME_PATTERN


class ReplyDecision(BaseModel):
    """A plain conversational answer; no tool is involved."""

    action: Literal["reply"]
    reply: str = Field(
        ...,
        min_length=1,
        max_length=MAX_DECISION_TEXT_LENGTH,
        description="Assistant answer to return verbatim",
    )


class ToolActionDecision(BaseModel):
    """A structured proposal to run exactly one registered tool."""

    action: Literal["tool_action"]
    tool: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=TOOL_NAME_PATTERN,
        description="Registered tool the model proposes",
    )
    arguments: dict[str, str] = Field(
        default_factory=dict,
        description="Bounded string arguments for the proposed tool",
    )
    explanation: Optional[str] = Field(
        default=None,
        max_length=MAX_EXPLANATION_LENGTH,
        description="Short user-facing reason for the proposal",
    )

    @field_validator("arguments")
    @classmethod
    def validate_arguments(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        """Reject oversized or non-string model output early."""
        if len(value) > MAX_PAYLOAD_ITEMS:
            raise ValueError(
                f"arguments must not exceed {MAX_PAYLOAD_ITEMS} items"
            )
        for key, argument_value in value.items():
            if not isinstance(key, str) or not isinstance(argument_value, str):
                raise ValueError("argument keys and values must be strings")
            if len(key) > MAX_PAYLOAD_KEY_LENGTH:
                raise ValueError(
                    f"argument keys must not exceed {MAX_PAYLOAD_KEY_LENGTH} characters"
                )
            if len(argument_value) > MAX_PAYLOAD_VALUE_LENGTH:
                raise ValueError(
                    f"argument values must not exceed "
                    f"{MAX_PAYLOAD_VALUE_LENGTH} characters"
                )
        return value

    @field_validator("explanation")
    @classmethod
    def validate_explanation(cls, value: Optional[str]) -> Optional[str]:
        """Treat empty explanations as absent."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class RawPlanStep(BaseModel):
    """The model's proposal for one plan step (pre-registry validation)."""

    step_id: str = Field(
        ...,
        min_length=1,
        max_length=32,
        pattern=r"^[a-zA-Z0-9_]{1,32}$",
    )
    description: str = Field(..., min_length=1, max_length=512)
    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=TOOL_NAME_PATTERN,
    )
    arguments: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(
        default_factory=list,
        max_length=4,
    )

    @field_validator("depends_on")
    @classmethod
    def validate_dependency_shape(cls, value: list[str]) -> list[str]:
        """Dependency ids must be well-formed step ids."""
        for step_id in value:
            if not isinstance(step_id, str) or len(step_id) > 32:
                raise ValueError("depends_on entries must be bounded step ids")
        return value


class PlanDecision(BaseModel):
    """A bounded multi-step plan proposal (no execution implied)."""

    action: Literal["plan"]
    goal: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="What the plan is meant to achieve",
    )
    steps: list[RawPlanStep] = Field(
        ...,
        min_length=1,
        max_length=8,
        description="Bounded ordered steps",
    )
    explanation: Optional[str] = Field(
        default=None,
        max_length=MAX_EXPLANATION_LENGTH,
    )

    @field_validator("steps")
    @classmethod
    def validate_unique_step_ids(cls, value: list[RawPlanStep]) -> list[RawPlanStep]:
        """Reject duplicate model-proposed step ids early."""
        seen: set[str] = set()
        for step in value:
            if step.step_id in seen:
                raise ValueError("plan step_ids must be unique")
            seen.add(step.step_id)
        return value

    @field_validator("explanation")
    @classmethod
    def validate_explanation(cls, value: Optional[str]) -> Optional[str]:
        """Treat empty explanations as absent."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


OrchestratorDecision = Union[ReplyDecision, ToolActionDecision, PlanDecision]


class ProposedAction(BaseModel):
    """A mutating action waiting for explicit user approval.

    Returned when the orchestrator forwarded a proposal through the
    existing approval workflow and nothing has executed yet.
    """

    approval_id: str = Field(..., min_length=1, max_length=64)
    tool_name: str = Field(..., min_length=1, max_length=64)
    arguments: dict[str, str] = Field(
        default_factory=dict,
        description="Exact validated arguments the approval is bound to",
    )
    action_description: str = Field(..., min_length=1, max_length=512)
    risk_level: RiskLevel
    approval_status: ApprovalStatus
    expires_at: datetime


class ExecutedAction(BaseModel):
    """Structured outcome of an already-executed registered tool."""

    tool_name: str = Field(..., min_length=1, max_length=64)
    status: Literal["success", "already_exists", "failure"]
    success: bool
    message: str = Field(..., min_length=1, max_length=512)
    data: Optional[dict[str, str]] = None
    error_code: Optional[str] = None


class OrchestratorDecisionParseError(ValueError):
    """Raised when action-bearing model output fails strict validation."""


# Deterministic fallback when the provider returns no usable text at all.
EMPTY_TEXT_REPLY = "The assistant did not return usable text."


def parse_orchestrator_decision(raw_text: str) -> OrchestratorDecision:
    """Interpret raw provider text as a safe orchestration decision.

    Args:
        raw_text: UNTRUSTED provider output.

    Returns:
        A validated decision. Text that is not a JSON object carrying an
        ``action`` discriminator is wrapped as a plain ReplyDecision so
        normal conversation keeps working without inferring anything.
        Output that *claims* to be a decision (carries ``action``) but
        fails validation raises OrchestratorDecisionParseError instead
        of being silently downgraded.
    """
    text = (raw_text or "").strip()
    candidate = _extract_json_object(text)

    if not isinstance(candidate, dict) or "action" not in candidate:
        return ReplyDecision(action="reply", reply=text or EMPTY_TEXT_REPLY)

    action = candidate.get("action")
    if action == "reply":
        return _validated_or_raise(ReplyDecision, candidate)
    if action == "tool_action":
        return _validated_or_raise(ToolActionDecision, candidate)
    if action == "plan":
        return _validated_or_raise(PlanDecision, candidate)
    raise OrchestratorDecisionParseError(
        "The assistant returned an unknown decision type."
    )


# Markdown code fences possibly labeled as json.
_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_object(text: str) -> object | None:
    """Return the first parseable JSON object in provider text, if any.

    Accepts a bare JSON object or one wrapped in markdown code fences.
    Fenced blocks are scanned with a resilient JSON decoder so nested
    objects, string contents, or trailing prose inside the fence cannot
    split or mis-slice the parse. Prose that merely contains braces is
    deliberately NOT extracted: without a complete, unambiguous object
    there is no proposal and the text stays plain conversation.
    """
    if not text:
        return None
    parsed = _try_json(text)
    if parsed is not None:
        return parsed

    for fence in _FENCE_PATTERN.findall(text):
        candidate = fence.strip()
        if not candidate:
            continue
        nested = _first_json_object_in(candidate)
        if nested is not None:
            return nested
    return None


def _first_json_object_in(text: str) -> object | None:
    """Scan a bounded fenced block for the first complete JSON object."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _try_json(text: str) -> object | None:
    """Parse text as JSON, returning None on any failure."""
    try:
        return json.loads(text)
    except ValueError:
        return None


def _validated_or_raise(
    model, candidate: dict
) -> OrchestratorDecision:
    """Validate one decision branch, raising on any deviation."""
    try:
        return model.model_validate(candidate)
    except ValueError as exc:
        raise OrchestratorDecisionParseError(
            "The assistant returned a malformed action proposal."
        ) from exc
