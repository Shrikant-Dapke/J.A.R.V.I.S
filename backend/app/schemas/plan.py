"""Validated task-plan schemas (bounded, single-plan execution foundation).

Phase 4 milestone 4.2: a typed plan representation used to capture and
validate a bounded sequence of tool steps proposed by the model.

Design constraints:
- Structure and dependencies are validated at construction time; cycles
  are impossible because a dependency may only reference an EARLIER step.
- Step arguments are bounded strings and secret-like keys are rejected
  (same policy as approvals).
- Only registered tools pass the service-level guard; the schema keeps
  only the shape check so it stays import-light.
- No autonomous execution loop lives in this module: status transitions
  are explicit and driven by the plan service or the approval workflow.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.approval import SENSITIVE_ARGUMENT_KEY_PARTS
from app.schemas.tool import (
    MAX_PAYLOAD_ITEMS,
    MAX_PAYLOAD_KEY_LENGTH,
    MAX_PAYLOAD_VALUE_LENGTH,
    TOOL_NAME_PATTERN,
    ToolResult,
)

MAX_PLAN_STEPS = 8
MAX_PLAN_GOAL_LENGTH = 512
MAX_STEP_DESCRIPTION_LENGTH = 512
MAX_STEP_ID_LENGTH = 32
MAX_PLAN_ID_LENGTH = 64
MAX_DEPENDENCIES_PER_STEP = 4
PLAN_STEP_ID_PATTERN = r"^[a-zA-Z0-9_]{1,32}$"


class PlanStepStatus(str, Enum):
    """Lifecycle of one plan step.

    COMPLETED means a successful policy-allowed execution whose
    ToolResult is recorded on the step; EXECUTED means an approved
    action ran successfully and its ToolResult was recorded by the
    approval-outcome listener. FAILED covers every other terminal
    outcome and always carries its recorded result. Only recorded
    outcomes count for dependency readiness — status alone never proves
    success.

    RECOVERY_REQUIRED marks a mutating step whose outcome could not be
    proven (e.g. the process died after the tool started, or a mutating
    execution exceeded its time budget). It is deliberately NOT
    terminal: whether the side effect occurred is unknown, so automatic
    reporting of success or failure would be dishonest. It resolves only
    through explicit reconciliation (resolve-recovery endpoint) to a
    FAILED step with an ``execution_uncertain`` result, and it can never
    satisfy dependency readiness.
    """

    PENDING = "PENDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    COMPLETED = "COMPLETED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class PlanStep(BaseModel):
    """One bounded step inside a validated task plan."""

    step_id: str = Field(
        ...,
        min_length=1,
        max_length=MAX_STEP_ID_LENGTH,
        pattern=PLAN_STEP_ID_PATTERN,
        description="Plan-unique step identifier",
    )
    description: str = Field(
        ...,
        min_length=1,
        max_length=MAX_STEP_DESCRIPTION_LENGTH,
        description="Human-readable description of the step",
    )
    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=TOOL_NAME_PATTERN,
        description="Registered tool executed by this step",
    )
    arguments: dict[str, str] = Field(
        default_factory=dict,
        description="Bounded string arguments for the step tool",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Earlier step_ids that must complete first",
    )
    status: PlanStepStatus = PlanStepStatus.PENDING
    approval_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Exact approval bound to this step, if any",
    )
    result: Optional[ToolResult] = Field(
        default=None,
        description="Resolved execution result, once the step ran",
    )

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, str]) -> dict[str, str]:
        """Keep step arguments bounded and reject secret-like fields."""
        if len(value) > MAX_PAYLOAD_ITEMS:
            raise ValueError(f"arguments must not exceed {MAX_PAYLOAD_ITEMS} items")
        for key, argument_value in value.items():
            normalized_key = key.lower()
            if any(part in normalized_key for part in SENSITIVE_ARGUMENT_KEY_PARTS):
                raise ValueError("plan arguments must not contain secret fields")
            if len(key) > MAX_PAYLOAD_KEY_LENGTH:
                raise ValueError(
                    f"argument keys must not exceed {MAX_PAYLOAD_KEY_LENGTH} characters"
                )
            if len(argument_value) > MAX_PAYLOAD_VALUE_LENGTH:
                raise ValueError(
                    f"argument values must not exceed {MAX_PAYLOAD_VALUE_LENGTH} characters"
                )
        return dict(value)

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, value: list[str]) -> list[str]:
        """Bound and de-duplicate the step's dependency list."""
        if len(value) > MAX_DEPENDENCIES_PER_STEP:
            raise ValueError(
                f"depends_on must not exceed {MAX_DEPENDENCIES_PER_STEP} entries"
            )
        seen: set[str] = set()
        unique: list[str] = []
        for step_id in value:
            if step_id in seen:
                continue
            seen.add(step_id)
            unique.append(step_id)
        return unique


class TaskPlan(BaseModel):
    """A bounded, validated sequence of tool steps proposed for one goal."""

    plan_id: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PLAN_ID_LENGTH,
        pattern=r"^[a-zA-Z0-9_-]{1,64}$",
        description="Unique plan identifier",
    )
    goal: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PLAN_GOAL_LENGTH,
        description="What the plan is meant to achieve",
    )
    steps: list[PlanStep] = Field(
        ...,
        min_length=1,
        max_length=MAX_PLAN_STEPS,
        description="Bounded, ordered sequence of steps",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Optional conversation the plan belongs to",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Plan creation timestamp (UTC)",
    )

    @model_validator(mode="after")
    def validate_dependencies(self) -> "TaskPlan":
        """Enforce unique step ids and earlier-only, existing dependencies.

        Requiring every dependency to point at a strictly earlier step
        makes dependency cycles impossible by construction.
        """
        positions: dict[str, int] = {}
        for index, step in enumerate(self.steps):
            if step.step_id in positions:
                raise ValueError(
                    f"step_id '{step.step_id}' is not unique within the plan"
                )
            positions[step.step_id] = index

        for step in self.steps:
            for dependency in step.depends_on:
                position = positions.get(dependency)
                if position is None:
                    raise ValueError(
                        f"step '{step.step_id}' depends on unknown step '{dependency}'"
                    )
                if position >= positions[step.step_id]:
                    raise ValueError(
                        f"step '{step.step_id}' depends on step '{dependency}' "
                        "which does not come earlier"
                    )
        return self
