"""Crash and restart recovery scenarios (Phase 4.3).

Each test drives a REAL service generation to a crash point (claim,
started boundary, persisted outcome — exactly the states the approval
workflow leaves behind), closes its file-backed façade, and then runs
startup recovery from a second generation over the same file.

Nothing here re-executes an action: recovery restores honest state
(idempotently) and leaves uncertain side effects to explicit
reconciliation.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.schemas.approval import ApprovalStatus
from app.schemas.plan import PlanStepStatus
from app.schemas.tool import ToolResult
from app.services.execution_db import ExecutionStateDb
from app.services.execution_lifecycle import (
    AttemptStatus,
    EVENT_EXECUTION_INTERRUPTED,
    EVENT_RECOVERY_REQUIRED,
    EVENT_RECOVERY_OUTCOME_APPLIED,
)
from app.services.approval_store import ApprovalStateError
from app.services.recovery_service import run_startup_recovery

from tests._exec_helpers import (
    OWNER_ID,
    SESSION_ID,
    FailingMutatingTool,
    HangingMutatingTool,
    InstantMutatingTool,
    make_graph,
    release_hanging_tools,
)


@pytest.fixture
def hanging_cleanup():
    yield
    release_hanging_tools()


def _bind_plan_step_to_approval(graph, tool_name, arguments):
    """Drive a real graph to the 'approval pending for step' crash state."""
    plan = graph.plans.create_plan(
        goal="Recovery probe",
        steps=[
            {
                "step_id": "s1",
                "description": "One mutating step",
                "tool_name": tool_name,
                "arguments": arguments,
                "depends_on": [],
            }
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    assert isinstance(approval, object) and approval.approval_id
    return plan, approval


def _crash_after_claim(graph, approval):
    """Reproduce the exact persisted state of a crash after the claim:
    approval consumed single-use, claimed attempt persisted, nothing executed."""
    graph.store.approve_request(approval.approval_id, OWNER_ID, SESSION_ID)
    graph.store.claim_approved_for_execution(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    attempt_id = str(uuid4())
    graph.db.create_attempt(
        attempt_id=attempt_id,
        approval_id=approval.approval_id,
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
        tool_name=approval.tool_name,
        mutating=True,
        status=AttemptStatus.CLAIMED,
    )
    return attempt_id


def _restart(path, *, with_hanging=True):
    """Second service generation over the same file (fresh objects)."""
    reopened = ExecutionStateDb(path=path)
    return make_graph(state_db=reopened, with_hanging=with_hanging)


EVENT_COUNTS = ("interrupted", "recovery_required", "outcome_applied")


def _assert_quiet_recovery(counts):
    assert all(counts[k] == 0 for k in EVENT_COUNTS)


# --- claimed, never started: provably no side effect --------------------------


def test_claimed_never_started_resolves_interrupted(tmp_path):
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path))
    plan, approval = _bind_plan_step_to_approval(
        graph, HangingMutatingTool.name, {"value": "v"}
    )
    _crash_after_claim(graph, approval)
    graph.db.close()

    gen2 = _restart(path)
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    assert counts["interrupted"] == 1
    assert counts["recovery_required"] == 0
    assert counts["outcome_applied"] == 0

    step = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.FAILED
    assert step.result is not None
    assert step.result.error_code == "execution_interrupted"
    assert "never took effect" in step.result.message

    attempt = gen2.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "FAILED"

    events = {e["event"] for e in gen2.db.recent_events(100)}
    assert EVENT_EXECUTION_INTERRUPTED in events

    # Idempotent second pass: nothing new happens.
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    _assert_quiet_recovery(counts)
    interrupted_events = [
        e for e in gen2.db.recent_events(200)
        if e["event"] == EVENT_EXECUTION_INTERRUPTED
    ]
    assert len(interrupted_events) == 1


def test_claim_committed_but_attempt_row_lost_resolves_interrupted(tmp_path):
    """A crash between the claim and the attempt insert still classifies
    as provably-not-started: no tool side effect was possible."""
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path))
    plan, approval = _bind_plan_step_to_approval(
        graph, InstantMutatingTool.name, {"value": "v"}
    )
    graph.store.approve_request(approval.approval_id, OWNER_ID, SESSION_ID)
    graph.store.claim_approved_for_execution(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    graph.db.close()

    gen2 = _restart(path, with_hanging=False)
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    assert counts["interrupted"] == 1
    assert counts["recovery_required"] == 0
    assert counts["outcome_applied"] == 0

    step = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.FAILED
    assert step.result.error_code == "execution_interrupted"


# --- started boundary: side effect unknown, human reconciliation only ---------


def test_started_boundary_requires_explicit_reconciliation(tmp_path, hanging_cleanup):
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path))
    plan, approval = _bind_plan_step_to_approval(
        graph, HangingMutatingTool.name, {"value": "v"}
    )
    attempt_id = _crash_after_claim(graph, approval)
    graph.db.transit_attempt(attempt_id, AttemptStatus.STARTED)  # crash here
    graph.db.close()

    gen2 = _restart(path)
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    assert counts["recovery_required"] == 1

    step = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.RECOVERY_REQUIRED
    # The mutation boundary STANDS: the attempt stays open non-terminal.
    attempt = gen2.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "STARTED"
    assert gen2.db.get_open_attempts() != []

    events = {e["event"] for e in gen2.db.recent_events(100)}
    assert EVENT_RECOVERY_REQUIRED in events

    # A second scan must NOT re-mark, re-count, or re-log.
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    _assert_quiet_recovery(counts)
    recovery_events = [
        e for e in gen2.db.recent_events(200) if e["event"] == EVENT_RECOVERY_REQUIRED
    ]
    assert len(recovery_events) == 1

    # Unique-open-attempt idempotency survives the restart: the consumed
    # approval can never open a second attempt (claim is single-use).
    with pytest.raises(ApprovalStateError):
        gen2.store.claim_approved_for_execution(
            approval.approval_id, OWNER_ID, SESSION_ID
        )

    # Explicit human reconciliation completes the lifecycle honestly.
    resolved = gen2.plans.resolve_step_recovery(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    step = resolved.steps[0]
    assert step.status is PlanStepStatus.FAILED
    assert step.result.error_code == "execution_uncertain"
    attempt = gen2.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "RECONCILED"

    # ... and it is exactly-once.
    from app.services.plan_service import PlanStepStateError

    with pytest.raises(PlanStepStateError):
        gen2.plans.resolve_step_recovery(plan.plan_id, "s1", OWNER_ID, SESSION_ID)


def test_timed_out_without_recorded_outcome_requires_reconciliation(tmp_path, hanging_cleanup):
    """A crash between a mutating timeout and the uncertain notification
    leaves the same standing boundary as a started crash."""
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path))
    plan, approval = _bind_plan_step_to_approval(
        graph, HangingMutatingTool.name, {"value": "v"}
    )
    attempt_id = _crash_after_claim(graph, approval)
    graph.db.transit_attempt(attempt_id, AttemptStatus.STARTED)
    graph.db.transit_attempt(attempt_id, AttemptStatus.TIMED_OUT)  # crash here
    graph.db.close()

    gen2 = _restart(path)
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    assert counts["recovery_required"] == 1

    step = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.RECOVERY_REQUIRED
    attempt = gen2.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "TIMED_OUT"
    assert attempt.outcome is None

    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    _assert_quiet_recovery(counts)


# --- proven outcomes are applied verbatim ------------------------------------


def test_proven_terminal_outcomes_are_replayed_once(tmp_path):
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path))

    # Success case: outcome durably recorded, listener never delivered.
    plan, approval = _bind_plan_step_to_approval(
        graph, InstantMutatingTool.name, {"value": "v"}
    )
    attempt_id = _crash_after_claim(graph, approval)
    success = ToolResult.success_result(
        tool_name=InstantMutatingTool.name, message="applied", data={"value": "v"}
    )
    graph.db.transit_attempt(attempt_id, AttemptStatus.STARTED)
    graph.db.transit_attempt(attempt_id, AttemptStatus.SUCCEEDED, outcome=success)

    # Failure case on a second independent plan.
    plan_b, approval_b = _bind_plan_step_to_approval(
        graph, FailingMutatingTool.name, {"value": "f"}
    )
    attempt_b = _crash_after_claim(graph, approval_b)
    failure = ToolResult.failure_result(
        tool_name=FailingMutatingTool.name,
        error_code="stub_mut_failure",
        message="Mutating stub failed deterministically.",
    )
    graph.db.transit_attempt(attempt_b, AttemptStatus.STARTED)
    graph.db.transit_attempt(attempt_b, AttemptStatus.FAILED, outcome=failure)
    graph.db.close()

    gen2 = _restart(path, with_hanging=False)
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    assert counts["outcome_applied"] == 2

    step_a = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step_a.status is PlanStepStatus.EXECUTED
    assert step_a.result.success is True
    assert step_a.result.message == "applied"  # verbatim evidence

    step_b = gen2.plans.get_plan(plan_b.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step_b.status is PlanStepStatus.FAILED
    assert step_b.result.error_code == "stub_mut_failure"

    events = {e["event"] for e in gen2.db.recent_events(200)}
    assert EVENT_RECOVERY_OUTCOME_APPLIED in events

    # Second pass: already-resolved steps are never overwritten.
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    _assert_quiet_recovery(counts)
    step_a = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step_a.status is PlanStepStatus.EXECUTED


# --- live approvals (PENDING / APPROVED-but-unclaimed) stay untouched ---------


def test_recovery_leaves_live_approvals_claimable(tmp_path, hanging_cleanup):
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path))
    plan_pending, approval_pending = _bind_plan_step_to_approval(
        graph, InstantMutatingTool.name, {"value": "p1"}
    )
    plan_approved, approval_approved = _bind_plan_step_to_approval(
        graph, InstantMutatingTool.name, {"value": "p2"}
    )
    graph.store.approve_request(approval_approved.approval_id, OWNER_ID, SESSION_ID)
    graph.db.close()

    gen2 = _restart(path, with_hanging=False)
    counts = run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    _assert_quiet_recovery(counts)

    step = gen2.plans.get_plan(plan_pending.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.AWAITING_APPROVAL
    record = gen2.store.get_approval_request(
        approval_pending.approval_id, OWNER_ID, SESSION_ID
    )
    assert record.status is ApprovalStatus.PENDING

    # APPROVED-but-unclaimed: a prior process approved and died before
    # the claim. The next approve continues the SAME single-use flow.
    result = gen2.approvals.approve_request(
        approval_approved.approval_id, OWNER_ID, SESSION_ID
    )
    assert result.success is True
    step = gen2.plans.get_plan(plan_approved.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.EXECUTED

    from app.services.approval_store import ApprovalStateError

    with pytest.raises(ApprovalStateError):
        gen2.approvals.approve_request(
            approval_approved.approval_id, OWNER_ID, SESSION_ID
        )


def test_expired_step_resolves_on_read_after_restart(tmp_path):
    """Terminal retention / expiry semantics keep working across restart.
    (TTL-level approval expiry is unit-covered in the phase-3 suite; here
    we revisit that the plan step mirrors it honestly after a restart.)"""
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path))
    plan, approval = _bind_plan_step_to_approval(
        graph, InstantMutatingTool.name, {"value": "e"}
    )
    graph.db.close()

    gen2 = _restart(path, with_hanging=False)
    run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    gen2.store.expire_request(approval.approval_id, OWNER_ID, SESSION_ID)
    step = gen2.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.FAILED
    assert step.result.error_code == "approval_expired"


def test_attempt_finalized_on_interrupt_cannot_suppress_evidence(tmp_path):
    """The interrupted attempt keeps its rows: errors are recorded once,
    and approval replay stays impossible after recovery."""
    path = str(tmp_path / "state.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path))
    _plan_unused, approval = _bind_plan_step_to_approval(
        graph, InstantMutatingTool.name, {"value": "x"}
    )
    _crash_after_claim(graph, approval)
    graph.db.close()

    gen2 = _restart(path, with_hanging=False)
    run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    assert gen2.db.get_open_attempts() == []
    record = gen2.db.get_approval_record(approval.approval_id)
    assert record.request.status is ApprovalStatus.EXECUTED  # still consumed
    with pytest.raises(ApprovalStateError):
        gen2.store.claim_approved_for_execution(
            approval.approval_id, OWNER_ID, SESSION_ID
        )
