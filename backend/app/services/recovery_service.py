"""Safe recovery for execution state interrupted by a shutdown or crash.

Phase 4.3. Runs at application startup (idempotent, defensive: a single
row error never aborts the scan) and is directly testable. It restores
HONEST state only — it never auto-repeats a mutating action, never
claims an outcome it cannot prove, and never overwrites an already
recorded resolution.

Rules applied to persisted evidence:

- attempt CLAIMED (claimed but not started)
  -> provably no tool side effect: the plan step resolves
     FAILED(execution_interrupted); the attempt ends FAILED.

- approval consumed (EXECUTED) with NO attempt row at all
  (claim committed but the attempt insert never happened)
  -> same as above: FAILED(execution_interrupted).

- attempt FAILED terminal without any recorded outcome
  (e.g. crashed immediately after a fault classification)
  -> provably the tool never produced an outcome: step resolves
     FAILED(execution_interrupted).

- attempt STARTED with no recorded outcome
  -> the tool invocation began; the side effect is UNKNOWN. The step
     becomes RECOVERY_REQUIRED and stays that way until a human
     resolves it explicitly. The attempt keeps its standing boundary.

- attempt TIMED_OUT with no recorded outcome (crash before the uncertain
  notification was delivered)
  -> same as STARTED: RECOVERY_REQUIRED.

- terminal attempt WITH a recorded ToolResult whose plan step is still
  AWAITING_APPROVAL -> the outcome is proven: it is applied verbatim
  (success -> EXECUTED, failure -> FAILED). Nothing is invented.

- approvals still PENDING / APPROVED-but-unclaimed remain live: TTL
  expiry and the tolerant re-continue in the approval workflow handle
  them; recovery touches nothing there.
"""

from app.core.logging import get_logger
from app.schemas.approval import ApprovalStatus
from app.schemas.plan import PlanStepStatus
from app.services.approval_service import ApprovalService
from app.services.execution_db import ExecutionStateDb, OutcomeBinding
from app.services.execution_lifecycle import (
    AttemptStatus,
    EVENT_EXECUTION_INTERRUPTED,
    EVENT_RECOVERY_REQUIRED,
    EVENT_RECOVERY_SCAN_COMPLETED,
)
from app.services.plan_service import PlanService


logger = get_logger(__name__)


def _step_ref_for_attempt(
    db: ExecutionStateDb, attempt
):
    """Resolve the plan step an attempt belongs to (approval bindings first)."""
    if attempt.approval_id:
        ref = db.find_step_ref_by_approval(
            attempt.approval_id, attempt.owner_id, attempt.session_id
        )
        if ref is not None:
            return ref
    if attempt.plan_id and attempt.step_id:
        return attempt.plan_id, attempt.step_id
    return None


def run_startup_recovery(
    *,
    plan_service: PlanService,
    approval_service: ApprovalService,
) -> dict[str, int]:
    """Recover interrupted execution state; returns audit counters."""
    counts = {
        "interrupted": 0,
        "recovery_required": 0,
        "outcome_applied": 0,
        "errors": 0,
    }
    db = approval_service.execution_db

    # Pass 1 — plan steps awaiting an approval the claim already
    # consumed: classify by the attempt trail attached to that approval.
    try:
        steps = db.find_steps_with_status(PlanStepStatus.AWAITING_APPROVAL)
    except Exception as exc:  # a broken scan must never kill startup
        logger.error(
            "Recovery scan failed to list awaiting steps",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )
        steps = []
        counts["errors"] += 1

    for plan_id, step_id, owner_id, session_id, approval_id in steps:
        try:
            if approval_id is None:
                continue
            record = db.get_approval_record(approval_id)
            if record is None or record.purged:
                # Read-path reconciliation handles missing records.
                continue
            if record.request.status is not ApprovalStatus.EXECUTED:
                # PENDING / APPROVED remain live and claimable;
                # REJECTED / EXPIRED are resolved by plan reads.
                continue
            attempt = db.get_latest_attempt(approval_id)
            if attempt is None:
                if plan_service.mark_step_interrupted(
                    plan_id, step_id, owner_id, session_id
                ):
                    db.append_event(
                        EVENT_EXECUTION_INTERRUPTED,
                        detail={
                            "plan_id": plan_id,
                            "step_id": step_id,
                            "approval_id": approval_id,
                        },
                    )
                    counts["interrupted"] += 1
                continue
            if attempt.status is AttemptStatus.FAILED and attempt.outcome is None:
                if plan_service.mark_step_interrupted(
                    plan_id, step_id, owner_id, session_id
                ):
                    db.append_event(
                        EVENT_EXECUTION_INTERRUPTED,
                        detail={
                            "plan_id": plan_id,
                            "step_id": step_id,
                            "approval_id": approval_id,
                        },
                    )
                    counts["interrupted"] += 1
                continue
            if attempt.status is AttemptStatus.TIMED_OUT and attempt.outcome is None:
                if plan_service.mark_step_uncertain_from_recovery(
                    plan_id, step_id, owner_id, session_id
                ):
                    db.append_event(
                        EVENT_RECOVERY_REQUIRED,
                        detail={
                            "plan_id": plan_id,
                            "step_id": step_id,
                            "approval_id": approval_id,
                        },
                    )
                    counts["recovery_required"] += 1
                continue
            # CLAIMED / STARTED open attempts and proven outcomes are
            # handled by the passes below.
        except Exception as exc:  # row-local failure: keep scanning
            counts["errors"] += 1
            logger.error(
                "Recovery pass 1 failed for one plan step",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )

    # Pass 2 — open attempts: interrupted claims and unknown outcomes.
    try:
        open_attempts = db.get_open_attempts()
    except Exception as exc:
        logger.error(
            "Recovery scan failed to list open attempts",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )
        open_attempts = []
        counts["errors"] += 1

    for attempt in open_attempts:
        try:
            ref = _step_ref_for_attempt(db, attempt)
            if attempt.status is AttemptStatus.CLAIMED:
                # Claimed but never started: provably no side effect.
                # The attempt itself becomes terminal here, so this case
                # is never re-encountered by a later scan.
                if ref is not None:
                    plan_service.mark_step_interrupted(
                        ref[0], ref[1], attempt.owner_id, attempt.session_id
                    )
                db.transit_attempt(attempt.attempt_id, AttemptStatus.FAILED)
                db.append_event(
                    EVENT_EXECUTION_INTERRUPTED,
                    execution_id=attempt.attempt_id,
                    detail={"tool_name": attempt.tool_name},
                )
                counts["interrupted"] += 1
            elif attempt.status is AttemptStatus.STARTED:
                # Started: side effect unknown until a human reconciles.
                # The attempt KEEPS its open boundary on purpose (that is
                # what blocks any second claim), so a later scan must be
                # a no-op: act only on real state transitions.
                if not attempt.mutating:
                    # Read-only: provably no side effect was possible, so
                    # the honest outcome is an interrupted failure, and
                    # the attempt becomes terminal (idempotent rescans).
                    if ref is not None:
                        plan_service.mark_step_interrupted_read_only(
                            ref[0], ref[1], attempt.owner_id, attempt.session_id
                        )
                    db.transit_attempt(attempt.attempt_id, AttemptStatus.FAILED)
                    db.append_event(
                        EVENT_EXECUTION_INTERRUPTED,
                        execution_id=attempt.attempt_id,
                        detail={"tool_name": attempt.tool_name},
                    )
                    counts["interrupted"] += 1
                elif ref is None:
                    db.append_event(
                        EVENT_RECOVERY_REQUIRED,
                        execution_id=attempt.attempt_id,
                        detail={"tool_name": attempt.tool_name},
                    )
                    counts["recovery_required"] += 1
                elif plan_service.mark_step_uncertain_from_recovery(
                    ref[0], ref[1], attempt.owner_id, attempt.session_id
                ):
                    db.append_event(
                        EVENT_RECOVERY_REQUIRED,
                        execution_id=attempt.attempt_id,
                        detail={"tool_name": attempt.tool_name},
                    )
                    counts["recovery_required"] += 1
        except Exception as exc:
            counts["errors"] += 1
            logger.error(
                "Recovery pass 2 failed for one attempt",
                extra={
                    "extra_fields": {
                        "error_type": type(exc).__name__,
                    }
                },
            )

    # Pass 3 — proven outcomes the step never saw (crash between the
    # outcome write and the listener): apply verbatim.
    try:
        bindings = db.find_unresolved_outcome_bindings()
    except Exception as exc:
        logger.error(
            "Recovery scan failed to list outcome bindings",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )
        bindings = []
        counts["errors"] += 1

    for binding in bindings:
        try:
            if not isinstance(binding, OutcomeBinding):
                continue
            applied = plan_service.apply_persisted_outcome(
                binding.plan_id,
                binding.step_id,
                binding.owner_id,
                binding.session_id,
                binding.outcome,
            )
            if applied:
                counts["outcome_applied"] += 1
        except Exception as exc:
            counts["errors"] += 1
            logger.error(
                "Recovery pass 3 failed for one outcome binding",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )

    try:
        db.append_event(
            EVENT_RECOVERY_SCAN_COMPLETED,
            detail={
                "interrupted": counts["interrupted"],
                "recovery_required": counts["recovery_required"],
                "outcome_applied": counts["outcome_applied"],
                "errors": counts["errors"],
            },
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Recovery summary event could not be recorded",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )

    logger.info(
        "Execution state recovery finished",
        extra={"extra_fields": dict(counts)},
    )
    return counts
