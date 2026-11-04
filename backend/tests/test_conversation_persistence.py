"""PersistentConversationStore (Phase 5.1-b): the public contract of the
old process-local store preserved on durable storage, atomic conversation
capacity, strict corruption handling, and persistence across restarts.

The contract tests mirror the exact semantics of the Phase 2
``InMemoryConversationStore`` (signatures, error types, and
identity-scoped behavior) so the chat service, orchestrator, and
conversation API can consume this store unchanged. Corruption tests
bypass the write path through the storage façade's connection (same
precedent as tests/test_execution_state.py) and must never observe
silent repair, fabrication, or discarded damage — and no error may
carry conversation content.
"""

import threading
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.conversation import ConversationTurn
from app.services.conversation_store import (
    MAX_CONVERSATIONS_PER_IDENTITY,
    ConversationCapacityError,
    ConversationCorruptError,
    ConversationNotFoundError,
    PersistentConversationStore,
)
from app.services.execution_db import (
    MAX_TURN_CONTENT_LENGTH,
    ConversationMessageRecord,
    ExecutionStateDb,
)


OWNER_ID = "persist-owner-a"
SESSION_ID = "persist-session-a"
OTHER_OWNER_ID = "persist-owner-b"
OTHER_SESSION_ID = "persist-session-b"

SENTINEL = "secret-user-content-marker-07d3f2"


def fresh_store(max_conversations: int = MAX_CONVERSATIONS_PER_IDENTITY):
    """A store over an isolated in-memory façade (no filesystem cost)."""
    return PersistentConversationStore(max_conversations=max_conversations)


# --- creation, contract compatibility ----------------------------------------


def test_create_generates_id_and_is_idempotent():
    store = fresh_store()
    generated = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    assert store.exists(generated)

    # Same identity: no-op returning the same id, timestamps untouched.
    record_before = store.execution_db.get_conversation(generated)
    again = store.create_conversation(
        generated, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    assert again == generated
    record_after = store.execution_db.get_conversation(generated)
    assert record_after == record_before


def test_create_rejects_foreign_conversation_without_it():
    """A caller-supplied id owned by another identity is never reused."""
    store = fresh_store()
    conv_id = str(uuid4())

    # Fully unknown id: the supplied id is reserved for THIS identity and
    # must never be handed to a second identity afterwards.
    created = store.create_conversation(
        conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    assert created == conv_id
    with pytest.raises(ConversationNotFoundError):
        store.create_conversation(
            conv_id, owner_id=OTHER_OWNER_ID, session_id=OTHER_SESSION_ID
        )
    record = store.execution_db.get_conversation(conv_id)
    assert (record.owner_id, record.session_id) == (OWNER_ID, SESSION_ID)


def test_create_never_rebinds_ownership_of_existing_conversation():
    """Capacity must not mint a second row for a foreign conversation."""
    store = fresh_store(max_conversations=4)
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    before = store.execution_db.count_conversations(OWNER_ID, SESSION_ID)

    with pytest.raises(ConversationNotFoundError):
        store.create_conversation(
            conv_id, owner_id=OTHER_OWNER_ID, session_id=OTHER_SESSION_ID
        )
    with pytest.raises(ConversationNotFoundError):
        # Same owner, different session: still a different identity.
        store.create_conversation(conv_id, owner_id=OWNER_ID, session_id=OTHER_SESSION_ID)

    # Foreign conversation steals no row and holds no capacity anywhere.
    assert store.execution_db.count_conversations(OWNER_ID, SESSION_ID) == before
    assert store.execution_db.count_conversations(OTHER_OWNER_ID, OTHER_SESSION_ID) == 0
    assert store.execution_db.count_conversations(OWNER_ID, OTHER_SESSION_ID) == 0
    # Original identity is untouched and fully usable.
    assert store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID) == []


def test_contract_exists_is_existence_not_ownership():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    assert store.exists(conv_id) is True
    assert store.exists(str(uuid4())) is False


def test_contract_history_semantics_unknown_foreign_and_empty():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)

    assert store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID) == []
    # Foreign owner / session / unknown ids are all indistinguishable "None".
    assert store.get_history(conv_id, owner_id=OTHER_OWNER_ID, session_id=SESSION_ID) is None
    assert store.get_history(conv_id, owner_id=OWNER_ID, session_id=OTHER_SESSION_ID) is None
    assert store.get_history(str(uuid4()), owner_id=OWNER_ID, session_id=SESSION_ID) is None


def test_contract_returned_list_and_turns_are_safe_copies():
    """Reads return fresh objects: callers cannot mutate stored state."""
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)

    history = store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    history.append(
        ConversationTurn(role="assistant", content="fabricated")
    )
    history[0].content = "tampered"

    again = store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert [t.content for t in again] == [SENTINEL]
    assert len(again) == 1


def test_turn_validation_happens_before_any_state_changes():
    """Invalid turns fail at schema validation and create nothing."""
    store = fresh_store()
    conv_id = str(uuid4())
    with pytest.raises(ValidationError):
        store.add_turn(conv_id, "system", "hello", owner_id=OWNER_ID, session_id=SESSION_ID)
    with pytest.raises(ValidationError):
        store.add_turn(conv_id, "user", 123, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert store.exists(conv_id) is False


def test_contract_clear_deletes_only_own_conversation():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)

    assert store.clear_conversation(conv_id, owner_id=OTHER_OWNER_ID, session_id=SESSION_ID) is False
    assert store.clear_conversation(conv_id, owner_id=OWNER_ID, session_id=OTHER_SESSION_ID) is False
    assert store.clear_conversation(str(uuid4()), owner_id=OWNER_ID, session_id=SESSION_ID) is False

    assert store.clear_conversation(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID) is True

    assert store.exists(conv_id) is False
    assert store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID) is None


# --- turn persistence, ordering ----------------------------------------------


def test_user_and_assistant_turns_persist_in_order():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)

    first = store.add_turn(conv_id, "user", "hello", owner_id=OWNER_ID, session_id=SESSION_ID)
    second = store.add_turn(
        conv_id, "assistant", "heLLo yourself", owner_id=OWNER_ID, session_id=SESSION_ID
    )
    third = store.add_turn(conv_id, "user", "compute 2+2", owner_id=OWNER_ID, session_id=SESSION_ID)

    assert isinstance(first, ConversationTurn)
    history = store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert [(t.role, t.content) for t in history] == [
        ("user", "hello"),
        ("assistant", "heLLo yourself"),
        ("user", "compute 2+2"),
    ]
    # Chronology is by stored sequence, not timestamp resolution.
    timestamps = [t.timestamp for t in history]
    assert timestamps == [first.timestamp, second.timestamp, third.timestamp]
    # t1 may share a timestamp with t2 (clock resolution); sequence decides.


def test_add_turn_returns_persisted_turn_roundtrip():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    stored = store.add_turn(conv_id, "user", "precise", owner_id=OWNER_ID, session_id=SESSION_ID)
    history = store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert history[0] == stored
    assert history[0].timestamp == stored.timestamp
    assert stored.timestamp.tzinfo is not None
    record = store.execution_db.list_conversation_messages(conv_id)[0]
    assert datetime.fromisoformat(record.created_at) == stored.timestamp


def test_identity_isolation_never_exposes_content():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "assistant", "reply to owner a", owner_id=OWNER_ID, session_id=SESSION_ID)

    # A different owner sees nothing at all - unknown-equivalent.
    assert store.get_history(conv_id, owner_id=OTHER_OWNER_ID, session_id=SESSION_ID) is None
    # The same owner through another session is also isolated.
    assert store.get_history(conv_id, owner_id=OWNER_ID, session_id=OTHER_SESSION_ID) is None
    # And a foreign add_turn can never append to (or create over) it.
    with pytest.raises(ConversationNotFoundError):
        store.add_turn(conv_id, "user", "hijack", owner_id=OTHER_OWNER_ID, session_id=SESSION_ID)
    history = store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert "hijack" not in [t.content for t in history]
    assert len(history) == 2


# --- capacity -----------------------------------------------------------------


def test_capacity_exhaustion_fails_closed():
    store = fresh_store(max_conversations=2)
    first = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    second = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    with pytest.raises(ConversationCapacityError):
        store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)

    # A failed create must leave nothing behind and keep existing state.
    assert store.execution_db.count_conversations(OWNER_ID, SESSION_ID) == 2
    assert store.get_history(first, owner_id=OWNER_ID, session_id=SESSION_ID) == []
    assert store.get_history(second, owner_id=OWNER_ID, session_id=SESSION_ID) == []

    # Capacity is identity-scoped: a second identity still has room.
    other = store.create_conversation(owner_id=OTHER_OWNER_ID, session_id=OTHER_SESSION_ID)
    assert store.get_history(other, owner_id=OTHER_OWNER_ID, session_id=OTHER_SESSION_ID) == []


def test_default_capacity_bound_is_256():
    store = fresh_store()
    made = [
        store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
        for _ in range(MAX_CONVERSATIONS_PER_IDENTITY)
    ]
    assert len(made) == MAX_CONVERSATIONS_PER_IDENTITY == 256
    assert store.execution_db.count_conversations(OWNER_ID, SESSION_ID) == 256
    with pytest.raises(ConversationCapacityError):
        store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)


def test_add_turn_enforces_capacity_for_new_conversation_only():
    store = fresh_store(max_conversations=1)
    # First add creates the only allowed conversation.
    created = str(uuid4())
    store.add_turn(created, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)
    # A second NEW conversation is refused...
    with pytest.raises(ConversationCapacityError):
        store.add_turn(str(uuid4()), "user", "x", owner_id=OWNER_ID, session_id=SESSION_ID)
    # ...but the EXISTING conversation keeps accepting turns.
    store.add_turn(created, "assistant", "reply", owner_id=OWNER_ID, session_id=SESSION_ID)
    history = store.get_history(created, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert [t.role for t in history] == ["user", "assistant"]


def test_concurrent_creation_never_exceeds_capacity():
    """Concurrent creates race the atomic check+create and stay bound."""
    store = fresh_store(max_conversations=4)
    threads = 8
    barrier = threading.Barrier(threads)
    results = {"ok": [], "capacity": 0}
    results_guard = threading.Lock()

    def worker():
        barrier.wait()
        try:
            conv_id = store.create_conversation(
                owner_id=OWNER_ID, session_id=SESSION_ID
            )
        except ConversationCapacityError:
            with results_guard:
                results["capacity"] += 1
        else:
            with results_guard:
                results["ok"].append(conv_id)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()

    assert len(results["ok"]) == 4
    assert len({conv_id for conv_id in results["ok"]}) == 4
    assert results["capacity"] == threads - 4
    # The database itself never exceeds the bound.
    assert store.execution_db.count_conversations(OWNER_ID, SESSION_ID) == 4


# --- corruption handling ------------------------------------------------------


def test_turn_from_record_rejects_bad_role_content_timestamp():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)

    def corrupt_record(role, content, created_at):
        return ConversationMessageRecord(
            conversation_id=conv_id, seq=1, role=role,
            content=content, created_at=created_at,
        )

    bad_roles = ("system", "tool", "", "USER", "user ")
    for role in bad_roles:
        with pytest.raises(ConversationCorruptError):
            store._turn_from_record(corrupt_record(role, SENTINEL, "2030-01-01T00:00:00+00:00"))

    bad_contents = ("", "x" * (MAX_TURN_CONTENT_LENGTH + 1))
    for content in bad_contents:
        with pytest.raises(ConversationCorruptError):
            store._turn_from_record(corrupt_record("user", content, "2030-01-01T00:00:00+00:00"))

    bad_timestamps = ("not-a-date", "", "2026-13-45T99:99:99+00:00")
    for stamp in bad_timestamps:
        with pytest.raises(ConversationCorruptError):
            store._turn_from_record(corrupt_record("user", SENTINEL, stamp))

    # A valid record still passes and produces the correct turn.
    ok = store._turn_from_record(
        corrupt_record("assistant", "fine turn", "2030-01-02T03:04:05+00:00")
    )
    assert (ok.role, ok.content) == ("assistant", "fine turn")
    assert ok.timestamp == datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_corrupted_timestamp_raises_and_never_leaks_content():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)

    # The storage layer intentionally leaves created_at format
    # unconstrained: the store must catch it on read instead.
    store.execution_db._conn.execute(
        "UPDATE conversation_messages SET created_at = 'not-a-date' "
        "WHERE conversation_id = ?",
        (conv_id,),
    )

    with pytest.raises(ConversationCorruptError) as excinfo:
        store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    message = str(excinfo.value)
    assert message == "Conversation history could not be validated."
    assert SENTINEL not in message
    # A healthy conversation keeps working while this one is damaged.
    healthy = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(healthy, "user", "still fine", owner_id=OWNER_ID, session_id=SESSION_ID)
    assert store.get_history(healthy, owner_id=OWNER_ID, session_id=SESSION_ID)


def test_corrupted_content_raises_and_never_leaks_row_value():
    """Storage CHECKs are bypassed with the documented SQLite pragma so
    the READ path (not the constraint) has to reject the damage."""
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)

    conn = store.execution_db._conn
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        conn.execute(
            "UPDATE conversation_messages SET content = '' WHERE conversation_id = ?",
            (conv_id,),
        )
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(ConversationCorruptError) as excinfo:
        store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert SENTINEL not in str(excinfo.value)


def test_deleting_corrupted_conversation_still_works():
    """Deletion must not be blocked by unreadable history."""
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)
    store.execution_db._conn.execute(
        "UPDATE conversation_messages SET created_at = 'garbage' WHERE conversation_id = ?",
        (conv_id,),
    )

    with pytest.raises(ConversationCorruptError):
        store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)

    assert store.clear_conversation(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID) is True
    assert store.exists(conv_id) is False
    assert store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID) is None

    # The recycled id starts completely clean and readable again.
    recreated = store.create_conversation(
        conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    store.add_turn(recreated, "user", "fresh start", owner_id=OWNER_ID, session_id=SESSION_ID)
    history = store.get_history(recreated, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert [t.content for t in history] == ["fresh start"]


def test_corrupt_role_via_pragma_raises_on_read():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)

    conn = store.execution_db._conn
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        conn.execute(
            "UPDATE conversation_messages SET role = 'system' WHERE conversation_id = ?",
            (conv_id,),
        )
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(ConversationCorruptError):
        store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    # The record remains in the store (no silent repair or deletion):
    record = store.execution_db.list_conversation_messages(conv_id)[0]
    assert record.role == "system"


# --- persistence across generations ------------------------------------------


def test_history_and_deletion_survive_restart(tmp_path):
    path = str(tmp_path / "state.db")

    gen1_db = ExecutionStateDb(path=path)
    gen1 = PersistentConversationStore(state_db=gen1_db)
    conv_id = gen1.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    gen1.add_turn(conv_id, "user", "first turn", owner_id=OWNER_ID, session_id=SESSION_ID)
    gen1.add_turn(
        conv_id, "assistant", "second turn", owner_id=OWNER_ID, session_id=SESSION_ID
    )
    doomed = gen1.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    gen1.add_turn(doomed, "user", "delete me", owner_id=OWNER_ID, session_id=SESSION_ID)
    assert gen1.clear_conversation(doomed, owner_id=OWNER_ID, session_id=SESSION_ID) is True
    before = gen1.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    gen1_db.close()

    # "Restart": a brand-new façade + store pair over the same file.
    gen2_db = ExecutionStateDb(path=path)
    try:
        gen2 = PersistentConversationStore(state_db=gen2_db)
        assert gen2.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID) == before
        assert gen2.get_history(doomed, owner_id=OWNER_ID, session_id=SESSION_ID) is None
        # The restarted process keeps mutating the same conversation.
        gen2.add_turn(conv_id, "user", "after restart", owner_id=OWNER_ID, session_id=SESSION_ID)
        history = gen2.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
        assert [t.content for t in history] == [
            "first turn", "second turn", "after restart",
        ]
    finally:
        gen2_db.close()


def test_capacity_accounting_persists_across_generations(tmp_path):
    path = str(tmp_path / "state.db")
    gen1_db = ExecutionStateDb(path=path)
    gen1 = PersistentConversationStore(state_db=gen1_db, max_conversations=2)
    gen1.create_conversation(str(uuid4()), owner_id=OWNER_ID, session_id=SESSION_ID)
    gen1.create_conversation(str(uuid4()), owner_id=OWNER_ID, session_id=SESSION_ID)
    with pytest.raises(ConversationCapacityError):
        gen1.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    gen1_db.close()

    gen2_db = ExecutionStateDb(path=path)
    try:
        gen2 = PersistentConversationStore(state_db=gen2_db, max_conversations=2)
        with pytest.raises(ConversationCapacityError):
            gen2.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
        assert gen2.execution_db.count_conversations(OWNER_ID, SESSION_ID) == 2
    finally:
        gen2_db.close()


# --- listing / windowed metadata reads (Phase 5.1-d store surface) -------------


def test_get_turn_count_is_identity_scoped_metadata_only():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(conv_id, "assistant", "reply", owner_id=OWNER_ID, session_id=SESSION_ID)

    assert store.get_turn_count(
        conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    ) == 2
    # Foreign identity: indistinguishable from unknown, no content touched.
    assert store.get_turn_count(
        conv_id, owner_id=OTHER_OWNER_ID, session_id=SESSION_ID
    ) is None
    assert store.get_turn_count(
        conv_id, owner_id=OWNER_ID, session_id=OTHER_SESSION_ID
    ) is None
    assert store.get_turn_count(
        str(uuid4()), owner_id=OWNER_ID, session_id=SESSION_ID
    ) is None


def test_get_history_window_returns_most_recent_turns_chronologically():
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    for i in range(5):
        store.add_turn(
            conv_id, "user" if i % 2 else "assistant", f"t{i}",
            owner_id=OWNER_ID, session_id=SESSION_ID,
        )
    window = store.get_history(
        conv_id, owner_id=OWNER_ID, session_id=SESSION_ID, limit=2
    )
    assert [t.content for t in window] == ["t3", "t4"]
    # Full retrieval still returns everything (default behavior unchanged).
    full = store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert [t.content for t in full] == ["t0", "t1", "t2", "t3", "t4"]
    # Foreign identity gets None with any limit.
    assert store.get_history(
        conv_id, owner_id=OTHER_OWNER_ID, session_id=SESSION_ID, limit=2
    ) is None


def test_list_and_count_wrap_identity_scoped_metadata():
    store = fresh_store()
    own = [
        store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
        for _ in range(3)
    ]
    foreign = store.create_conversation(
        owner_id=OTHER_OWNER_ID, session_id=OTHER_SESSION_ID
    )
    store.add_turn(own[2], "user", "bump", owner_id=OWNER_ID, session_id=SESSION_ID)

    listed = store.list_conversations(
        owner_id=OWNER_ID, session_id=SESSION_ID, limit=10
    )
    assert [r.conversation_id for r in listed] == [own[2], own[1], own[0]]
    assert all(r.owner_id == OWNER_ID for r in listed)
    assert foreign not in [r.conversation_id for r in listed]
    assert store.count_conversations(OWNER_ID, SESSION_ID) == 3
    assert store.count_conversations(OTHER_OWNER_ID, OTHER_SESSION_ID) == 1
    page = store.list_conversations(
        owner_id=OWNER_ID, session_id=SESSION_ID, limit=1, offset=2
    )
    assert [r.conversation_id for r in page] == [own[0]]


# --- Phase 5.1-e: atomic page pairing & strict metadata reads --------------------


def test_list_page_pairs_page_with_total():
    store = fresh_store()
    made = [
        store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
        for _ in range(5)
    ]
    page, total = store.list_conversations_page(
        owner_id=OWNER_ID, session_id=SESSION_ID, limit=2, offset=1
    )
    assert [r.conversation_id for r in page] == [made[3], made[2]]
    assert total == 5
    # Same values as the separate reads, when nothing is mutating.
    assert page == store.list_conversations(
        owner_id=OWNER_ID, session_id=SESSION_ID, limit=2, offset=1
    )
    assert total == store.count_conversations(OWNER_ID, SESSION_ID)
    # Foreign identity unaffected.
    assert store.list_conversations_page(
        owner_id=OTHER_OWNER_ID, session_id=OTHER_SESSION_ID, limit=5
    ) == ([], 0)


def test_list_page_is_consistent_under_concurrent_appends():
    """A writer thread appends turns while snapshots are taken; every
    snapshot's (page, total) pairing stays bound by its own instant."""
    store = fresh_store()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    done = threading.Event()
    writer_error = []

    def writer():
        try:
            while not done.is_set():
                store.add_turn(
                    conv_id, "user", "w", owner_id=OWNER_ID, session_id=SESSION_ID
                )
        except Exception as exc:  # pragma: no cover - surfaced below
            writer_error.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        for _ in range(25):
            page, total = store.list_conversations_page(
                owner_id=OWNER_ID, session_id=SESSION_ID, limit=3, offset=0
            )
            assert len(page) <= 3
            assert total >= len(page)  # pairing from the same instant
            stamps = [(r.updated_at, r.conversation_id) for r in page]
            assert stamps == sorted(stamps, reverse=True)
            assert conv_id == page[0].conversation_id  # leader is the writer
    finally:
        done.set()
        writer_thread.join()
    assert writer_error == []
    # After the writer stopped, page and total are exactly consistent.
    page, total = store.list_conversations_page(
        owner_id=OWNER_ID, session_id=SESSION_ID, limit=10, offset=0
    )
    assert total == 1 and [r.conversation_id for r in page] == [conv_id]


def _corrupt_metadata_and_get(store, field="created_at", conversation_id=None):
    conv_id = conversation_id or store.create_conversation(
        owner_id=OWNER_ID, session_id=SESSION_ID
    )
    store.add_turn(conv_id, "user", SENTINEL, owner_id=OWNER_ID, session_id=SESSION_ID)
    store.execution_db._conn.execute(
        f"UPDATE conversations SET {field} = 'not-a-date' "
        "WHERE conversation_id = ?",
        (conv_id,),
    )
    return conv_id


def test_corrupt_metadata_raises_on_every_data_serving_read():
    store = fresh_store()
    conv_id = _corrupt_metadata_and_get(store, "created_at")

    with pytest.raises(ConversationCorruptError):
        store.get_turn_count(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    with pytest.raises(ConversationCorruptError):
        store.get_history(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    with pytest.raises(ConversationCorruptError):
        store.add_turn(conv_id, "user", "next", owner_id=OWNER_ID, session_id=SESSION_ID)
    with pytest.raises(ConversationCorruptError):
        store.create_conversation(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    # A foreign identity still gets plain None (no damage intel leaked):
    assert store.get_turn_count(
        conv_id, owner_id=OTHER_OWNER_ID, session_id=SESSION_ID
    ) is None


def test_corrupt_updated_at_rejected_in_listing():
    store = fresh_store()
    store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    _corrupt_metadata_and_get(store, "updated_at")
    with pytest.raises(ConversationCorruptError):
        store.list_conversations_page(
            owner_id=OWNER_ID, session_id=SESSION_ID, limit=10
        )
    # The same identity's OTHER conversations are untouched, but listing
    # is strict: it never skips a damaged row.
    assert store.count_conversations(OWNER_ID, SESSION_ID) == 2


def test_delete_still_works_with_corrupt_metadata():
    """Clearing damaged rows stays possible; the id then comes back clean."""
    store = fresh_store()
    conv_id = _corrupt_metadata_and_get(store, "updated_at")
    with pytest.raises(ConversationCorruptError):
        store.get_turn_count(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)

    assert store.clear_conversation(
        conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    ) is True
    assert store.exists(conv_id) is False
    # Recreate: virgin timestamps, everything reads fine again.
    store.create_conversation(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
    assert (
        store.get_turn_count(conv_id, owner_id=OWNER_ID, session_id=SESSION_ID)
        == 0
    )


# --- ChatOrchestrator integration ---------------------------------------------


class _ScriptedProvider:
    """Minimal scripted provider recording every message list it sees."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_response(self, messages):
        from app.schemas.llm import LLMResponse

        self.calls.append(list(messages))
        if len(self.calls) > len(self._responses):
            raise AssertionError("unexpected extra provider call")
        return LLMResponse(
            content=self._responses[len(self.calls) - 1], model="scripted"
        )


DECISION_REPLY = '{"action": "reply", "reply": "Direct reply."}'


def _orchestrator_over(store, provider):
    from app.services.approval_store import ApprovalStore
    from app.services.approval_service import ApprovalService
    from app.services.plan_service import PlanService
    from app.services.tool_executor import ToolExecutor
    from app.services.tool_registry import tool_registry
    from app.services.orchestrator import ChatOrchestrator

    approvals = ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=ApprovalStore(),
    )
    orchestrator = ChatOrchestrator(
        provider_factory=lambda: provider,
        approvals=approvals,
        plans=PlanService(registry=tool_registry, approvals=approvals),
        registry=tool_registry,
        conversations=store,
    )
    return orchestrator, store


def test_orchestrator_persists_history_across_restarts(tmp_path):
    """Two orchestrator generations over one file-backed store replay the
    persisted history to the provider - persistence is real, not
    process-local."""
    path = str(tmp_path / "state.db")
    owner_a = OWNER_ID
    session_a = SESSION_ID

    conv_id = str(uuid4())
    gen1_db = ExecutionStateDb(path=path)
    provider1 = _ScriptedProvider([DECISION_REPLY, DECISION_REPLY])
    orchestrator1, _ = _orchestrator_over(
        PersistentConversationStore(state_db=gen1_db), provider1
    )
    orchestrator1.handle_chat_message(
        "Turn one", conv_id, owner_id=owner_a, session_id=session_a
    )
    assert len(provider1.calls) == 1
    seen_by_provider_1 = [message.content for message in provider1.calls[0]]
    assert "Turn one" in seen_by_provider_1
    gen1_db.close()

    # Restart: a fresh orchestrator over the same persisted conversation.
    gen2_db = ExecutionStateDb(path=path)
    try:
        orchestrator2, store2 = _orchestrator_over(
            PersistentConversationStore(state_db=gen2_db),
            _ScriptedProvider([DECISION_REPLY]),
        )
        orchestrator2.handle_chat_message(
            "Turn two", conv_id, owner_id=owner_a, session_id=session_a
        )
        history_seen = [
            message.content
            for message in store2.get_history(
                conv_id, owner_id=owner_a, session_id=session_a
            )
        ]
        assert "Turn one" in history_seen
        assert "Turn two" in history_seen
        assert gen2_db.get_conversation(conv_id).turn_count == 4
    finally:
        gen2_db.close()


def test_orchestrator_rejects_foreign_conversation_before_provider_call(tmp_path):
    path = str(tmp_path / "state.db")
    gen_db = ExecutionStateDb(path=path)
    try:
        provider = _ScriptedProvider([DECISION_REPLY])
        orchestrator, _ = _orchestrator_over(
            PersistentConversationStore(state_db=gen_db), provider
        )
        conv_id = str(uuid4())
        orchestrator.handle_chat_message(
            "Mine", conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
        )
        with pytest.raises(ConversationNotFoundError):
            orchestrator.handle_chat_message(
                "Hijack", conv_id,
                owner_id=OTHER_OWNER_ID, session_id=OTHER_SESSION_ID,
            )
        assert len(provider.calls) == 1
        # The hijack appended nothing foreign: the stored turns are only
        # the owner's user request and the assistant reply.
        record = gen_db.get_conversation(conv_id)
        assert (record.owner_id, record.session_id) == (OWNER_ID, SESSION_ID)
        turns = gen_db.list_conversation_messages(conv_id)
        assert [t.role for t in turns] == ["user", "assistant"]
    finally:
        gen_db.close()

