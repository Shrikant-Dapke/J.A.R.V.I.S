"""Thread-safe in-memory approval store with TTL and single-use claims."""

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Optional
from uuid import uuid4

from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.policy import RiskLevel


DEFAULT_APPROVAL_TTL_SECONDS = 5 * 60


class ApprovalNotFoundError(LookupError):
    """Raised when an approval identifier is unknown."""


class ApprovalStateError(ValueError):
    """Raised when a lifecycle transition is not allowed."""


class ApprovalStore:
    """Replaceable storage boundary for exact, short-lived approvals."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._requests: dict[str, ApprovalRequest] = {}
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

    def _expire_if_needed(self, approval_id: str) -> ApprovalRequest:
        request = self._requests.get(approval_id)
        if request is None:
            raise ApprovalNotFoundError(
                f"Approval request '{approval_id}' was not found"
            )
        if (
            request.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
            and self._now() >= request.expires_at
        ):
            request = self._with_status(
                request, ApprovalStatus.EXPIRED, self._now()
            )
            self._requests[approval_id] = request
        return request

    def create_approval_request(
        self,
        tool_name: str,
        arguments: dict[str, str],
        action_description: str,
        risk_level: RiskLevel,
    ) -> ApprovalRequest:
        """Create a pending approval bound to validated arguments."""
        created_at = self._now()
        request = ApprovalRequest(
            approval_id=str(uuid4()),
            tool_name=tool_name,
            arguments=dict(arguments),
            action_description=action_description,
            risk_level=risk_level,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=self._ttl_seconds),
        )
        with self._lock:
            self._requests[request.approval_id] = request
            return self._copy(request)

    def get_approval_request(self, approval_id: str) -> ApprovalRequest:
        """Retrieve an approval and lazily expire a stale pending request."""
        with self._lock:
            return self._copy(self._expire_if_needed(approval_id))

    def approve_request(self, approval_id: str) -> ApprovalRequest:
        """Move exactly one pending request to APPROVED."""
        with self._lock:
            request = self._expire_if_needed(approval_id)
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError(
                    f"Approval request is already {request.status.value}"
                )
            request = self._with_status(request, ApprovalStatus.APPROVED, None)
            self._requests[approval_id] = request
            return self._copy(request)

    def reject_request(self, approval_id: str) -> ApprovalRequest:
        """Move exactly one pending request to REJECTED."""
        with self._lock:
            request = self._expire_if_needed(approval_id)
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError(
                    f"Approval request is already {request.status.value}"
                )
            request = self._with_status(
                request, ApprovalStatus.REJECTED, self._now()
            )
            self._requests[approval_id] = request
            return self._copy(request)

    def expire_request(self, approval_id: str) -> ApprovalRequest:
        """Explicitly expire a pending request, primarily for lifecycle control."""
        with self._lock:
            request = self._expire_if_needed(approval_id)
            if request.status is ApprovalStatus.EXPIRED:
                return self._copy(request)
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalStateError(
                    f"Approval request is already {request.status.value}"
                )
            request = self._with_status(request, ApprovalStatus.EXPIRED, self._now())
            self._requests[approval_id] = request
            return self._copy(request)

    def claim_approved_for_execution(self, approval_id: str) -> ApprovalRequest:
        """Atomically consume APPROVED and prevent all future replays."""
        with self._lock:
            request = self._expire_if_needed(approval_id)
            if request.status is not ApprovalStatus.APPROVED:
                raise ApprovalStateError(
                    f"Approval request is not approved; current state is {request.status.value}"
                )
            request = self._with_status(request, ApprovalStatus.EXECUTED, self._now())
            self._requests[approval_id] = request
            return self._copy(request)


approval_store = ApprovalStore()
