"""Execution reliability scenarios (Phase 4.3): lifecycle, timeouts,
idempotency, and audit hygiene.

Every test builds a fully isolated service graph in memory
(``_exec_helpers.make_graph``) with deterministic stub tools, so there
are no network calls and no real side effects. Hanging stubs are
released in teardown; concurrency tests synchronize with barriers
instead of sleeps.
"""

import threading
from uuid import uuid4

import pytest

from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.plan import PlanStepStatus
from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolDefinition, ToolResult
from app.services.approval_store import ApprovalNotFoundError, ApprovalStateError
from app.services.execution_db import ExecutionStateError
from app.services.execution_lifecycle import (
    AttemptStatus,
    ExecutionLifecycleError,
)
from app.services.plan_service import (
    PlanNotFoundError,
    PlanStepNotFoundError,
    PlanStepStateError,
)
from app.services.tool_base import Tool
from app.services.tool_executor import ToolExecutor, ToolNotImplementedError
from app.services.tool_registry import ToolRegistry

from tests._exec_helpers import (
    OTHER_SESSION_ID,
    OWNER_ID,
    SESSION_ID,
    FailingMutatingTool,
    FailingReadOnlyStubTool,
    HangingMutatingTool,
    InstantMutatingTool,
    ReadOnlyStubTool,
    SlowReadOnlyTool,
    _ValueInput,
    make_graph,
    release_hanging_tools,
)


TIMEOUT_BUDGET = 0.05  # the bounded floor; deterministic, well above zero


@pytest.fixture
def hanging_cleanup():
    """Release every hung stub worker thread when the test ends."""
    yield
    release_hanging_tools()


def _plan(graph, tool_name, arguments=None, step_id="s1"):
    return graph.plans.create_plan(
        goal="Reliability check",
        steps=[
            {
                "step_id": step_id,
                "description": "One bounded step",
                "tool_name": tool_name,
                "arguments": arguments or {},
                "depends_on": [],
            }
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )


# --- lifecycle of normal mutating executions ---------------------------------


def test_mutating_success_records_attempt_and_step_once():
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, InstantMutatingTool.name, {"value": "v"})

    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    assert isinstance(approval, ApprovalRequest)
    result = graph.approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    assert result.success is True

    step = graph.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.EXECUTED
    assert step.result is not None and step.result.success is True

    attempt = graph.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "SUCCEEDED"
    assert attempt.outcome is not None and attempt.outcome.success is True
    assert attempt.mutating is True

    events = {e["event"] for e in graph.db.recent_events(100)}
    assert {"execution_claimed", "execution_started", "execution_succeeded"} <= events
    started = [
        e for e in graph.db.recent_events(100) if e["event"] == "execution_started"
    ]
    assert started and all(e["execution_id"] for e in started)


def test_mutating_failure_records_honest_failed_outcome():
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, FailingMutatingTool.name, {"value": "v"})

    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    result = graph.approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    assert result.success is False  # the tool's own deterministic failure

    step = graph.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.FAILED
    assert step.result.success is False

    attempt = graph.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "FAILED"
    assert attempt.outcome is not None and attempt.outcome.success is False


# --- tool timeout behavior (requirement E) -----------------------------------


def test_mutating_timeout_requires_explicit_recovery(hanging_cleanup):
    """A mutating timeout is NEVER reported success or clean failure:
    the boundary stands and the step awaits human reconciliation."""
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, HangingMutatingTool.name, {"value": "v"})

    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    result = graph.approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    # The caller observes the timeout, not a fabricated outcome.
    assert result.success is False
    assert result.error_code == "execution_timeout"

    step = graph.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.RECOVERY_REQUIRED
    assert step.result is not None
    assert step.result.error_code == "execution_timeout"

    attempt = graph.db.get_latest_attempt(approval.approval_id)
    # The TIMED_OUT attempt carries NO outcome: a timeout proves nothing
    # about whether the side effect occurred (distinct from FAILED).
    assert attempt.status.value == "TIMED_OUT"
    assert attempt.outcome is None

    events = {e["event"] for e in graph.db.recent_events(100)}
    assert "execution_timed_out" in events
    assert "recovery_required" in events

    # No blind retries: the uncertain step cannot be re-requested.
    with pytest.raises(PlanStepStateError) as exc:
        graph.plans.request_step_execution(
            plan.plan_id, "s1", OWNER_ID, SESSION_ID
        )
    assert "recovery" in str(exc.value).lower()


def test_read_only_timeout_is_an_ordinary_failure(hanging_cleanup):
    """Read-only tools are provably side-effect free: their timeout
    resolves FAILED with recorded evidence, never RECOVERY_REQUIRED."""
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, SlowReadOnlyTool.name, {"value": "v"})  # registered hanging stub

    result = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error_code == "execution_timeout"

    step = graph.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.FAILED
    assert step.result.error_code == "execution_timeout"

    assert graph.db.get_open_attempts() == []  # terminal, boundary closed
    events = {e["event"] for e in graph.db.recent_events(100)}
    assert "execution_timed_out" not in events
    assert "recovery_required" not in events


def test_timeout_result_never_leaks_internals(hanging_cleanup):
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, HangingMutatingTool.name, {"value": "v"})
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    result = graph.approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    dumped = result.model_dump()
    assert dumped["error_code"] == "execution_timeout"
    # A safe bounded message: no trace, no argument values, no ids.
    assert "Traceback" not in dumped["message"]
    assert "{'value': 'v'}" not in dumped["message"]
    assert approval.approval_id not in dumped["message"]


def test_mutating_tool_exception_becomes_honest_failure_not_500():
    """A tool bug after the invocation started is recorded as a failure
    outcome at the tool layer; internals never surface to the caller."""

    class RaisingMutatingTool(Tool[_ValueInput]):
        name = "stub_mut_raise"
        description = "Stub mutating tool that raises an internal error."
        read_only = False
        requires_approval = True
        risk_level = RiskLevel.LOW_RISK
        input_model = _ValueInput

        def execute(self, arguments):
            raise RuntimeError("sensitive internal structure")

    graph = make_graph(timeout_seconds=0.2)
    graph.registry.register(RaisingMutatingTool())
    plan = _plan(graph, RaisingMutatingTool.name, {"value": "v"})

    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    result = graph.approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    assert result.success is False
    assert "sensitive internal structure" not in result.message
    assert "RuntimeError" not in result.message

    step = graph.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.FAILED
    attempt = graph.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "FAILED"
    assert attempt.outcome is not None and attempt.outcome.success is False


# --- double resolution and persisted idempotency (requirements A/D) ----------


def test_double_approval_resolution_is_impossible():
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, InstantMutatingTool.name, {"value": "v"})
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    first = graph.approvals.approve_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    assert first.success is True

    with pytest.raises(ApprovalStateError):
        graph.approvals.approve_request(approval.approval_id, OWNER_ID, SESSION_ID)

    # The consumed approval produced exactly one attempt, now terminal.
    assert graph.db.get_open_attempts() == []
    record = graph.store.get_approval_request(
        approval.approval_id, OWNER_ID, SESSION_ID
    )
    assert record.status is ApprovalStatus.EXECUTED


def test_claimed_approval_cannot_be_claimed_twice_concurrently():
    """Single-use claim is serialized by the store lock: one wins."""
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, HangingMutatingTool.name, {"value": "v"})
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    graph.store.approve_request(approval.approval_id, OWNER_ID, SESSION_ID)

    barrier = threading.Barrier(2)
    outcomes = []

    def claim_thread():
        barrier.wait()
        try:
            consumed = graph.store.claim_approved_for_execution(
                approval.approval_id, OWNER_ID, SESSION_ID
            )
            outcomes.append(("consumed", consumed.status))
        except ApprovalStateError:
            outcomes.append(("refused", None))

    threads = [
        threading.Thread(target=claim_thread) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(o[0] for o in outcomes) == ["consumed", "refused"]
    consumed = [o for o in outcomes if o[0] == "consumed"]
    assert consumed[0][1] is ApprovalStatus.EXECUTED


def test_concurrent_same_step_request_admits_exactly_one():
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, InstantMutatingTool.name, {"value": "v"})

    barrier = threading.Barrier(2)
    outcomes = []

    def request_thread():
        barrier.wait()
        try:
            outcome = graph.plans.request_step_execution(
                plan.plan_id, "s1", OWNER_ID, SESSION_ID
            )
            outcomes.append(("approval", outcome.approval_id))
        except PlanStepStateError as exc:
            outcomes.append(("conflict", str(exc)))

    threads = [
        threading.Thread(target=request_thread) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(o[0] for o in outcomes) == ["approval", "conflict"]

    winning = [o for o in outcomes if o[0] == "approval"][0][1]
    step = graph.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.AWAITING_APPROVAL
    assert step.approval_id == winning


def test_unready_dependencies_and_unknown_steps_refuse_execution():
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = graph.plans.create_plan(
        goal="g",
        steps=[
            {
                "step_id": "s1",
                "description": "read",
                "tool_name": ReadOnlyStubTool.name,
                "arguments": {"value": "a"},
                "depends_on": [],
            },
            {
                "step_id": "s2",
                "description": "needs s1",
                "tool_name": FailingReadOnlyStubTool.name,
                "arguments": {"value": "b"},
                "depends_on": ["s1"],
            },
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )
    # Not ready: dependency s1 has no recorded success yet.
    with pytest.raises(PlanStepStateError):
        graph.plans.request_step_execution(plan.plan_id, "s2", OWNER_ID, SESSION_ID)
    with pytest.raises(PlanStepNotFoundError):
        graph.plans.request_step_execution(
            plan.plan_id, "unknown", OWNER_ID, SESSION_ID
        )

    first = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    assert first.success is True  # read-only executes with no approval
    assert graph.plans.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == ["s2"]


def test_failing_dependency_never_unblocks_dependents():
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = graph.plans.create_plan(
        goal="g",
        steps=[
            {
                "step_id": "s1",
                "description": "fails",
                "tool_name": FailingReadOnlyStubTool.name,
                "arguments": {"value": "a"},
                "depends_on": [],
            },
            {
                "step_id": "s2",
                "description": "blocked",
                "tool_name": ReadOnlyStubTool.name,
                "arguments": {"value": "b"},
                "depends_on": ["s1"],
            },
        ],
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )
    result = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    assert result.success is False
    assert graph.plans.ready_step_ids(plan.plan_id, OWNER_ID, SESSION_ID) == []


# --- owner/session isolation --------------------------------------------------


def test_foreign_identity_observes_not_found_on_every_operation():
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, InstantMutatingTool.name, {"value": "v"})
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )

    with pytest.raises(PlanNotFoundError):
        graph.plans.request_step_execution(
            plan.plan_id, "s1", OWNER_ID, OTHER_SESSION_ID
        )
    with pytest.raises(ApprovalNotFoundError):
        graph.store.get_approval_request(
            approval.approval_id, OWNER_ID, OTHER_SESSION_ID
        )
    with pytest.raises(PlanNotFoundError):
        graph.plans.get_plan(plan.plan_id, OWNER_ID, OTHER_SESSION_ID)


# --- audit event hygiene (requirement F) --------------------------------------


def _assert_audit_hygiene(events):
    """Audit details carry classifications and identifiers only."""
    allowed_keys = {
        "tool_name", "approval_id", "error_code", "outcome", "reason",
        "risk_level", "mutating", "result_status", "plan_id", "step_id",
    }
    forbidden_values = (OWNER_ID, OTHER_SESSION_ID, SESSION_ID)
    for event in events:
        detail = event.get("detail") or {}
        unexpected = set(detail) - allowed_keys
        assert not unexpected, f"unexpected audit keys: {unexpected} in {event}"
        serialized = str(detail)
        for value in forbidden_values:
            assert value not in serialized


def test_audit_details_carry_classifications_only(hanging_cleanup):
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, HangingMutatingTool.name, {"value": "v"})
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    graph.approvals.approve_request(approval.approval_id, OWNER_ID, SESSION_ID)
    graph.plans.resolve_step_recovery(plan.plan_id, "s1", OWNER_ID, SESSION_ID)

    _assert_audit_hygiene(graph.db.recent_events(200))
    events = {e["event"] for e in graph.db.recent_events(200)}
    assert {
        "execution_requested", "approval_created", "approval_approved",
        "execution_claimed", "execution_started", "execution_timed_out",
        "recovery_required", "execution_reconciled",
    } <= events


def test_argument_values_never_appear_in_audit_rows(hanging_cleanup):
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, InstantMutatingTool.name, {"value": "v"})
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    graph.approvals.approve_request(approval.approval_id, OWNER_ID, SESSION_ID)

    serialized = str(graph.db.recent_events(200))
    # The approved argument VALUE is user data; audit rows must not copy
    # it alongside identifiers.
    assert "'value': 'v'" not in serialized
    assert "value=v" not in serialized


# --- read-only direct execution persists attempts ------------------------------


def test_read_only_executions_persist_attempts_with_plan_refs():
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, ReadOnlyStubTool.name, {"value": "v"})

    result = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    assert result.success is True
    step = graph.plans.get_plan(plan.plan_id, OWNER_ID, SESSION_ID).steps[0]
    assert step.status is PlanStepStatus.COMPLETED

    assert graph.db.get_open_attempts() == []
    started = [
        e for e in graph.db.recent_events(50) if e["event"] == "execution_started"
    ]
    assert started and all(e["execution_id"] for e in started)


# --- executor unit behavior ----------------------------------------------------


def test_executor_re_raises_exception_from_bounded_run():
    """The executor's budgeted runner re-raises what escapes the invoke
    boundary (the tool base catches tool-layer errors itself; this
    tests the executor contract for anything that propagates)."""

    class InvokeRaisingTool(Tool[_ValueInput]):
        name = "stub_invoke_raise"
        description = "Stub tool whose invoke boundary raises."
        read_only = True
        requires_approval = False
        risk_level = RiskLevel.READ_ONLY
        input_model = _ValueInput

        def execute(self, arguments):
            raise RuntimeError("sensitive internal structure")

        def invoke(self, arguments):
            # Deliberately bypasses the base exception-wrapping machinery
            # so the executor's re-raise contract is what gets exercised.
            raise RuntimeError("sensitive internal structure")

    registry = ToolRegistry()
    registry.register(InvokeRaisingTool())
    executor = ToolExecutor(registry, timeout_seconds=0.2)

    with pytest.raises(RuntimeError):
        executor.execute("stub_invoke_raise", {"value": "v"})

    # Inline legacy mode keeps the same contract.
    with pytest.raises(RuntimeError):
        ToolExecutor(registry).execute("stub_invoke_raise", {"value": "v"})


def test_executor_inline_legacy_mode_still_works_without_budget():
    registry = ToolRegistry()
    registry.register(ReadOnlyStubTool())
    result = ToolExecutor(registry).execute(ReadOnlyStubTool.name, {"value": "v"})
    assert result.success is True


def test_executor_rejects_non_positive_budget():
    registry = ToolRegistry()
    registry.register(ReadOnlyStubTool())
    executor = ToolExecutor(registry, timeout_seconds=0)
    with pytest.raises(ValueError):
        executor.execute(ReadOnlyStubTool.name, {"value": "v"})


def test_executor_unimplemented_tool_raises_without_persisting_anything():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="stub_metadata_only",
            description="metadata only",
            read_only=True,
            requires_approval=False,
        )
    )
    executor = ToolExecutor(registry)
    with pytest.raises(ToolNotImplementedError):
        executor.execute("stub_metadata_only", {})


# --- uncertain attempt quarantine at the façade boundary -----------------------


def test_timed_out_attempt_cannot_absorb_a_late_success(hanging_cleanup):
    """A late tool result can never fabricate success for a timed-out
    action: the state machine rejects it and the boundary stands."""
    graph = make_graph(timeout_seconds=TIMEOUT_BUDGET)
    plan = _plan(graph, HangingMutatingTool.name, {"value": "v"})
    approval = graph.plans.request_step_execution(
        plan.plan_id, "s1", OWNER_ID, SESSION_ID
    )
    graph.approvals.approve_request(approval.approval_id, OWNER_ID, SESSION_ID)
    attempt = graph.db.get_latest_attempt(approval.approval_id)
    assert attempt.status.value == "TIMED_OUT"

    with pytest.raises((ExecutionLifecycleError, ExecutionStateError)):
        graph.db.transit_attempt(
            attempt.attempt_id,
            AttemptStatus.SUCCEEDED,
            outcome=ToolResult.success_result(
                tool_name=attempt.tool_name, message="late", data={}
            ),
        )
    assert (
        graph.db.get_latest_attempt(approval.approval_id).status.value
        == "TIMED_OUT"
    )
