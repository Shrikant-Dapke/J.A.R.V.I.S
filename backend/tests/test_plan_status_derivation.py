"""Plan status derivation and detail enrichment (Phase 5.2-c).

The derivation (``plan_service.derive_plan_status``) is pure: it reads
a validated plan and derives overall_status / ready_step_ids /
attention flags with an exact precedence. These tests pin every
precedence level and the explicitly-required edge cases, including
shapes that cannot be persisted (empty plans, unknown dependencies)
via ``model_construct`` — the derivation must remain deterministic and
fail closed for malformed data.

One API-level test covers the composed detail route; corruption
mapping is pinned separately (test_detail_corruption_api.py).
"""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.schemas.plan import PlanDetail, PlanOverallStatus, PlanStep, TaskPlan
from app.services.plan_service import (
    PlanCorruptError,
    derive_plan_status,
    plan_service,
)

API_SESSION_ID = "test-session-012345678901234567"


def _api_owner() -> str:
    """The token owner identity every HTTP call authenticates as."""
    return hashlib.sha256(
        settings.api_token.get_secret_value().encode("utf-8")
    ).hexdigest()


def _plan(steps: list[PlanStep], plan_id: str = "11111111-2222-3333-4444-555555555555") -> TaskPlan:
    return TaskPlan.model_validate(
        {
            "plan_id": plan_id,
            "goal": "derive probe",
            "steps": [step.model_dump() for step in steps],
        }
    )


def _step(
    step_id: str,
    *,
    status: str = "PENDING",
    depends_on: list[str] | None = None,
    result=None,
    tool_name: str = "stub_ro",
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=f"{step_id} description",
        tool_name=tool_name,
        arguments={"value": "x"},
        depends_on=depends_on or [],
        status=status,
        result=result,
    )


def _success(tool_name: str = "stub_ro", message: str = "ok"):
    from app.schemas.tool import ToolResult

    return ToolResult.success_result(tool_name=tool_name, message=message)


def _failure(tool_name: str = "stub_ro", message: str = "bad"):
    from app.schemas.tool import ToolResult

    return ToolResult.failure_result(
        tool_name=tool_name, error_code="probe_failure", message=message
    )


# --- precedence levels --------------------------------------------------------


def test_pending_only_plan_is_actionable():
    derived = derive_plan_status(
        _plan([_step("s1"), _step("s2", depends_on=["s1"])])
    )
    assert derived["overall_status"] is PlanOverallStatus.ACTIONABLE
    assert derived["ready_step_ids"] == ["s1"]  # s2 waits on s1
    assert derived["recovery_required"] == []
    assert derived["awaiting_approval"] == []
    assert derived["needs_attention"] is False


def test_recovery_required_outranks_everything():
    from app.schemas.plan import PlanStepStatus

    completed = _step("done", status="COMPLETED", result=_success())
    uncertain = _step("bad", status="RECOVERY_REQUIRED")
    ready = _step("next")
    derived = derive_plan_status(_plan([completed, uncertain, ready]))
    assert derived["overall_status"] is PlanOverallStatus.RECOVERY_REQUIRED
    assert derived["recovery_required"] == ["bad"]
    assert derived["ready_step_ids"] == ["next"]  # computed, not wiped
    assert derived["needs_attention"] is True


def test_awaiting_approval_outranks_actionable_and_failed():
    from app.schemas.plan import PlanStepStatus

    failed = _step("bad", status="FAILED", result=_failure())
    ready = _step("next")
    awaiting = _step("gate", status="AWAITING_APPROVAL")
    derived = derive_plan_status(_plan([failed, ready, awaiting]))
    assert derived["overall_status"] is PlanOverallStatus.AWAITING_APPROVAL
    assert derived["awaiting_approval"] == ["gate"]
    assert derived["ready_step_ids"] == ["next"]
    assert derived["needs_attention"] is True


def test_actionable_outranks_failed_per_precedence_order():
    failed = _step("bad", status="FAILED", result=_failure())
    ready = _step("next")  # independent branch, dependency-free
    derived = derive_plan_status(_plan([failed, ready]))
    assert derived["overall_status"] is PlanOverallStatus.ACTIONABLE
    assert derived["ready_step_ids"] == ["next"]
    assert derived["needs_attention"] is False


def test_failed_shell_blocks_dependents_and_wins_over_in_progress():
    parent = _step("bad", status="FAILED", result=_failure())
    child = _step("later", depends_on=["bad"])  # permanently not ready
    derived = derive_plan_status(_plan([parent, child]))
    assert derived["overall_status"] is PlanOverallStatus.FAILED
    assert derived["ready_step_ids"] == []
    # Honest failure needs no attention flag; dependents stay blocked either way.
    assert derived["needs_attention"] is False


def test_in_progress_when_pending_chains_cannot_start():
    """Defensive edge: a pending step blocked by an UNKNOWN dependency.

    Valid plans cannot contain unknown dependencies (the schema
    forbids them), but the derivation must stay deterministic if one
    ever exists: the missing dependency is simply unsatisfied. With
    the root step not ready and nothing failed/awaiting, the plan is
    in_progress. Constructed via model_construct (validation bypass)
    because this shape cannot be persisted.
    """
    from app.schemas.plan import PlanStepStatus

    plan = TaskPlan.model_construct(
        plan_id="defensive-probe",
        goal="unknown dependency",
        steps=[
            PlanStep.model_construct(
                step_id="s1",
                description="blocked step",
                tool_name="stub_ro",
                arguments={},
                depends_on=["ghost"],
                status=PlanStepStatus.PENDING,
            )
        ],
        created_at=None,
        conversation_id=None,
    )
    derived = derive_plan_status(plan)
    assert derived["overall_status"] is PlanOverallStatus.IN_PROGRESS
    assert derived["ready_step_ids"] == []
    assert derived["needs_attention"] is False


def test_completed_plan_and_executed_variant():
    completed = _step("s1", status="COMPLETED", result=_success())
    derived = derive_plan_status(_plan([completed]))
    assert derived["overall_status"] is PlanOverallStatus.COMPLETED
    assert derived["ready_step_ids"] == []

    from app.schemas.plan import PlanStepStatus

    executed = _step("s2", status="EXECUTED", result=_success())
    derived = derive_plan_status(_plan([completed, executed]))
    assert derived["overall_status"] is PlanOverallStatus.COMPLETED


def test_completed_precedes_nothing_but_requires_recorded_results():
    """A terminal-success status with NO recorded result stays honest:
    the plan is failed, never completed, and its dependents stay blocked."""
    outcome_blind = _step("fake", status="COMPLETED", result=None)
    dependent = _step("later", depends_on=["fake"])
    derived = derive_plan_status(_plan([outcome_blind, dependent]))
    assert derived["overall_status"] is PlanOverallStatus.FAILED
    assert derived["ready_step_ids"] == []


def test_failed_wins_at_same_level_over_in_progress():
    """A FAILED step and a pending chain whose root is not ready —
    resolvable only by recorded-evidence rules — resolves failed."""
    from app.schemas.plan import PlanStepStatus

    plan = TaskPlan.model_construct(
        plan_id="mixed-probe",
        goal="mixed terminal and unresolved",
        steps=[
            PlanStep.model_construct(
                step_id="bad",
                description="failed step",
                tool_name="stub_ro",
                arguments={},
                depends_on=[],
                status=PlanStepStatus.FAILED,
                result=_failure(),
            ),
            PlanStep.model_construct(
                step_id="waiter",
                description="behind failed",
                tool_name="stub_ro",
                arguments={},
                depends_on=["bad"],
                status=PlanStepStatus.PENDING,
            ),
        ],
        created_at=None,
        conversation_id=None,
    )
    derived = derive_plan_status(plan)
    assert derived["overall_status"] is PlanOverallStatus.FAILED


def test_mixed_terminal_and_unresolved_resolves_to_worst_outstanding():
    from app.schemas.plan import PlanStepStatus

    steps = [
        _step("done", status="COMPLETED", result=_success()),
        _step("ran", status="EXECUTED", result=_success()),
        _step("bad", status="RECOVERY_REQUIRED"),
        _step("gated", status="AWAITING_APPROVAL"),
        _step("blocked", depends_on=["bad"]),
    ]
    derived = derive_plan_status(_plan(steps))
    assert derived["overall_status"] is PlanOverallStatus.RECOVERY_REQUIRED
    assert derived["ready_step_ids"] == []
    assert derived["needs_attention"] is True
    # Lists follow plan step order deterministically.
    assert derived["awaiting_approval"] == ["gated"]


# --- edge cases -----------------------------------------------------------------


def test_empty_plan_fails_closed():
    plan = TaskPlan.model_construct(plan_id="empty", goal="none", steps=[])
    with pytest.raises(PlanCorruptError) as excinfo:
        derive_plan_status(plan)
    assert str(excinfo.value) == "Plan state could not be validated."


def test_unknown_step_states_are_impossible_post_validation():
    """The schema parses exactly the six enum values; anything else is a
    parse-time corruption (validated upstream), which this test pins."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _step("s1", status="NOT_A_STATUS")


def test_derivation_is_pure_and_does_not_reconcile():
    from app.schemas.plan import PlanStepStatus

    awaiting = _step("gate", status="AWAITING_APPROVAL")
    plan = _plan([awaiting])
    before = plan.model_dump()
    derive_plan_status(plan)
    assert plan.model_dump() == before  # nothing mutated


def test_ready_ids_follow_plan_step_order():
    derived = derive_plan_status(_plan([_step("b1"), _step("a2")]))
    assert derived["ready_step_ids"] == ["b1", "a2"]


def test_plan_detail_schema_composition():
    steps = [_step("s1", status="COMPLETED", result=_success()), _step("s2")]
    plan = _plan(steps)
    detail = PlanDetail(**plan.model_dump(), **derive_plan_status(plan))
    assert detail.plan_id == plan.plan_id
    assert detail.overall_status is PlanOverallStatus.ACTIONABLE
    assert detail.ready_step_ids == ["s2"]
    assert detail.recovery_required == []
    assert detail.awaiting_approval == []
    assert detail.needs_attention is False
    # Serialized form uses the lowercase precedence vocabulary.
    assert detail.model_dump(mode="json")["overall_status"] == "actionable"


# --- API level ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_route_serves_derived_status(api_headers):
    owner_id = _api_owner()
    plan = plan_service.create_plan(
        goal="derive route probe",
        steps=[
            {
                "step_id": "s1",
                "description": "only step",
                "tool_name": "get_system_info",
                "arguments": {},
            }
        ],
        owner_id=owner_id,
        session_id=API_SESSION_ID,
    )
    plan_service.record_result(
        plan.plan_id,
        "s1",
        _success(tool_name="get_system_info"),
        owner_id=owner_id,
        session_id=API_SESSION_ID,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers=api_headers,
    ) as client:
        response = await client.get(f"/api/plans/{plan.plan_id}")
    assert response.status_code == 200
    data = response.json()
    # Enrichment fields present alongside the existing TaskPlan fields.
    assert data["overall_status"] == "completed"
    assert data["ready_step_ids"] == []
    assert data["recovery_required"] == []
    assert data["awaiting_approval"] == []
    assert data["needs_attention"] is False
    assert data["steps"][0]["status"] == "COMPLETED"
