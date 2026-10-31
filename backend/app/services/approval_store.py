"""Thread-safe in-memory approval store with TTL and single-use claims."""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Optional
from uuid import uuid4

from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.policy import RiskLevel


DEFAULT_APPROVAL_TTL_SECONDS = 5 * 60
DEFAULT_MAX_APPROVAL_ENTRIES = 1024
DEFAULT_TERMINAL_RETENTION_SECONDS = 60
DEFAULT_OWNER_ID = "internal-owner"
DEFAULT_SESSION_ID = "internal-session"


class ApprovalNotFoundError(LookupError):
    """Raised when an approval identifier is unknown."""


class ApprovalStateError(ValueError):
    """Raised when a lifecycle transition is not allowed."""


class ApprovalCapacityError(RuntimeError):
    """Raised when bounded in-memory approval capacity is exhausted."""


class ApprovalStore:
    """Replaceable storage boundary for exact, short-lived approvals."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
        max_entries: int = DEFAULT_MAX_APPROVAL_ENTRIES,
        terminal_retention_seconds: int = DEFAULT_TERMINAL_RETENTION_SECONDS,
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
        self._requests: dict[str, ApprovalRequest] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._argument_digests: dict[str, str] = {}
        self._lock = RLock()

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

    def _cleanup(self, now: datetime) -> None:
        """Expire stale requests and remove old terminal records."""
        for approval_id, request in list(self._requests.items()):
            if (
                request.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                and now >= request.expires_at
            ):
                request = self._with_status(request, ApprovalStatus.EXPIRED, now)
                self._requests[approval_id] = request

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
                self._requests.pop(approval_id, None)
                self._owners.pop(approval_id, None)
                self._argument_digests.pop(approval_id, None)

    @staticmethod
    def _arguments_digest(arguments: dict[str, str]) -> str:
        """Create a canonical integrity digest for the approved arguments."""
        canonical = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _owned_request(
        self, approval_id: str, owner_id: str, session_id: str
    ) -> ApprovalRequest:
        request = self._requests.get(approval_id)
        identity = self._owners.get(approval_id)
        if request is None or identity != (owner_id, session_id):
            raise ApprovalNotFoundError("Approval request was not found")
        return request

    def _expire_if_needed(self, approval_id: str) -> ApprovalRequest:
        request = self._requests.get(approval_id)
        if request is None:
            raise ApprovalNotFoundError("Approval request was not found")
        if (
            request.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
            and self._now() >= request.expires_at
        ):
            request = self._with_status(
                request, ApprovalStatus.EXPIRED, self._now()
            )
            self._requests[approval_id] = request
        return request

    def _expire_if_needed_for_owner(
        self, approval_id: str, owner_id: str, session_id: str
    ) -> ApprovalRequest:
        """Authorize access before applying lazy expiration."""
        self._owned_request(approval_id, owner_id, session_id)
        return self._expire_if_needed(approval_id)

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
            if len(self._requests) >= self._max_entries:
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
            self._requests[request.approval_id] = request
            self._owners[request.approval_id] = (owner_id, session_id)
            self._argument_digests[request.approval_id] = self._arguments_digest(
                request.arguments
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
            request = self._with_status(request, ApprovalStatus.APPROVED, None)
            self._requests[approval_id] = request
            return self._copy(request)

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
            expected = self._argument_digests.get(approval_id)
            actual = self._arguments_digest(arguments)
            return expected is not None and secrets.compare_digest(expected, actual)

    def reject_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Move exactly one pending request to REJECTED."""
        with self._lock:
            self._cleanup(self._now())
            request = self._expire_if_needed_for_owner(
                approval_id, owner_id, session_id
            )
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError("Approval request is not pending.")
            request = self._with_status(
                request, ApprovalStatus.REJECTED, self._now()
            )
            self._requests[approval_id] = request
            return self._copy(request)

    def expire_request(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Explicitly expire a pending request, primarily for lifecycle control."""
        with self._lock:
            self._cleanup(self._now())
            request = self._expire_if_needed_for_owner(
                approval_id, owner_id, session_id
            )
            if request.status is ApprovalStatus.EXPIRED:
                return self._copy(request)
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError("Approval request is not pending.")
            request = self._with_status(request, ApprovalStatus.EXPIRED, self._now())
            self._requests[approval_id] = request
            return self._copy(request)

    def claim_approved_for_execution(
        self,
        approval_id: str,
        owner_id: str = DEFAULT_OWNER_ID,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> ApprovalRequest:
        """Atomically consume APPROVED and prevent all future replays."""
        with self._lock:
            self._cleanup(self._now())
            request = self._expire_if_needed_for_owner(
                approval_id, owner_id, session_id
            )
            if request.status is not ApprovalStatus.APPROVED:
                raise ApprovalStateError("Approval request is not approved.")
            request = self._with_status(request, ApprovalStatus.EXECUTED, self._now())
            self._requests[approval_id] = request
            return self._copy(request)


approval_store = ApprovalStore()
