"""Execution-state façade conversation storage (Phase 5.1-a): schema
migration of legacy version-1 databases, identity-scoped conversation
rows, bounded FIFO retention, transactional append/delete, and survival
across two generations over one file-backed database.

The frozen version-1 schema snapshot below is kept verbatim on purpose:
it is the exact DDL a Phase 4.3 database was created with, so the
migration test exercises the upgrade path a real deployment takes.
"""

import sqlite3
from datetime import datetime
from uuid import uuid4

import pytest

from app.schemas.plan import PlanStep, TaskPlan
from app.services.execution_db import (
    MAX_CONVERSATION_TURNS,
    MAX_TURN_CONTENT_LENGTH,
    ExecutionStateDb,
    ExecutionStateError,
)


# Verbatim Phase 4.3 (schema version 1) _SCHEMA snapshot.
_V1_SCHEMA = """
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


def _raw(path):
    return sqlite3.connect(path)


def _fresh_db():
    """In-memory façade: no filesystem cost, isolated per test."""
    return ExecutionStateDb()


# --- schema migration ---------------------------------------------------------


def test_fresh_database_is_created_at_version_2(tmp_path):
    path = str(tmp_path / "state.db")
    ExecutionStateDb(path=path).close()
    with _raw(path) as raw:
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 2
        names = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"conversations", "conversation_messages"} <= names


def test_v1_database_migrates_in_place_preserving_all_data(tmp_path):
    """A Phase 4.3 database upgrades to version 2 without losing rows."""
    path = str(tmp_path / "legacy.db")
    with _raw(path) as raw:
        raw.executescript(_V1_SCHEMA)
        raw.execute("PRAGMA user_version = 1")
        raw.execute(
            "INSERT INTO plans (plan_id, owner_id, session_id, payload, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("plan-legacy", "owner-a", "sess-a", '{"legacy": true}',
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        raw.execute(
            "INSERT INTO plan_steps (plan_id, step_id, approval_id, status, "
            "owner_id, session_id) VALUES ('plan-legacy', 's1', NULL, "
            "'PENDING', 'owner-a', 'sess-a')",
        )
        raw.execute(
            "INSERT INTO execution_events (ts, event, detail_json) "
            "VALUES (?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "legacy_event", None),
        )
        raw.commit()

    # Migration happens for the caller at construction time.
    ExecutionStateDb(path=path).close()
    with _raw(path) as raw:
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 2
        names = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "conversations" in names
        assert "conversation_messages" in names
        assert raw.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
        assert raw.execute(
            "SELECT payload FROM plans WHERE plan_id = 'plan-legacy'"
        ).fetchone()[0] == '{"legacy": true}'
        assert raw.execute("SELECT COUNT(*) FROM plan_steps").fetchone()[0] == 1
        assert raw.execute(
            "SELECT COUNT(*) FROM execution_events WHERE event = 'legacy_event'"
        ).fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_migrated_database_is_fully_functional(tmp_path):
    """The upgraded façade still executes its Phase 4.3 contract."""
    path = str(tmp_path / "legacy.db")
    with _raw(path) as raw:
        raw.executescript(_V1_SCHEMA)
        raw.execute("PRAGMA user_version = 1")
        raw.commit()

    db = ExecutionStateDb(path=path)
    plan = TaskPlan(
        plan_id=str(uuid4()),
        goal="Survive a schema migration",
        steps=[
            PlanStep(
                step_id="s1",
                description="One bounded step",
                tool_name="stub_ro",
            )
        ],
    )
    db.save_plan(plan, "owner-a", "sess-a")
    db.close()

    reopened = ExecutionStateDb(path=path)
    record = reopened.get_plan_record(plan.plan_id)
    assert record is not None
    assert record.owner_id == "owner-a"
    assert record.plan.goal == "Survive a schema migration"

    # The migrated file also accepts new conversation rows.
    assert reopened.upsert_conversation("conv-x", "owner-a", "sess-a") is True
    reopened.close()


# --- conversation rows and identity -----------------------------------------


def test_upsert_creates_once_and_never_rebinds_identity():
    db = _fresh_db()
    assert db.upsert_conversation("conv-1", "owner-a", "sess-a") is True
    record = db.get_conversation("conv-1")
    assert record is not None
    assert (record.owner_id, record.session_id, record.turn_count) == (
        "owner-a", "sess-a", 0,
    )
    created_at = record.created_at
    assert record.updated_at == created_at

    # Idempotent re-create: no rebinding, no timestamp churn.
    assert db.upsert_conversation("conv-1", "owner-a", "sess-a") is False
    assert db.upsert_conversation("conv-1", "owner-b", "sess-a") is False
    after = db.get_conversation("conv-1")
    assert (after.owner_id, after.session_id, after.created_at) == (
        "owner-a", "sess-a", created_at,
    )


def test_identity_scoped_counts_and_lists():
    db = _fresh_db()
    for conv in ("c1", "c2", "c3"):
        db.upsert_conversation(conv, "owner-a", "sess-a")
    db.upsert_conversation("c4", "owner-a", "sess-b")
    db.upsert_conversation("c5", "owner-b", "sess-a")

    assert db.count_conversations("owner-a", "sess-a") == 3
    assert db.count_conversations("owner-a", "sess-b") == 1
    assert db.count_conversations("owner-b", "sess-a") == 1
    assert db.count_conversations("owner-b", "sess-b") == 0

    ids = [
        record.conversation_id
        for record in db.list_conversations("owner-a", "sess-a", limit=10)
    ]
    assert set(ids) == {"c1", "c2", "c3"}


def test_empty_identifiers_rejected():
    db = _fresh_db()
    with pytest.raises(ValueError):
        db.upsert_conversation("  ", "owner-a", "sess-a")
    with pytest.raises(ValueError):
        db.get_conversation("")
    with pytest.raises(ValueError):
        db.list_conversations("owner-a", "", limit=5)


# --- message persistence ------------------------------------------------------


def test_messages_get_monotone_sequence_and_verbatim_timestamps():
    db = _fresh_db()
    db.upsert_conversation("conv-1", "owner-a", "sess-a")
    stamps = (
        "2030-01-01T00:00:00+00:00",
        "2030-01-01T00:00:00+00:00",  # identical on purpose: tie allowed
        "2030-01-01T00:00:01.500000+00:00",
    )
    for index, stamp in enumerate(stamps, start=1):
        record = db.append_conversation_message(
            "conv-1", "user" if index % 2 else "assistant",
            f"turn {index}", created_at=stamp,
        )
        assert record.seq == index
        assert record.created_at == stamp  # preserved verbatim
        assert record.conversation_id == "conv-1"

    metadata = db.get_conversation("conv-1")
    assert metadata.turn_count == 3
    assert metadata.updated_at >= metadata.created_at

    stored = db.list_conversation_messages("conv-1")
    assert [m.seq for m in stored] == [1, 2, 3]
    assert [m.content for m in stored] == ["turn 1", "turn 2", "turn 3"]
    assert [m.created_at for m in stored] == list(stamps)
    assert [m.role for m in stored] == ["user", "assistant", "user"]


def test_default_timestamp_is_utc_and_valid():
    db = _fresh_db()
    db.upsert_conversation("conv-1", "owner-a", "sess-a")
    record = db.append_conversation_message("conv-1", "user", "hello")
    assert record.created_at is not None
    parsed = datetime.fromisoformat(record.created_at)
    assert parsed.tzinfo is not None


def test_bounded_recent_window_returns_chronological_turns():
    db = _fresh_db()
    db.upsert_conversation("conv-1", "owner-a", "sess-a")
    for index in range(1, 6):
        db.append_conversation_message("conv-1", "user", f"m{index}")

    full = db.list_conversation_messages("conv-1")
    assert [m.seq for m in full] == [1, 2, 3, 4, 5]

    window = db.list_conversation_messages("conv-1", limit=2)
    assert [m.seq for m in window] == [4, 5]  # most recent, chronological

    over = db.list_conversation_messages("conv-1", limit=100)
    assert [m.seq for m in over] == [1, 2, 3, 4, 5]

    with pytest.raises(ValueError):
        db.list_conversation_messages("conv-1", limit=0)


def test_fifo_retention_keeps_last_bound_and_sequence_grows():
    db = _fresh_db()
    db.upsert_conversation("conv-1", "owner-a", "sess-a")
    for stamp in ("2030-01-01T00:00:00+00:00", "2030-01-01T00:00:01+00:00"):
        db.append_conversation_message("conv-1", "user", "old", created_at=stamp)
    extra = MAX_CONVERSATION_TURNS + 3
    for index in range(extra):
        db.append_conversation_message("conv-1", "user", f"m{index}")

    metadata = db.get_conversation("conv-1")
    assert metadata.turn_count == MAX_CONVERSATION_TURNS

    stored = db.list_conversation_messages("conv-1")
    assert len(stored) == MAX_CONVERSATION_TURNS
    # FIFO kept exactly the newest turns: the two "old" turns are pruned.
    assert [m.content for m in stored] == [
        f"m{i}" for i in range(extra - MAX_CONVERSATION_TURNS, extra)
    ]
    # Sequence indexing keeps growing past pruned turns; ordering and
    # timestamps of surviving messages are never rewritten.
    last_seq = 2 + extra
    expected_first_seq = last_seq - MAX_CONVERSATION_TURNS + 1
    assert [m.seq for m in stored] == list(
        range(expected_first_seq, last_seq + 1)
    )
    assert stored[0].content == f"m{extra - MAX_CONVERSATION_TURNS}"
    assert stored[-1].content == f"m{extra - 1}"


def test_window_respects_retention_bound_content_limit():
    db = _fresh_db()
    db.upsert_conversation("conv-1", "owner-a", "sess-a")
    exact = "a" * MAX_TURN_CONTENT_LENGTH
    record = db.append_conversation_message("conv-1", "user", exact)
    assert record.content == exact


# --- transactional behavior ---------------------------------------------------


def test_constraint_rejection_rolls_back_atomically():
    db = _fresh_db()
    db.upsert_conversation("conv-1", "owner-a", "sess-a")
    db.append_conversation_message("conv-1", "user", "keep", created_at="2030-01-01T00:00:00+00:00")

    before = db.get_conversation("conv-1")
    for role, content in (
        ("system", "unknown role"),
        ("user", ""),           # empty content
        ("user", "x" * (MAX_TURN_CONTENT_LENGTH + 1)),
    ):
        with pytest.raises(ExecutionStateError):
            db.append_conversation_message("conv-1", role, content)

    after = db.get_conversation("conv-1")
    assert after.turn_count == 1                # no partial row persisted
    assert after.updated_at == before.updated_at  # metadata untouched
    stored = db.list_conversation_messages("conv-1")
    assert [m.seq for m in stored] == [1]       # sequence unharmed

    good = db.append_conversation_message("conv-1", "assistant", "next")
    assert good.seq == 2                        # next append resumes cleanly


def test_append_requires_existing_conversation():
    db = _fresh_db()
    with pytest.raises(ExecutionStateError):
        db.append_conversation_message("conv-missing", "user", "hello")
    assert db.get_conversation("conv-missing") is None


def test_invalid_message_timestamp_rejected():
    db = _fresh_db()
    db.upsert_conversation("conv-1", "owner-a", "sess-a")
    with pytest.raises(ValueError):
        db.append_conversation_message(
            "conv-1", "user", "hello", created_at="not-a-timestamp"
        )
    assert db.get_conversation("conv-1").turn_count == 0


def test_delete_is_transactional_and_removes_turns():
    db = _fresh_db()
    db.upsert_conversation("conv-1", "owner-a", "sess-a")
    for index in range(3):
        db.append_conversation_message("conv-1", "user", f"m{index}")

    assert db.delete_conversation("conv-1") is True
    assert db.delete_conversation("conv-1") is False  # idempotent
    assert db.get_conversation("conv-1") is None
    assert db.list_conversation_messages("conv-1") == []

    # A new conversation with the recycled id starts completely clean.
    assert db.upsert_conversation("conv-1", "owner-a", "sess-a") is True
    assert db.get_conversation("conv-1").turn_count == 0
    fresh = db.append_conversation_message("conv-1", "user", "again")
    assert fresh.seq == 1                       # seq numbering restarts


# --- deterministic pagination -------------------------------------------------


def test_list_pagination_is_deterministic_and_replicable():
    db = _fresh_db()
    convs = [f"con-{i}" for i in range(5)]
    for conv in convs:
        db.upsert_conversation(conv, "owner-a", "sess-a")
    # Most recently appended turns float to the top deterministically.
    for conv in convs:
        db.append_conversation_message(conv, "user", "bump")

    page_one = db.list_conversations("owner-a", "sess-a", limit=2)
    page_two = db.list_conversations("owner-a", "sess-a", limit=2, offset=2)
    remainder = db.list_conversations("owner-a", "sess-a", limit=2, offset=4)

    assert len(page_one) == 2 and len(page_two) == 2 and len(remainder) == 1
    ordered = page_one + page_two + remainder
    assert len(ordered) == 5 and len({r.conversation_id for r in ordered}) == 5

    # Identical repeated queries yield one stable, non-increasing
    # updated_at order (tiebreak conversation_id DESC when equal), and
    # every summary carries an integer turn count.
    again = db.list_conversations("owner-a", "sess-a", limit=5)
    assert [r.conversation_id for r in again] == [
        r.conversation_id for r in db.list_conversations("owner-a", "sess-a", limit=5)
    ]
    stamps = [(r.updated_at, r.conversation_id) for r in again]
    assert stamps == sorted(stamps, reverse=True)
    assert all(isinstance(r.turn_count, int) and r.turn_count >= 0 for r in again)


def test_list_beyond_total_returns_empty():
    db = _fresh_db()
    db.upsert_conversation("c1", "owner-a", "sess-a")
    assert db.list_conversations("owner-a", "sess-a", limit=5, offset=1) == []
    with pytest.raises(ValueError):
        db.list_conversations("owner-a", "sess-a", limit=0)
    with pytest.raises(ValueError):
        db.list_conversations("owner-a", "sess-a", limit=5, offset=-1)


# --- persistence across two generations --------------------------------------


def test_conversations_survive_two_generations(tmp_path):
    path = str(tmp_path / "state.db")
    gen1 = ExecutionStateDb(path=path)
    gen1.upsert_conversation("conv-1", "owner-a", "sess-a")
    gen1.append_conversation_message(
        "conv-1", "user", "first turn", created_at="2030-01-01T00:00:00+00:00"
    )
    gen1.append_conversation_message(
        "conv-1", "assistant", "second turn",
        created_at="2030-01-01T00:00:00.250000+00:00",
    )
    gen1.upsert_conversation("conv-gone", "owner-a", "sess-a")
    gen1.append_conversation_message("conv-gone", "user", "deleted soon")
    assert gen1.delete_conversation("conv-gone") is True

    first = gen1.get_conversation("conv-1")
    turns = gen1.list_conversation_messages("conv-1")
    gen1.close()

    gen2 = ExecutionStateDb(path=path)
    restored = gen2.get_conversation("conv-1")
    assert restored == first  # identity, timestamps, turn_count preserved
    restored_turns = gen2.list_conversation_messages("conv-1")
    assert restored_turns == turns
    assert [m.created_at for m in restored_turns] == [
        m.created_at for m in turns
    ]
    # Deletions persist too.
    assert gen2.get_conversation("conv-gone") is None
    assert gen2.delete_conversation("conv-gone") is False
    # Identity-scoped list includes the survivor with the stored count.
    listed = gen2.list_conversations("owner-a", "sess-a", limit=10)
    assert [r.conversation_id for r in listed] == ["conv-1"]
    assert listed[0].turn_count == 2
    gen2.close()


def test_two_generations_share_capacity_accounting(tmp_path):
    path = str(tmp_path / "state.db")
    gen1 = ExecutionStateDb(path=path)
    gen1.upsert_conversation("c1", "owner-a", "sess-a")
    gen1.upsert_conversation("c2", "owner-a", "sess-a")
    gen1.close()

    gen2 = ExecutionStateDb(path=path)
    assert gen2.count_conversations("owner-a", "sess-a") == 2
    ids = [r.conversation_id for r in gen2.list_conversations("owner-a", "sess-a", limit=10)]
    assert set(ids) == {"c1", "c2"}
    gen2.close()
