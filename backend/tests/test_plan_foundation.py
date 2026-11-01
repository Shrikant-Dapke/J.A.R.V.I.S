"""Phase 4 milestone 4.2 plan foundation tests.

Coverage: schema validation (unique ids, earlier-only dependencies,
bounds), registry fail-closed checks, ownership, readiness computation,
single-step execution through the approval workflow, and honest status
transitions. Approval outcomes (executed, rejected, expired, unavailable,
failed execution) are recorded against their step immediately, so a
purged approval record can never corrupt plan state. There is
intentionally no loop to test: each execution is an explicit call, and a
dependent step only becomes ready when its prerequisites recorded a
successful result.
"""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.plan import (
    MAX_PLAN_STEPS,
    PlanStep,
    PlanStepStatus,
    TaskPlan,
)
from app.schemas.tool import ToolDefinition, ToolResult
from app.services import open_application_tool
from app.services.approval_service import ApprovalService
from app.services.approval_store import (
    ApprovalNotFoundError,
    ApprovalStateError,
    ApprovalStore,
)
from app.services.plan_service import (
    PlanCapacityError,
    PlanNotFoundError,
    PlanService,
    PlanStepNotFoundError,
    PlanStepStateError,
    PlanValidationError,
)
from app.services.system_info_tool import SystemInfoTool
from app.services.tool_executor import ToolExecutor
from app.services.tool_registry import tool_registry


OWNER_ID = "plan-owner-hash"
SESSION_ID = "plan-session-0123456789"
OTHER_SESSION_ID = "plan-other-sess-012345678"

SYSTEM_INFO = "get_system_info"
OPEN_APP = "open_application"

TWO_STEP_PLAN = [
    {
        "step_id": "s1",
        "description": "Get system info",
        "tool_name": SYSTEM_INFO,
        "arguments": {},
        "depends_on": [],
    },
    {
        "step_id": "s2",
        "description": "Open notepad",
        "tool_name": OPEN_APP,
        "arguments": {"app_id": "notepad"},
        "depends_on": ["s1"],
    },
]

# Metadata-only read-only definition with no implementation anywhere:
# policy allows it, the executor refuses it (registered tooling mistake),
# and the plan must record an honest terminal failure.
UNIMPLEMENTED_TOOL_NAME = "stub_readonly_tool"

if not tool_registry.exists(UNIMPLEMENTED_TOOL_NAME):
    tool_registry.register(
        ToolDefinition(
            name=UNIMPLEMENTED_TOOL_NAME,
            description="Test-only read-only metadata stub (no implementation).",
            read_only=True,
            requires_approval=False,
        )
    )


def make_service(approvals=None, **kwargs):
    """PlanService wired to a fresh approval service for isolation."""
    if approvals is None:
        approvals = ApprovalService(
            registry=tool_registry,
            executor=ToolExecutor(tool_registry),
            store=ApprovalStore(),
        )
    return PlanService(registry=tool_registry, approvals=approvals, **kwargs)


def make_clock(start: datetime):
    """Return a mutable clock used to test retention and expiry."""
    moment = [start]
    return moment, lambda: moment[0]


# --- schema-level validation -------------------------------------------


def test_plan_rejects_duplicate_step_ids():
    raw_steps = [
        {"step_id": "same", "description": "A", "tool_name": SYSTEM_INFO},
        {"step_id": "same", "description": "B", "tool_name": SYSTEM_INFO},
    ]
    with pytest.raises(ValueError, match="not unique"):
        TaskPlan.model_validate(
            {"plan_id": "p1", "goal": "g", "steps": raw_steps}
        )


def test_plan_rejects_unknown_dependency():
    raw_steps = [
        {"step_id": "a", "description": "A", "tool_name": SYSTEM_INFO, "depends_on": ["ghost"]},
    ]
    with pytest.raises(ValueError, match="unknown step"):
        TaskPlan.model_validate(
            {"plan_id": "p1", "goal": "g", "steps": raw_steps}
        )


def test_plan_rejects_forward_dependency_cycles_by_construction():
    """A dependency on a LATER step is structurally impossible."""
    raw_steps = [
        {"step_id": "a", "description": "A", "tool_name": SYSTEM_INFO, "depends_on": ["b"]},
        {"step_id": "b", "description": "B", "tool_name": SYSTEM_INFO},
    ]
    with pytest.raises(ValueError, match="earlier"):
        TaskPlan.model_validate(
            {"plan_id": "p1", "goal": "g", "steps": raw_steps}
        )


def test_plan_rejects_self_dependency():
    raw_steps = [
        {"step_id": "a", "description": "A", "tool_name": SYSTEM_INFO, "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="earlier"):
        TaskPlan.model_validate(
            {"plan_id": "p1", "goal": "g", "steps": raw_steps}
        )


def test_plan_rejects_too_many_steps():
    raw_steps = [
        {
            "step_id": f"s{index}",
            "description": f"Step {index}",
            "tool_name": SYSTEM_INFO,
        }
        for index in range(MAX_PLAN_STEPS + 1)
    ]
    with pytest.raises(ValueError):
        TaskPlan.model_validate({"plan_id": "p1", "goal": "g", "steps": raw_steps})


def test_plan_rejects_secret_like_arguments():
    raw_steps = [
        {
            "step_id": "a",
            "description": "A",
            "tool_name": SYSTEM_INFO,
            "arguments": {"api_key": "should-not-store"},
        },
    ]
    with pytest.raises(ValueError, match="secret"):
        TaskPlan.model_validate(
            {"plan_id": "p1", "goal": "g", "steps": raw_steps}
        )


def test_plan_rejects_oversized_arguments():
    raw_steps = [
        {
            "step_id": "a",
            "description": "A",
            "tool_name": SYSTEM_INFO,
            "arguments": {"path": "x" * 600},
        },
    ]
    with pytest.raises(ValueError):
        TaskPlan.model_validate(
            {"plan_id": "p1", "goal": "g", "steps": raw_steps}
        )


# --- service-level behavior ---------------------------------------------


def test_create_plan_validates_against_registry():
    service = make_service()
    plan = service.create_plan(
        goal="Show info",
        steps=[{"step_id": "s1", "description": "Info", "tool_name": SYSTEM_INFO}],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )
    assert plan.steps[0].status is PlanStepStatus.PENDING

    with pytest.raises(PlanValidationError, match="not registered"):
        service.create_plan(
            goal="Impossible",
            steps=[
                {
                    "step_id": "s1",
                    "description": "Bad",
                    "tool_name": "not_a_tool",
                }
            ],
            owner_id=OWNER_ID,
            session_id=SESSION_ID,
        )


def test_plan_is_owner_scoped():
    service = make_service()
    plan = service.create_plan(
        goal="g",
        steps=TWO_STEP_PLAN,
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )

    from app.services.plan_service import PlanNotFoundError as NotFound

    with pytest.raises(NotFound):
        service.get_plan(plan.plan_id, OWNER_ID, OTHER_SESSION_ID)


def test_ready_steps_respect_dependencies():
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    assert service.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == ["s1"]


def test_request_step_executes_registry_read_only_once():
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    result = service.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )

    assert result.success is True
    updated = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert updated.steps[0].status is PlanStepStatus.COMPLETED
    assert updated.steps[1].status is PlanStepStatus.PENDING
    # No automatic advancement: the second step is simply now ready.
    assert service.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == ["s2"]


def test_request_step_blocks_unready_step():
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    with pytest.raises(PlanStepStateError, match="dependencies"):
        service.request_step_execution(plan.plan_id, "s2", OWNER_ID, SESSION_ID)


def test_request_step_rejects_duplicate_execution():
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)

    with pytest.raises(PlanStepStateError, match="not pending"):
        service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)


def test_request_step_unknown_plan_or_step():
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    with pytest.raises(PlanNotFoundError):
        service.request_step_execution("missing", "s1", OWNER_ID, SESSION_ID)
    with pytest.raises(PlanStepNotFoundError):
        service.request_step_execution(plan.plan_id, "ghost", OWNER_ID, SESSION_ID)


def test_mutating_step_goes_through_approval(monkeypatch):
    """Mutating plan steps create pending approvals and execute nothing."""
    from app.services import open_application_tool

    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)
    outcome = service.request_step_execution(
        plan.plan_id, "s2", OWNER_ID, SESSION_ID
    )

    from app.schemas.approval import ApprovalRequest

    assert isinstance(outcome, ApprovalRequest)
    assert outcome.status is ApprovalStatus.PENDING
    assert outcome.arguments == {"app_id": "notepad"}
    popen.assert_not_called()

    updated = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert updated.steps[1].status is PlanStepStatus.AWAITING_APPROVAL
    assert updated.steps[1].approval_id == outcome.approval_id

    # Approval executes once through the exact-argument workflow...
    approvals = service._approvals
    approvals.approve_request(outcome.approval_id, OWNER_ID, SESSION_ID)
    with pytest.raises(ApprovalStateError):
        approvals.approve_request(outcome.approval_id, OWNER_ID, SESSION_ID)
    popen.assert_called_once()
    # ...and the plan adopts the executed outcome immediately (recorded by
    # the approval-outcome listener, not by later re-reading the approval).
    resolved = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert resolved.steps[1].status is PlanStepStatus.EXECUTED
    assert resolved.steps[1].result is not None
    assert resolved.steps[1].result.success is True


def test_rejected_approval_reconciles_to_failed():
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)
    outcome = service.request_step_execution(
        plan.plan_id, "s2", OWNER_ID, SESSION_ID
    )

    service._approvals.reject_request(outcome.approval_id, OWNER_ID, SESSION_ID)

    updated = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert updated.steps[1].status is PlanStepStatus.FAILED
    assert updated.steps[1].result.error_code == "action_rejected"
    assert service.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == []


def test_approval_store_unavailable_never_executes(monkeypatch):
    """Capacity errors propagate: the step stays untouched, nothing runs."""
    from app.services.approval_store import ApprovalCapacityError

    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)

    monkeypatch.setattr(
        service._approvals._store,
        "create_approval_request",
        lambda *a, **k: (_ for _ in ()).throw(ApprovalCapacityError("full")),
    )
    with pytest.raises(ApprovalCapacityError):
        service.request_step_execution(plan.plan_id, "s2", OWNER_ID, SESSION_ID)

    updated = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert updated.steps[1].status is PlanStepStatus.PENDING


def test_record_result_rejects_final_state():
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)

    from app.schemas.tool import ToolResult

    with pytest.raises(PlanStepStateError, match="final state"):
        service.record_result(
            plan.plan_id,
            "s1",
            ToolResult.success_result(SYSTEM_INFO, "Again."),
            OWNER_ID,
            SESSION_ID,
        )


def test_plan_store_is_bounded():
    service = make_service(max_entries=1)
    service.create_plan(
        goal="one", steps=[TWO_STEP_PLAN[0]], owner_id=OWNER_ID, session_id=SESSION_ID
    )

    with pytest.raises(PlanCapacityError):
        service.create_plan(
            goal="two",
            steps=[TWO_STEP_PLAN[0]],
            owner_id=OWNER_ID,
            session_id=SESSION_ID,
        )


# --- approval outcome synchronization and record retention ---------------


def _retention_service(moment, clock):
    """PlanService whose approval store purges terminal records instantly."""
    store = ApprovalStore(terminal_retention_seconds=0, clock=clock)
    approvals = ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=store,
    )
    return make_service(approvals=approvals)


START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_executed_outcome_survives_approval_record_purge():
    """An executed approval keeps its recorded outcome; never 'unavailable'.

    Regression test for the reconcile-after-purge defect: with zero
    terminal retention the approval record is purged long before the
    client reads the plan, and the step must still report the executed
    action honestly.
    """
    moment, clock = make_clock(START)
    service = _retention_service(moment, clock)
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)
    outcome = service.request_step_execution(
        plan.plan_id, "s2", OWNER_ID, SESSION_ID
    )

    service._approvals.approve_request(outcome.approval_id, OWNER_ID, SESSION_ID)

    # Advance far past the zero-retention purge horizon.
    moment[0] = START + timedelta(minutes=10)

    with pytest.raises(ApprovalNotFoundError):
        service._approvals.get_request(outcome.approval_id, OWNER_ID, SESSION_ID)

    resolved = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert resolved.steps[1].status is PlanStepStatus.EXECUTED
    assert resolved.steps[1].result is not None
    assert resolved.steps[1].result.success is True
    assert resolved.steps[1].result.error_code is None


def test_rejected_outcome_survives_approval_record_purge():
    """A rejected approval stays FAILED('action_rejected') after purge."""
    moment, clock = make_clock(START)
    service = _retention_service(moment, clock)
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)
    outcome = service.request_step_execution(
        plan.plan_id, "s2", OWNER_ID, SESSION_ID
    )

    rejected = service._approvals.reject_request(
        outcome.approval_id, OWNER_ID, SESSION_ID
    )
    assert rejected.status is ApprovalStatus.REJECTED

    moment[0] = START + timedelta(minutes=10)
    with pytest.raises(ApprovalNotFoundError):
        service._approvals.get_request(outcome.approval_id, OWNER_ID, SESSION_ID)

    resolved = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert resolved.steps[1].status is PlanStepStatus.FAILED
    assert resolved.steps[1].result.error_code == "action_rejected"


def test_expired_approval_reconciles_to_failed():
    """A pending approval that ages past its TTL resolves approval_expired."""
    moment, clock = make_clock(START)
    store = ApprovalStore(ttl_seconds=1, clock=clock)
    approvals = ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=store,
    )
    service = make_service(approvals=approvals)
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)
    outcome = service.request_step_execution(
        plan.plan_id, "s2", OWNER_ID, SESSION_ID
    )

    moment[0] = START + timedelta(seconds=2)
    resolved = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert resolved.steps[1].status is PlanStepStatus.FAILED
    assert resolved.steps[1].result.error_code == "approval_expired"

    # Terminal state survives further time and record removal.
    moment[0] = START + timedelta(hours=1)
    final = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert final.steps[1].status is PlanStepStatus.FAILED
    assert final.steps[1].result.error_code == "approval_expired"


# --- dependency success semantics (M1) ------------------------------------


def test_failed_parent_never_readies_dependent_step():
    """A step whose approval was rejected cannot satisfy its dependents."""
    service = make_service()
    mutating_parent = [
        {
            "step_id": "p",
            "description": "Open notepad",
            "tool_name": OPEN_APP,
            "arguments": {"app_id": "notepad"},
            "depends_on": [],
        },
        {
            "step_id": "c",
            "description": "Read system info after",
            "tool_name": SYSTEM_INFO,
            "arguments": {},
            "depends_on": ["p"],
        },
    ]
    plan = service.create_plan(
        goal="g", steps=mutating_parent, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    outcome = service.request_step_execution(
        plan.plan_id, "p", OWNER_ID, SESSION_ID
    )
    service._approvals.reject_request(outcome.approval_id, OWNER_ID, SESSION_ID)

    assert service.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == []
    with pytest.raises(PlanStepStateError, match="dependencies"):
        service.request_step_execution(plan.plan_id, "c", OWNER_ID, SESSION_ID)


def test_approved_execution_failure_records_honest_plan_state():
    """Approve with a failing executor lands FAILED, not 'ready'."""
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    service.request_step_execution(plan.plan_id, "s1", OWNER_ID, SESSION_ID)
    outcome = service.request_step_execution(
        plan.plan_id, "s2", OWNER_ID, SESSION_ID
    )

    failing = Mock(side_effect=ValueError("no implementation"))
    service._approvals._executor.execute = failing

    failed_result = service._approvals.approve_request(
        outcome.approval_id, OWNER_ID, SESSION_ID
    )
    assert failed_result.success is False
    assert failed_result.error_code == "tool_unavailable"

    resolved = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert resolved.steps[1].status is PlanStepStatus.FAILED
    assert resolved.steps[1].result.error_code == "tool_unavailable"
    # The parent's success is irrelevant: no dependent step is satisfied
    # by a failed execution.
    assert service.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == []


# --- unimplemented registered tools ---------------------------------------


def test_unimplemented_readonly_step_records_terminal_failure():
    """A read-only metadata stub cannot leave the step pending forever."""
    service = make_service()
    plan = service.create_plan(
        goal="g",
        steps=[
            {
                "step_id": "s1",
                "description": "Use stub",
                "tool_name": UNIMPLEMENTED_TOOL_NAME,
                "arguments": {},
                "depends_on": [],
            }
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )

    result = service.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )

    assert result.success is False
    assert result.error_code == "tool_unavailable"
    resolved = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert resolved.steps[0].status is PlanStepStatus.FAILED
    assert resolved.steps[0].result.error_code == "tool_unavailable"
    assert service.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == []


# --- concurrency -----------------------------------------------------------


def test_concurrent_step_requests_execute_once():
    """Two racing requests for one step: one executes, one conflicts."""
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=TWO_STEP_PLAN, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    executions = []
    original_execute = SystemInfoTool.execute

    def counting_execute(self, arguments):
        executions.append(arguments)
        return original_execute(self, arguments)

    with patch.object(SystemInfoTool, "execute", counting_execute):
        barrier = threading.Barrier(2, timeout=10)
        outcomes: dict[str, object] = {}

        def worker():
            barrier.wait()
            try:
                outcomes["executed"] = service.request_step_execution(
                    plan.plan_id, "s1", OWNER_ID, SESSION_ID
                )
            except PlanStepStateError as exc:
                outcomes["conflict"] = str(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    assert len(executions) == 1
    assert set(outcomes) == {"executed", "conflict"}
    assert outcomes["executed"].success is True  # type: ignore[attr-defined]

    resolved = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert resolved.steps[0].status is PlanStepStatus.COMPLETED


# --- transient claim-to-execute window (F1) --------------------------------


def test_reconcile_during_execution_preserves_listener_outcome():
    """A plan read inside the claim window must not discard the outcome.

    Deterministic F1 regression, single-threaded by an executor hook so
    the interleaving is exact:
      1. the approval is claimed (store says EXECUTED),
      2. a reconcile-style plan read/ready check occurs BEFORE the
         outcome listener fires,
      3. the real tool execution completes,
      4. the outcome notification records the ToolResult,
      5. the plan reports the step EXECUTED with the correct ToolResult,
      6. the dependent becomes ready only because the actual result
         succeeded.
    """
    plan_steps = [
        {
            "step_id": "s2",
            "description": "Open notepad",
            "tool_name": OPEN_APP,
            "arguments": {"app_id": "notepad"},
            "depends_on": [],
        },
        {
            "step_id": "s3",
            "description": "Read system info after",
            "tool_name": SYSTEM_INFO,
            "arguments": {},
            "depends_on": ["s2"],
        },
    ]
    service = make_service()
    plan = service.create_plan(
        goal="g", steps=plan_steps, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    approval = service.request_step_execution(
        plan.plan_id, "s2", OWNER_ID, SESSION_ID
    )
    assert isinstance(approval, ApprovalRequest)

    observed_mid_flight: list[PlanStepStatus] = []
    real_execute = service._approvals._executor.execute

    def executing_probe(tool_name, arguments):
        # (1) The approval is already claimed at this instant.
        claimed = service._approvals.get_request(
            approval.approval_id, OWNER_ID, SESSION_ID
        )
        assert claimed.status is ApprovalStatus.EXECUTED
        # (2) Reconcile/read happens BEFORE the outcome notification.
        snapshot = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
        step = next(s for s in snapshot.steps if s.step_id == "s2")
        observed_mid_flight.append(step.status)
        # An unrecorded claim never satisfies dependencies.
        assert service.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == []
        # (3) The real execution completes.
        return ToolResult.success_result(
            tool_name=tool_name,
            message="The allowlisted application launch succeeded.",
            data={"outcome": "launched"},
        )

    service._approvals._executor.execute = executing_probe
    # (4) approve_request proceeds to notify the outcome listener itself.
    result = service._approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    assert result.success is True
    # Restore the real executor before exercising the dependent step.
    service._approvals._executor.execute = real_execute

    # The transient window was deferred, not terminalized blind:
    # pre-fix this observation was a terminal outcome-blind EXECUTED.
    assert observed_mid_flight == [PlanStepStatus.AWAITING_APPROVAL]

    # (5) The plan carries the authoritative ToolResult.
    resolved = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    s2 = next(step for step in resolved.steps if step.step_id == "s2")
    assert s2.status is PlanStepStatus.EXECUTED
    assert s2.result is not None
    assert s2.result.success is True
    assert s2.result.data == {"outcome": "launched"}

    # (6) Dependent readiness reflects the actual successful result...
    assert service.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == ["s3"]
    # ...and the dependent step is now genuinely executable.
    dependent = service.request_step_execution(
        plan.plan_id, "s3", OWNER_ID, SESSION_ID
    )
    assert dependent.success is True

    final = service.get_plan(plan.plan_id, OWNER_ID, SESSION_ID)
    assert next(
        step for step in final.steps if step.step_id == "s3"
    ).status is PlanStepStatus.COMPLETED
