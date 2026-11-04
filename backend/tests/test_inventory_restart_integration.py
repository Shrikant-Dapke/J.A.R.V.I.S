"""Restart and recovery integration tests (Phase 5.2-c).

Each flow drives a real, file-backed service generation to a persisted
state, CLOSES the database, reopens it as a second generation, and then
runs the startup recovery scan — the same close/reopen pattern as
test_recovery.py, but with the 5.2 inventory and detail surfaces added:

- flows assert the READ-ONLY plan and approval inventories (the exact
  functions the HTTP routes call) reflect the persisted state,
- flows then also read through the REAL HTTP routes by monkeypatching
  the route wiring onto the flow's generation,
- recovery behavior stays exactly what test_recovery pins: no
  automatic retries, human resolution for uncertain outcomes,
  proven outcomes applied verbatim exactly once,
- the ``EVENT_RECOVERY_SCAN_COMPLETED`` contract is asserted from the
  real implementation, never fabricated: a process-level summary with
  the four counters, no identity fields, one event per launch scan.

The approval lifecycle exercise uses the normal authorization path
over the fresh generation — the claim itself never launches a tool,
so no hanging-tool cleanup fixtures are needed.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.schemas.approval import ApprovalStatus
from app.schemas.plan import PlanStepStatus
from app.schemas.tool import ToolResult
from app.services.approval_store import ApprovalStateError
from app.services.execution_db import ExecutionStateDb
from app.services.execution_lifecycle import (
    AttemptStatus,
    EVENT_RECOVERY_OUTCOME_APPLIED,
    EVENT_RECOVERY_SCAN_COMPLETED,
)
from app.services.plan_service import (
    PlanOverallStatus,
    PlanStepStateError,
    derive_plan_status,
    summarize_plan_steps,
)
from app.services.recovery_service import run_startup_recovery
from tests._exec_helpers import InstantMutatingTool, make_graph

API_SESSION = "test-session-012345678901234567"


def _api_owner() -> str:
    """The token owner identity every HTTP call authenticates as.

    Computed per call: the autouse auth fixture overrides the token for
    every test, so an import-time hash would not match the principal.
    """
    return hashlib.sha256(
        settings.api_token.get_secret_value().encode("utf-8")
    ).hexdigest()


def _bind(graph, tool_name, arguments, owner, session, step_id="s1"):
    """Create a one-step plan and its pending approval (real workflow)."""
    plan = graph.plans.create_plan(
        goal=f"restart flow {tool_name} {step_id}",
        steps=[
            {
                "step_id": step_id,
                "description": f"restart integration step {step_id}",
                "tool_name": tool_name,
                "arguments": arguments,
                "depends_on": [],
            }
        ],
        owner_id=owner,
        session_id=session,
    )
    approval = graph.plans.request_step_execution(
        plan.plan_id, step_id, owner, session
    )
    assert approval.approval_id
    return plan, approval


def _crash_after_claim(graph, approval, owner, session):
    """Persist a claim-crashed state (approval consumed, attempt CLAIMED)."""
    graph.store.approve_request(approval.approval_id, owner, session)
    graph.store.claim_approved_for_execution(
        approval.approval_id, owner, session
    )
    attempt_id = str(uuid4())
    graph.db.create_attempt(
        attempt_id=attempt_id,
        approval_id=approval.approval_id,
        owner_id=owner,
        session_id=session,
        tool_name=approval.tool_name,
        mutating=True,
        status=AttemptStatus.CLAIMED,
    )
    return attempt_id


def _plan_summary_flags(graph, plan_id, owner, session):
    """Read the plan's listing-side attention flags from persistence."""
    views, total = graph.plans.list_plans_page(
        owner_id=owner, session_id=session, limit=50, offset=0
    )
    assert plan_id in [view.plan_id for view in views]
    for view in views:
        if view.plan_id == plan_id:
            return summarize_plan_steps(view.plan)
    raise AssertionError("plan row unexpectedly missing")


def _approval_summaries(graph, owner, session):
    views, total = graph.store.list_approvals_page(
        owner, session, limit=50, offset=0
    )
    return views, total


def _quiet(counts, *, errors_expected=0):
    assert counts["interrupted"] == 0
    assert counts["recovery_required"] == 0
    assert counts["outcome_applied"] == 0
    assert counts["errors"] == errors_expected


@pytest.fixture
def as_api(monkeypatch):
    """Real HTTP routes resolved onto a flow-local generation."""

    def _patch(graph):
        monkeypatch.setattr("app.api.plans.plan_service", graph.plans)
        monkeypatch.setattr(
            "app.api.approvals.approval_service", graph.approvals
        )

    return _patch


async def _get(api_headers, path):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=api_headers,
    ) as client:
        return await client.get(path)


def _scan_events(graph):
    return [
        event
        for event in graph.db.recent_events(200)
        if event["event"] == EVENT_RECOVERY_SCAN_COMPLETED
    ]


# --- Flow A: pending approval survives restart --------------------------------


@pytest.mark.asyncio
async def test_flow_a_pending_approval_survives_restart(
    tmp_path, as_api, api_headers
):
    owner, session = _api_owner(), API_SESSION
    path = str(tmp_path / "flowa.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    plan, approval = _bind(
        graph, InstantMutatingTool.name, {"value": "flow-a"}, owner, session
    )

    # Crash state: process restarts while the approval is pending.
    graph.db.close()
    gen2 = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    as_api(gen2)

    # No automatic action: recovery leaves pending approvals untouched.
    counts = run_startup_recovery(
        plan_service=gen2.plans, approval_service=gen2.approvals
    )
    _quiet(counts)

    # Inventory (plan): the surviving plan still reports the approval gate.
    flags = _plan_summary_flags(gen2, plan.plan_id, owner, session)
    assert flags["awaiting_approval"] == ["s1"]
    assert flags["recovery_required"] == []
    assert flags["needs_attention"] is True
    assert flags["status_counts"] == {"AWAITING_APPROVAL": 1}

    # Inventory (approval): metadata-only PENDING summary, re-bound by id.
    views, total = _approval_summaries(gen2, owner, session)
    assert total == 1
    [view] = views
    assert view.approval_id == approval.approval_id
    assert view.status == ApprovalStatus.PENDING
    assert view.tool_name == InstantMutatingTool.name
    # Metadata-only: raw arguments never reach summaries.
    assert not hasattr(view, "arguments")

    # TTL semantics intact: the persisted expires_at is unchanged,
    # strictly in the future, and equal to the payload copy.
    row = gen2.db._conn.execute(
        "SELECT expires_at, payload FROM approval_requests "
        "WHERE approval_id = ?",
        (approval.approval_id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    # The column and the payload may format the same instant differently
    # (Z suffix vs +00:00): compare parsed instants.
    assert datetime.fromisoformat(
        row["expires_at"].replace("Z", "+00:00")
    ) == datetime.fromisoformat(
        payload["expires_at"].replace("Z", "+00:00")
    )
    assert datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) > (
        datetime.now(timezone.utc)
    )

    # HTTP routes agree with the service inventories (real read-only ends).
    plans_list = await _get(api_headers, "/api/plans")
    assert plans_list.status_code == 200
    mine = [
        p
        for p in plans_list.json()["plans"]
        if p["plan_id"] == plan.plan_id
    ]
    assert len(mine) == 1
    assert mine[0]["awaiting_approval"] == ["s1"]
    assert mine[0]["needs_attention"] is True

    approvals_list = await _get(api_headers, "/api/approvals")
    assert approvals_list.status_code == 200
    match = [
        a
        for a in approvals_list.json()["approvals"]
        if a["approval_id"] == approval.approval_id
    ]
    assert len(match) == 1
    assert match[0]["status"] == "PENDING"
    assert '"arguments"' not in approvals_list.text

    # Detail enrichment: awaiting_approval, nothing ready, user attention.
    detail = await _get(api_headers, f"/api/plans/{plan.plan_id}")
    assert detail.status_code == 200
    assert detail.json()["overall_status"] == "awaiting_approval"
    assert detail.json()["awaiting_approval"] == ["s1"]
    assert detail.json()["ready_step_ids"] == []
    assert detail.json()["needs_attention"] is True

    # The authoritative lifecycle still works over the SAME record:
    # the normal approval path executes exactly one attempt.
    result = gen2.approvals.approve_request(approval.approval_id, owner, session)
    assert isinstance(result, ToolResult)
    assert result.success is True
    attempt = gen2.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "SUCCEEDED"
    step = gen2.plans.get_plan(plan.plan_id, owner, session).steps[0]
    assert step.status is PlanStepStatus.EXECUTED

    # Single-use: the consumed approval can never be claimed again.
    with pytest.raises(ApprovalStateError):
        gen2.store.claim_approved_for_execution(
            approval.approval_id, owner, session
        )


# --- Flow B: uncertain mutating execution survives restart ---------------------


@pytest.mark.asyncio
async def test_flow_b_uncertain_execution_survives_restart(
    tmp_path, as_api, api_headers
):
    owner, session = _api_owner(), API_SESSION
    path = str(tmp_path / "flowb.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    plan, approval = _bind(
        graph, InstantMutatingTool.name, {"value": "flow-b"}, owner, session
    )
    attempt_id = _crash_after_claim(graph, approval, owner, session)
    graph.db.transit_attempt(attempt_id, AttemptStatus.STARTED)  # crash here
    graph.db.close()

    gen2 = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    as_api(gen2)
    counts = run_startup_recovery(
        plan_service=gen2.plans, approval_service=gen2.approvals
    )
    assert counts["recovery_required"] == 1
    assert counts["interrupted"] == 0
    assert counts["outcome_applied"] == 0
    assert counts["errors"] == 0

    # Inventory reflects the recovery-required step (advisory listing).
    flags = _plan_summary_flags(gen2, plan.plan_id, owner, session)
    assert flags["recovery_required"] == ["s1"]
    assert flags["needs_attention"] is True
    assert flags["status_counts"] == {"RECOVERY_REQUIRED": 1}

    # Service detail derives the same truth; HUMAN resolution is needed.
    service_plan = gen2.plans.get_plan(plan.plan_id, owner, session)
    derived = derive_plan_status(service_plan)
    assert derived["overall_status"] is PlanOverallStatus.RECOVERY_REQUIRED
    assert service_plan.steps[0].status is PlanStepStatus.RECOVERY_REQUIRED

    # The mutation boundary STANDS: the attempt stays open, never retried.
    attempt = gen2.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "STARTED"
    assert attempt.outcome is None
    assert gen2.db.get_open_attempts() != []

    # The HTTP detail exposes the derived status to the user.
    detail = await _get(api_headers, f"/api/plans/{plan.plan_id}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["overall_status"] == "recovery_required"
    assert data["recovery_required"] == ["s1"]
    assert data["ready_step_ids"] == []
    assert data["needs_attention"] is True

    # The existing human resolution path stays authoritative.
    resolved = gen2.plans.resolve_step_recovery(
        plan.plan_id, "s1", owner, session
    )
    step = resolved.steps[0]
    assert step.status is PlanStepStatus.FAILED
    assert step.result is not None
    assert step.result.error_code == "execution_uncertain"

    attempt = gen2.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "RECONCILED"

    # Post-resolution reads: detail reflects the honest terminal state.
    revised = await _get(api_headers, f"/api/plans/{plan.plan_id}")
    assert revised.status_code == 200
    revised_data = revised.json()
    assert revised_data["overall_status"] == "failed"
    assert revised_data["recovery_required"] == []
    assert revised_data["steps"][0]["status"] == "FAILED"

    # Exactly-once resolution: RECOVERY_REQUIRED is gone for good.
    with pytest.raises(PlanStepStateError):
        gen2.plans.resolve_step_recovery(plan.plan_id, "s1", owner, session)

    # No automatic retry: still exactly ONE attempt for this approval.
    assert gen2.db.get_latest_attempt(approval.approval_id).attempt_id == attempt_id

    # A later scan is a no-op: nothing new, nothing re-marked.
    counts = run_startup_recovery(
        plan_service=gen2.plans, approval_service=gen2.approvals
    )
    _quiet(counts)


# --- Flow C: proven outcome applied exactly once -------------------------------


@pytest.mark.asyncio
async def test_flow_c_proven_outcome_applied_exactly_once(
    tmp_path, as_api, api_headers
):
    owner, session = _api_owner(), API_SESSION
    path = str(tmp_path / "flowc.db")
    graph = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    plan, approval = _bind(
        graph, InstantMutatingTool.name, {"value": "flow-c"}, owner, session
    )
    attempt_id = _crash_after_claim(graph, approval, owner, session)
    # The tool finished and durably recorded its outcome, then crashed
    # before the plan step ever saw it.
    outcome = ToolResult.success_result(
        tool_name=InstantMutatingTool.name,
        message="proven applied before restart",
        data={"value": "flow-c"},
    )
    graph.db.transit_attempt(attempt_id, AttemptStatus.STARTED)
    graph.db.transit_attempt(attempt_id, AttemptStatus.SUCCEEDED, outcome=outcome)
    graph.db.close()

    gen2 = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    as_api(gen2)
    counts = run_startup_recovery(
        plan_service=gen2.plans, approval_service=gen2.approvals
    )
    assert counts["outcome_applied"] == 1
    assert counts["interrupted"] == 0
    assert counts["recovery_required"] == 0
    assert counts["errors"] == 0

    # Plan reflects the applied outcome; derivation sees it as completed.
    current = gen2.plans.get_plan(plan.plan_id, owner, session)
    assert current.steps[0].status is PlanStepStatus.EXECUTED
    assert current.steps[0].result is not None
    assert current.steps[0].result.success is True
    derived = derive_plan_status(current)
    assert derived["overall_status"] is PlanOverallStatus.COMPLETED
    assert derived["ready_step_ids"] == []
    assert derived["needs_attention"] is False

    # The HTTP detail and inventory agree with the service reads.
    detail = await _get(api_headers, f"/api/plans/{plan.plan_id}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["overall_status"] == "completed"
    assert data["steps"][0]["status"] == "EXECUTED"
    listing = await _get(api_headers, "/api/plans")
    assert listing.status_code == 200
    listed = [p for p in listing.json()["plans"] if p["plan_id"] == plan.plan_id]
    assert len(listed) == 1
    assert listed[0]["needs_attention"] is False

    # Repeated recovery scans apply the outcome EXACTLY ONCE.
    _quiet(
        run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    )
    _quiet(
        run_startup_recovery(plan_service=gen2.plans, approval_service=gen2.approvals)
    )
    outcome_events = [
        event
        for event in gen2.db.recent_events(200)
        if event["event"] == EVENT_RECOVERY_OUTCOME_APPLIED
    ]
    assert len(outcome_events) == 1

    # Exactly one attempt chain survived; no duplication anywhere.
    assert gen2.db.get_latest_attempt(approval.approval_id).attempt_id == attempt_id


# --- Flow D: recovery scan audit event contract --------------------------------


def test_flow_d_recovery_scan_event_contract(tmp_path):
    owner, session = _api_owner(), API_SESSION
    path = str(tmp_path / "flowd.db")
    gen2 = make_graph(state_db=ExecutionStateDb(path=path), with_hanging=False)
    gen2.plans.create_plan(
        goal="quiet scan probe",
        steps=[
            {
                "step_id": "s1",
                "description": "dry scan step",
                "tool_name": "stub_ro",
                "arguments": {"value": "x"},
                "depends_on": [],
            }
        ],
        owner_id=owner,
        session_id=session,
    )

    counts = run_startup_recovery(
        plan_service=gen2.plans, approval_service=gen2.approvals
    )
    _quiet(counts)

    scans = _scan_events(gen2)
    assert len(scans) == 1
    event = scans[0]
    detail = event["detail"]
    assert set(detail.keys()) == {
        "interrupted",
        "recovery_required",
        "outcome_applied",
        "errors",
    }
    for key in detail:
        assert isinstance(detail[key], int)
    assert detail == {
        "interrupted": 0,
        "recovery_required": 0,
        "outcome_applied": 0,
        "errors": 0,
    }

    # Process-level summary: no identity fields, never a per-plan ref.
    assert event["correlation_id"] is None
    assert event["execution_id"] is None
    for forbidden in ("plan_id", "step_id", "approval_id", "owner_id",
                      "session_id"):
        assert forbidden not in detail

    # Exactly one scan event per launch scan; counters match the return.
    counts = run_startup_recovery(
        plan_service=gen2.plans, approval_service=gen2.approvals
    )
    _quiet(counts)
    scans = _scan_events(gen2)
    assert len(scans) == 2
    # recent_events is newest first; the newest scan mirrors the return.
    assert scans[0]["detail"] == {
        "interrupted": counts["interrupted"],
        "recovery_required": counts["recovery_required"],
        "outcome_applied": counts["outcome_applied"],
        "errors": counts["errors"],
    }
