"""Central execution lifecycle state machines, transition validation, and
the auditable event/error-code vocabulary.

Phase 4.3. Single source of truth for:

- execution attempt states and their allowed transitions,
- plan step states and their allowed transitions,
- stable, non-sensitive error codes (classification only, never internals),
- lifecycle event names used by structured logs and audit records.

Fail-closed rules:
- Any transition not listed here is rejected; terminal statuses absorb.
- A mutating action that timed out (or whose outcome was lost) must be
  explicitly reconciled by a human decision before it can become terminal;
  the system never guesses the outcome.
- Recovery-required steps can only be resolved to an explicit failure; the
  user verifies side effects externally and re-plans if needed.
"""

from enum import Enum
from typing import FrozenSet, Mapping


class ExecutionLifecycleError(ValueError):
    """Raised when a lifecycle transition is not allowed."""


class AttemptStatus(str, Enum):
    """Lifecycle of one execution attempt.

    CLAIMED  - authorization was consumed; may still be abandoned before
               the tool was invoked (provable: no side effect possible).
    STARTED  - the tool invocation began (persisted *before* invoking);
               if the process dies here, the side effect is unknown.
    SUCCEEDED / FAILED - terminal states backed by a recorded ToolResult.
    TIMED_OUT - the execution exceeded its bounded budget; the tool may
               still be running detached, so the side effect is UNKNOWN
               until explicit reconciliation (mutation boundary standing).
    RECONCILED - final: the uncertainty was explicitly resolved by a
               human decision and the decision was recorded.
    """

    CLAIMED = "CLAIMED"
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    RECONCILED = "RECONCILED"


TERMINAL_ATTEMPT_STATUSES: FrozenSet[AttemptStatus] = frozenset(
    {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.RECONCILED,
    }
)

#: TIMED_OUT is quasi-terminal: it stands as a mutation boundary awaiting
#: explicit reconciliation and may progress only to RECONCILED.
ATTEMPT_TRANSITIONS: Mapping[AttemptStatus, FrozenSet[AttemptStatus]] = {
    AttemptStatus.CLAIMED: frozenset(
        {AttemptStatus.STARTED, AttemptStatus.FAILED}
    ),
    AttemptStatus.STARTED: frozenset(
        {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.RECONCILED,
        }
    ),
    AttemptStatus.TIMED_OUT: frozenset({AttemptStatus.RECONCILED}),
    AttemptStatus.SUCCEEDED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.RECONCILED: frozenset(),
}


def validate_attempt_transition(
    current: AttemptStatus, target: AttemptStatus
) -> None:
    """Enforce the attempt state machine; raise on illegal transitions."""
    allowed = ATTEMPT_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ExecutionLifecycleError(
            f"Attempt transition {current.value} -> {target.value} "
            "is not allowed."
        )


OPEN_ATTEMPT_STATUSES: FrozenSet[AttemptStatus] = frozenset(
    {AttemptStatus.CLAIMED, AttemptStatus.STARTED}
)

# Stable, non-sensitive error codes. Values satisfy the ToolResult
# error_code pattern so they can be attached to recorded results.
ERROR_EXECUTION_INTERRUPTED = "execution_interrupted"
ERROR_EXECUTION_UNCERTAIN = "execution_uncertain"
ERROR_EXECUTION_TIMEOUT = "execution_timeout"
ERROR_EXECUTION_CONFLICT = "execution_conflict"
ERROR_TOOL_UNAVAILABLE = "tool_unavailable"
ERROR_ACTION_REJECTED = "action_rejected"
ERROR_APPROVAL_EXPIRED = "approval_expired"
ERROR_APPROVAL_UNAVAILABLE = "approval_unavailable"

# Lifecycle event names for audit records and structured logs. These are
# classifications only; payloads must never include secret material,
# sensitive argument values, or session identifiers.
EVENT_EXECUTION_REQUESTED = "execution_requested"
EVENT_EXECUTION_CLAIMED = "execution_claimed"
EVENT_EXECUTION_STARTED = "execution_started"
EVENT_EXECUTION_SUCCEEDED = "execution_succeeded"
EVENT_EXECUTION_FAILED = "execution_failed"
EVENT_EXECUTION_TIMED_OUT = "execution_timed_out"
EVENT_EXECUTION_INTERRUPTED = "execution_interrupted"
EVENT_EXECUTION_RECONCILED = "execution_reconciled"
EVENT_RECOVERY_REQUIRED = "recovery_required"
EVENT_RECOVERY_OUTCOME_APPLIED = "recovery_outcome_applied"
EVENT_APPROVAL_CREATED = "approval_created"
EVENT_APPROVAL_APPROVED = "approval_approved"
EVENT_APPROVAL_REJECTED = "approval_rejected"
EVENT_APPROVAL_EXPIRED = "approval_expired"
EVENT_RECOVERY_SCAN_COMPLETED = "recovery_scan_completed"


from app.schemas.plan import PlanStepStatus


#: Allowed plan-step transitions. RECOVERY_REQUIRED is non-terminal and
#: resolves only through explicit human reconciliation to FAILED.
PLAN_STEP_TRANSITIONS: Mapping[str, FrozenSet[str]] = {
    PlanStepStatus.PENDING.value: frozenset(
        {
            PlanStepStatus.AWAITING_APPROVAL.value,
            PlanStepStatus.COMPLETED.value,
            PlanStepStatus.FAILED.value,
        }
    ),
    PlanStepStatus.AWAITING_APPROVAL.value: frozenset(
        {
            PlanStepStatus.COMPLETED.value,
            PlanStepStatus.EXECUTED.value,
            PlanStepStatus.RECOVERY_REQUIRED.value,
            PlanStepStatus.FAILED.value,
        }
    ),
    PlanStepStatus.RECOVERY_REQUIRED.value: frozenset(
        {PlanStepStatus.FAILED.value}
    ),
    PlanStepStatus.COMPLETED.value: frozenset(),
    PlanStepStatus.EXECUTED.value: frozenset(),
    PlanStepStatus.FAILED.value: frozenset(),
}


def validate_plan_step_transition(current: str, target: str) -> None:
    """Enforce the plan step state machine; raise on illegal transitions."""
    allowed = PLAN_STEP_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ExecutionLifecycleError(
            f"Plan step transition {current} -> {target} is not allowed."
        )


RECOVERY_PLAN_STEP_STATUS = PlanStepStatus.RECOVERY_REQUIRED.value
