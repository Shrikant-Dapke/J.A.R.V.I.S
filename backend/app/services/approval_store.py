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
- Phase 5.2-b adds ``list_approvals_page``: a strictly read-only,
  identity-scoped inventory of persisted approval summaries. It skips
  cleanup and lazy expiration on purpose (the listing never mutates
  state) and never surfaces raw arguments, digests, or retention flags.
- Phase 5.3 hardens the maintenance scan itself: expiry/retention
  decisions read only persisted metadata columns, and each row is
  handled locally — one record with invalid metadata or a damaged
  payload is skipped and logged instead of poisoning the cleanup of
  unrelated records. Healthy TTL and retention semantics, single-use
  claims, and lifecycle transitions are unchanged.

Secret hygiene: arguments are validated at the schema layer
(secret-like keys and oversized values are rejected before this store
ever sees them), and the persisted arguments are the integrity-verified
exact values the user approved. Audit rows carry identifiers and
classifications only.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Optional
from uuid import uuid4

from app.core.logging import get_logger
from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.policy import RiskLevel
from app.services.execution_db import (
    ApprovalMetadataRecord,
    ApprovalRecord,
    ExecutionIntegrityError,
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


class ApprovalCorruptError(RuntimeError):
    """Raised when a stored approval payload cannot be validated.

    Same fail-closed doctrine as ``PlanCorruptError``: damaged state is
    never served, repaired, or silently skipped; the message is a
    fixed, non-sensitive string (identifiers go to logs only).
    """


@dataclass(frozen=True)
class ApprovalSummaryView:
    """One identity-authorized approval summary with NO raw arguments.

    Every field is extracted from the strictly validated payload; the
    arguments are structurally absent because the full request object
    is never carried here.
    """

    approval_id: str
    tool_name: str
    action_description: str
    risk_level: RiskLevel
    status: ApprovalStatus
    created_at: str
    expires_at: str
    resolved_at: Optional[str]


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

        Phase 5.3: expiry and retention decisions come purely from the
        persisted metadata columns, and every row is handled locally —
        one record with invalid metadata or a damaged payload is skipped
        (logged, lifecycle untouched) instead of aborting the scan, so
        healthy approvals of unrelated identities keep working.
        """
        for row in self._db.list_approval_metadata(include_purged=False):
            self._cleanup_row(row, now)

    def _cleanup_row(
        self, row: ApprovalMetadataRecord, now: datetime
    ) -> None:
        """Apply the cleanup decisions for one row, locally.

        The row's payload is read exactly once, and only to rewrite a
        record that expires right now. Rows whose metadata cannot be
        parsed keep their lifecycle untouched: no invented timestamps,
        no invented transitions, and no purging — a damaged record is
        left exactly as persisted.
        """
        try:
            status = ApprovalStatus(row.status)
        except ValueError:
            self._log_unusable_row(
                row.approval_id, "invalid_persisted_metadata"
            )
            return
        if status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
            expires_at = self._metadata_utc(row.expires_at)
            if expires_at is None:
                self._log_unusable_row(
                    row.approval_id, "invalid_persisted_metadata"
                )
                return
            if now < expires_at:
                return
            self._expire_row_locally(row, now, expires_at)
            return
        if status not in {
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.EXECUTED,
        }:
            return
        resolved_at = (
            self._metadata_utc(row.resolved_at)
            if row.resolved_at is not None
            else None
        )
        if resolved_at is None:
            self._log_unusable_row(
                row.approval_id, "invalid_persisted_metadata"
            )
            return
        # Retention purging is metadata-only, so a damaged payload can
        # never block a terminal record from leaving capacity. The
        # purge targets the exact persisted row (column identity plus
        # its owner/session columns), never a payload-controlled id.
        if now >= resolved_at + self._terminal_retention:
            try:
                self._db.mark_approval_purged(
                    row.approval_id, row.owner_id, row.session_id
                )
            except ExecutionIntegrityError:
                self._log_unusable_row(
                    row.approval_id, "record_integrity_mismatch"
                )

    def _expire_row_locally(
        self, row: ApprovalMetadataRecord, now: datetime, expires_at: datetime
    ) -> None:
        """Rewrite one expiring record; a damaged payload is skipped.

        Fail-closed corruption policy: the record is left exactly as
        persisted (still PENDING/APPROVED on disk), so nothing is
        silently expired, executed, or reconciled by the scan.
        """
        try:
            record = self._db.get_approval_record(row.approval_id)
        except (TypeError, ValueError) as exc:
            # ValidationError and JSONDecodeError are ValueError
            # subclasses; identifiers and internals stay out of logs.
            self._log_unusable_row(row.approval_id, type(exc).__name__)
            return
        if record is None or record.purged:
            return
        # The payload still has to agree with the metadata that raised
        # the expiry decision, otherwise the rewrite could contradict
        # the persisted state. Phase 5.4: the payload's own identity
        # and every duplicated lifecycle field must agree too — a
        # corrupted payload can never steer this write at another row
        # or smuggle in forged lifecycle data.
        if (
            record.request.status
            not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
            or record.request.status.value != row.status
            or record.request.expires_at != expires_at
            or record.request.approval_id != row.approval_id
            or record.request.created_at
            != self._metadata_utc(row.created_at)
            or record.request.resolved_at
            != (
                self._metadata_utc(row.resolved_at)
                if row.resolved_at is not None
                else None
            )
        ):
            self._log_unusable_row(
                row.approval_id, "invalid_persisted_metadata"
            )
            return
        expired = self._with_status(
            record.request, ApprovalStatus.EXPIRED, now
        )
        try:
            self._db.save_approval(
                expired,
                row.approval_id,
                record.owner_id,
                record.session_id,
                record.digest,
            )
        except ExecutionIntegrityError:
            # Row-local policy: the corrupted record is skipped with
            # its lifecycle untouched; the retention scan continues.
            self._log_unusable_row(
                row.approval_id, "record_integrity_mismatch"
            )
            return
        self._db.append_event(
            EVENT_APPROVAL_EXPIRED,
            detail={"approval_id": row.approval_id, "reason": "ttl"},
        )

    @staticmethod
    def _metadata_utc(value: str) -> Optional[datetime]:
        """Parse one persisted UTC timestamp column; None when invalid."""
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _log_unusable_row(approval_id: str, error_type: str) -> None:
        """Record a row-local cleanup skip; no corruption details."""
        logger.warning(
            "Approval cleanup skipped an unusable record locally",
            extra={
                "extra_fields": {
                    "error_type": error_type,
                    "record": "approval",
                    "approval_id": approval_id,
                }
            },
        )

    def _validate_record_agreement(
        self,
        request: ApprovalRequest,
        record: Any,
    ) -> None:
        """Fail closed unless a parsed payload agrees with its row.

        The persisted columns are the authoritative source: record
        identity (``approval_id``) and duplicated lifecycle metadata
        (``status``/``created_at``/``expires_at``/``resolved_at``) in
        the payload must match them exactly. Owner and session are
        column-only by schema (``ApprovalRequest`` forbids extra
        fields), so a payload cannot express a foreign identity at all.
        Mismatches mean the record is corrupt: no repair, no serving,
        no further use (Phase 5.4).
        """
        resolved = (
            self._metadata_utc(record.resolved_at)
            if record.resolved_at is not None
            else None
        )
        if (
            request.approval_id != record.approval_id
            or request.status.value != record.status
            or request.created_at != self._metadata_utc(record.created_at)
            or request.expires_at != self._metadata_utc(record.expires_at)
            or request.resolved_at != resolved
        ):
            self._log_corrupt_record(
                record.approval_id,
                "record_integrity_mismatch",
            )
            raise ApprovalCorruptError(
                "Approval state could not be validated."
            )

    @staticmethod
    def _log_corrupt_record(approval_id: str, error_type: str) -> None:
        """Record a fail-closed corruption rejection; no corruption details."""
        logger.warning(
            "Corrupted approval state rejected fail-closed",
            extra={
                "extra_fields": {
                    "error_type": error_type,
                    "record": "approval",
                    "approval_id": approval_id,
                }
            },
        )

    def _persist_transition(
        self,
        request: ApprovalRequest,
        record: Any,
    ) -> None:
        """Save one lifecycle transition for a previously validated row.

        ``record`` supplies the trusted database row identity and the
        write context (row id, owner, session, creation-time digest) —
        never the payload being written. A facade-level integrity
        rejection is translated into the fixed corruption failure so
        damaged state always fails closed with the same envelope.
        """
        try:
            self._db.save_approval(
                request,
                record.approval_id,
                record.owner_id,
                record.session_id,
                record.digest,
            )
        except ExecutionIntegrityError as exc:
            self._log_corrupt_record(record.approval_id, type(exc).__name__)
            raise ApprovalCorruptError(
                "Approval state could not be validated."
            ) from exc

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
                self._persist_transition(expired, record)
                self._db.append_event(
                    EVENT_APPROVAL_EXPIRED,
                    detail={"approval_id": approval_id, "reason": "ttl"},
                )
                request = expired
            return request

    def _owned_record(
        self, approval_id: str, owner_id: str, session_id: str
    ) -> ApprovalRecord:
        """Authorize access, then parse strictly (Phase 5.2-c).

        The identity and purge gate runs on the raw row BEFORE any
        payload parse: unknown, purged, and foreign approvals always
        observe not-found — a corrupt foreign row never reveals
        itself. Only the owning identity's corrupt payload then fails
        closed with ``ApprovalCorruptError``.
        """
        row = self._db.get_approval_row(approval_id)
        if (
            row is None
            or row.purged
            or row.owner_id != owner_id
            or row.session_id != session_id
        ):
            raise ApprovalNotFoundError("Approval request was not found")
        try:
            payload = json.loads(row.payload)
            if not isinstance(payload, dict):
                raise TypeError("approval payload is not a JSON object")
            request = ApprovalRequest.model_validate(payload)
        except (TypeError, ValueError) as exc:
            # ValidationError and JSONDecodeError are ValueError
            # subclasses; identifiers and internals stay out of logs.
            logger.warning(
                "Corrupted approval payload rejected on owned read",
                extra={
                    "extra_fields": {
                        "error_type": type(exc).__name__,
                        "record": "approval",
                        "approval_id": approval_id,
                    }
                },
            )
            raise ApprovalCorruptError(
                "Approval state could not be validated."
            ) from exc
        # The payload must agree with the authoritative columns before
        # the record may be used at all: a flipped payload identity or
        # forged lifecycle metadata is corruption, not stale data.
        self._validate_record_agreement(request, row)
        return ApprovalRecord(
            approval_id=row.approval_id,
            owner_id=row.owner_id,
            session_id=row.session_id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            resolved_at=row.resolved_at,
            digest=row.digest,
            purged=row.purged,
            request=request,
        )

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
            self._db.insert_approval(
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

    def list_approvals_page(
        self,
        owner_id: str,
        session_id: str,
        *,
        limit: int,
        offset: int = 0,
    ) -> tuple[list[ApprovalSummaryView], int]:
        """Identity-scoped approval inventory: (page, total) in one snapshot.

        STRICTLY READ-ONLY (Phase 5.2-b): no cleanup, no lazy expiration,
        no claim, no lifecycle transition, no audit event. Rows come from
        the façade's single ``BEGIN IMMEDIATE`` transaction (page +
        total, deterministic ``created_at DESC, approval_id DESC``
        ordering, purged rows excluded).

        Every payload is validated strictly; a corrupted approval raises
        ``ApprovalCorruptError`` with a fixed non-sensitive message
        instead of serving, repairing, or silently skipping the row.
        Summaries are built from the validated payload with the raw
        arguments structurally dropped — the digest and retention flags
        never leave the storage layer.
        """
        records, total = self._db.list_approval_records_page(
            owner_id, session_id, limit=limit, offset=offset
        )
        views = [self._validated_approval_summary(record) for record in records]
        return views, total

    def _validated_approval_summary(self, record) -> ApprovalSummaryView:
        """Validate one stored approval payload strictly, or fail closed."""
        try:
            payload = json.loads(record.payload)
            if not isinstance(payload, dict):
                raise TypeError("approval payload is not a JSON object")
            request = ApprovalRequest.model_validate(payload)
        except (TypeError, ValueError) as exc:
            # ValidationError and JSONDecodeError are ValueError
            # subclasses; identifiers and internals stay out of logs.
            logger.warning(
                "Corrupted approval payload rejected on inventory read",
                extra={
                    "extra_fields": {
                        "error_type": type(exc).__name__,
                        "record": "approval",
                        "approval_id": record.approval_id,
                    }
                },
            )
            raise ApprovalCorruptError(
                "Approval state could not be validated."
            ) from exc
        self._validate_record_agreement(request, record)
        return ApprovalSummaryView(
            approval_id=record.approval_id,
            tool_name=request.tool_name,
            action_description=request.action_description,
            risk_level=request.risk_level,
            status=request.status,
            created_at=request.created_at.isoformat(),
            expires_at=request.expires_at.isoformat(),
            resolved_at=(
                request.resolved_at.isoformat()
                if request.resolved_at is not None
                else None
            ),
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
            self._persist_transition(approved, record)
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
        row = self._db.get_approval_row(request.approval_id)
        if row is None or row.purged:
            raise ApprovalNotFoundError("Approval request was not found")
        # Even this operator hook must fail closed before modifying a
        # record: duplicated lifecycle metadata is validated against the
        # persisted columns, and this method remains a tests-only
        # simulation hook — never a production write path.
        self._validate_record_agreement(request, row)
        self._persist_transition(request, row)

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
            self._persist_transition(rejected, record)
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
            self._persist_transition(expired, record)
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
            self._persist_transition(consumed, record)
            return self._copy(consumed)


approval_store = ApprovalStore(state_db=shared_state_db)
