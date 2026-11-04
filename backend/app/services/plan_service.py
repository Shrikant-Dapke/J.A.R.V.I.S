"""Plan foundation: validated storage and manual single-step execution.

Phase 4 milestone 4.2 delivered the plan/step model and its explicit
execution rules. Phase 4.3 moved the storage from process-local
dictionaries to the shared SQLite execution-state façade so plans,
their step states, and approval bindings survive restarts, and added
the lifecycle, recovery-required, and reconciliation machinery described below.

This service is deliberately NOT an execution engine: it stores
validated plans, tracks step status, and executes at most one step per
explicit call, always through the existing policy and approval workflow
(`approval_service.request_tool`). There is no automatic advancement,
no loop, and no step may run before its dependencies are verifiably
satisfied.

Ownership model is persisted with every plan: a plan is bound to the
(owner_id, session_id) identity that captured it; another identity sees
it as unknown (404 semantics), never as state it can read or mutate.

Status semantics are kept honest:
- COMPLETED: a policy-allowed non-approval execution returned a
  successful ToolResult, recorded on the step.
- EXECUTED: an approved action executed successfully; its ToolResult is
  recorded exactly once on the step by the approval-workflow listener.
- FAILED: any other terminal outcome, always with a recorded result
  (execution failure, rejection, expiry, or unavailable approval).
- RECOVERY_REQUIRED: a mutating step whose side effect could not be
  proven (crash after invocation started, or a mutating timeout). It is
  non-terminal and resolves only through explicit reconciliation.

Approval outcomes are attached to their owning plan step the moment the
approval workflow resolves them (approve/execute, reject) — never by
re-reading approval records that terminal retention may purge. A
dependent step becomes ready only when the steps it depends on have a
RECORDED successful result; an executed-but-failed parent can never
make a dependent ready on status alone.

Failed, rejected, expired, and unavailable steps are terminal: a new
plan must be captured instead of repeating the same execution request.
Uncertain steps require an explicit, human-initiated resolution.
"""

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
from typing import Callable, Optional, Union

from app.core.logging import get_logger
from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.plan import (
    PlanOverallStatus,
    MAX_PLAN_STEPS,
    PlanStep,
    PlanStepStatus,
    TaskPlan,
)
from app.schemas.tool import ToolResult
from app.services.approval_service import ApprovalService, approval_service
from app.services.approval_store import ApprovalNotFoundError
from app.services.tool_executor import ToolNotImplementedError
from app.services.tool_registry import ToolRegistry, UnknownToolError, tool_registry
from app.services.execution_db import ExecutionStateDb
from app.services.execution_lifecycle import (
    ERROR_EXECUTION_INTERRUPTED,
    ERROR_EXECUTION_UNCERTAIN,
    ExecutionLifecycleError,
    validate_plan_step_transition,
    EVENT_RECOVERY_REQUIRED,
    EVENT_RECOVERY_OUTCOME_APPLIED,
    EVENT_EXECUTION_RECONCILED,
)


logger = get_logger(__name__)


DEFAULT_MAX_PLAN_ENTRIES = 256

StepOutcome = Union[ToolResult, ApprovalRequest]


class PlanValidationError(ValueError):
    """Raised when a proposed plan references unknown tools or is invalid."""


class PlanNotFoundError(LookupError):
    """Raised when a plan is unknown or owned by another identity."""


class PlanStepNotFoundError(LookupError):
    """Raised when a step id is unknown within an existing plan."""


class PlanStepStateError(ValueError):
    """Raised when a step transition or readiness rule forbids the action."""


class PlanCapacityError(RuntimeError):
    """Raised when the bounded plan store is exhausted."""


class PlanCorruptError(RuntimeError):
    """Raised when a stored plan payload cannot be validated.

    Same fail-closed doctrine as ``ConversationCorruptError``: damaged
    state is never served, repaired, or silently skipped; the message is
    a fixed, non-sensitive string (identifiers go to logs only).
    """


@dataclass(frozen=True)
class PlanSummaryView:
    """One identity-authorized plan ready for inventory summarization."""

    plan_id: str
    created_at: str
    updated_at: str
    plan: TaskPlan


def summarize_plan_steps(plan: TaskPlan) -> dict:
    """Derive inventory attention fields from persisted steps (pure).

    Never mutates anything and never reconciles: fields describe the
    plan exactly as stored, advisory until an authoritative detail read.

    Derivation rules (Phase 5.2-a):
    - ``status_counts`` counts every step of every status present.
    - ``recovery_required`` lists step ids of RECOVERY_REQUIRED steps.
    - ``awaiting_approval`` lists step ids of AWAITING_APPROVAL steps.
    - ``needs_attention`` is deterministically True while ANY step is
      RECOVERY_REQUIRED or AWAITING_APPROVAL — a plan containing
      unresolved recovery work or pending approvals is never presented
      as safe; a plan of only terminal successful steps (or plain
      pending steps) is False.
    """
    counts: dict[str, int] = {}
    recovery_required: list[str] = []
    awaiting_approval: list[str] = []
    for step in plan.steps:
        status = step.status.value
        counts[status] = counts.get(status, 0) + 1
        if step.status is PlanStepStatus.RECOVERY_REQUIRED:
            recovery_required.append(step.step_id)
        elif step.status is PlanStepStatus.AWAITING_APPROVAL:
            awaiting_approval.append(step.step_id)
    return {
        "status_counts": counts,
        "recovery_required": recovery_required,
        "awaiting_approval": awaiting_approval,
        "needs_attention": bool(recovery_required or awaiting_approval),
    }


def _recorded_success(step) -> bool:
    """Did this step verifiably succeed, per persisted evidence?

    Dependency readiness and overall completion count ONLY a terminal
    success status (COMPLETED/EXECUTED) whose recorded ToolResult is
    present and successful. A terminal-success status without a
    recorded successful result is treated as an unresolved failure:
    an outcome-blind status can never unblock dependents and never
    completes a plan (same rule the 4.2 execution foundation applies
    to dependencies).
    """
    result = step.result
    return (
        result is not None
        and result.success
        and step.status in (PlanStepStatus.COMPLETED, PlanStepStatus.EXECUTED)
    )


def derive_plan_status(plan: TaskPlan) -> dict:
    """Derive whole-plan status fields (pure; persisted state only).

    Never reconciles, mutates, or writes anything; every field is a
    deterministic read of the plan exactly as it is persisted. This is
    the detail-side enrichment; the listing side flags come from
    ``summarize_plan_steps`` (which this function reuses for the
    attention lists so both sides can never disagree).

    Derivation rules (Phase 5.2-c precedence, applied in order):
    - ``recovery_required`` — at least one step is RECOVERY_REQUIRED.
    - ``awaiting_approval`` — at least one step is AWAITING_APPROVAL.
    - ``actionable`` — at least one PENDING step is dependency-ready
      (every dependency satisfied by a recorded successful result).
    - ``failed`` — at least one step is FAILED, or carries a
      terminal-success status without a recorded successful result
      (an outcome-blind status resolves as an honest failure), while
      no earlier rule applied.
    - ``in_progress`` — PENDING steps remain but none is ready
      (chains waiting on work that is not yet done).
    - ``completed`` — every step verifiably succeeded (recorded
      successful results included).

    Edge cases:
    - A plan with no steps is malformed (the schema requires at least
      one) and fails closed with ``PlanCorruptError`` like other
      damaged state.
    - Unknown step states cannot reach this function: the schema
      parses only the six enum values and a parse failure is a
      corruption error.
    - Dependencies that do not exist are simply unsatisfied (the
      schema forbids them, so this is a defensive no-block rather
      than an exception).
    - Dependent steps of RECOVERY_REQUIRED / AWAITING_APPROVAL /
      FAILED steps are never ready: those steps verifiably did not
      succeed.
    """
    if not plan.steps:
        raise PlanCorruptError("Plan state could not be validated.")

    attention = summarize_plan_steps(plan)
    succeeded_ids = {
        step.step_id for step in plan.steps if _recorded_success(step)
    }
    ready_step_ids = [
        step.step_id
        for step in plan.steps
        if step.status is PlanStepStatus.PENDING
        and all(dependency in succeeded_ids for dependency in step.depends_on)
    ]
    has_failed_step = any(
        step.status is PlanStepStatus.FAILED or (
            step.status in (PlanStepStatus.COMPLETED, PlanStepStatus.EXECUTED)
            and not _recorded_success(step)
        )
        for step in plan.steps
    )
    has_pending_step = any(
        step.status is PlanStepStatus.PENDING for step in plan.steps
    )

    if attention["recovery_required"]:
        overall = PlanOverallStatus.RECOVERY_REQUIRED
    elif attention["awaiting_approval"]:
        overall = PlanOverallStatus.AWAITING_APPROVAL
    elif ready_step_ids:
        overall = PlanOverallStatus.ACTIONABLE
    elif has_failed_step:
        overall = PlanOverallStatus.FAILED
    elif has_pending_step:
        overall = PlanOverallStatus.IN_PROGRESS
    else:
        overall = PlanOverallStatus.COMPLETED

    return {
        "overall_status": overall,
        "ready_step_ids": ready_step_ids,
        "recovery_required": attention["recovery_required"],
        "awaiting_approval": attention["awaiting_approval"],
        "needs_attention": attention["needs_attention"],
    }


class PlanService:
    """Store validated plans and drive their steps one explicit call at a time."""

    def __init__(
        self,
        registry: ToolRegistry = tool_registry,
        approvals: ApprovalService = approval_service,
        *,
        max_entries: int = DEFAULT_MAX_PLAN_ENTRIES,
        clock: Optional[Callable[[], datetime]] = None,
        plans_db: Optional[ExecutionStateDb] = None,
    ) -> None:
        self._registry = registry
        self._approvals = approvals
        if isinstance(self._approvals, ApprovalService):
            # Record approval outcomes (approve/execute, reject) on the
            # owning plan step immediately, so the plan never depends on
            # approval records that terminal retention may purge.
            self._approvals.set_outcome_listener(self.resolve_approval_outcome)
            # Uncertain mutating outcomes (timeouts with unknown side
            # effects) move their step to RECOVERY_REQUIRED instead of
            # success or failure.
            self._approvals.set_uncertain_listener(self.resolve_step_uncertain)
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if plans_db is not None:
            self._db = plans_db
        elif isinstance(self._approvals, ApprovalService):
            # Share the approvals' façade: one database per object graph
            # (in-memory for isolated test stores, or the shared file).
            self._db = self._approvals.execution_db
        else:
            self._db = ExecutionStateDb()
        self._lock = threading.RLock()

    @property
    def execution_db(self) -> ExecutionStateDb:
        """The façade backing this service's plan persistence."""
        return self._db

    @staticmethod
    def _copy(plan: TaskPlan) -> TaskPlan:
        """Prevent callers from mutating the store through returned objects."""
        return TaskPlan.model_validate(plan.model_dump())

    def _owned_plan(self, plan_id: str, owner_id: str, session_id: str) -> TaskPlan:
        """Authorize access before any payload is parsed.

        The identity gate runs on the raw row first, so unknown and
        foreign plans always observe not-found — a corrupt foreign row
        never reveals itself. Only the owning identity's corrupt rows
        then fail closed with ``PlanCorruptError`` (5.2-c).
        """
        row = self._db.get_plan_row(plan_id)
        if (
            row is None
            or row.owner_id != owner_id
            or row.session_id != session_id
        ):
            raise PlanNotFoundError("Plan was not found")
        return self._validated_plan_payload(plan_id, row.payload)

    def _store_plan(
        self, plan: TaskPlan, owner_id: str, session_id: str
    ) -> None:
        self._db.save_plan(plan, owner_id, session_id)

    def create_plan(
        self,
        *,
        goal: str,
        steps: list[Union[PlanStep, "dict"]],
        owner_id: str,
        session_id: str,
        conversation_id: Optional[str] = None,
    ) -> TaskPlan:
        """Validate model-proposed steps and store a bounded plan.

        Raises:
            PlanValidationError: On unknown tools or malformed steps.
            PlanCapacityError: When the bounded plan store is full.
        """
        if len(steps) > MAX_PLAN_STEPS:
            raise PlanValidationError(
                f"plans must not exceed {MAX_PLAN_STEPS} steps"
            )

        validated_steps: list[PlanStep] = []
        for raw in steps:
            try:
                step = (
                    raw if isinstance(raw, PlanStep) else PlanStep.model_validate(raw)
                )
            except ValueError as exc:
                raise PlanValidationError(
                    "The assistant proposed an invalid task plan."
                ) from exc
            definition = self._registry.get(step.tool_name)
            if definition is None:
                raise PlanValidationError(
                    f"Tool '{step.tool_name}' is not registered"
                )
            validated_steps.append(step)

        try:
            plan = TaskPlan.model_validate(
                {
                    "plan_id": str(uuid4()),
                    "goal": goal,
                    "steps": [step.model_dump() for step in validated_steps],
                    "conversation_id": conversation_id,
                    "created_at": self._clock(),
                }
            )
        except ValueError as exc:
            raise PlanValidationError(
                "The assistant proposed an invalid task plan."
            ) from exc

        with self._lock:
            if self._db.count_plans() >= self._max_entries:
                raise PlanCapacityError(
                    "The plan store has reached its configured capacity."
                )
            self._db.save_plan(self._copy(plan), owner_id, session_id)
        return self._copy(plan)

    def get_plan(
        self, plan_id: str, owner_id: str, session_id: str
    ) -> TaskPlan:
        """Return the owned plan after reconciling approval-driven states."""
        with self._lock:
            plan = self._owned_plan(plan_id, owner_id, session_id)
            reconciled = self._reconcile(plan, owner_id, session_id)
            return self._copy(reconciled)

    def list_plans_page(
        self,
        *,
        owner_id: str,
        session_id: str,
        limit: int,
        offset: int = 0,
    ) -> tuple[list[PlanSummaryView], int]:
        """Identity-scoped plan inventory: (page, total) in one snapshot.

        STRICTLY READ-ONLY (Phase 5.2-a): never reconciles approvals,
        never mutates step status, never appends audit events, and never
        creates attempts. Rows arrive from the façade's single
        ``BEGIN IMMEDIATE`` transaction (page + total, deterministic
        ``created_at DESC, plan_id DESC`` ordering).

        Every payload is validated strictly; a corrupted plan raises
        ``PlanCorruptError`` with a fixed non-sensitive message instead
        of serving, repairing, or silently skipping the damaged row.
        Owner/session columns are read from the filtered rows, so only
        the requesting identity's plans can ever appear.
        """
        records, total = self._db.plan_summaries_page(
            owner_id, session_id, limit=limit, offset=offset
        )
        views = [self._validated_plan_view(record) for record in records]
        return views, total

    def _validated_plan_view(self, record) -> PlanSummaryView:
        """Validate one stored plan payload strictly, or fail closed."""
        plan = self._validated_plan_payload(record.plan_id, record.payload)
        return PlanSummaryView(
            plan_id=record.plan_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            plan=plan,
        )

    def _validated_plan_payload(self, plan_id: str, payload: str) -> TaskPlan:
        """Parse and strictly validate one stored plan payload.

        Fail closed: malformed payloads raise ``PlanCorruptError`` with
        a fixed non-sensitive message — never served, repaired, or
        skipped; identifiers go to logs only.
        """
        try:
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise TypeError("plan payload is not a JSON object")
            return TaskPlan.model_validate(parsed)
        except (TypeError, ValueError) as exc:
            # ValidationError and JSONDecodeError are ValueError
            # subclasses; identifiers and internals stay out of logs.
            logger.warning(
                "Corrupted plan payload rejected on read",
                extra={
                    "extra_fields": {
                        "error_type": type(exc).__name__,
                        "record": "plan",
                        "plan_id": plan_id,
                    }
                },
            )
            raise PlanCorruptError(
                "Plan state could not be validated."
            ) from exc

    @staticmethod
    def _step_succeeded(step: PlanStep) -> bool:
        """Only a recorded successful outcome proves a step succeeded.

        Status alone is insufficient: a recovery fallback may leave a
        step without an observable result, and such a step must not
        unblock dependents. RECOVERY_REQUIRED can never satisfy
        readiness: an unproven side effect is not a success.

        Delegates to the module-level ``_recorded_success`` so the
        detail-side status derivation (5.2-c) and dependency readiness
        can never disagree about what counts as a success.
        """
        return _recorded_success(step)

    def ready_step_ids(
        self, plan_id: str, owner_id: str, session_id: str
    ) -> list[str]:
        """List pending step ids whose dependencies genuinely succeeded."""
        plan = self.get_plan(plan_id, owner_id, session_id)
        satisfied = {
            step.step_id
            for step in plan.steps
            if self._step_succeeded(step)
        }
        ready: list[str] = []
        for step in plan.steps:
            if step.status is not PlanStepStatus.PENDING:
                continue
            if all(dependency in satisfied for dependency in step.depends_on):
                ready.append(step.step_id)
        return ready

    def request_step_execution(
        self, plan_id: str, step_id: str, owner_id: str, session_id: str
    ) -> StepOutcome:
        """Request execution of exactly one ready step (never a sequence).

        The whole check-then-execute sequence holds the plan lock so two
        concurrent requests for the same step can never both pass the
        pending-state check and double-execute. The step passes through
        the existing policy and approval workflow: registered read-only
        tools may execute immediately; mutating tools return a pending
        approval bound to the exact arguments. No further step is
        advanced automatically, and steps awaiting approval are tracked
        so the approval workflow stays the single execution path.

        Raises:
            PlanNotFoundError: Unknown plan or foreign identity.
            PlanStepNotFoundError: Unknown step in the plan.
            PlanStepStateError: Step not pending or dependencies unsatisfied.
            ApprovalCapacityError: Approval store unavailable (no execution).
            ApprovalWorkflowError: Policy denied the step's tool.
            ApprovalArgumentsError: Model arguments failed tool validation.
            UnknownToolError: The step's tool vanished from the registry.
        """
        with self._lock:
            plan = self._owned_plan(plan_id, owner_id, session_id)
            plan = self._reconcile(plan, owner_id, session_id)

            positions = {step.step_id: i for i, step in enumerate(plan.steps)}
            if step_id not in positions:
                raise PlanStepNotFoundError("Plan step was not found")
            step = plan.steps[positions[step_id]]

            if step.status is PlanStepStatus.RECOVERY_REQUIRED:
                raise PlanStepStateError(
                    "Plan step is uncertain and requires explicit "
                    "recovery resolution first."
                )
            if step.status is not PlanStepStatus.PENDING:
                raise PlanStepStateError("Plan step is not pending.")
            satisfied = {
                s.step_id
                for s in plan.steps
                if self._step_succeeded(s)
            }
            if any(dep not in satisfied for dep in step.depends_on):
                raise PlanStepStateError(
                    "The step's dependencies are not complete."
                )

            # The approval workflow is the single execution boundary. It is
            # safe under this lock: its own locks are distinct objects, and
            # mutating tools only create a pending approval here.
            try:
                outcome = self._approvals.request_tool(
                    step.tool_name,
                    dict(step.arguments),
                    owner_id,
                    session_id,
                    plan_id=plan_id,
                    step_id=step_id,
                )
            except ToolNotImplementedError:
                # Registered metadata without an implementation must not
                # leave the step pending forever: record an honest,
                # final failure instead (the registry, not the model,
                # caused this).
                failure = ToolResult.failure_result(
                    tool_name=step.tool_name,
                    error_code="tool_unavailable",
                    message=(
                        "The tool is registered without an executable "
                        "implementation."
                    ),
                )
                plan = self._record_result_locked(plan, step_id, failure, owner_id, session_id)
                logger.warning(
                    "Plan step failed: registered tool has no implementation",
                    extra={"extra_fields": {"tool_name": step.tool_name}},
                )
                return failure

            if isinstance(outcome, ToolResult):
                plan = self._record_result_locked(plan, step_id, outcome, owner_id, session_id)
                return outcome
            if isinstance(outcome, ApprovalRequest):
                plan = self._update_step(
                    plan,
                    step_id,
                    {
                        "status": PlanStepStatus.AWAITING_APPROVAL,
                        "approval_id": outcome.approval_id,
                    },
                    owner_id,
                    session_id,
                )
                return outcome
            raise PlanStepStateError("Unexpected approval workflow result.")

    def record_result(
        self,
        plan_id: str,
        step_id: str,
        result: ToolResult,
        owner_id: str,
        session_id: str,
    ) -> None:
        """Resolve one executed step with its authoritative ToolResult."""
        with self._lock:
            plan = self._owned_plan(plan_id, owner_id, session_id)
            self._record_result_locked(plan, step_id, result, owner_id, session_id)

    def resolve_approval_outcome(
        self,
        approval_id: str,
        owner_id: str,
        session_id: str,
        result: ToolResult,
    ) -> None:
        """Record one approval resolution on its bound plan step.

        Registered as the approval service's outcome listener. Outcomes
        are attached the moment the workflow resolves an approval
        (approve→execute, reject), so the plan never depends on reading
        approval records that terminal retention may purge later.
        Approvals created from plain chat without a bound plan step are
        silently ignored.
        """
        with self._lock:
            ref = self._db.find_step_ref_by_approval(
                approval_id, owner_id, session_id
            )
            if ref is None:
                # Approval belongs to no plan step (plain chat proposal).
                return
            plan_id, step_id = ref
            plan = self._owned_plan(plan_id, owner_id, session_id)
            self._record_approval_result_locked(plan, step_id, result, owner_id, session_id)
            logger.info(
                "Recorded approval outcome on the owning plan step",
                extra={
                    "extra_fields": {
                        "tool_name": result.tool_name,
                        "result_status": result.status,
                    }
                },
            )

    def resolve_step_uncertain(
        self,
        approval_id: str,
        owner_id: str,
        session_id: str,
        result: ToolResult,
    ) -> None:
        """Move a mutating step with UNKNOWN outcome to RECOVERY_REQUIRED.

        Registered as the approval service's uncertain listener for
        mutating timeouts: the side effect can neither be proven present
        nor absent, so the step must await explicit reconciliation
        instead of being marked successful or failed. Steps already
        resolved (e.g. by persisted evidence) keep their state and the
        mismatch is logged.
        """
        with self._lock:
            ref = self._db.find_step_ref_by_approval(
                approval_id, owner_id, session_id
            )
            if ref is None:
                # Uncertain outcome of a plain-chat proposal: no plan
                # step to mark; the attempt row carries the evidence.
                logger.warning(
                    "Uncertain mutating outcome has no bound plan step",
                    extra={
                        "extra_fields": {
                            "error_code": result.error_code,
                        }
                    },
                )
                return
            plan_id, step_id = ref
            plan = self._owned_plan(plan_id, owner_id, session_id)
            positions = {step.step_id: i for i, step in enumerate(plan.steps)}
            step = plan.steps[positions[step_id]]
            if step.status is not PlanStepStatus.AWAITING_APPROVAL:
                logger.warning(
                    "Uncertain outcome ignored: step no longer awaits approval",
                    extra={
                        "extra_fields": {
                            "step_status": step.status.value,
                        }
                    },
                )
                return
            self._update_step(
                plan,
                step_id,
                {
                    "status": PlanStepStatus.RECOVERY_REQUIRED,
                    "result": result.model_dump(),
                },
                owner_id,
                session_id,
            )
            self._db.append_event(
                EVENT_RECOVERY_REQUIRED,
                detail={
                    "plan_id": plan_id,
                    "step_id": step_id,
                    "approval_id": approval_id,
                    "error_code": result.error_code,
                },
            )
            logger.warning(
                "Plan step marked RECOVERY_REQUIRED (uncertain mutation)",
                extra={
                    "extra_fields": {
                        "result_status": result.status,
                    }
                },
            )

    def resolve_step_recovery(
        self,
        plan_id: str,
        step_id: str,
        owner_id: str,
        session_id: str,
    ) -> TaskPlan:
        """Explicitly reconcile an uncertain step to an honest failure.

        The only legal transition out of RECOVERY_REQUIRED. The outcome
        stays unproven, so the step resolves FAILED with the
        ``execution_uncertain`` classification and the mutation boundary
        attempt is finalized RECONCILED; the user verifies the external
        world and captures a new plan if needed.
        """
        with self._lock:
            plan = self._owned_plan(plan_id, owner_id, session_id)
            positions = {step.step_id: i for i, step in enumerate(plan.steps)}
            if step_id not in positions:
                raise PlanStepNotFoundError("Plan step was not found")
            step = plan.steps[positions[step_id]]
            if step.status is not PlanStepStatus.RECOVERY_REQUIRED:
                raise PlanStepStateError(
                    "Plan step does not require recovery reconciliation."
                )
            updated = self._update_step(
                plan,
                step_id,
                {
                    "status": PlanStepStatus.FAILED,
                    "result": ToolResult.failure_result(
                        tool_name=step.tool_name,
                        error_code=ERROR_EXECUTION_UNCERTAIN,
                        message=(
                            "Execution outcome is uncertain; verify the "
                            "external effect manually before retrying."
                        ),
                    ).model_dump(),
                },
                owner_id,
                session_id,
            )
            if step.approval_id:
                self._approvals.reconcile_uncertain(
                    step.approval_id, owner_id, session_id
                )
            self._db.append_event(
                EVENT_EXECUTION_RECONCILED,
                detail={
                    "plan_id": plan_id,
                    "step_id": step_id,
                },
            )
            logger.info(
                "Plan step reconciled from RECOVERY_REQUIRED to FAILED",
                extra={"extra_fields": {"result_status": "failure"}},
            )
            return self._copy(updated)

    # -- persisted-evidence application (used by the recovery service) -----

    def apply_persisted_outcome(
        self,
        plan_id: str,
        step_id: str,
        owner_id: str,
        session_id: str,
        result: ToolResult,
    ) -> bool:
        """Apply a proven ToolResult recorded before a crash.

        Called by the recovery service for attempts whose outcome was
        durably recorded but whose plan step never saw it. Success
        resolves EXECUTED, failure resolves FAILED; no other state
        changes and existing resolutions are never overwritten.
        """
        with self._lock:
            plan = self._owned_plan(plan_id, owner_id, session_id)
            positions = {step.step_id: i for i, step in enumerate(plan.steps)}
            if step_id not in positions:
                return False
            step = plan.steps[positions[step_id]]
            if step.status is not PlanStepStatus.AWAITING_APPROVAL:
                return False
            self._update_step(
                plan,
                step_id,
                {
                    "status": (
                        PlanStepStatus.EXECUTED
                        if result.success
                        else PlanStepStatus.FAILED
                    ),
                    "result": result.model_dump(),
                },
                owner_id,
                session_id,
            )
            self._db.append_event(
                EVENT_RECOVERY_OUTCOME_APPLIED,
                detail={
                    "plan_id": plan_id,
                    "step_id": step_id,
                    "tool_name": result.tool_name,
                },
            )
            return True

    def mark_step_interrupted(
        self,
        plan_id: str,
        step_id: str,
        owner_id: str,
        session_id: str,
    ) -> bool:
        """Resolve an approval-bound step that provably never started.

        Reached when the process died after the claim but before the
        tool invocation began: no tool side effect is possible, so the
        honest terminal outcome is FAILED(execution_interrupted).
        """
        return self._mark_step_transition(
            plan_id,
            step_id,
            owner_id,
            session_id,
            allowed_source={PlanStepStatus.AWAITING_APPROVAL},
            target=PlanStepStatus.FAILED,
            failure_result=ToolResult.failure_result(
                tool_name=self._step_tool_name(plan_id, step_id, owner_id, session_id),
                error_code=ERROR_EXECUTION_INTERRUPTED,
                message=(
                    "Execution was interrupted before the tool started; "
                    "the action never took effect."
                ),
            ),
        )

    def mark_step_uncertain_from_recovery(
        self,
        plan_id: str,
        step_id: str,
        owner_id: str,
        session_id: str,
    ) -> bool:
        """Move an approval-bound step to RECOVERY_REQUIRED on restore.

        Reached when the process died after the tool invocation began
        but before any outcome was recorded: the side effect is unknown,
        so the step awaits explicit reconciliation. Returns whether the
        transition happened; the caller (recovery service) owns the
        lifecycle audit event, keeping rescans exactly idempotent.
        """
        return self._mark_step_transition(
            plan_id,
            step_id,
            owner_id,
            session_id,
            allowed_source={PlanStepStatus.AWAITING_APPROVAL},
            target=PlanStepStatus.RECOVERY_REQUIRED,
            failure_result=None,
        )

    def mark_step_interrupted_read_only(
        self,
        plan_id: str,
        step_id: str,
        owner_id: str,
        session_id: str,
    ) -> bool:
        """Resolve a crashed read-only step honestly (no uncertainty).

        Read-only tools are provably side-effect free, so a crash after
        their invocation started is an honest interrupted failure —
        NEVER a recovery-required state. Applies to direct (non-approval)
        executions whose step is still PENDING or awaiting approval.
        """
        return self._mark_step_transition(
            plan_id,
            step_id,
            owner_id,
            session_id,
            allowed_source={PlanStepStatus.PENDING, PlanStepStatus.AWAITING_APPROVAL},
            target=PlanStepStatus.FAILED,
            failure_result=ToolResult.failure_result(
                tool_name=(
                    self._step_tool_name(plan_id, step_id, owner_id, session_id)
                ),
                error_code=ERROR_EXECUTION_INTERRUPTED,
                message=(
                    "Execution was interrupted before its outcome was "
                    "recorded; no side effect was possible."
                ),
            ).model_dump(),
        )

    def _mark_step_transition(
        self,
        plan_id: str,
        step_id: str,
        owner_id: str,
        session_id: str,
        *,
        allowed_source: set[PlanStepStatus],
        target: PlanStepStatus,
        failure_result: Optional[ToolResult],
    ) -> bool:
        """Apply one recovery transition if the step is still in source."""
        with self._lock:
            plan = self._owned_plan(plan_id, owner_id, session_id)
            positions = {step.step_id: i for i, step in enumerate(plan.steps)}
            if step_id not in positions:
                return False
            step = plan.steps[positions[step_id]]
            if step.status not in allowed_source:
                return False
            updates: dict = {"status": target}
            if failure_result is not None:
                updates["result"] = failure_result.model_dump()
            self._update_step(plan, step_id, updates, owner_id, session_id)
            return True

    def _step_tool_name(
        self, plan_id: str, step_id: str, owner_id: str, session_id: str
    ) -> str:
        """Best-effort tool name for an interrupted failure result."""
        try:
            plan = self._owned_plan(plan_id, owner_id, session_id)
            return (
                next(
                    (
                        s.tool_name
                        for s in plan.steps
                        if s.step_id == step_id
                    ),
                    step_id,
                )
            )
        except PlanNotFoundError:
            return step_id

    # -- internals -----------------------------------------------------

    def _record_result_locked(
        self,
        plan: TaskPlan,
        step_id: str,
        result: ToolResult,
        owner_id: str,
        session_id: str,
    ) -> TaskPlan:
        """Apply the authoritative ToolResult to one step (lock held)."""
        positions = {step.step_id: i for i, step in enumerate(plan.steps)}
        if step_id not in positions:
            raise PlanStepNotFoundError("Plan step was not found")
        step = plan.steps[positions[step_id]]
        allowed_states = {
            PlanStepStatus.PENDING,
            PlanStepStatus.AWAITING_APPROVAL,
        }
        if step.status not in allowed_states:
            raise PlanStepStateError("Plan step already reached a final state.")

        new_status = (
            PlanStepStatus.COMPLETED if result.success else PlanStepStatus.FAILED
        )
        return self._update_step(
            plan,
            step_id,
            {
                "status": new_status,
                "result": result.model_dump(),
            },
            owner_id,
            session_id,
        )

    def _record_approval_result_locked(
        self,
        plan: TaskPlan,
        step_id: str,
        result: ToolResult,
        owner_id: str,
        session_id: str,
    ) -> TaskPlan:
        """Apply an approved execution outcome exactly once (lock held).

        Success resolves EXECUTED (approved execution succeeded with the
        recorded ToolResult); anything else resolves FAILED honestly.
        """
        positions = {step.step_id: i for i, step in enumerate(plan.steps)}
        if step_id not in positions:
            raise PlanStepNotFoundError("Plan step was not found")
        step = plan.steps[positions[step_id]]
        if step.status is not PlanStepStatus.AWAITING_APPROVAL:
            raise PlanStepStateError(
                "Plan step does not await this approval outcome."
            )
        new_status = (
            PlanStepStatus.EXECUTED if result.success else PlanStepStatus.FAILED
        )
        return self._update_step(
            plan,
            step_id,
            {
                "status": new_status,
                "result": result.model_dump(),
            },
            owner_id,
            session_id,
        )

    def _reconcile(
        self, plan: TaskPlan, owner_id: str, session_id: str
    ) -> TaskPlan:
        """Synchronize AWAITING_APPROVAL steps with the approval workflow.

        Executed approvals are deliberately NOT resolved here: between
        the store claim and the outcome listener there is a transient
        window in which the ToolResult exists only in the approving call.
        Writing an outcome-blind EXECUTED status on read would make the
        step terminal without a result and permanently discard the
        authoritative outcome. The listener records it moments later; if
        the listener never fires, the step stays AWAITING_APPROVAL until
        terminal retention purges the record and the
        ApprovalNotFoundError branch below resolves it FAILED honestly.
        """
        for step in list(plan.steps):
            if step.status is not PlanStepStatus.AWAITING_APPROVAL:
                continue
            if not step.approval_id:
                continue
            try:
                approval = self._approvals.get_request(
                    step.approval_id, owner_id, session_id
                )
            except ApprovalNotFoundError:
                # Executed and rejected approvals are already recorded the
                # moment the workflow resolves them, so a missing record
                # here can only be an approval that expired (or was
                # removed by retention) without ever being acted on. Mark
                # the terminal failure honestly either way.
                plan = self._update_step(
                    plan,
                    step.step_id,
                    {
                        "status": PlanStepStatus.FAILED,
                        "result": ToolResult.failure_result(
                            tool_name=step.tool_name,
                            error_code="approval_unavailable",
                            message=(
                                "The approval request is no longer available."
                            ),
                        ).model_dump(),
                    },
                    owner_id,
                    session_id,
                )
                continue
            if approval.status is ApprovalStatus.EXECUTED:
                # Transient claim-to-execute window: the store marked the
                # approval EXECUTED at claim, but the approving call has
                # not notified the outcome listener yet. Leave the step
                # AWAITING_APPROVAL so the listener can still record the
                # authoritative ToolResult exactly once — never
                # terminalize blind, never overwrite a recorded result.
                # If the listener never fires, the record is purged by
                # terminal retention and the ApprovalNotFoundError branch
                # above resolves the step FAILED with an explicit
                # unavailable result (dependents stay blocked either
                # way: only a recorded success unblocks).
                continue
            elif approval.status is ApprovalStatus.REJECTED:
                plan = self._update_step(
                    plan,
                    step.step_id,
                    {
                        "status": PlanStepStatus.FAILED,
                        "result": ToolResult.failure_result(
                            tool_name=step.tool_name,
                            error_code="action_rejected",
                            message="The proposed action was rejected.",
                        ).model_dump(),
                    },
                    owner_id,
                    session_id,
                )
            elif approval.status is ApprovalStatus.EXPIRED:
                plan = self._update_step(
                    plan,
                    step.step_id,
                    {
                        "status": PlanStepStatus.FAILED,
                        "result": ToolResult.failure_result(
                            tool_name=step.tool_name,
                            error_code="approval_expired",
                            message="The proposed action expired without approval.",
                        ).model_dump(),
                    },
                    owner_id,
                    session_id,
                )
        return plan

    def _update_step(
        self,
        plan: TaskPlan,
        step_id: str,
        updates: dict,
        owner_id: str,
        session_id: str,
    ) -> TaskPlan:
        """Apply validated updates to one step, persist, and return the plan.

        Every status change is centrally validated against the plan-step
        transition map before it is allowed to persist.
        """
        dumps = plan.model_dump()
        for step_data in dumps["steps"]:
            if step_data["step_id"] == step_id:
                new_status = updates.get("status")
                if new_status is not None:
                    try:
                        validate_plan_step_transition(
                            step_data["status"],
                            new_status.value
                            if hasattr(new_status, "value")
                            else str(new_status),
                        )
                    except ExecutionLifecycleError as exc:
                        raise PlanStepStateError(str(exc)) from exc
                step_data.update(updates)
                break
        updated = TaskPlan.model_validate(dumps)
        self._store_plan(updated, owner_id, session_id)
        return updated


plan_service = PlanService()
