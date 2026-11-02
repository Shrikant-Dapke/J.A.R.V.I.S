"""Plan persistence semantics (Phase 4.3): plans, step states, and
approval bindings survive restarts; identity isolation and capacity
bounds persist with them. Performed across two service generations
over the same file-backed façade.
"""

import pytest

from app.schemas.plan import PlanStepStatus
from app.schemas.tool import ToolResult
from app.services.execution_db import ExecutionStateDb
from app.services.plan_service import (
    PlanCapacityError,
    PlanNotFoundError,
    PlanValidationError,
)

from tests._exec_helpers import (
    OTHER_SESSION_ID,
    OWNER_ID,
    SESSION_ID,
    FailingMutatingTool,
    FailingReadOnlyStubTool,
    InstantMutatingTool,
    ReadOnlyStubTool,
    _ValueInput,
    make_graph,
)


def test_plans_steps_and_outcomes_survive_restart(tmp_path):
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)

    plan = graph.plans.create_plan(
        goal="Survive a restart",
        steps=[
            {
                "step_id": "read",
                "description": "read something",
                "tool_name": ReadOnlyStubTool.name,
                "arguments": {"value": "r"},
                "depends_on": [],
            },
            {
                "step_id": "write",
                "description": "mutate something",
                "tool_name": InstantMutatingTool.name,
                "arguments": {"value": "w"},
                "depends_on": ["read"],
            },
            {
                "step_id": "later",
                "description": "still unused",
                "tool_name": FailingReadOnlyStubTool.name,
                "arguments": {"value": "l"},
                "depends_on": ["write"],
            },
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )

    first = graph.plans.request_step_execution(
        plan.plan_id, "read", OWNER_ID, SESSION_ID
    )
    assert first.success is True
    approval = graph.plans.request_step_execution(
        plan.plan_id, "write", OWNER_ID, SESSION_ID
    )
    outcome = graph.approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    assert outcome.success is True
    graph.approvals.execution_db.close()

    gen2 = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    restored = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert restored.goal == "Survive a restart"
    statuses = {s.step_id: s.status for s in restored.steps}
    results = {s.step_id: s.result for s in restored.steps}
    assert statuses["read"] is PlanStepStatus.COMPLETED
    assert statuses["write"] is PlanStepStatus.EXECUTED
    assert statuses["later"] is PlanStepStatus.PENDING  # untouched
    assert results["read"].success is True
    assert results["write"].success is True
    assert gen2.plans.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == ["later"]


def test_failed_mutating_step_survives_restart_with_evidence(tmp_path):
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    plan = graph.plans.create_plan(
        goal="g",
        steps=[
            {
                "step_id": "s1",
                "description": "fails",
                "tool_name": FailingMutatingTool.name,
                "arguments": {"value": "f"},
                "depends_on": [],
            }
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    outcome = graph.approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    assert outcome.success is False
    graph.approvals.execution_db.close()

    gen2 = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    step = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.FAILED
    assert step.result.success is False
    # Executed-but-failed parent never unblocks dependents after restart.
    assert gen2.plans.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == []


def test_identity_isolation_survives_restart(tmp_path):
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    plan = graph.plans.create_plan(
        goal="secret goal",
        steps=[
            {
                "step_id": "s1",
                "description": "d",
                "tool_name": ReadOnlyStubTool.name,
                "arguments": {"value": "v"},
                "depends_on": [],
            }
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )
    graph.approvals.execution_db.close()

    gen2 = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    with pytest.raises(PlanNotFoundError):
        gen2.plans.get_plan(plan.plan_id, OWNER_ID, OTHER_SESSION_ID)
    with pytest.raises(PlanNotFoundError):
        gen2.plans.get_plan(plan.plan_id, OTHER_SESSION_ID, SESSION_ID)


def test_plan_capacity_is_bounded():
    graph = make_graph(with_hanging=False)
    for index in range(2):
        graph.plans.create_plan(
            goal=f"g{index}",
            steps=[
                {
                    "step_id": "s",
                    "description": "d",
                    "tool_name": ReadOnlyStubTool.name,
                    "arguments": {"value": "v"},
                    "depends_on": [],
                }
            ],
            owner_id=OWNER_ID,
            session_id=SESSION_ID,
        )
    # Default bound is far above two PlanServices in the same façade:
    # a custom bounded service proves the capacity guard fails closed.
    bounded = make_graph(with_hanging=False)
    from app.services.plan_service import PlanService

    small = PlanService(
        registry=bounded.registry,
        approvals=bounded.approvals,
        max_entries=1,
    )
    small.create_plan(
        goal="first",
        steps=[
            {
                "step_id": "s",
                "description": "d",
                "tool_name": ReadOnlyStubTool.name,
                "arguments": {"value": "v"},
                "depends_on": [],
            }
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )
    with pytest.raises(PlanCapacityError):
        small.create_plan(
            goal="second",
            steps=[
                {
                    "step_id": "s",
                    "description": "d",
                    "tool_name": ReadOnlyStubTool.name,
                    "arguments": {"value": "v"},
                    "depends_on": [],
                }
            ],
            owner_id=OWNER_ID,
            session_id=SESSION_ID,
        )


def test_unknown_tool_and_secret_arguments_rejected_before_persistence():
    graph = make_graph(with_hanging=False)
    with pytest.raises(PlanValidationError):
        graph.plans.create_plan(
            goal="g",
            steps=[
                {
                    "step_id": "s1",
                    "description": "d",
                    "tool_name": "format_hard_drive",
                    "arguments": {},
                    "depends_on": [],
                }
            ],
            owner_id=OWNER_ID,
            session_id=SESSION_ID,
        )

    # Secret-like argument keys are rejected by the step schema; the
    # service layer surfaces this as a plan-validation failure and
    # persists nothing.
    with pytest.raises(PlanValidationError):
        graph.plans.create_plan(
            goal="g",
            steps=[
                {
                    "step_id": "s1",
                    "description": "d",
                    "tool_name": ReadOnlyStubTool.name,
                    "arguments": {"api_key": "sk-never-store-this"},
                    "depends_on": [],
                }
            ],
            owner_id=OWNER_ID,
            session_id=SESSION_ID,
        )
    # Nothing was persisted by the rejected attempts beyond the empty store.
    assert graph.db.count_plans() == 0
