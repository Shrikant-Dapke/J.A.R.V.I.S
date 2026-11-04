"""Approval record integrity (Phase 5.4).

The database row's ``approval_id`` and its owner/session columns are
the ONLY authoritative record identity: every approval update targets
an explicitly trusted row id (never one derived from the JSON payload
being written), inserts are separate from updates (server-generated
id at creation), and every parsed payload must agree with the
persisted lifecycle columns before a record is read or modified —
corrupt state fails closed everywhere, is never repaired, and can
never steer a write (or purge) at another identity's row.

Tests here use isolated object graphs (``make_graph`` over in-memory
or file-backed façades) with controlled clocks; the API-level probes
monkeypatch an isolated graph onto the approval routes exactly like
``test_detail_corruption_api.py``.
"""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.services.approval_store import (
    ApprovalCorruptError,
    ApprovalStateError,
)
from app.services.execution_db import ExecutionIntegrityError, ExecutionStateDb
from tests._exec_helpers import make_graph, release_hanging_tools
from tests.conftest import OTHER_SESSION_ID as API_OTHER_SESSION
from tests.conftest import TEST_SESSION_ID as API_SESSION

BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)
IDENTITY_A = ("integrity-owner-A", "integrity-session-A-0123456789")
IDENTITY_B = ("integrity-owner-B", "integrity-session-B-0123456789")

FIXED_CORRUPT_MESSAGE = "Approval state could not be validated."
ARG_MARKER_A = "integrity-argument-A-do-not-leak-6f31c2"
ARG_MARKER_B = "integrity-argument-B-do-not-leak-91d7a5"


def _token_owner() -> str:
    """The owner identity every HTTP call authenticates as."""
    return hashlib.sha256(
        settings.api_token.get_secret_value().encode("utf-8")
    ).hexdigest()


@pytest.fixture
def hanging_cleanup():
    """Release every hung stub worker thread when the test ends."""
    yield
    release_hanging_tools()


def _headers_for(session_id: str, api_headers: dict) -> dict:
    """Same authenticated identity, different session header."""
    return {
        "Authorization": api_headers["Authorization"],
        "X-JARVIS-Session": session_id,
    }


def _controlled_clock():
    """A mutable clock the test advances explicitly."""
    return {"value": BASE}


def _raw_row(graph_db, approval_id: str) -> sqlite3.Row:
    return graph_db._conn.execute(
        "SELECT * FROM approval_requests WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()


def _flip_payload_id(graph_db, approval_id: str, new_id: str) -> None:
    """Corrupt one record's payload identity (simulated storage damage)."""
    row = graph_db.get_approval_row(approval_id)
    payload = json.loads(row.payload)
    payload["approval_id"] = new_id
    graph_db._conn.execute(
        "UPDATE approval_requests SET payload = ? WHERE approval_id = ?",
        (json.dumps(payload), approval_id),
    )


def _flip_payload_field(graph_db, approval_id: str, field: str, value) -> None:
    """Corrupt one duplicated metadata field inside a record's payload."""
    row = graph_db.get_approval_row(approval_id)
    payload = json.loads(row.payload)
    payload[field] = value
    graph_db._conn.execute(
        "UPDATE approval_requests SET payload = ? WHERE approval_id = ?",
        (json.dumps(payload), approval_id),
    )


def _minimal_request(approval_id: str) -> ApprovalRequest:
    """A valid pending request for façade-level probe rows."""
    return ApprovalRequest(
        approval_id=approval_id,
        tool_name="stub_mut_ok",
        arguments={"value": "probe"},
        action_description="façade-level integrity probe",
        risk_level="LOW_RISK",
        created_at=BASE,
        expires_at=BASE + timedelta(minutes=5),
    )


def _create_pair(graph):
    """Approval A (identity A) and approval B (identity B)."""
    owner_a, session_a = IDENTITY_A
    owner_b, session_b = IDENTITY_B
    a = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "A-SECRET-MARKER"},
        action_description="row A probe",
        risk_level="LOW_RISK",
        owner_id=owner_a,
        session_id=session_a,
    )
    b = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "B-SECRET-MARKER"},
        action_description="row B probe",
        risk_level="LOW_RISK",
        owner_id=owner_b,
        session_id=session_b,
    )
    return a, b


# --- Test 1: cross-identity overwrite reproduction -----------------------------


def test_cross_identity_payload_flips_fail_closed_and_leave_b_untouched():
    """The originally reported overwrite: A's corrupted payload id must
    fail every operation on A closed, and B's row must keep exactly its
    own payload, arguments, status transition, owner and session."""
    clock = _controlled_clock()
    graph = make_graph(clock=lambda: clock["value"], with_hanging=False)
    a, b = _create_pair(graph)

    _flip_payload_id(graph.db, a.approval_id, b.approval_id)
    clock["value"] = BASE + timedelta(minutes=10)  # past both TTLs

    with pytest.raises(ApprovalCorruptError):
        graph.store.get_approval_request(
            a.approval_id, IDENTITY_A[0], IDENTITY_A[1]
        )
    with pytest.raises(ApprovalCorruptError):
        graph.store.approve_request(
            a.approval_id, IDENTITY_A[0], IDENTITY_A[1]
        )
    with pytest.raises(ApprovalCorruptError):
        graph.store.reject_request(
            a.approval_id, IDENTITY_A[0], IDENTITY_A[1]
        )
    with pytest.raises(ApprovalCorruptError):
        graph.store.expire_request(
            a.approval_id, IDENTITY_A[0], IDENTITY_A[1]
        )

    # A's row was never repaired or transitioned in place.
    row_a = _raw_row(graph.db, a.approval_id)
    assert row_a["status"] == "PENDING"
    assert row_a["resolved_at"] is None
    assert json.loads(row_a["payload"])["approval_id"] == b.approval_id

    # B was lazily expired by its OWN legitimate lifecycle (columns and
    # payload agree), and its content is exactly as created.
    row_b = _raw_row(graph.db, b.approval_id)
    payload_b = json.loads(row_b["payload"])
    assert payload_b["arguments"] == {"value": "B-SECRET-MARKER"}
    assert payload_b["action_description"] == "row B probe"
    assert payload_b["approval_id"] == b.approval_id
    assert payload_b["status"] == row_b["status"] == "EXPIRED"
    assert row_b["owner_id"] == IDENTITY_B[0]
    assert row_b["session_id"] == IDENTITY_B[1]
    assert "A-SECRET-MARKER" not in row_b["payload"]


def test_facade_rejects_payload_id_mismatch_on_direct_update():
    """Façade level: an update whose payload id differs from the trusted
    row id is refused before any column is written."""
    db = ExecutionStateDb()
    trusted_id = "11111111-1111-4111-8111-111111111111"
    db.insert_approval(_minimal_request(trusted_id), "owner-x",
                       "session-x", "digest")

    forged = _minimal_request("22222222-2222-4222-8222-222222222222")
    with pytest.raises(ExecutionIntegrityError):
        db.save_approval(forged, trusted_id, "owner-x", "session-x", "digest")

    row = db.get_approval_record(trusted_id)
    assert row.request.approval_id == trusted_id
    assert row.request.status is ApprovalStatus.PENDING
    assert row.owner_id == "owner-x"


# --- Tests 2 and 3: owner / session mismatch -----------------------------------


@pytest.mark.parametrize("field", ["owner_id", "session_id"])
def test_payload_identity_fields_cannot_be_forged(field):
    """Owner and session are column-only: the schema forbids injecting
    the field into a payload, and a persisted payload that was tampered
    with anyway fails closed on read with no write and no effect on
    other identities."""
    # Structurally column-only: the payload schema rejects the field.
    assert field not in ApprovalRequest.model_fields

    clock = _controlled_clock()
    graph = make_graph(clock=lambda: clock["value"], with_hanging=False)
    own = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "own-probe"},
        action_description="identity probe (own)",
        risk_level="LOW_RISK",
        owner_id=IDENTITY_A[0],
        session_id=IDENTITY_A[1],
    )
    foreign = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "foreign-probe"},
        action_description="identity probe (foreign)",
        risk_level="LOW_RISK",
        owner_id=IDENTITY_B[0],
        session_id=IDENTITY_B[1],
    )

    _flip_payload_field(graph.db, own.approval_id, field,
                        "injected-foreign-identity")
    clock["value"] = BASE + timedelta(minutes=1)

    with pytest.raises(ApprovalCorruptError):
        graph.store.get_approval_request(
            own.approval_id, IDENTITY_A[0], IDENTITY_A[1]
        )
    with pytest.raises(ApprovalCorruptError):
        graph.store.approve_request(
            own.approval_id, IDENTITY_A[0], IDENTITY_A[1]
        )

    row_own = _raw_row(graph.db, own.approval_id)
    payload_own = json.loads(row_own["payload"])
    assert payload_own[field] == "injected-foreign-identity"  # no repair
    assert row_own["status"] == "PENDING"                    # no transition

    # Foreign identity untouched: columns intact, no injected value
    # ever migrated into the foreign row's payload.
    row_foreign = _raw_row(graph.db, foreign.approval_id)
    assert row_foreign["status"] == "PENDING"
    assert row_foreign["owner_id"] == IDENTITY_B[0]
    assert row_foreign["session_id"] == IDENTITY_B[1]
    assert "injected-foreign-identity" not in row_foreign["payload"]


def test_facade_update_validates_owner_and_session_columns():
    """Façade level: an update whose write context does not match the
    row's owner/session columns is refused before any write."""
    db = ExecutionStateDb()
    trusted_id = "11111111-1111-4111-8111-111111111111"
    db.insert_approval(_minimal_request(trusted_id), "owner-x",
                       "session-x", "digest")

    update = _minimal_request(trusted_id)
    with pytest.raises(ExecutionIntegrityError):
        db.save_approval(update, trusted_id, "wrong-owner", "session-x",
                         "digest")
    with pytest.raises(ExecutionIntegrityError):
        db.save_approval(update, trusted_id, "owner-x", "wrong-session",
                         "digest")
    row = db.get_approval_record(trusted_id)
    assert row.request.status is ApprovalStatus.PENDING
    assert row.owner_id == "owner-x"

    # Positive control: matching context writes, wrong id is still fatal.
    approved = update.model_copy(
        update={"status": ApprovalStatus.APPROVED, "resolved_at": None}
    )
    db.save_approval(approved, trusted_id, "owner-x", "session-x", "digest")
    assert db.get_approval_record(trusted_id).request.status is (
        ApprovalStatus.APPROVED
    )


# --- Test 4: lifecycle metadata mismatch ----------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "APPROVED"),
        ("created_at", "2099-01-01T00:00:00Z"),
        ("expires_at", "2099-01-01T00:00:00Z"),
        ("resolved_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_lifecycle_metadata_mismatch_fails_closed_without_repair(
    field, value
):
    """Each duplicated lifecycle field must agree with its authoritative
    column; a mismatched record is refused on read AND on transition,
    never silently repaired."""
    clock = _controlled_clock()
    graph = make_graph(clock=lambda: clock["value"], with_hanging=False)
    owner, session = IDENTITY_A
    created = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "meta-probe"},
        action_description="lifecycle probe",
        risk_level="LOW_RISK",
        owner_id=owner,
        session_id=session,
    )
    _flip_payload_field(graph.db, created.approval_id, field, value)
    clock["value"] = BASE + timedelta(minutes=10)

    with pytest.raises(ApprovalCorruptError):
        graph.store.get_approval_request(created.approval_id, owner, session)
    with pytest.raises(ApprovalCorruptError):
        graph.store.approve_request(created.approval_id, owner, session)

    row = _raw_row(graph.db, created.approval_id)
    payload = json.loads(row["payload"])
    assert payload[field] == value     # corrupt value NOT rewritten
    assert row["status"] == "PENDING"  # lifecycle untouched (fail closed)


# --- Test 5: cleanup safety ------------------------------------------------------


def test_cleanup_skips_corrupt_row_and_never_touches_foreign_rows():
    """Cleanup stays row-local: one corrupt payload identity is skipped
    and no foreign record's lifecycle is rewritten by it."""
    clock = _controlled_clock()
    graph = make_graph(clock=lambda: clock["value"], with_hanging=False)
    a, b = _create_pair(graph)
    tail = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "tail-probe"},
        action_description="healthy tail probe",
        risk_level="LOW_RISK",
        owner_id=IDENTITY_A[0],
        session_id=IDENTITY_A[1],
    )

    _flip_payload_id(graph.db, a.approval_id, b.approval_id)
    clock["value"] = BASE + timedelta(hours=1)
    graph.store.get_approval_request(
        b.approval_id, IDENTITY_B[0], IDENTITY_B[1]
    )  # triggers a scan over rows of every identity

    row_a = _raw_row(graph.db, a.approval_id)
    assert row_a["status"] == "PENDING"    # corrupt row skipped, not expired
    assert row_a["resolved_at"] is None
    assert row_a["purged"] == 0            # non-terminal corrupt row stays
    assert json.loads(row_a["payload"])["approval_id"] == b.approval_id

    # Every other row handled its own legitimate expiry independently.
    assert _raw_row(graph.db, b.approval_id)["status"] == "EXPIRED"
    assert _raw_row(graph.db, tail.approval_id)["status"] == "EXPIRED"


# --- Test 6: purge safety --------------------------------------------------------


def test_retention_purge_targets_only_the_intended_row():
    """The retention purge uses column identities: a terminal corrupt
    record purges exactly its own row and can never remove another
    approval via a payload-controlled id."""
    clock = _controlled_clock()
    graph = make_graph(clock=lambda: clock["value"], with_hanging=False)
    a, b = _create_pair(graph)
    victim_owner = "victim-owner"
    victim_session = "victim-session-0123456789"
    victim = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "victim-probe"},
        action_description="terminal victim probe",
        risk_level="LOW_RISK",
        owner_id=victim_owner,
        session_id=victim_session,
    )
    graph.store.reject_request(victim.approval_id, victim_owner,
                               victim_session)

    # The corrupt terminal record's payload id points at B; retention
    # must rely on the row's OWN column identity, not that payload id.
    _flip_payload_id(graph.db, victim.approval_id, b.approval_id)
    clock["value"] = BASE + timedelta(hours=2)
    graph.store.create_approval_request(  # scan trigger (fresh identity)
        tool_name="stub_ro",
        arguments={"value": "scan"},
        action_description="scan trigger",
        risk_level="READ_ONLY",
        owner_id="scan-owner",
        session_id="scan-session-0123456789",
    )

    row_victim = _raw_row(graph.db, victim.approval_id)
    assert row_victim["purged"] == 1            # exactly the terminal row
    row_victim_payload = json.loads(row_victim["payload"])
    assert row_victim_payload["approval_id"] == b.approval_id  # audit intact

    # A stayed non-terminal (its corrupt payload blocked its own expiry
    # row-locally), and B's retention clock runs separately.
    assert _raw_row(graph.db, a.approval_id)["purged"] == 0
    assert _raw_row(graph.db, b.approval_id)["purged"] == 0


def test_mark_approval_purged_rejects_identity_mismatch():
    """Façade level: the retention write validates owner/session columns
    against the scan's record before flagging anything."""
    db = ExecutionStateDb()
    request = _minimal_request("33333333-3333-4333-8333-333333333333")
    db.insert_approval(request, "purge-owner", "purge-session-0123", "digest")
    with pytest.raises(ExecutionIntegrityError):
        db.mark_approval_purged(
            request.approval_id, "wrong-owner", "purge-session-0123"
        )
    assert db.get_approval_record(request.approval_id).purged is False


# --- Test 7: concurrent update safety -------------------------------------------


def test_concurrent_transitions_keep_identity_invariants():
    """Two threads transition DIFFERENT approvals concurrently; every
    write keeps its own row identity and no row ever adopts foreign
    payload content."""
    clock = _controlled_clock()
    graph = make_graph(clock=lambda: clock["value"], with_hanging=False)
    a, b = _create_pair(graph)
    errors: list[Exception] = []
    barrier = threading.Barrier(2, timeout=5)

    def transition(request, identity, action):
        owner, session = identity
        try:
            barrier.wait()
            action(request.approval_id, owner, session)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread_a = threading.Thread(
        target=transition,
        args=(
            a,
            IDENTITY_A,
            lambda aid, o, s: graph.store.approve_request(aid, o, s),
        ),
    )
    thread_b = threading.Thread(
        target=transition,
        args=(
            b,
            IDENTITY_B,
            lambda aid, o, s: graph.store.reject_request(aid, o, s),
        ),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join()
    thread_b.join()

    assert errors == []

    row_a = _raw_row(graph.db, a.approval_id)
    row_b = _raw_row(graph.db, b.approval_id)
    payload_a = json.loads(row_a["payload"])
    payload_b = json.loads(row_b["payload"])
    assert payload_a["approval_id"] == a.approval_id
    assert payload_b["approval_id"] == b.approval_id
    assert row_a["owner_id"] == IDENTITY_A[0]
    assert row_b["owner_id"] == IDENTITY_B[0]
    assert row_a["session_id"] == IDENTITY_A[1]
    assert row_b["session_id"] == IDENTITY_B[1]
    assert row_a["status"] == payload_a["status"] == "APPROVED"
    assert row_b["status"] == payload_b["status"] == "REJECTED"


# --- Test 8: normal lifecycle regression -----------------------------------------


def test_normal_lifecycle_still_works_end_to_end(hanging_cleanup):
    """Creation, approval, rejection, expiry, claim, execution and
    uncertain-recovery behavior are unchanged under the integrity
    guard."""
    clock = _controlled_clock()
    graph = make_graph(
        clock=lambda: clock["value"], with_hanging=True, timeout_seconds=0.05
    )
    owner, session = IDENTITY_A

    def _new_request(description):
        return graph.store.create_approval_request(
            tool_name="stub_mut_ok",
            arguments={"value": "lifecycle"},
            action_description=description,
            risk_level="LOW_RISK",
            owner_id=owner,
            session_id=session,
        )

    created = _new_request("approval regression probe")
    approved = graph.store.approve_request(created.approval_id, owner, session)
    assert approved.status is ApprovalStatus.APPROVED

    rejected = _new_request("rejection regression probe")
    rejected = graph.store.reject_request(rejected.approval_id, owner, session)
    assert rejected.status is ApprovalStatus.REJECTED

    # Single-use claim: exactly once (while still within TTL).
    consumed = graph.store.claim_approved_for_execution(
        created.approval_id, owner, session
    )
    assert consumed.status is ApprovalStatus.EXECUTED
    with pytest.raises(ApprovalStateError):
        graph.store.claim_approved_for_execution(
            created.approval_id, owner, session
        )

    expiring = _new_request("expiry regression probe")
    clock["value"] = BASE + timedelta(minutes=10)
    view = graph.store.get_approval_request(
        expiring.approval_id, owner, session
    )
    assert view.status is ApprovalStatus.EXPIRED

    # Uncertain (timed-out) mutation stays honest and reconciles
    # explicitly without any fabricated outcome.
    hang = graph.store.create_approval_request(
        tool_name="stub_mut_hang",
        arguments={"value": "lifecycle"},
        action_description="uncertain recovery probe",
        risk_level="LOW_RISK",
        owner_id=owner,
        session_id=session,
    )
    result = graph.approvals.approve_request(hang.approval_id, owner, session)
    assert result.success is False
    assert result.error_code == "execution_timeout"
    assert graph.approvals.reconcile_uncertain(
        hang.approval_id, owner, session
    ) is True
    assert graph.approvals.reconcile_uncertain(
        hang.approval_id, owner, session
    ) is False  # no double reconciliation


# --- Test 9: restart persistence --------------------------------------------------


def test_corrupt_and_healthy_records_survive_restart(tmp_path):
    """A file-backed database reopened from disk keeps failing closed on
    the corrupted record and keeps serving the healthy one unchanged."""
    clock = _controlled_clock()
    path = str(tmp_path / "integrity-restart.db")
    creator = make_graph(
        clock=lambda: clock["value"],
        state_db=ExecutionStateDb(path=path),
        with_hanging=False,
    )
    a, b = _create_pair(creator)
    approved = creator.store.approve_request(
        b.approval_id, IDENTITY_B[0], IDENTITY_B[1]
    )
    assert approved.status is ApprovalStatus.APPROVED
    _flip_payload_id(creator.db, a.approval_id, b.approval_id)
    creator.db.close()

    reopened = make_graph(
        clock=lambda: clock["value"],
        state_db=ExecutionStateDb(path=path),
        with_hanging=False,
    )
    with pytest.raises(ApprovalCorruptError):
        reopened.store.get_approval_request(
            a.approval_id, IDENTITY_A[0], IDENTITY_A[1]
        )
    restored = reopened.store.get_approval_request(
        b.approval_id, IDENTITY_B[0], IDENTITY_B[1]
    )
    assert restored.status is ApprovalStatus.APPROVED
    assert restored.approval_id == b.approval_id
    assert restored.arguments == {"value": "B-SECRET-MARKER"}
    # The corrupt row did not gain a lifecycle rewrite while closed.
    row_a = _raw_row(reopened.db, a.approval_id)
    assert row_a["status"] == "PENDING"
    assert row_a["resolved_at"] is None


# --- Test 10: API-level protection -------------------------------------------------


@pytest.fixture
def isolated_approvals(monkeypatch):
    """Route the approval API onto an isolated object graph."""
    graph = make_graph()
    monkeypatch.setattr("app.api.approvals.approval_service", graph.approvals)
    return graph


@pytest.mark.asyncio
async def test_corrupt_own_record_and_foreign_isolation_via_api(
    isolated_approvals, api_headers
):
    """The owning identity's corrupt record answers the fixed safe
    corruption response; foreign identities observe not-found — and no
    payload ids, arguments, digests, owner or session ids ever leak."""
    graph = isolated_approvals
    owner = _token_owner()
    damaged = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": ARG_MARKER_A},
        action_description="api corruption probe",
        risk_level="LOW_RISK",
        owner_id=owner,
        session_id=API_SESSION,
    )
    _flip_payload_id(graph.db, damaged.approval_id,
                     "44444444-4444-4444-8444-444444444444")

    foreign = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": ARG_MARKER_B},
        action_description="api foreign probe",
        risk_level="LOW_RISK",
        owner_id=owner,
        session_id=API_OTHER_SESSION,
    )

    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://127.0.0.1"
    ) as client:
        corrupt = await client.get(
            f"/api/approvals/{damaged.approval_id}",
            headers=_headers_for(API_SESSION, api_headers),
        )
        foreign_read = await client.get(
            f"/api/approvals/{foreign.approval_id}",
            headers=_headers_for(API_SESSION, api_headers),
        )
        foreign_actions = [
            await client.post(
                f"/api/approvals/{foreign.approval_id}/{action}",
                headers=_headers_for(API_SESSION, api_headers),
            )
            for action in ("approve", "reject")
        ]

    assert corrupt.status_code == 500
    body = corrupt.json()
    assert body["error"]["code"] == "HTTP_500"
    assert body["error"]["message"] == FIXED_CORRUPT_MESSAGE
    assert body["error"]["request_id"]
    assert "44444444" not in corrupt.text          # no payload ids
    assert ARG_MARKER_A not in corrupt.text        # no arguments
    assert "digest" not in corrupt.text
    assert owner not in corrupt.text               # no owner ids
    assert API_SESSION not in corrupt.text         # no session ids

    assert foreign_read.status_code == 404
    assert "Approval request was not found" in foreign_read.text
    assert ARG_MARKER_B not in foreign_read.text
    for response in foreign_actions:
        assert response.status_code == 404
        assert "Approval request was not found" in response.text
