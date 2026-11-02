"""Policy-aware approval workflow between proposals and tool execution.

Phase 4.3 additions on top of the Phase 3 workflow:

- Every execution gets a unique, persisted attempt identity (created at
  the claim, with the tool-invocation boundary persisted BEFORE the
  tool runs), so crashed runs leave inspectable lifecycle evidence.
- Tool execution runs under a bounded wall-clock timeout. For mutating
  tools a timeout is NEVER treated as "no side effect": the attempt is
  recorded TIMED_OUT and the bound plan step becomes RECOVERY_REQUIRED
  through the uncertain-outcome listener, requiring explicit human
  reconciliation. Read-only timeouts are ordinary failures.
- A crash between approving and claiming leaves the record APPROVED;
  re-calling approve continues the same single-use resolution instead
  of dead-ending the flow (fail-closed tolerance: the approval can be
  claimed exactly once either way).

Route code must keep mapping ApprovalStateError to 409 for already-
claimed or replayed approvals: duplicate approval resolution remains
impossible.
"""

from typing import Any, Callable, Mapping, Optional, Union
from uuid import uuid4

from app.core.logging import get_logger
from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.policy import PolicyDecision
from app.schemas.tool import ToolResult
from app.services.approval_store import (
    DEFAULT_OWNER_ID,
    DEFAULT_SESSION_ID,
    ApprovalStore,
    approval_store,
)
from app.services.execution_db import (
    ExecutionConflictError,
    ExecutionStateDb,
)
from app.services.execution_lifecycle import (
    ERROR_EXECUTION_TIMEOUT,
    ERROR_TOOL_UNAVAILABLE,
    AttemptStatus,
    EVENT_APPROVAL_APPROVED,
    EVENT_EXECUTION_CLAIMED,
    EVENT_EXECUTION_FAILED,
    EVENT_EXECUTION_RECONCILED,
    EVENT_EXECUTION_REQUESTED,
    EVENT_EXECUTION_STARTED,
    EVENT_EXECUTION_SUCCEEDED,
    EVENT_EXECUTION_TIMED_OUT,
)
from app.services.policy_service import evaluate_tool_policy
from app.services.tool_executor import ToolExecutor, default_tool_executor
from app.services.tool_base import Tool, ToolExecutionError
from app.services.tool_registry import ToolRegistry, UnknownToolError, tool_registry


logger = get_logger(__name__)
ApprovalWorkflowResult = Union[ToolResult, ApprovalRequest]


class ApprovalWorkflowError(ValueError):
    """Safe service error that can be mapped to a structured HTTP response."""


class ApprovalPolicyError(ApprovalWorkflowError):
    """Raised when an approval no longer satisfies current policy."""


class ApprovalIntegrityError(ApprovalWorkflowError):
    """Raised when stored arguments no longer match the tool schema exactly."""


class ApprovalArgumentsError(ApprovalWorkflowError):
    """Raised when a proposed action cannot pass the tool input schema."""


class ApprovalService:
    """Coordinate policy, approval state, and the already-authorized executor."""

    def __init__(
        self,
        registry: ToolRegistry = tool_registry,
        executor: ToolExecutor = default_tool_executor,
        store: ApprovalStore = approval_store,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._store = store
        # Attempts share the store's façade so the open-attempt unique
        # index and audit rows live in the same database.
        self._db: ExecutionStateDb = store.execution_db
        self._on_resolved: Optional[
            Callable[[str, str, str, ToolResult], None]
        ] = None
        self._on_uncertain: Optional[
            Callable[[str, str, str, ToolResult], None]
        ] = None

    @property
    def execution_db(self) -> ExecutionStateDb:
        """The store-shared façade backing attempts and audit events."""
        return self._db

    def set_outcome_listener(
        self,
        listener: Callable[[str, str, str, ToolResult], None],
    ) -> None:
        """Bind a listener notified when this workflow resolves an approval.

        The listener receives (approval_id, owner_id, session_id,
        ToolResult) at the moment the outcome is authoritative — for
        approve→execute and reject. Storage consumers such as the plan
        service use it to record outcomes without later approval-record
        reads (which terminal retention may purge).
        """
        self._on_resolved = listener

    def set_uncertain_listener(
        self,
        listener: Callable[[str, str, str, ToolResult], None],
    ) -> None:
        """Bind a listener for mutating executions with UNKNOWN outcomes.

        Called whenever a mutating tool times out: the side effect can
        never be proven absent, so the consumer must mark the bound step
        RECOVERY_REQUIRED instead of success or failure.
        """
        self._on_uncertain = listener

    def _notify_resolved(
        self,
        approval_id: str,
        owner_id: str,
        session_id: str,
        result: ToolResult,
    ) -> None:
        """Deliver an authoritative outcome (listener failures are logged)."""
        listener = self._on_resolved
        if listener is None:
            return
        try:
            listener(approval_id, owner_id, session_id, result)
        except Exception as exc:  # bookkeeping must not hide the outcome
            logger.warning(
                "Approval outcome listener failed",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )

    def _notify_uncertain(
        self,
        approval_id: str,
        owner_id: str,
        session_id: str,
        result: ToolResult,
    ) -> None:
        """Deliver an uncertain mutating outcome (failures logged, not fatal)."""
        listener = self._on_uncertain
        if listener is None:
            logger.warning(
                "Uncertain mutating outcome had no recovery listener bound",
                extra={
                    "extra_fields": {
                        "approval_id": approval_id,
                        "error_code": result.error_code,
                    }
                },
            )
            return
        try:
            listener(approval_id, owner_id, session_id, result)
        except Exception as exc:  # bookkeeping must not hide the outcome
            logger.warning(
                "Uncertain outcome listener failed",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )

    @staticmethod
    def _action_description(
        tool_name: str, description: str, arguments: Mapping[str, Any]
    ) -> str:
        """Build a bounded description from already validated arguments."""
        if not arguments:
            return f"{tool_name}: {description}"
        rendered = ", ".join(
            f"{key}={value}" for key, value in sorted(arguments.items())
        )
        return f"{tool_name}: {description} ({rendered})"[:512]

    def _decision(self, tool_name: str) -> PolicyDecision:
        return evaluate_tool_policy(tool_name, registry=self._registry)

    def _normalized_arguments(
        self, tool_name: str, arguments: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Validate a proposal without crossing the execution boundary."""
        registered = self._registry.require(tool_name)
        if not isinstance(registered, Tool):
            raise ApprovalArgumentsError("The requested tool is not executable.")
        return registered.normalized_arguments(arguments)

    def request_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
        *,
        plan_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> ApprovalWorkflowResult:
        """Execute read-only tools or create a pending exact approval.

        ``plan_id``/``step_id`` are optional audit bindings recorded on
        the execution attempt of a direct (read-only) execution so that
        interrupted runs can be attributed back to the requesting plan.
        """
        decision = self._decision(tool_name)
        if self._registry.get(tool_name) is None:
            raise UnknownToolError(f"Tool '{tool_name}' is not registered")

        if decision.allowed:
            attempt_ref = {
                "plan_id": plan_id,
                "step_id": step_id,
            }
            self._db.append_event(
                EVENT_EXECUTION_REQUESTED,
                detail={"tool_name": tool_name, "outcome": "read_only"},
            )
            return self._execute_read_only(
                tool_name,
                arguments,
                owner_id,
                session_id,
                **attempt_ref,
            )

        if not decision.requires_approval or decision.risk_level is None:
            raise ApprovalWorkflowError(decision.reason)

        definition = self._registry.get_definition(tool_name)
        if definition is None:
            raise UnknownToolError(f"Tool '{tool_name}' is not registered")

        try:
            normalized = self._normalized_arguments(tool_name, arguments)
        except ValueError as exc:
            raise ApprovalArgumentsError("The tool arguments are invalid.") from exc

        request = self._store.create_approval_request(
            tool_name=tool_name,
            arguments={key: str(value) for key, value in normalized.items()},
            action_description=self._action_description(
                tool_name, definition.description, normalized
            ),
            risk_level=decision.risk_level,
            owner_id=owner_id,
            session_id=session_id,
        )
        self._db.append_event(
            EVENT_EXECUTION_REQUESTED,
            detail={
                "approval_id": request.approval_id,
                "tool_name": tool_name,
                "outcome": "approval_pending",
            },
        )
        return request

    # -- direct (read-only) execution path ---------------------------------

    def _execute_read_only(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        owner_id: str,
        session_id: str,
        *,
        plan_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> ToolResult:
        """Run a policy-allowed read-only tool under an attempt lifecycle."""
        attempt_id = self._db.create_attempt(
            attempt_id=str(uuid4()),
            owner_id=owner_id,
            session_id=session_id,
            tool_name=tool_name,
            mutating=False,
            status=AttemptStatus.STARTED,
            plan_id=plan_id,
            step_id=step_id,
        )
        self._db.append_event(
            EVENT_EXECUTION_STARTED,
            execution_id=attempt_id,
            detail={"tool_name": tool_name, "mutating": False},
        )
        try:
            result = self._executor.execute(tool_name, arguments)
        except ValueError as exc:
            self._fail_attempt(
                attempt_id, ERROR_TOOL_UNAVAILABLE, tool_name
            )
            logger.warning(
                "Read-only tool had no executable implementation",
                extra={
                    "extra_fields": {
                        "tool_name": tool_name,
                        "error_type": type(exc).__name__,
                        "attempt_id": attempt_id,
                    }
                },
            )
            raise
        # Read-only tools are provably free of external side effects, so
        # even a timeout ends the attempt FAILED with its evidence kept.
        status = (
            AttemptStatus.SUCCEEDED if result.success else AttemptStatus.FAILED
        )
        self._db.transit_attempt(attempt_id, status, outcome=result)
        self._append_outcome_event(attempt_id, status, tool_name, result)
        return result

    def _fail_attempt(
        self,
        attempt_id: str,
        error_code: str,
        tool_name: str,
    ) -> None:
        """Fault-style transition helper ending an attempt in FAILED."""
        self._db.transit_attempt(attempt_id, AttemptStatus.FAILED)
        self._db.append_event(
            EVENT_EXECUTION_FAILED,
            execution_id=attempt_id,
            detail={"tool_name": tool_name, "error_code": error_code},
        )

    def _finish_failed(
        self,
        attempt_id: str,
        result: ToolResult,
        tool_name: str,
    ) -> None:
        """End an attempt in FAILED with its recorded failure outcome."""
        self._db.transit_attempt(attempt_id, AttemptStatus.FAILED, outcome=result)
        self._append_outcome_event(attempt_id, AttemptStatus.FAILED, tool_name, result)

    def _append_outcome_event(
        self,
        attempt_id: str,
        status: AttemptStatus,
        tool_name: str,
        result: ToolResult,
    ) -> None:
        if status is AttemptStatus.SUCCEEDED:
            event = EVENT_EXECUTION_SUCCEEDED
        elif status is AttemptStatus.TIMED_OUT:
            event = EVENT_EXECUTION_TIMED_OUT
        else:
            event = EVENT_EXECUTION_FAILED
        self._db.append_event(
            event,
            execution_id=attempt_id,
            detail={
                "tool_name": tool_name,
                "error_code": result.error_code,
            },
        )

    # -- approval lifecycle -------------------------------------------------

    def get_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Return approval state with lazy expiration applied."""
        return self._store.get_approval_request(approval_id, owner_id, session_id)

    def reject_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Reject a pending request without reaching the executor."""
        request = self._store.reject_request(approval_id, owner_id, session_id)
        self._notify_resolved(
            approval_id,
            owner_id,
            session_id,
            ToolResult.failure_result(
                tool_name=request.tool_name,
                error_code="action_rejected",
                message="The proposed action was rejected.",
            ),
        )
        return request

    def approve_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ToolResult:
        """Authorize, consume, and execute exactly one approved request."""
        request = self._store.get_approval_request(approval_id, owner_id, session_id)
        decision = self._decision(request.tool_name)
        if decision.allowed or not decision.requires_approval:
            raise ApprovalPolicyError(
                "The approval no longer satisfies the current tool policy."
            )
        if decision.risk_level != request.risk_level:
            raise ApprovalPolicyError(
                "The approval risk classification no longer matches policy."
            )

        try:
            normalized = self._normalized_arguments(
                request.tool_name, request.arguments
            )
        except ValueError as exc:
            raise ApprovalIntegrityError(
                "The approved arguments are no longer valid."
            ) from exc

        normalized_strings = {
            key: str(value) for key, value in normalized.items()
        }
        if normalized_strings != request.arguments:
            raise ApprovalIntegrityError(
                "The approved arguments no longer match the original request."
            )
        if not self._store.arguments_match_original(
            approval_id,
            request.arguments,
            owner_id,
            session_id,
        ):
            raise ApprovalIntegrityError(
                "The approved arguments failed integrity verification."
            )

        # Authorization single-use, claim single-use, one open attempt.
        if request.status is ApprovalStatus.PENDING:
            self._store.approve_request(approval_id, owner_id, session_id)
            self._db.append_event(
                EVENT_APPROVAL_APPROVED,
                detail={"approval_id": approval_id},
            )
        elif request.status is ApprovalStatus.APPROVED:
            # APPROVED-but-unclaimed: a prior process approved this and
            # died before the claim. Continue the SAME single-use
            # resolution instead of dead-ending; the claim below stays
            # exactly-once regardless.
            logger.info(
                "Continuing approved-but-unclaimed approval after restart",
                extra={"extra_fields": {"approval_id": approval_id}},
            )
        consumed = self._store.claim_approved_for_execution(
            approval_id, owner_id, session_id
        )

        attempt_id = self._create_claimed_attempt(
            approval_id, consumed, owner_id, session_id
        )
        # Persist the mutation boundary BEFORE invoking the tool. What
        # happens from here on is unknown if the process dies.
        self._db.transit_attempt(attempt_id, AttemptStatus.STARTED)
        self._db.append_event(
            EVENT_EXECUTION_STARTED,
            execution_id=attempt_id,
            detail={"tool_name": consumed.tool_name, "mutating": True},
        )

        try:
            result = self._executor.execute(
                consumed.tool_name, consumed.arguments
            )
        except ToolExecutionError as exc:
            # The tool layer classified its own failure with a safe,
            # bounded error code; that classification is authoritative
            # and must be recorded, never leaked further up.
            result = ToolResult.failure_result(
                tool_name=consumed.tool_name,
                error_code=exc.error_code,
                message=exc.safe_message,
            )
            self._finish_failed(attempt_id, result, consumed.tool_name)
            self._notify_resolved(approval_id, owner_id, session_id, result)
            return result
        except ValueError as exc:
            # ToolNotImplementedError: the tool provably never ran.
            logger.warning(
                "Approved tool had no executable implementation",
                extra={
                    "extra_fields": {
                        "tool_name": consumed.tool_name,
                        "error_type": type(exc).__name__,
                        "attempt_id": attempt_id,
                    }
                },
            )
            result = ToolResult.failure_result(
                tool_name=consumed.tool_name,
                error_code=ERROR_TOOL_UNAVAILABLE,
                message="The approved tool is not available for execution.",
            )
            self._fail_attempt(attempt_id, ERROR_TOOL_UNAVAILABLE, consumed.tool_name)
            self._notify_resolved(approval_id, owner_id, session_id, result)
            return result
        except Exception:
            # An unexpected tool-layer failure (never a timeout): record
            # it honestly as a failed execution at the tool boundary.
            # The failure result carries a fixed safe message only — no
            # exception text, no stack trace, no internals (requirement E).
            logger.warning(
                "Approved tool failed with an unclassified error",
                extra={
                    "extra_fields": {
                        "tool_name": consumed.tool_name,
                        "attempt_id": attempt_id,
                    }
                },
            )
            result = ToolResult.failure_result(
                tool_name=consumed.tool_name,
                error_code=ERROR_TOOL_UNAVAILABLE,
                message="The tool failed without a classified error.",
            )
            self._finish_failed(attempt_id, result, consumed.tool_name)
            self._notify_resolved(approval_id, owner_id, session_id, result)
            return result

        if result.success:
            self._db.transit_attempt(
                attempt_id, AttemptStatus.SUCCEEDED, outcome=result
            )
            self._append_outcome_event(
                attempt_id, AttemptStatus.SUCCEEDED, consumed.tool_name, result
            )
            self._notify_resolved(approval_id, owner_id, session_id, result)
            return result

        if result.error_code == ERROR_EXECUTION_TIMEOUT:
            # A mutating timeout NEVER implies the side effect did not
            # happen. Keep the mutation boundary standing; require a
            # human reconciliation. Never blind-retry.
            self._db.transit_attempt(attempt_id, AttemptStatus.TIMED_OUT)
            self._append_outcome_event(
                attempt_id, AttemptStatus.TIMED_OUT, consumed.tool_name, result
            )
            self._notify_uncertain(approval_id, owner_id, session_id, result)
            return result

        self._db.transit_attempt(attempt_id, AttemptStatus.FAILED, outcome=result)
        self._append_outcome_event(
            attempt_id, AttemptStatus.FAILED, consumed.tool_name, result
        )
        self._notify_resolved(approval_id, owner_id, session_id, result)
        return result

    def _create_claimed_attempt(
        self,
        approval_id: str,
        consumed: ApprovalRequest,
        owner_id: str,
        session_id: str,
    ) -> str:
        """Persist the claimed attempt (one open attempt per approval)."""
        attempt_id = str(uuid4())
        try:
            self._db.create_attempt(
                attempt_id=attempt_id,
                owner_id=owner_id,
                session_id=session_id,
                tool_name=consumed.tool_name,
                mutating=True,
                status=AttemptStatus.CLAIMED,
                approval_id=approval_id,
            )
        except ExecutionConflictError as exc:
            raise ApprovalWorkflowError(
                "This approval already has an open execution attempt."
            ) from exc
        self._db.append_event(
            EVENT_EXECUTION_CLAIMED,
            execution_id=attempt_id,
            detail={
                "approval_id": approval_id,
                "tool_name": consumed.tool_name,
            },
        )
        return attempt_id

    def reconcile_uncertain(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> bool:
        """Record a human reconciliation for an uncertain mutation.

        Attempts of this approval still carrying the mutation boundary
        (STARTED after a restart, or TIMED_OUT after a timeout) are
        finalized RECONCILED. Never auto-retries and never fabricates an
        outcome; the plan step resolution is the caller's decision.
        """
        attempt = self._db.get_latest_attempt(approval_id)
        if attempt is None:
            return False
        if attempt.status not in {
            AttemptStatus.STARTED,
            AttemptStatus.TIMED_OUT,
        }:
            return False
        self._db.transit_attempt(
            attempt.attempt_id, AttemptStatus.RECONCILED
        )
        self._db.append_event(
            EVENT_EXECUTION_RECONCILED,
            execution_id=attempt.attempt_id,
            detail={
                "approval_id": approval_id,
                "tool_name": attempt.tool_name,
            },
        )
        return True


approval_service = ApprovalService()
