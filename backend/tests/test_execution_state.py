"""Execution-state façade and central lifecycle validation (Phase 4.3).

Covers the persisted-state contract the rest of the reliability work
depends on: schema migration, transactional saves, immutable plan
identity, the unique-open-attempt-per-approval idempotency index,
central transition validation, purge semantics, and survival across
close/reopen of file-backed databases.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.plan import PlanStep, PlanStepStatus, TaskPlan
from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolResult
from app.services.execution_db import (
    DEFAULT_EVENT_RETENTION,
    ExecutionConflictError,
    ExecutionStateDb,
    ExecutionStateError,
    resolve_state_db_path,
)
from app.services.execution_lifecycle import (
    AttemptStatus,
    ExecutionLifecycleError,
    validate_attempt_transition,
    validate_plan_step_transition,
)


def _plan(step_status: PlanStepStatus = PlanStepStatus.PENDING,
          approval_id=None, result=None) -> TaskPlan:
    step = PlanStep(
        step_id="s1",
        description="Do one bounded thing",
        tool_name="stub_ro",
        arguments={},
        depends_on=[],
        status=step_status,
        approval_id=approval_id,
        result=result,
    )
    return TaskPlan(plan_id=str(uuid4()), goal="g", steps=[step])


def _approval(status=ApprovalStatus.PENDING, resolved_at=None) -> ApprovalRequest:
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ApprovalRequest(
        approval_id=str(uuid4()),
        tool_name="stub_mut_ok",
        arguments={"value": "v"},
        action_description="stub: apply effect",
        risk_level=RiskLevel.LOW_RISK,
        created_at=created,
        expires_at=created + timedelta(seconds=300),
        status=status,
        resolved_at=resolved_at,
    )


# --- central transition validation ------------------------------------------

def test_attempt_transitions_central_and_fail_closed():
    validate_attempt_transition(AttemptStatus.CLAIMED, AttemptStatus.STARTED)
    validate_attempt_transition(AttemptStatus.CLAIMED, AttemptStatus.FAILED)
    validate_attempt_transition(AttemptStatus.STARTED, AttemptStatus.SUCCEEDED)
    validate_attempt_transition(AttemptStatus.STARTED, AttemptStatus.TIMED_OUT)
    validate_attempt_transition(AttemptStatus.STARTED, AttemptStatus.RECONCILED)
    validate_attempt_transition(AttemptStatus.TIMED_OUT, AttemptStatus.RECONCILED)

    with pytest.raises(ExecutionLifecycleError):
        validate_attempt_transition(AttemptStatus.CLAIMED, AttemptStatus.SUCCEEDED)
    with pytest.raises(ExecutionLifecycleError):
        validate_attempt_transition(AttemptStatus.TIMED_OUT, AttemptStatus.SUCCEEDED)
    with pytest.raises(ExecutionLifecycleError):
        validate_attempt_transition(AttemptStatus.FAILED, AttemptStatus.STARTED)


def test_plan_step_transitions_central_and_fail_closed():
    validate_plan_step_transition("PENDING", "AWAITING_APPROVAL")
    validate_plan_step_transition("PENDING", "COMPLETED")
    validate_plan_step_transition("PENDING", "FAILED")
    validate_plan_step_transition("AWAITING_APPROVAL", "EXECUTED")
    validate_plan_step_transition("AWAITING_APPROVAL", "RECOVERY_REQUIRED")
    validate_plan_step_transition("RECOVERY_REQUIRED", "FAILED")

    with pytest.raises(ExecutionLifecycleError):
        validate_plan_step_transition("FAILED", "COMPLETED")
    with pytest.raises(ExecutionLifecycleError):
        validate_plan_step_transition("COMPLETED", "RECOVERY_REQUIRED")
    with pytest.raises(ExecutionLifecycleError):
        validate_plan_step_transition("RECOVERY_REQUIRED", "EXECUTED")
    with pytest.raises(ExecutionLifecycleError):
        validate_plan_step_transition("RECOVERY_REQUIRED", "PENDING")


# --- schema and migration ----------------------------------------------------

def test_file_schema_is_created_and_versioned(tmp_path):
    path = str(tmp_path / "state.db")
    db = ExecutionStateDb(path=path)
    row = db._conn.execute("PRAGMA user_version").fetchone()
    assert int(row[0]) == 1
    names = {
        r["name"]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {
        "plans", "plan_steps", "approval_requests",
        "execution_attempts", "execution_events",
    } <= names
    db.close()
    # Reopening stays on version 1 without recreating anything.
    db2 = ExecutionStateDb(path=path)
    assert int(db2._conn.execute("PRAGMA user_version").fetchone()[0]) == 1
    db2.close()


def test_unsupported_newer_schema_fails_closed(tmp_path):
    path = str(tmp_path / "future.db")
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    with pytest.raises(ExecutionStateError):
        ExecutionStateDb(path=path)


def test_resolve_default_path_is_local_data_dir():
    resolved = resolve_state_db_path()
    assert resolved.endswith("jarvis_state.db")


# --- plans -------------------------------------------------------------------

def test_plan_round_trip_and_identity_immutability():
    db = ExecutionStateDb()
    plan = _plan()
    db.save_plan(plan, "owner-a", "sess-a")
    record = db.get_plan_record(plan.plan_id)
    assert record.owner_id == "owner-a"
    assert record.session_id == "sess-a"
    assert record.plan.plan_id == plan.plan_id

    drifted = _plan()
    drifted.plan_id = plan.plan_id
    with pytest.raises(ExecutionStateError):
        db.save_plan(drifted, "owner-b", "sess-a")


def test_plan_step_index_tracks_status_and_unknown_approval_lookup():
    db = ExecutionStateDb()
    with_approval = _plan(step_status=PlanStepStatus.AWAITING_APPROVAL,
                          approval_id="approval-1")
    db.save_plan(with_approval, "owner-a", "sess-a")

    assert db.find_step_ref_by_approval(
        "approval-1", "owner-a", "sess-a"
    ) == (with_approval.plan_id, "s1")
    # Foreign identity sees nothing.
    assert db.find_step_ref_by_approval(
        "approval-1", "owner-b", "sess-a"
    ) is None

    resolved = _plan(step_status=PlanStepStatus.RECOVERY_REQUIRED,
                     approval_id="approval-1")
    resolved.plan_id = with_approval.plan_id
    db.save_plan(resolved, "owner-a", "sess-a")
    assert db.find_steps_with_status(
        PlanStepStatus.RECOVERY_REQUIRED
    ) == [(with_approval.plan_id, "s1", "owner-a", "sess-a", "approval-1")]


def test_plan_count_is_global_for_bounded_capacity():
    db = ExecutionStateDb()
    db.save_plan(_plan(), "owner-a", "sess-a")
    db.save_plan(_plan(), "owner-b", "sess-b")
    assert db.count_plans() == 2


# --- attempts ----------------------------------------------------------------

def test_attempt_open_unique_per_approval_is_backend_idempotency():
    db = ExecutionStateDb()
    approval = _approval()
    db.save_approval(approval, "owner-a", "sess-a", "digest")
    first = str(uuid4())

    db.create_attempt(
        attempt_id=first, approval_id=approval.approval_id, owner_id="owner-a",
        session_id="sess-a", tool_name=approval.tool_name, mutating=True,
        status=AttemptStatus.CLAIMED,
    )
    with pytest.raises(ExecutionConflictError):
        db.create_attempt(
            attempt_id=str(uuid4()), approval_id=approval.approval_id,
            owner_id="owner-a", session_id="sess-a",
            tool_name=approval.tool_name, mutating=True,
            status=AttemptStatus.CLAIMED,
        )

    # A FAILED attempt (the interrupted outcome for claim-only states)
    # releases the slot; a reconciliation retry may open a new one
    # without ever resurrecting the consumed approval.
    db.transit_attempt(first, AttemptStatus.FAILED)
    db.create_attempt(
        attempt_id=str(uuid4()), approval_id=approval.approval_id,
        owner_id="owner-a", session_id="sess-a",
        tool_name=approval.tool_name, mutating=True,
        status=AttemptStatus.CLAIMED,
    )


def test_attempt_transitions_are_enforced_at_persistence():
    db = ExecutionStateDb()
    attempt_id = str(uuid4())
    db.create_attempt(
        attempt_id=attempt_id, owner_id="o", session_id="s",
        tool_name="stub_mut_ok", mutating=True, status=AttemptStatus.CLAIMED,
    )
    with pytest.raises(ExecutionLifecycleError):
        db.transit_attempt(attempt_id, AttemptStatus.SUCCEEDED)
    db.transit_attempt(attempt_id, AttemptStatus.STARTED)
    db.transit_attempt(attempt_id, AttemptStatus.FAILED)
    with pytest.raises(ExecutionStateError):
        db.transit_attempt(str(uuid4()), AttemptStatus.FAILED)


def test_attempts_must_start_in_an_open_state():
    db = ExecutionStateDb()
    with pytest.raises(ExecutionLifecycleError):
        db.create_attempt(
            attempt_id=str(uuid4()), owner_id="o", session_id="s",
            tool_name="stub_ro", mutating=False,
            status=AttemptStatus.SUCCEEDED,
        )


def test_outcome_bindings_join_proven_outcomes_only():
    db = ExecutionStateDb()
    approval = _approval(status=ApprovalStatus.EXECUTED,
                         resolved_at=datetime.now(timezone.utc))
    db.save_approval(approval, "owner-a", "sess-a", "digest")
    step_plan = _plan(step_status=PlanStepStatus.AWAITING_APPROVAL,
                      approval_id=approval.approval_id)
    db.save_plan(step_plan, "owner-a", "sess-a")

    attempt = str(uuid4())
    db.create_attempt(
        attempt_id=attempt, approval_id=approval.approval_id, owner_id="owner-a",
        session_id="sess-a", tool_name=approval.tool_name, mutating=True,
        status=AttemptStatus.CLAIMED,
    )
    # STEP 1: a claimed boundary holds no proven outcome yet.
    assert db.find_unresolved_outcome_bindings() == []
    db.transit_attempt(attempt, AttemptStatus.STARTED)
    # STEP 2: even an open STARTED boundary carries no proven outcome.
    assert db.find_unresolved_outcome_bindings() == []

    # STEP 3: a proven outcome becomes replayable exactly once recorded.
    outcome = ToolResult.success_result(
        tool_name="stub_mut_ok", message="done", data={"value": "v"}
    )
    db.transit_attempt(attempt, AttemptStatus.SUCCEEDED, outcome=outcome)
    bindings = db.find_unresolved_outcome_bindings()
    assert [b.attempt_id for b in bindings] == [attempt]
    assert bindings[0].outcome.success is True
    assert bindings[0].plan_id == step_plan.plan_id


# --- approvals ----------------------------------------------------------------

def test_approval_save_get_and_digest_round_trip():
    db = ExecutionStateDb()
    approval = _approval()
    db.save_approval(approval, "owner-a", "sess-a", "digest-original")
    record = db.get_approval_record(approval.approval_id)
    assert record.request.status is ApprovalStatus.PENDING
    assert record.digest == "digest-original"
    assert record.purged is False
    assert record.owner_id == "owner-a"

    terminal = approval.model_copy(
        update={"status": ApprovalStatus.EXECUTED,
                "resolved_at": datetime.now(timezone.utc)}
    )
    db.save_approval(terminal, "owner-a", "sess-a", "digest-original")
    updated = db.get_approval_record(approval.approval_id)
    assert updated.request.status is ApprovalStatus.EXECUTED
    assert updated.digest == "digest-original"


def test_approval_purge_flags_rows_without_deleting_them():
    db = ExecutionStateDb()
    approval = _approval()
    db.save_approval(approval, "owner-a", "sess-a", "digest")
    assert db.count_active_approvals() == 1
    assert db.mark_approval_purged(approval.approval_id) is True
    record = db.get_approval_record(approval.approval_id)
    assert record.purged is True          # audit row remains
    assert db.count_active_approvals() == 0  # capacity excludes it


# --- events --------------------------------------------------------------------

def test_events_append_list_and_prune_bounded():
    db = ExecutionStateDb()
    for index in range(200):
        db.append_event(
            "execution_requested",
            execution_id=f"exec-{index}",
            detail={"tool_name": "stub_ro"},
        )
    recent = db.recent_events(5)
    assert [e["event"] for e in recent] == ["execution_requested"] * 5
    assert recent[0]["execution_id"] == "exec-199"
    assert "tool_name" in recent[0]["detail"]

    # Prune keeps the configured retention window, never grows unbounded.
    type(db)._prune_events_locked(db, 50)
    assert db.recent_events(1000)[0]["execution_id"] == "exec-199"
    assert len(db.recent_events(1000)) == 50


def test_file_backed_state_survives_close_and_reopen(tmp_path):
    path = str(tmp_path / "restart.db")
    plan = _plan()
    approval = _approval()
    db = ExecutionStateDb(path=path)
    db.save_plan(plan, "owner-a", "sess-a")
    db.save_approval(approval, "owner-a", "sess-a", "digest")
    attempt_id = str(uuid4())
    db.create_attempt(
        attempt_id=attempt_id, approval_id=approval.approval_id, owner_id="owner-a",
        session_id="sess-a", tool_name=approval.tool_name, mutating=True,
        status=AttemptStatus.CLAIMED,
    )
    db.append_event("execution_claimed", execution_id=attempt_id,
                    detail={"tool_name": approval.tool_name})
    db.close()

    restored = ExecutionStateDb(path=path)
    assert restored.get_plan_record(plan.plan_id).plan.goal == plan.goal
    assert restored.get_approval_record(approval.approval_id).digest == "digest"
    attempt = restored.get_attempt(attempt_id)
    assert attempt.status is AttemptStatus.CLAIMED
    assert restored.get_latest_attempt(approval.approval_id).attempt_id == attempt_id
    assert restored.recent_events(10)[0]["event"] == "execution_claimed"
    restored.close()
