"""Bounded SQLite persistence façade for execution lifecycle state.

Phase 4.3. Single storage boundary for plans, plan-step states, approval
records and outcomes, execution attempts, and audit events. Everything
here is local-first: one file, one connection, no network services.

Properties and invariants:

- stdlib ``sqlite3`` only (no new dependencies). WAL journal mode for
  file databases, ``BEGIN IMMEDIATE`` transactions, ``PRAGMA
  user_version``-driven migrations created by this module.
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

Conversations are intentionally NOT persisted here (Phase 4.3 scope):
the conversation store remains process-local and is documented as such.
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


SCHEMA_VERSION = 1
DEFAULT_EVENT_RETENTION = 10000
EVENT_PRUNE_INTERVAL = 1024

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
"""

_SUCCEEDED = AttemptStatus.SUCCEEDED.value
_FAILED = AttemptStatus.FAILED.value


class ExecutionStateError(RuntimeError):
    """Raised when persisted execution state cannot be used safely."""


class ExecutionConflictError(ExecutionStateError):
    """Raised when a uniqueness constraint forbids a duplicate write."""


@dataclass(frozen=True)
class PlanRecord:
    """A stored plan plus its owning identity."""

    plan_id: str
    owner_id: str
    session_id: str
    plan: TaskPlan


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
        """Create or verify the schema via ``PRAGMA user_version``."""
        row = conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else 0
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            with self._lock:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            logger.info(
                "Execution state database schema created",
                extra={
                    "extra_fields": {
                        "schema_version": SCHEMA_VERSION,
                        "backed_by_file": bool(self._path),
                    }
                },
            )
            return
        raise ExecutionStateError(
            "Execution state database schema version "
            f"{version} is newer than this application supports "
            f"({SCHEMA_VERSION})."
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

    # -- approvals ---------------------------------------------------------

    def save_approval(
        self,
        request: ApprovalRequest,
        owner_id: str,
        session_id: str,
        digest: str,
    ) -> None:
        """Upsert one approval record with its integrity digest."""
        self._write_approval(
            request,
            owner_id,
            session_id,
            digest,
            purged=False,
        )

    def _write_approval(
        self,
        request: ApprovalRequest,
        owner_id: str,
        session_id: str,
        digest: str,
        *,
        purged: bool,
    ) -> None:
        payload = json.dumps(request.model_dump(mode="json"))
        resolved_at = (
            request.resolved_at.isoformat()
            if request.resolved_at is not None
            else None
        )
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, owner_id, session_id, status,
                    created_at, expires_at, resolved_at, payload,
                    digest, purged
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    status = excluded.status,
                    resolved_at = excluded.resolved_at,
                    payload = excluded.payload,
                    purged = excluded.purged
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
                    1 if purged else 0,
                ),
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

    def mark_approval_purged(self, approval_id: str) -> bool:
        """Flag one terminal approval as purged (row stays for audit)."""
        with self._lock:
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

    def list_approval_records(
        self, include_purged: bool = False
    ) -> List[ApprovalRecord]:
        """All stored approval rows (cleanup scans iterate non-purged)."""
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
