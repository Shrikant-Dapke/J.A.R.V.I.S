"""Persistent approval store with TTL, single-use claims, and retention.

Phase 4.3: the store internals moved from process-local dictionaries to
the SQLite execution-state façade, so approval records and their
outcomes survive restarts and crashed mid-execution states remain
inspectable. The public API and semantics of the previous in-memory
store are preserved exactly:

- PENDING / APPROVED requests lazily expire at their persisted absolute
  ``expires_at``.
- ``claim_approved_for_execution`` consumes APPROVED exactly once —
  EXECUTED can never be reclaimed or replayed.
- Terminal records are retained for a bounded window for audit, then
  flagged purged; reads of purged records fail with
  ``ApprovalNotFoundError`` (404 semantics) and purged rows no longer
  occupy capacity.
- Owner/session isolation: a foreign identity observes not-found, never
  the record's state.
- Returned objects are defensive copies; callers cannot mutate store
  state through them.

Secret hygiene: arguments are validated at the schema layer
(secret-like keys and oversized values are rejected before this store
ever sees them), and the persisted arguments are the integrity-verified
exact values the user approved. Audit rows carry identifiers and
classifications only.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Optional
from uuid import uuid4

from app.core.logging import get_logger
from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.policy import RiskLevel
from app.services.execution_db import (
    ApprovalRecord,
    ExecutionStateDb,
    state_db as shared_state_db,
)
from app.services.execution_lifecycle import (
    EVENT_APPROVAL_CREATED,
    EVENT_APPROVAL_EXPIRED,
)


logger = get_logger(__name__)


DEFAULT_APPROVAL_TTL_SECONDS = 5 * 60
DEFAULT_MAX_APPROVAL_ENTRIES = 1024
DEFAULT_TERMINAL_RETENTION_SECONDS = 60
DEFAULT_OWNER_ID = "internal-owner"
DEFAULT_SESSION_ID = "internal-session"


class ApprovalNotFoundError(LookupError):
    """Raised when an approval identifier is unknown (or was purged)."""


class ApprovalStateError(ValueError):
    """Raised when a lifecycle transition is not allowed."""


class ApprovalCapacityError(RuntimeError):
    """Raised when the bounded approval store capacity is exhausted."""


class ApprovalStore:
    """Replaceable storage boundary for exact, short-lived approvals."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
        max_entries: int = DEFAULT_MAX_APPROVAL_ENTRIES,
        terminal_retention_seconds: int = DEFAULT_TERMINAL_RETENTION_SECONDS,
        state_db: Optional[ExecutionStateDb] = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if terminal_retention_seconds < 0:
            raise ValueError("terminal_retention_seconds must not be negative")
        self._ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_entries = max_entries
        self._terminal_retention = timedelta(seconds=terminal_retention_seconds)
        # No explicit façade => an isolated in-memory database for this
        # store (test object graphs stay independent by construction).
        self._db = state_db if state_db is not None else ExecutionStateDb()
        self._lock = RLock()

    @property
    def execution_db(self) -> ExecutionStateDb:
        """The façade backing this store (attempts share its connection)."""
        return self._db

    def _now(self) -> datetime:
        """Return a timezone-aware UTC timestamp from the injected clock."""
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _copy(request: ApprovalRequest) -> ApprovalRequest:
        """Prevent callers from mutating the store through returned objects."""
        return ApprovalRequest.model_validate(request.model_dump())

    @staticmethod
    def _with_status(
        request: ApprovalRequest,
        status: ApprovalStatus,
        resolved_at: Optional[datetime],
    ) -> ApprovalRequest:
        values = request.model_dump()
        values["status"] = status
        values["resolved_at"] = resolved_at
        return ApprovalRequest(**values)

    # -- persistence helpers ---------------------------------------------

    def _cleanup(self, now: datetime) -> None:
        """Expire stale requests and purge old terminal records (persisted).

        Terminal rows are flagged purged for the audit trail rather than
        deleted; reads treat them as unknown afterwards and capacity
        counts no longer include them.
        """
        for record in self._db.list_approval_records(include_purged=False):
            request = record.request
            if (
                request.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                and now >= request.expires_at
            ):
                expired = self._with_status(request, ApprovalStatus.EXPIRED, now)
                self._db.save_approval(
                    expired, record.owner_id, record.session_id, record.digest
                )
                self._db.append_event(
                    EVENT_APPROVAL_EXPIRED,
                    detail={"approval_id": record.approval_id, "reason": "ttl"},
                )
                request = expired
            if (
                request.status
                in {
                    ApprovalStatus.REJECTED,
                    ApprovalStatus.EXPIRED,
                    ApprovalStatus.EXECUTED,
                }
                and request.resolved_at is not None
                and now >= request.resolved_at + self._terminal_retention
            ):
                self._db.mark_approval_purged(record.approval_id)

    @staticmethod
    def _arguments_digest(arguments: dict[str, str]) -> str:
        """Create a canonical integrity digest for the approved arguments."""
        canonical = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _expire_if_needed_for_owner(
        self, approval_id: str, owner_id: str, session_id: str
    ) -> ApprovalRequest:
        """Authorize access, then apply persisted lazy expiration."""
        with self._lock:
            record = self._owned_record(approval_id, owner_id, session_id)
            request = record.request
            if (
                request.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                and self._now() >= request.expires_at
            ):
                expired = self._with_status(
                    request,
                    ApprovalStatus.EXPIRED,
                    self._now(),
                )
                self._db.save_approval(
                    expired, record.owner_id, record.session_id, record.digest
                )
                self._db.append_event(
                    EVENT_APPROVAL_EXPIRED,
                    detail={"approval_id": approval_id, "reason": "ttl"},
                )
                request = expired
            return request

    def _owned_record(
        self, approval_id: str, owner_id: str, session_id: str
    ) -> ApprovalRecord:
        record = self._db.get_approval_record(approval_id)
        if (
            record is None
            or record.purged
            or record.owner_id != owner_id
            or record.session_id != session_id
        ):
            raise ApprovalNotFoundError("Approval request was not found")
        return record

    # -- public API (unchanged shapes) ------------------------------------

    def create_approval_request(
        self,
        tool_name: str,
        arguments: dict[str, str],
        action_description: str,
        risk_level: RiskLevel,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Create a pending approval bound to validated arguments."""
        with self._lock:
            created_at = self._now()
            self._cleanup(created_at)
            if self._db.count_active_approvals() >= self._max_entries:
                raise ApprovalCapacityError(
                    "The approval store has reached its configured capacity."
                )
            request = ApprovalRequest(
                approval_id=str(uuid4()),
                tool_name=tool_name,
                arguments=dict(arguments),
                action_description=action_description,
                risk_level=risk_level,
                created_at=created_at,
                expires_at=created_at + timedelta(seconds=self._ttl_seconds),
            )
            self._db.save_approval(
                request,
                owner_id,
                session_id,
                self._arguments_digest(request.arguments),
            )
            self._db.append_event(
                EVENT_APPROVAL_CREATED,
                detail={
                    "approval_id": request.approval_id,
                    "tool_name": request.tool_name,
                    "risk_level": request.risk_level.value,
                },
            )
            return self._copy(request)

    def get_approval_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Retrieve an approval and lazily expire a stale pending request."""
        with self._lock:
            self._cleanup(self._now())
            return self._copy(
                self._expire_if_needed_for_owner(approval_id, owner_id, session_id)
            )

    def approve_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Move exactly one pending request to APPROVED."""
        with self._lock:
            self._cleanup(self._now())
            request = self._expire_if_needed_for_owner(
                approval_id, owner_id, session_id
            )
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError(
                    "Approval request is not pending."
                )
            approved = self._with_status(request, ApprovalStatus.APPROVED, None)
            record = self._owned_record(approval_id, owner_id, session_id)
            self._db.save_approval(
                approved, record.owner_id, record.session_id, record.digest
            )
            return self._copy(approved)

    def seed_stored_request(
        self,
        request: ApprovalRequest,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> None:
        """Persist an operator-provided record, replacing the payload.

        Simulation hook (tests only). The identity and the creation-time
        integrity digest are left untouched, exactly like a tampering or
        policy-drift event would leave them: the record's payload
        changes while the stored evidence no longer matches it.
        """
        record = self._db.get_approval_record(request.approval_id)
        if record is None:
            raise ApprovalNotFoundError("Approval request was not found")
        self._db.save_approval(request, record.owner_id, record.session_id, record.digest)

    def arguments_match_original(
        self,
        approval_id: str,
        arguments: dict[str, str],
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> bool:
        """Check stored arguments against the creation-time integrity digest."""
        with self._lock:
            self._cleanup(self._now())
            self._expire_if_needed_for_owner(approval_id, owner_id, session_id)
            record = self._owned_record(approval_id, owner_id, session_id)
            expected = record.digest
            actual = self._arguments_digest(arguments)
            return expected is not None and secrets.compare_digest(
                expected, actual
            )

    def reject_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Move exactly one pending request to REJECTED."""
        with self._lock:
            self._cleanup(self._now())
            record = self._owned_record(approval_id, owner_id, session_id)
            request = self._expire_if_needed_for_owner(
                approval_id, owner_id, session_id
            )
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError("Approval request is not pending.")
            rejected = self._with_status(
                request, ApprovalStatus.REJECTED, self._now()
            )
            self._db.save_approval(
                rejected, record.owner_id, record.session_id, record.digest
            )
            return self._copy(rejected)

    def expire_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Explicitly expire a pending request, primarily for lifecycle control."""
        with self._lock:
            self._cleanup(self._now())
            record = self._owned_record(approval_id, owner_id, session_id)
            request = self._expire_if_needed_for_owner(
                approval_id, owner_id, session_id
            )
            if request.status is ApprovalStatus.EXPIRED:
                return self._copy(request)
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError("Approval request is not pending.")
            expired = self._with_status(
                request, ApprovalStatus.EXPIRED, self._now()
            )
            self._db.save_approval(
                expired, record.owner_id, record.session_id, record.digest
            )
            return self._copy(expired)

    def claim_approved_for_execution(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Atomically consume APPROVED and prevent all future replays."""
        with self._lock:
            self._cleanup(self._now())
            record = self._owned_record(approval_id, owner_id, session_id)
            request = self._expire_if_needed_for_owner(
                approval_id, owner_id, session_id
            )
            if request.status is not ApprovalStatus.APPROVED:
                raise ApprovalStateError("Approval request is not approved.")
            consumed = self._with_status(
                request, ApprovalStatus.EXECUTED, self._now()
            )
            self._db.save_approval(
                consumed, record.owner_id, record.session_id, record.digest
            )
            return self._copy(consumed)


approval_store = ApprovalStore(state_db=shared_state_db)
