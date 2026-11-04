"""Bounded SQLite persistence façade for execution lifecycle state.

Phase 4.3 introduced the single local-first storage boundary for plans,
plan-step states, approval records and outcomes, execution attempts,
and audit events; Phase 5.1 extended the same boundary with persistent
conversation history. One file, one connection, no network services.

Properties and invariants:

- stdlib ``sqlite3`` only (no new dependencies). WAL journal mode for
  file databases, ``BEGIN IMMEDIATE`` transactions, ``PRAGMA
  user_version``-driven migrations created by this module.
- Conversations are owner/session-scoped like every other row here.
  Message ordering is a per-conversation monotone ``seq``, so turn
  chronology never depends on timestamp resolution; stored turn
  timestamps are preserved verbatim. Retention is FIFO-bounded per
  conversation, and turn content is bounded by database CHECK
  constraints (fail-closed writes, no half-written turns possible).
- One ``RLock`` serializes every operation for the single shared
  connection (``check_same_thread=False``). The lock is the innermost
  lock in the service layer: no façade method ever calls back into
  services, so no lock-order inversion is possible.
- Owner/session isolation is enforced here: plan rows carry their
  identity, and rebinding a plan to a different identity is rejected.
- The partial unique index on open attempts per approval makes
  duplicate authorization consumption impossible to persist, even
  across restarts (backend-enforced idempotency, not just in-memory
  locks).
- Secret hygiene: approval argument schemas already reject secret-like
  keys and bound values; audit events store classifications and
  identifiers only — never tokens, key material, sensitive argument
  values, or session identifiers.
- A constructor without an explicit path opens an in-memory database so
  each test object graph stays isolated by construction; production
  singletons wire the shared file-backed façade (see
  ``resolve_state_db_path``).
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.approval import ApprovalRequest
from app.schemas.plan import PlanStepStatus, TaskPlan
from app.schemas.tool import ToolResult
from app.services.execution_lifecycle import (
    OPEN_ATTEMPT_STATUSES,
    TERMINAL_ATTEMPT_STATUSES,
    AttemptStatus,
    ExecutionLifecycleError,
    validate_attempt_transition,
)


logger = get_logger(__name__)


SCHEMA_VERSION = 2
DEFAULT_EVENT_RETENTION = 10000
EVENT_PRUNE_INTERVAL = 1024

# Phase 5.1 conversation bounds (storage-side, fail-closed).
# MAX_TURN_CONTENT_LENGTH mirrors the chat request / orchestrator reply
# bounds so every stored turn can always be replayed to a provider.
MAX_CONVERSATION_TURNS = 400
MAX_TURN_CONTENT_LENGTH = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id     TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_plans_identity
    ON plans (owner_id, session_id);

CREATE TABLE IF NOT EXISTS plan_steps (
    plan_id     TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    approval_id TEXT,
    status      TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    PRIMARY KEY (plan_id, step_id)
);
CREATE INDEX IF NOT EXISTS ix_plan_steps_approval
    ON plan_steps (approval_id);
CREATE INDEX IF NOT EXISTS ix_plan_steps_status
    ON plan_steps (status);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    resolved_at TEXT,
    payload     TEXT NOT NULL,
    digest      TEXT NOT NULL,
    purged      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_approvals_identity
    ON approval_requests (owner_id, session_id);

CREATE TABLE IF NOT EXISTS execution_attempts (
    attempt_id  TEXT PRIMARY KEY,
    approval_id TEXT,
    plan_id     TEXT,
    step_id     TEXT,
    owner_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    mutating    INTEGER NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    outcome_json TEXT,
    error_code  TEXT
);
-- Backend-enforced idempotency: an approval may hold at most ONE open
-- attempt (claimed or started) at any time, and this holds across
-- restarts because it is enforced by the database itself.
CREATE UNIQUE INDEX IF NOT EXISTS ux_attempts_open_per_approval
    ON execution_attempts (approval_id)
    WHERE status IN ('CLAIMED', 'STARTED') AND approval_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_attempts_status
    ON execution_attempts (status);
CREATE INDEX IF NOT EXISTS ix_attempts_approval
    ON execution_attempts (approval_id);

CREATE TABLE IF NOT EXISTS execution_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    correlation_id TEXT,
    execution_id   TEXT,
    event          TEXT NOT NULL,
    detail_json    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_execution
    ON execution_events (execution_id);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    turn_count      INTEGER NOT NULL DEFAULT 0
);
-- Covers identity-scoped listing (most recent first, deterministic
-- tiebreak) and capacity counting with one index.
CREATE INDEX IF NOT EXISTS ix_conversations_identity
    ON conversations (owner_id, session_id, updated_at, conversation_id);

CREATE TABLE IF NOT EXISTS conversation_messages (
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL CHECK (
        length(content) >= 1 AND length(content) <= {content_bound}
    ),
    created_at      TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
""".format(content_bound=MAX_TURN_CONTENT_LENGTH)

_SUCCEEDED = AttemptStatus.SUCCEEDED.value
_FAILED = AttemptStatus.FAILED.value


class ExecutionStateError(RuntimeError):
    """Raised when persisted execution state cannot be used safely."""


class ExecutionConflictError(ExecutionStateError):
    """Raised when a uniqueness constraint forbids a duplicate write."""


class ExecutionIntegrityError(ExecutionStateError):
    """Raised when a write would not match the persisted row identity.

    The database row's ``approval_id`` (and its owner/session columns)
    are the authoritative record identity: every approval update must
    target that trusted row identity explicitly. A payload-supplied
    identifier can therefore never steer an update to a foreign row.
    """


@dataclass(frozen=True)
class PlanRecord:
    """A stored plan plus its owning identity."""

    plan_id: str
    owner_id: str
    session_id: str
    plan: TaskPlan


@dataclass(frozen=True)
class PlanRowRecord:
    """A stored plan row with its payload UNPARSED.

    Strict payload validation happens above the façade so the identity
    gate can run first: unknown and foreign rows observe not-found even
    when damaged (Phase 5.2-c).
    """

    plan_id: str
    owner_id: str
    session_id: str
    payload: str


@dataclass(frozen=True)
class PlanSummaryRecord:
    """A raw plan row for inventory listings (payload NOT parsed).

    Carrying the payload verbatim keeps parsing/validation at the
    store layer, where a damaged plan can fail closed instead of being
    silently skipped (Phase 5.2-a decision).
    """

    plan_id: str
    owner_id: str
    session_id: str
    created_at: str
    updated_at: str
    payload: str


@dataclass(frozen=True)
class ApprovalRecord:
    """A stored approval plus columns the store needs beyond the schema."""

    approval_id: str
    owner_id: str
    session_id: str
    created_at: str
    expires_at: str
    resolved_at: Optional[str]
    digest: str
    purged: bool
    request: ApprovalRequest


@dataclass(frozen=True)
class ApprovalRowRecord:
    """A stored approval row with its payload UNPARSED.

    Raw access for identity-gated reads (Phase 5.2-c): the ownership
    and purge gate runs before any payload parse, so unknown and
    foreign rows observe not-found even when damaged.
    """

    approval_id: str
    owner_id: str
    session_id: str
    status: str
    created_at: str
    expires_at: str
    resolved_at: Optional[str]
    digest: str
    purged: bool
    payload: str


@dataclass(frozen=True)
class ApprovalMetadataRecord:
    """Approval maintenance-scan row: metadata columns ONLY.

    The cleanup scan decides expiry and retention purely from persisted
    columns (Phase 5.3), so it never parses — or even selects — the
    payload of another identity's record. One damaged payload therefore
    cannot poison cleanup-triggered operations for unrelated records.
    """

    approval_id: str
    owner_id: str
    session_id: str
    status: str
    created_at: str
    expires_at: str
    resolved_at: Optional[str]
    digest: str
    purged: bool


@dataclass(frozen=True)
class ApprovalSummaryRecord:
    """One approval row for inventory listings (payload NOT parsed).

    Carrying the payload verbatim keeps parsing/validation at the store
    layer, where damaged approval state fails closed instead of being
    silently served or skipped (Phase 5.2-b decision).
    """

    approval_id: str
    owner_id: str
    session_id: str
    status: str
    created_at: str
    expires_at: str
    resolved_at: Optional[str]
    payload: str


@dataclass(frozen=True)
class AttemptRecord:
    """One persisted execution attempt."""

    attempt_id: str
    approval_id: Optional[str]
    plan_id: Optional[str]
    step_id: Optional[str]
    owner_id: str
    session_id: str
    tool_name: str
    mutating: bool
    status: AttemptStatus
    started_at: str
    outcome: Optional[ToolResult]
    error_code: Optional[str]


@dataclass(frozen=True)
class OutcomeBinding:
    """A terminal outcome looking for its (still unresolved) plan step."""

    plan_id: str
    step_id: str
    owner_id: str
    session_id: str
    attempt_id: str
    approval_id: str
    outcome: ToolResult


@dataclass(frozen=True)
class ConversationRecord:
    """A stored conversation plus its owning identity and metadata.

    ``owner_id`` / ``session_id`` carry the conversation's identity so
    callers can enforce isolation; the façade itself never filters by
    them on single-conversation reads (same contract as
    ``get_plan_record`` / ``get_approval_record``).
    """

    conversation_id: str
    owner_id: str
    session_id: str
    created_at: str
    updated_at: str
    turn_count: int


@dataclass(frozen=True)
class ConversationMessageRecord:
    """One persisted conversation turn.

    ``seq`` is the per-conversation monotone index and is the
    authoritative ordering key; ``created_at`` is the verbatim stored
    turn timestamp.
    """

    conversation_id: str
    seq: int
    role: str
    content: str
    created_at: str


def _utc_now_iso() -> str:
    """Return a timezone-aware UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def resolve_state_db_path() -> str:
    """Resolve the shared file-backed state database path.

    ``JARVIS_STATE_DB_PATH`` (or ``settings.state_db_path``) wins;
    otherwise the default ``backend/data/jarvis_state.db`` next to the
    package root is used.
    """
    configured = settings.state_db_path
    if configured and configured.strip():
        return configured.strip()
    return str(
        Path(__file__).resolve().parents[2] / "data" / "jarvis_state.db"
    )


class ExecutionStateDb:
    """Transactional storage boundary for execution lifecycle state."""

    def __init__(self, path: Optional[str] = None) -> None:
        # None -> in-memory database (test isolation by construction).
        self._path = path
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect_and_migrate()

    # -- connection ------------------------------------------------------

    @property
    def path(self) -> Optional[str]:
        """Backing file path, or None for in-memory databases."""
        return self._path

    def _connect_and_migrate(self) -> None:
        target = self._path if self._path else ":memory:"
        if self._path:
            parent = Path(self._path).parent
            parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            target, check_same_thread=False, isolation_level=None
        )
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        if self._path:
            # Durability of committed lifecycle evidence beats speed.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        with self._lock:
            self._conn = conn
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Create or upgrade the schema via ``PRAGMA user_version``.

        A fresh database is created at the current version and an older
        database is migrated in place. Every statement in ``_SCHEMA`` is
        ``CREATE ... IF NOT EXISTS``, so a migration never touches
        existing tables or rows — it only adds the objects a previous
        version is missing (today: the Phase 5.1 conversation tables).
        The version stamp is written last, so an interrupted migration
        simply re-runs idempotently on the next startup.
        """
        row = conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else 0
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise ExecutionStateError(
                "Execution state database schema version "
                f"{version} is newer than this application supports "
                f"({SCHEMA_VERSION})."
            )
        prior = version
        with self._lock:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if prior == 0:
            logger.info(
                "Execution state database schema created",
                extra={
                    "extra_fields": {
                        "schema_version": SCHEMA_VERSION,
                        "backed_by_file": bool(self._path),
                    }
                },
            )
        else:
            logger.info(
                "Execution state database schema migrated",
                extra={
                    "extra_fields": {
                        "prior_version": prior,
                        "schema_version": SCHEMA_VERSION,
                        "backed_by_file": bool(self._path),
                    }
                },
            )

    def close(self) -> None:
        """Close the connection; safe to call repeatedly."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- transactions ----------------------------------------------------

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        """Run a ``BEGIN IMMEDIATE`` transaction, lock held for its body.

        Nesting is supported: an inner ``transaction()`` joins the
        outermost one on the same thread. Exceptions roll back only when
        the outermost transaction is active.
        """
        depth = getattr(self._tls, "depth", 0)
        with self._lock:
            if depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tls.depth = depth + 1
            try:
                yield
            except BaseException:
                self._tls.depth = depth
                if depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._tls.depth = depth
                if depth == 0:
                    self._conn.execute("COMMIT")

    def _fetchone(self, sql: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- plans -----------------------------------------------------------

    def count_plans(self) -> int:
        """Total plans stored (global bounded capacity basis)."""
        row = self._fetchone("SELECT COUNT(*) AS n FROM plans")
        return int(row["n"]) if row else 0

    def save_plan(
        self, plan: TaskPlan, owner_id: str, session_id: str
    ) -> None:
        """Upsert one plan (payload + normalized step index) atomically.

        Ownership is immutable: saving under a different identity than
        the stored row raises rather than silently rebinding the plan.
        """
        payload = json.dumps(plan.model_dump(mode="json"))
        now = _utc_now_iso()
        with self.transaction():
            existing = self._fetchone(
                "SELECT owner_id, session_id FROM plans WHERE plan_id = ?",
                (plan.plan_id,),
            )
            if existing is not None and (
                existing["owner_id"] != owner_id
                or existing["session_id"] != session_id
            ):
                raise ExecutionStateError(
                    "Plan identity mismatch while saving execution state."
                )
            self._conn.execute(
                """
                INSERT INTO plans (
                    plan_id, owner_id, session_id, payload,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    plan.plan_id,
                    owner_id,
                    session_id,
                    payload,
                    now if existing is None else plan.created_at.isoformat(),
                    now,
                ),
            )
            # Rebuild the normalized step index for lookups by approval
            # id and status; the payload remains source of truth.
            self._conn.execute(
                "DELETE FROM plan_steps WHERE plan_id = ?",
                (plan.plan_id,),
            )
            for step in plan.steps:
                self._conn.execute(
                    """
                    INSERT INTO plan_steps (
                        plan_id, step_id, approval_id, status,
                        owner_id, session_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan.plan_id,
                        step.step_id,
                        step.approval_id,
                        step.status.value,
                        owner_id,
                        session_id,
                    ),
                )

    def get_plan_record(self, plan_id: str) -> Optional[PlanRecord]:
        """Return the stored plan plus its identity, or None."""
        row = self._fetchone(
            "SELECT owner_id, session_id, payload FROM plans WHERE plan_id = ?",
            (plan_id,),
        )
        if row is None:
            return None
        plan = TaskPlan.model_validate(json.loads(row["payload"]))
        return PlanRecord(
            plan_id=plan_id,
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            plan=plan,
        )

    def get_plan_row(self, plan_id: str) -> Optional[PlanRowRecord]:
        """Return the stored plan row with its payload UNPARSED, or None.

        Raw access for identity-gated reads (Phase 5.2-c): parsing and
        strict validation stay with the caller so a damaged payload
        fails closed only for the authorized identity.
        """
        row = self._fetchone(
            "SELECT owner_id, session_id, payload FROM plans WHERE plan_id = ?",
            (plan_id,),
        )
        if row is None:
            return None
        return PlanRowRecord(
            plan_id=plan_id,
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            payload=row["payload"],
        )

    def list_plan_ids(
        self, owner_id: str, session_id: str
    ) -> List[str]:
        """Plan ids owned by one identity (ordered by creation)."""
        rows = self._fetchall(
            """
            SELECT plan_id FROM plans
            WHERE owner_id = ? AND session_id = ?
            ORDER BY created_at, plan_id
            """,
            (owner_id, session_id),
        )
        return [row["plan_id"] for row in rows]

    def find_step_ref_by_approval(
        self, approval_id: str, owner_id: str, session_id: str
    ) -> Optional[Tuple[str, str]]:
        """The (plan_id, step_id) bound to an approval for one identity."""
        row = self._fetchone(
            """
            SELECT plan_id, step_id FROM plan_steps
            WHERE approval_id = ?
              AND owner_id = ? AND session_id = ?
            """,
            (approval_id, owner_id, session_id),
        )
        if row is None:
            return None
        return row["plan_id"], row["step_id"]

    def find_steps_with_status(
        self, status: PlanStepStatus
    ) -> List[Tuple[str, str, str, str, Optional[str]]]:
        """Rows in one plan-step status: (plan, step, owner, session, approval)."""
        rows = self._fetchall(
            """
            SELECT plan_id, step_id, owner_id, session_id, approval_id
            FROM plan_steps
            WHERE status = ?
            ORDER BY plan_id, step_id
            """,
            (status.value,),
        )
        return [
            (
                row["plan_id"],
                row["step_id"],
                row["owner_id"],
                row["session_id"],
                row["approval_id"],
            )
            for row in rows
        ]

    def plan_summaries_page(
        self,
        owner_id: str,
        session_id: str,
        *,
        limit: int,
        offset: int = 0,
    ) -> Tuple[List[PlanSummaryRecord], int]:
        """One consistent (page, total) pair for one identity's plans.

        The page query and the matching identity-scoped total run inside
        ONE ``BEGIN IMMEDIATE`` transaction, so a caller can never pair
        rows from one instant with a count from a different instant —
        even while plans are created, re-saved, or (later) reconciled
        concurrently. Ordering is deterministic: ``created_at DESC``
        with ``plan_id DESC`` as the tiebreak, so a page is reproducible
        while rows are unchanged.

        Rows carry the stored payload verbatim; payload parsing and
        strict validation are the caller's responsibility (fail closed,
        never skip) to keep the façade free of business semantics.
        """
        owner = self._require_non_empty(owner_id, "owner_id")
        session = self._require_non_empty(session_id, "session_id")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must not be negative")
        with self.transaction():
            rows = self._fetchall(
                """
                SELECT plan_id, owner_id, session_id,
                       payload, created_at, updated_at
                FROM plans
                WHERE owner_id = ? AND session_id = ?
                ORDER BY created_at DESC, plan_id DESC
                LIMIT ? OFFSET ?
                """,
                (owner, session, limit, offset),
            )
            count_row = self._fetchone(
                """
                SELECT COUNT(*) AS n FROM plans
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner, session),
            )
        page = [
            PlanSummaryRecord(
                plan_id=row["plan_id"],
                owner_id=row["owner_id"],
                session_id=row["session_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                payload=row["payload"],
            )
            for row in rows
        ]
        return page, int(count_row["n"]) if count_row is not None else 0

    # -- approvals ---------------------------------------------------------

    def save_approval(
        self,
        request: ApprovalRequest,
        approval_id: str,
        owner_id: str,
        session_id: str,
        digest: str,
    ) -> None:
        """Rewrite one EXISTING approval row, identity-checked.

        ``approval_id`` is the trusted persisted row identity: the caller
        supplies the record's real database row id, never one derived
        from the payload being written. The row must exist, its
        owner/session columns must match the caller's context, and the
        payload's own ``approval_id`` must agree with the trusted row id
        before any column is touched (Phase 5.4).
        """
        self._write_approval(
            request,
            approval_id,
            owner_id,
            session_id,
            digest,
        )

    def insert_approval(
        self,
        request: ApprovalRequest,
        owner_id: str,
        session_id: str,
        digest: str,
    ) -> None:
        """Create one approval row; never upsert.

        Creation is insert-only and separate from updates: the row id is
        the server-generated identifier embedded in the fresh request,
        and an existing row with the same id is a hard conflict (fail
        closed) instead of an overwrite of another identity's record.
        """
        payload = json.dumps(request.model_dump(mode="json"))
        resolved_at = (
            request.resolved_at.isoformat()
            if request.resolved_at is not None
            else None
        )
        with self.transaction():
            try:
                self._conn.execute(
                    """
                    INSERT INTO approval_requests (
                        approval_id, owner_id, session_id, status,
                        created_at, expires_at, resolved_at, payload,
                        digest, purged
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        request.approval_id,
                        owner_id,
                        session_id,
                        request.status.value,
                        request.created_at.isoformat(),
                        request.expires_at.isoformat(),
                        resolved_at,
                        payload,
                        digest,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ExecutionConflictError(
                    "Approval state could not be stored securely."
                ) from exc

    def _write_approval(
        self,
        request: ApprovalRequest,
        approval_id: str,
        owner_id: str,
        session_id: str,
        digest: str,
    ) -> None:
        """Identity-checked update keyed on the trusted row id.

        All identity validation happens BEFORE the update statement and
        inside one ``BEGIN IMMEDIATE`` transaction, so a failed check
        leaves every row unchanged. The update rewrites only the
        lifecycle/payload columns: identity, creation evidence
        (``created_at``/``expires_at``/``digest``) and the retention
        flag are never rewritten afterwards.
        """
        payload = json.dumps(request.model_dump(mode="json"))
        resolved_at = (
            request.resolved_at.isoformat()
            if request.resolved_at is not None
            else None
        )
        with self.transaction():
            row = self._fetchone(
                """
                SELECT owner_id, session_id, purged FROM approval_requests
                WHERE approval_id = ?
                """,
                (approval_id,),
            )
            if row is None:
                raise ExecutionIntegrityError(
                    "Approval update target does not exist."
                )
            if row["owner_id"] != owner_id or row["session_id"] != session_id:
                raise ExecutionIntegrityError(
                    "Approval identity does not match the persisted record."
                )
            if row["purged"]:
                raise ExecutionIntegrityError(
                    "Approval update target has been retired."
                )
            if request.approval_id != approval_id:
                raise ExecutionIntegrityError(
                    "Approval payload identity does not match the "
                    "persisted record."
                )
            cursor = self._conn.execute(
                """
                UPDATE approval_requests
                SET status = ?,
                    resolved_at = ?,
                    payload = ?
                WHERE approval_id = ?
                """,
                (
                    request.status.value,
                    resolved_at,
                    payload,
                    approval_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionConflictError(
                    "Approval update target changed concurrently."
                )

    def get_approval_record(
        self, approval_id: str
    ) -> Optional[ApprovalRecord]:
        """Return the stored approval row (payload parsed) or None."""
        row = self._fetchone(
            """
            SELECT approval_id, owner_id, session_id, created_at,
                   expires_at, resolved_at, digest, purged, payload
            FROM approval_requests
            WHERE approval_id = ?
            """,
            (approval_id,),
        )
        if row is None:
            return None
        request = ApprovalRequest.model_validate(json.loads(row["payload"]))
        return ApprovalRecord(
            approval_id=row["approval_id"],
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            resolved_at=row["resolved_at"],
            digest=row["digest"],
            purged=bool(row["purged"]),
            request=request,
        )

    def get_approval_row(self, approval_id: str) -> Optional[ApprovalRowRecord]:
        """Return the stored approval row with its payload UNPARSED, or None.

        Raw access for identity-gated reads (Phase 5.2-c): parsing and
        strict validation stay with the caller so a damaged payload
        fails closed only for the authorized identity.
        """
        row = self._fetchone(
            """
            SELECT approval_id, owner_id, session_id, status,
                   created_at, expires_at, resolved_at, digest, purged, payload
            FROM approval_requests
            WHERE approval_id = ?
            """,
            (approval_id,),
        )
        if row is None:
            return None
        return ApprovalRowRecord(
            approval_id=row["approval_id"],
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            resolved_at=row["resolved_at"],
            digest=row["digest"],
            purged=bool(row["purged"]),
            payload=row["payload"],
        )

    def mark_approval_purged(
        self, approval_id: str, owner_id: str, session_id: str
    ) -> bool:
        """Flag one terminal approval as purged (row stays for audit).

        Identity-checked retention write: the purge targets an explicit
        persisted row id whose owner/session columns must match the
        maintenance scan's record, so no payload-controlled identifier
        can ever purge another approval.
        """
        with self._lock:
            row = self._fetchone(
                """
                SELECT owner_id, session_id FROM approval_requests
                WHERE approval_id = ?
                """,
                (approval_id,),
            )
            if row is None:
                return False
            if row["owner_id"] != owner_id or row["session_id"] != session_id:
                raise ExecutionIntegrityError(
                    "Approval identity does not match the persisted record."
                )
            cursor = self._conn.execute(
                "UPDATE approval_requests SET purged = 1 WHERE approval_id = ?",
                (approval_id,),
            )
            return cursor.rowcount > 0

    def get_approval_digest(self, approval_id: str) -> Optional[str]:
        """Integrity digest recorded at creation time."""
        row = self._fetchone(
            "SELECT digest FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
        )
        return row["digest"] if row else None

    def count_active_approvals(self) -> int:
        """Approval rows that still occupy capacity (purged excluded)."""
        row = self._fetchone(
            "SELECT COUNT(*) AS n FROM approval_requests WHERE purged = 0"
        )
        return int(row["n"]) if row else 0

    def list_approval_metadata(
        self, include_purged: bool = False
    ) -> List[ApprovalMetadataRecord]:
        """Maintenance-scan rows built from metadata columns ONLY.

        Mirrors the ordering and purge filtering of
        :meth:`list_approval_records` but never selects the payload, so
        the cleanup scan needs no payload parse to drive its expiry and
        retention decisions (Phase 5.3).
        """
        if include_purged:
            rows = self._fetchall(
                "SELECT approval_id, owner_id, session_id, status, "
                "created_at, expires_at, resolved_at, digest, purged "
                "FROM approval_requests ORDER BY approval_id"
            )
        else:
            rows = self._fetchall(
                "SELECT approval_id, owner_id, session_id, status, "
                "created_at, expires_at, resolved_at, digest, purged "
                "FROM approval_requests WHERE purged = 0 "
                "ORDER BY approval_id"
            )
        return [
            ApprovalMetadataRecord(
                approval_id=row["approval_id"],
                owner_id=row["owner_id"],
                session_id=row["session_id"],
                status=row["status"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
                resolved_at=row["resolved_at"],
                digest=row["digest"],
                purged=bool(row["purged"]),
            )
            for row in rows
        ]

    def list_approval_records(
        self, include_purged: bool = False
    ) -> List[ApprovalRecord]:
        """All stored approval rows with parsed payloads.

        Full-payload reads; maintenance scans use
        :meth:`list_approval_metadata` instead so one damaged row cannot
        interrupt the scan for unrelated records (Phase 5.3).
        """
        if include_purged:
            rows = self._fetchall(
                "SELECT * FROM approval_requests ORDER BY approval_id"
            )
        else:
            rows = self._fetchall(
                "SELECT * FROM approval_requests "
                "WHERE purged = 0 ORDER BY approval_id"
            )
        records: List[ApprovalRecord] = []
        for row in rows:
            records.append(
                ApprovalRecord(
                    approval_id=row["approval_id"],
                    owner_id=row["owner_id"],
                    session_id=row["session_id"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    resolved_at=row["resolved_at"],
                    digest=row["digest"],
                    purged=bool(row["purged"]),
                    request=ApprovalRequest.model_validate(
                        json.loads(row["payload"])
                    ),
                )
            )
        return records

    def list_approval_records_page(
        self,
        owner_id: str,
        session_id: str,
        *,
        limit: int,
        offset: int = 0,
    ) -> Tuple[List[ApprovalSummaryRecord], int]:
        """One consistent (page, total) pair for one identity's approvals.

        Mirrors ``plan_summaries_page`` (Phase 5.2-a): the page query and
        the matching identity-scoped total run inside ONE ``BEGIN
        IMMEDIATE`` transaction, so a caller never pairs rows from one
        instant with a count from another. Ordering is deterministic:
        ``created_at DESC`` with ``approval_id DESC`` as the tiebreak.

        Rows are filtered in SQL (``owner_id``/``session_id``; the
        ``ix_approvals_identity`` index covers both queries) and purged
        rows are excluded: purged rows are retired audit rows that every
        other gated read already treats as unknown (detail => 404).

        Read-only: no cleanup, no lazy expiration, no lifecycle
        transition. Rows carry the stored payload verbatim; parsing and
        strict validation are the caller's responsibility (fail closed,
        never skip), and summary callers must not surface the payload's
        raw arguments or the digest column.
        """
        owner = self._require_non_empty(owner_id, "owner_id")
        session = self._require_non_empty(session_id, "session_id")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must not be negative")
        with self.transaction():
            rows = self._fetchall(
                """
                SELECT approval_id, owner_id, session_id, status,
                       created_at, expires_at, resolved_at, payload
                FROM approval_requests
                WHERE owner_id = ? AND session_id = ? AND purged = 0
                ORDER BY created_at DESC, approval_id DESC
                LIMIT ? OFFSET ?
                """,
                (owner, session, limit, offset),
            )
            count_row = self._fetchone(
                """
                SELECT COUNT(*) AS n FROM approval_requests
                WHERE owner_id = ? AND session_id = ? AND purged = 0
                """,
                (owner, session),
            )
        page = [
            ApprovalSummaryRecord(
                approval_id=row["approval_id"],
                owner_id=row["owner_id"],
                session_id=row["session_id"],
                status=row["status"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
                resolved_at=row["resolved_at"],
                payload=row["payload"],
            )
            for row in rows
        ]
        return page, int(count_row["n"]) if count_row is not None else 0

    # -- attempts ----------------------------------------------------------

    def create_attempt(
        self,
        *,
        attempt_id: str,
        owner_id: str,
        session_id: str,
        tool_name: str,
        mutating: bool,
        status: AttemptStatus,
        approval_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> str:
        """Persist one execution attempt at its first lifecycle state."""
        if status not in OPEN_ATTEMPT_STATUSES:
            raise ExecutionLifecycleError(
                "Attempts must be created in an open state."
            )
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO execution_attempts (
                        attempt_id, approval_id, plan_id, step_id,
                        owner_id, session_id, tool_name, mutating,
                        status, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        approval_id,
                        plan_id,
                        step_id,
                        owner_id,
                        session_id,
                        tool_name,
                        1 if mutating else 0,
                        status.value,
                        _utc_now_iso(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ExecutionConflictError(
                "An open execution attempt already holds this token."
            ) from exc
        return attempt_id

    def get_attempt(self, attempt_id: str) -> Optional[AttemptRecord]:
        row = self._fetchone(
            "SELECT * FROM execution_attempts WHERE attempt_id = ?",
            (attempt_id,),
        )
        return self._attempt_from_row(row) if row else None

    def _attempt_from_row(self, row: sqlite3.Row) -> AttemptRecord:
        outcome_json = row["outcome_json"]
        outcome = (
            ToolResult.model_validate(json.loads(outcome_json))
            if outcome_json
            else None
        )
        return AttemptRecord(
            attempt_id=row["attempt_id"],
            approval_id=row["approval_id"],
            plan_id=row["plan_id"],
            step_id=row["step_id"],
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            tool_name=row["tool_name"],
            mutating=bool(row["mutating"]),
            status=AttemptStatus(row["status"]),
            started_at=row["started_at"],
            outcome=outcome,
            error_code=row["error_code"],
        )

    def transit_attempt(
        self,
        attempt_id: str,
        target: AttemptStatus,
        outcome: Optional[ToolResult] = None,
        error_code: Optional[str] = None,
    ) -> AttemptRecord:
        """Apply one validated attempt transition and return the row."""
        record = self.get_attempt(attempt_id)
        if record is None:
            raise ExecutionStateError("Execution attempt was not found.")
        validate_attempt_transition(record.status, target)
        outcome_json = (
            json.dumps(outcome.model_dump(mode="json"))
            if outcome is not None
            else None
        )
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE execution_attempts
                SET status = ?,
                    outcome_json = COALESCE(?, outcome_json),
                    error_code = COALESCE(?, error_code)
                WHERE attempt_id = ?
                """,
                (
                    target.value,
                    outcome_json,
                    error_code,
                    attempt_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ExecutionStateError(
                    "Execution attempt was not found."
                )
        updated = self.get_attempt(attempt_id)
        assert updated is not None
        return updated

    def get_open_attempts(self) -> List[AttemptRecord]:
        """All CLAIMED / STARTED attempts across identities."""
        params = sorted(status.value for status in OPEN_ATTEMPT_STATUSES)
        placeholders = ", ".join("?" for _ in params)
        rows = self._fetchall(
            "SELECT * FROM execution_attempts "
            f"WHERE status IN ({placeholders}) "
            "ORDER BY started_at, attempt_id",
            tuple(params),
        )
        return [self._attempt_from_row(row) for row in rows]

    def get_latest_attempt(
        self, approval_id: str
    ) -> Optional[AttemptRecord]:
        row = self._fetchone(
            """
            SELECT * FROM execution_attempts
            WHERE approval_id = ?
            ORDER BY started_at DESC, attempt_id DESC
            LIMIT 1
            """,
            (approval_id,),
        )
        return self._attempt_from_row(row) if row else None

    def find_open_attempt_by_approval(
        self, approval_id: str
    ) -> Optional[AttemptRecord]:
        row = self._fetchone(
            """
            SELECT * FROM execution_attempts
            WHERE approval_id = ?
              AND status IN ('CLAIMED', 'STARTED')
            LIMIT 1
            """,
            (approval_id,),
        )
        return self._attempt_from_row(row) if row else None

    def find_unresolved_outcome_bindings(self) -> List[OutcomeBinding]:
        """Terminal, provable outcomes whose plan step is unresolved.

        Joins terminal attempts that recorded a ToolResult with plan
        steps still awaiting approval. TIMED_OUT attempts carry no
        outcome at all (timeout never implies an absent or present side
        effect), so they cannot appear here.
        """
        terminal = ", ".join(
            "?" for _ in sorted({s.value for s in TERMINAL_ATTEMPT_STATUSES})
        )
        sql = (
            "SELECT a.attempt_id, a.approval_id, ps.plan_id, ps.step_id, "
            "ps.owner_id, ps.session_id, a.outcome_json "
            "FROM execution_attempts a "
            "JOIN plan_steps ps "
            "ON ps.approval_id = a.approval_id AND ps.status = ? "
            f"WHERE a.status IN ({terminal}) "
            "AND a.outcome_json IS NOT NULL "
            "ORDER BY a.started_at"
        )
        rows = self._fetchall(
            sql,
            (PlanStepStatus.AWAITING_APPROVAL.value,)
            + tuple(sorted({s.value for s in TERMINAL_ATTEMPT_STATUSES})),
        )
        bindings: List[OutcomeBinding] = []
        for row in rows:
            bindings.append(
                OutcomeBinding(
                    plan_id=row["plan_id"],
                    step_id=row["step_id"],
                    owner_id=row["owner_id"],
                    session_id=row["session_id"],
                    attempt_id=row["attempt_id"],
                    approval_id=row["approval_id"],
                    outcome=ToolResult.model_validate(
                        json.loads(row["outcome_json"])
                    ),
                )
            )
        return bindings

    # -- conversations ----------------------------------------------------

    @staticmethod
    def _require_non_empty(value: str, label: str) -> str:
        """Reject empty identifiers before they reach SQL."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        return value

    @staticmethod
    def _validate_message_timestamp(created_at: Optional[str]) -> str:
        """Accept only parseable ISO-8601 turn timestamps.

        Rejecting garbage at the write boundary means a stored turn can
        always be read back later: nothing is persisted that readers
        would have to discard (fail-closed, not fail-silent).
        """
        if created_at is None:
            return _utc_now_iso()
        try:
            datetime.fromisoformat(created_at)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "created_at must be an ISO-8601 datetime string"
            ) from exc
        return created_at

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> ConversationMessageRecord:
        return ConversationMessageRecord(
            conversation_id=row["conversation_id"],
            seq=int(row["seq"]),
            role=row["role"],
            content=row["content"],
            created_at=row["created_at"],
        )

    def upsert_conversation(
        self,
        conversation_id: str,
        owner_id: str,
        session_id: str,
    ) -> bool:
        """Create a conversation row if it is missing.

        An existing conversation keeps its original identity and
        timestamps: the create path never rebinds ownership (a caller
        targeting another identity's conversation fails its ownership
        check on the next message access, not here). Creation is
        idempotent, so every chat turn may reserve the conversation it
        is about to use.

        Returns:
            True when the row was created, False when it already existed.
        """
        conv_id = self._require_non_empty(conversation_id, "conversation_id")
        owner = self._require_non_empty(owner_id, "owner_id")
        session = self._require_non_empty(session_id, "session_id")
        with self.transaction():
            row = self._fetchone(
                "SELECT conversation_id FROM conversations "
                "WHERE conversation_id = ?",
                (conv_id,),
            )
            if row is not None:
                return False
            now = _utc_now_iso()
            self._conn.execute(
                """
                INSERT INTO conversations (
                    conversation_id, owner_id, session_id,
                    created_at, updated_at, turn_count
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (conv_id, owner, session, now, now),
            )
            return True

    def get_conversation(
        self, conversation_id: str
    ) -> Optional[ConversationRecord]:
        """Return one conversation record (identity included) or None.

        Identity scoping is the caller's responsibility, exactly like
        ``get_plan_record`` / ``get_approval_record``.
        """
        conv_id = self._require_non_empty(conversation_id, "conversation_id")
        row = self._fetchone(
            """
            SELECT conversation_id, owner_id, session_id,
                   created_at, updated_at, turn_count
            FROM conversations
            WHERE conversation_id = ?
            """,
            (conv_id,),
        )
        if row is None:
            return None
        return ConversationRecord(
            conversation_id=row["conversation_id"],
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            turn_count=int(row["turn_count"]),
        )

    def count_conversations(self, owner_id: str, session_id: str) -> int:
        """Conversations stored for one identity (capacity basis)."""
        owner = self._require_non_empty(owner_id, "owner_id")
        session = self._require_non_empty(session_id, "session_id")
        row = self._fetchone(
            """
            SELECT COUNT(*) AS n FROM conversations
            WHERE owner_id = ? AND session_id = ?
            """,
            (owner, session),
        )
        return int(row["n"]) if row else 0

    def list_conversations(
        self,
        owner_id: str,
        session_id: str,
        *,
        limit: int,
        offset: int = 0,
    ) -> List[ConversationRecord]:
        """Identity-scoped conversation summaries, newest first.

        Ordering is deterministic: ``updated_at`` descending with
        ``conversation_id`` descending as the tiebreak, so a page is
        reproducible while the underlying rows are unchanged. Appending
        a turn bumps that conversation's ``updated_at`` and moves it to
        the front of the next page. ``limit`` must be provided by the
        caller (the façade itself takes no pagination policy ceiling).
        """
        records, _total = self.list_conversations_page(
            owner_id, session_id, limit=limit, offset=offset
        )
        return records

    def list_conversations_page(
        self,
        owner_id: str,
        session_id: str,
        *,
        limit: int,
        offset: int = 0,
    ) -> Tuple[List[ConversationRecord], int]:
        """One consistent (page, total) pair from a single transaction.

        The page query and the matching identity-scoped total run inside
        one ``BEGIN IMMEDIATE`` transaction, so a caller can never pair
        rows from one instant with a count from a different instant —
        even while concurrent chat turns append, delete, or reorder
        conversation rows. Same deterministic ordering as
        ``list_conversations``.
        """
        owner = self._require_non_empty(owner_id, "owner_id")
        session = self._require_non_empty(session_id, "session_id")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must not be negative")
        with self.transaction():
            rows = self._fetchall(
                """
                SELECT conversation_id, owner_id, session_id,
                       created_at, updated_at, turn_count
                FROM conversations
                WHERE owner_id = ? AND session_id = ?
                ORDER BY updated_at DESC, conversation_id DESC
                LIMIT ? OFFSET ?
                """,
                (owner, session, limit, offset),
            )
            count_row = self._fetchone(
                """
                SELECT COUNT(*) AS n FROM conversations
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner, session),
            )
        page = [
            ConversationRecord(
                conversation_id=row["conversation_id"],
                owner_id=row["owner_id"],
                session_id=row["session_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                turn_count=int(row["turn_count"]),
            )
            for row in rows
        ]
        return page, int(count_row["n"]) if count_row is not None else 0

    def append_conversation_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        created_at: Optional[str] = None,
    ) -> ConversationMessageRecord:
        """Append one bounded turn atomically.

        One ``BEGIN IMMEDIATE`` transaction allocates the strictly
        monotone ``seq``, inserts the turn, prunes the conversation back
        to ``MAX_CONVERSATION_TURNS`` (FIFO), recounts ``turn_count``,
        and bumps ``updated_at`` — a rollback therefore leaves no
        half-written turn and no stale metadata. The conversation row
        must already exist; identity scoping is the caller's
        responsibility before this call.

        Raises:
            ExecutionStateError: If the conversation does not exist or
                the turn violates database constraints (unknown role or
                out-of-bounds content). The safe message never echoes
                turn content.
        """
        conv_id = self._require_non_empty(conversation_id, "conversation_id")
        stamp = self._validate_message_timestamp(created_at)
        now = _utc_now_iso()
        try:
            with self.transaction():
                row = self._fetchone(
                    "SELECT conversation_id FROM conversations "
                    "WHERE conversation_id = ?",
                    (conv_id,),
                )
                if row is None:
                    raise ExecutionStateError("Conversation was not found.")
                self._conn.execute(
                    """
                    INSERT INTO conversation_messages (
                        conversation_id, seq, role, content, created_at
                    ) VALUES (
                        ?,
                        (SELECT COALESCE(MAX(seq), 0) + 1
                           FROM conversation_messages
                          WHERE conversation_id = ?),
                        ?, ?, ?
                    )
                    """,
                    (conv_id, conv_id, role, content, stamp),
                )
                seq = int(
                    self._fetchone(
                        "SELECT MAX(seq) AS seq FROM conversation_messages "
                        "WHERE conversation_id = ?",
                        (conv_id,),
                    )["seq"]
                )
                self._conn.execute(
                    """
                    DELETE FROM conversation_messages
                    WHERE conversation_id = ?
                      AND seq <= (SELECT MAX(seq)
                                    FROM conversation_messages
                                   WHERE conversation_id = ?)
                          - ?
                    """,
                    (conv_id, conv_id, MAX_CONVERSATION_TURNS),
                )
                self._conn.execute(
                    """
                    UPDATE conversations
                    SET turn_count = (
                            SELECT COUNT(*) FROM conversation_messages
                            WHERE conversation_id = ?
                        ),
                        updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (conv_id, now, conv_id),
                )
                message = self._fetchone(
                    """
                    SELECT conversation_id, seq, role, content, created_at
                    FROM conversation_messages
                    WHERE conversation_id = ? AND seq = ?
                    """,
                    (conv_id, seq),
                )
                if message is None:  # pragma: no cover - defensive
                    raise ExecutionStateError(
                        "Conversation message could not be confirmed."
                    )
                return self._message_from_row(message)
        except sqlite3.IntegrityError as exc:
            raise ExecutionStateError(
                "Conversation message rejected by storage constraints."
            ) from exc

    def list_conversation_messages(
        self,
        conversation_id: str,
        *,
        limit: Optional[int] = None,
    ) -> List[ConversationMessageRecord]:
        """Stored turns in chronological ``seq`` order.

        With ``limit`` the most recent turns form the window, returned
        oldest-to-newest. Reading never mutates state; callers validate
        and scope identity before this call.
        """
        conv_id = self._require_non_empty(conversation_id, "conversation_id")
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1 when given")
        if limit is None:
            rows = self._fetchall(
                """
                SELECT conversation_id, seq, role, content, created_at
                FROM conversation_messages
                WHERE conversation_id = ?
                ORDER BY seq
                """,
                (conv_id,),
            )
            return [self._message_from_row(row) for row in rows]
        rows = self._fetchall(
            """
            SELECT conversation_id, seq, role, content, created_at
            FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY seq DESC
            LIMIT ?
            """,
            (conv_id, limit),
        )
        records = [self._message_from_row(row) for row in rows]
        records.reverse()
        return records

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation and every turn in ONE transaction.

        Turns are removed before (not via foreign-key cascade — the
        schema keeps the explicit-delete style of every other table), so
        a rollback can never leave an orphaned metadata row or orphaned
        turns, and the deletion persists across restarts.

        Returns:
            True if deleted, False if the conversation does not exist.
        """
        conv_id = self._require_non_empty(conversation_id, "conversation_id")
        with self.transaction():
            row = self._fetchone(
                "SELECT conversation_id FROM conversations "
                "WHERE conversation_id = ?",
                (conv_id,),
            )
            if row is None:
                return False
            self._conn.execute(
                "DELETE FROM conversation_messages WHERE conversation_id = ?",
                (conv_id,),
            )
            self._conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conv_id,),
            )
            return True

    # -- audit events ------------------------------------------------------

    def append_event(
        self,
        event: str,
        *,
        execution_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Record one lifecycle audit event (classifications only)."""
        detail_json = (
            json.dumps(detail, sort_keys=True, separators=(",", ":"))
            if detail is not None
            else None
        )
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO execution_events (
                    ts, correlation_id, execution_id, event, detail_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (_utc_now_iso(), correlation_id, execution_id, event, detail_json),
            )
            event_id = int(cursor.lastrowid)
            if event_id % EVENT_PRUNE_INTERVAL == 0:
                self._prune_events_locked(DEFAULT_EVENT_RETENTION)
        return event_id

    def _prune_events_locked(self, keep: int) -> None:
        self._conn.execute(
            """
            DELETE FROM execution_events
            WHERE id NOT IN (
                SELECT id FROM execution_events
                ORDER BY id DESC LIMIT ?
            )
            """,
            (keep,),
        )

    def recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Most recent audit events, newest first."""
        rows = self._fetchall(
            """
            SELECT id, ts, correlation_id, execution_id, event, detail_json
            FROM execution_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        events: List[Dict[str, Any]] = []
        for row in rows:
            events.append(
                {
                    "id": int(row["id"]),
                    "ts": row["ts"],
                    "correlation_id": row["correlation_id"],
                    "execution_id": row["execution_id"],
                    "event": row["event"],
                    "detail": (
                        json.loads(row["detail_json"])
                        if row["detail_json"]
                        else None
                    ),
                }
            )
        return events


def _build_shared_state_db() -> ExecutionStateDb:
    """Shared production façade; path resolved from settings/env."""
    return ExecutionStateDb(path=resolve_state_db_path())


state_db = _build_shared_state_db()
