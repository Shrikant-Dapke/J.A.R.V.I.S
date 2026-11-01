"""Plan foundation: validated storage and manual single-step execution.

Phase 4 milestone 4.2. This service is deliberately NOT an execution
engine: it stores validated plans, tracks step status, and executes at
most one step per explicit call, always through the existing policy and
approval workflow (`approval_service.request_tool`). There is no
automatic advancement, no loop, and no step may run before its
dependencies are verifiably satisfied.

Ownership model mirrors the conversation store: a plan is bound to the
(owner_id, session_id) identity that captured it; another identity sees
it as unknown (404 semantics), never as state it can read or mutate.

Status semantics are kept honest:
- COMPLETED: a policy-allowed non-approval execution returned a
  successful ToolResult, recorded on the step.
- EXECUTED: an approved action executed successfully; its ToolResult is
  recorded exactly once on the step by the approval-workflow listener.
- FAILED: any other terminal outcome, always with a recorded result
  (execution failure, rejection, expiry, or unavailable approval).

Approval outcomes are attached to their owning plan step the moment the
approval workflow resolves them (approve/execute, reject) — never by
re-reading approval records that terminal retention may purge. A
dependent step becomes ready only when the steps it depends on have a
RECORDED successful result; an executed-but-failed parent can never
make a dependent ready on status alone.

Failed, rejected, expired, and unavailable steps are terminal: a new
plan must be captured instead of repeating the same execution request.

There is no automatic advancement, no loop, and no step may run before
its dependencies are verifiably satisfied.
"""

import threading
from datetime import datetime, timezone
from uuid import uuid4
from typing import Callable, Optional, Union

from app.core.logging import get_logger
from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.plan import (
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
    """Raised when the bounded in-memory plan store is exhausted."""


class PlanService:
    """Store validated plans and drive their steps one explicit call at a time."""

    def __init__(
        self,
        registry: ToolRegistry = tool_registry,
        approvals: ApprovalService = approval_service,
        *,
        max_entries: int = DEFAULT_MAX_PLAN_ENTRIES,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._registry = registry
        self._approvals = approvals
        if isinstance(self._approvals, ApprovalService):
            # Record approval outcomes (approve/execute, reject) on the
            # owning plan step immediately, so the plan never depends on
            # approval records that terminal retention may purge.
            self._approvals.set_outcome_listener(self.resolve_approval_outcome)
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._plans: dict[str, TaskPlan] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _copy(plan: TaskPlan) -> TaskPlan:
        """Prevent callers from mutating the store through returned objects."""
        return TaskPlan.model_validate(plan.model_dump())

    def _owned_plan(self, plan_id: str, owner_id: str, session_id: str) -> TaskPlan:
        """Authorize access before any state is read."""
        plan = self._plans.get(plan_id)
        identity = self._owners.get(plan_id)
        if plan is None or identity != (owner_id, session_id):
            raise PlanNotFoundError("Plan was not found")
        return plan

    def _store_plan(self, plan: TaskPlan) -> None:
        self._plans[plan.plan_id] = plan
        return None

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
            if len(self._plans) >= self._max_entries:
                raise PlanCapacityError(
                    "The plan store has reached its configured capacity."
                )
            self._plans[plan.plan_id] = self._copy(plan)
            self._owners[plan.plan_id] = (owner_id, session_id)
        return self._copy(plan)

    def get_plan(
        self, plan_id: str, owner_id: str, session_id: str
    ) -> TaskPlan:
        """Return the owned plan after reconciling approval-driven states."""
        with self._lock:
            plan = self._owned_plan(plan_id, owner_id, session_id)
            reconciled = self._reconcile(plan, owner_id, session_id)
            return self._copy(reconciled)

    @staticmethod
    def _step_succeeded(step: PlanStep) -> bool:
        """Only a recorded successful outcome proves a step succeeded.

        Status alone is insufficient: a reconcile fallback may leave a
        step EXECUTED without an observable result, and such a step must
        not unblock dependents.
        """
        result = step.result
        return (
            result is not None
            and result.success
            and step.status
            in {PlanStepStatus.COMPLETED, PlanStepStatus.EXECUTED}
        )

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
                    step.tool_name, dict(step.arguments), owner_id, session_id
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
                plan = self._record_result_locked(plan, step_id, failure)
                logger.warning(
                    "Plan step failed: registered tool has no implementation",
                    extra={"extra_fields": {"tool_name": step.tool_name}},
                )
                return failure

            if isinstance(outcome, ToolResult):
                plan = self._record_result_locked(plan, step_id, outcome)
                return outcome
            if isinstance(outcome, ApprovalRequest):
                plan = self._update_step(
                    plan,
                    step_id,
                    {
                        "status": PlanStepStatus.AWAITING_APPROVAL,
                        "approval_id": outcome.approval_id,
                    },
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
            self._record_result_locked(plan, step_id, result)

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
            for plan_id, identity in list(self._owners.items()):
                if identity != (owner_id, session_id):
                    continue
                plan = self._plans.get(plan_id)
                if plan is None:
                    continue
                match = next(
                    (
                        step
                        for step in plan.steps
                        if step.approval_id == approval_id
                        and step.status is PlanStepStatus.AWAITING_APPROVAL
                    ),
                    None,
                )
                if match is None:
                    continue
                self._record_approval_result_locked(plan, match.step_id, result)
                logger.info(
                    "Recorded approval outcome on the owning plan step",
                    extra={
                        "extra_fields": {
                            "tool_name": result.tool_name,
                            "result_status": result.status,
                        }
                    },
                )
                return
            # Approval belongs to no plan step (plain chat proposal).

    def _record_approval_result_locked(
        self, plan: TaskPlan, step_id: str, result: ToolResult
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
        )

    # -- internals -----------------------------------------------------

    def _record_result_locked(
        self, plan: TaskPlan, step_id: str, result: ToolResult
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
        )

    # -- internals -----------------------------------------------------

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
                )
        return plan

    def _update_step(self, plan: TaskPlan, step_id: str, updates: dict) -> TaskPlan:
        """Apply validated updates to one step, persist, and return the plan."""
        dumps = plan.model_dump()
        for step_data in dumps["steps"]:
            if step_data["step_id"] == step_id:
                step_data.update(updates)
                break
        updated = TaskPlan.model_validate(dumps)
        self._store_plan(updated)
        return updated


plan_service = PlanService()
