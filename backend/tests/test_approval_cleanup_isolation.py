"""Cleanup-scan corruption isolation (Phase 5.3, FIX A).

The maintenance scan decides expiry and retention from the persisted
metadata columns only and handles each row locally: one record with
invalid metadata or a damaged payload is skipped (logged, lifecycle
untouched) instead of aborting the scan. A corrupt approval of one
identity therefore cannot poison listings, detail reads, or creation
for unrelated identities, and healthy TTL/retention semantics,
single-use claims, lifecycle transitions, and the expiry audit event
are preserved exactly.

API tests here use throwaway session identities over the shared state
database and retire self-created rows via the purged flag in teardown
(fail-closed discipline: the suite never leaves damaged payloads in
the shared database for other suites to read). Store-level lifecycle
probing runs on isolated object graphs (``make_graph``).
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.schemas.approval import ApprovalStatus
from app.services.approval_store import (
    ApprovalCorruptError,
    ApprovalNotFoundError,
    ApprovalStateError,
)
from app.services.execution_db import ExecutionStateDb
from app.services.plan_service import plan_service
from tests._exec_helpers import OWNER_ID as GRAPH_OWNER
from tests._exec_helpers import SESSION_ID as GRAPH_SESSION, make_graph

ISOLATION_DAMAGE_SESSION = "approvals-cleanup-damage-ses-012345678"
ISOLATION_HEALTHY_SESSION = "approvals-cleanup-healthy-se-01234567"
ISOLATION_FOREIGN_SESSION = "approvals-cleanup-foreign-se-01234567"

# Payload damage marker: must never appear in any error response.
DAMAGE_MARKER = "cleanup-isolation-payload-damage-marker-92741"
DAMAGED_PAYLOAD = '{"tool_name": "cleanup-isolation-payload-damage-' \
                  'marker-92741", "args": [nope'
ARG_MARKER = "cleanup-argument-marker-do-not-leak"
# Store-level bind for isolated graphs (identity for detail assertions).
# The real open_application tool accepts [a-z0-9_-] app ids only, so the
# HTTP creation tests bind this app_id-valid arg value instead.
API_ARG_MARKER = "cleanup-arg-not-leak-32741"


def _token_owner() -> str:
    """The owner identity every HTTP call authenticates as."""
    return hashlib.sha256(
        settings.api_token.get_secret_value().encode("utf-8")
    ).hexdigest()


def _headers_for(session_id: str, api_headers: dict) -> dict:
    """Same authenticated identity, different session header."""
    return {
        "Authorization": api_headers["Authorization"],
        "X-JARVIS-Session": session_id,
    }


def _retire(graphdb, approval_id: str) -> None:
    """Mark a self-created test row purged (metadata-only teardown).

    Purged rows are excluded from every gated scan, listing, and detail
    read, so the shared database stays clean for later suites; this is
    exactly the retention write production performs.
    """
    graphdb._conn.execute(
        "UPDATE approval_requests SET purged = 1 WHERE approval_id = ?",
        (approval_id,),
    )


def _corrupt_payload(graph_db, approval_id: str) -> None:
    """Persistently damage one approval row's payload in its graph."""
    graph_db._conn.execute(
        f"UPDATE approval_requests SET payload = '{DAMAGED_PAYLOAD}' "
        "WHERE approval_id = ?",
        (approval_id,),
    )


def _set_columns(graph_db, approval_id: str, **columns) -> None:
    """Overwrite persisted metadata columns (whitelisted identifiers)."""
    allowed = {"status", "created_at", "expires_at", "resolved_at",
               "purged"}
    for column, value in columns.items():
        assert column in allowed
        graph_db._conn.execute(
            f"UPDATE approval_requests SET {column} = ? "
            "WHERE approval_id = ?",
            (value, approval_id),
        )


def _row(graph_db, approval_id: str):
    """Raw persisted row (payload UNPARSED) for lifecycle assertions."""
    row = graph_db.get_approval_row(approval_id)
    assert row is not None
    return row


# --- API level: cross-identity isolation over the shared database ------------


@pytest.mark.asyncio
async def test_corrupt_identity_does_not_break_other_identity_listing(
    api_headers
):
    """Identity A's corrupt row leaves identity B's inventory served."""
    db = plan_service.execution_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=_headers_for(ISOLATION_DAMAGE_SESSION, api_headers),
    ) as damaged:
        damage_pending = await damaged.post(
            "/api/tools/open_application/invoke",
            json={"arguments": {"app_id": "notepad"}},
        )
    assert damage_pending.status_code == 202
    damage_id = damage_pending.json()["approval_id"]
    _corrupt_payload(db, damage_id)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_HEALTHY_SESSION, api_headers),
        ) as healthy_session:
            response = await healthy_session.get("/api/approvals?limit=200")
    finally:
        _retire(db, damage_id)

    assert response.status_code == 200
    data = response.json()
    assert data["approvals"] == [] and data["total"] == 0
    # Other identities' rows never appear in B's inventory.
    assert DAMAGE_MARKER not in response.text
    assert damage_id not in response.text


@pytest.mark.asyncio
async def test_corrupt_identity_does_not_break_other_identity_detail(
    api_headers
):
    """Identity B's own detail read stays fully served with A corrupt."""
    db = plan_service.execution_db
    damage_id = healthy_id = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_DAMAGE_SESSION, api_headers),
        ) as damaged:
            damage_pending = await damaged.post(
                "/api/tools/open_application/invoke",
                json={"arguments": {"app_id": "notepad"}},
            )
            assert damage_pending.status_code == 202
            damage_id = damage_pending.json()["approval_id"]
        _corrupt_payload(db, damage_id)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_HEALTHY_SESSION, api_headers),
        ) as healthy_session:
            healthy_pending = await healthy_session.post(
                "/api/tools/open_application/invoke",
                json={"arguments": {"app_id": "notepad"}},
            )
            assert healthy_pending.status_code == 202
            healthy_id = healthy_pending.json()["approval_id"]
            detail = await healthy_session.get(
                f"/api/approvals/{healthy_id}"
            )
    finally:
        if damage_id:
            _retire(db, damage_id)
        if healthy_id:
            _retire(db, healthy_id)

    assert detail.status_code == 200
    assert detail.json()["status"] == "PENDING"
    assert detail.json()["arguments"] == {"app_id": "notepad"}


@pytest.mark.asyncio
async def test_corrupt_identity_does_not_break_other_identity_creation(
    api_headers
):
    """A corrupt sibling row cannot abort identity B's create path.

    Creation runs the maintenance scan first; the scan previously
    parsed and raised on the corrupt payload, so B's creation 500ed.
    Phase 5.3: the scan skips the damaged row locally and B's request
    is created and served normally.
    """
    db = plan_service.execution_db
    damage_id = created_id = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_DAMAGE_SESSION, api_headers),
        ) as damaged:
            damage_pending = await damaged.post(
                "/api/tools/open_application/invoke",
                json={"arguments": {"app_id": "notepad"}},
            )
            assert damage_pending.status_code == 202
            damage_id = damage_pending.json()["approval_id"]
        _corrupt_payload(db, damage_id)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_HEALTHY_SESSION, api_headers),
        ) as healthy_session:
            created = await healthy_session.post(
                "/api/tools/open_application/invoke",
                json={"arguments": {"app_id": API_ARG_MARKER}},
            )
            assert created.status_code == 202
            created_id = created.json()["approval_id"]
            assert created_id != damage_id

            # The freshly created record reads back healthy end to end.
            detail = await healthy_session.get(
                f"/api/approvals/{created_id}"
            )
    finally:
        if damage_id:
            _retire(db, damage_id)
        if created_id:
            _retire(db, created_id)

    assert detail.status_code == 200
    assert detail.json()["status"] == "PENDING"
    assert detail.json()["arguments"] == {"app_id": API_ARG_MARKER}


@pytest.mark.asyncio
async def test_corrupt_row_fails_safely_for_its_own_identity(api_headers):
    """The damaged identity's own detail read fail closes, fixed 500."""
    db = plan_service.execution_db
    damage_id = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_DAMAGE_SESSION, api_headers),
        ) as damaged:
            pending = await damaged.post(
                "/api/tools/open_application/invoke",
                json={"arguments": {"app_id": "notepad"}},
            )
            assert pending.status_code == 202
            damage_id = pending.json()["approval_id"]
        _corrupt_payload(db, damage_id)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_DAMAGE_SESSION, api_headers),
        ) as damaged:
            response = await damaged.get(f"/api/approvals/{damage_id}")
    finally:
        if damage_id:
            _retire(db, damage_id)

    assert response.status_code == 500
    data = response.json()
    assert data["error"]["code"] == "HTTP_500"
    assert data["error"]["message"] == (
        "Approval state could not be validated."
    )
    assert data["error"]["request_id"]
    # No payload content, arguments, digests, or internals leak.
    assert DAMAGE_MARKER not in response.text
    assert "nope" not in response.text
    assert "digest" not in response.text
    assert "owner_id" not in response.text
    assert "session_id" not in response.text


@pytest.mark.asyncio
async def test_corrupt_foreign_row_stays_not_found(api_headers):
    """A foreign identity reads A's corrupt row as any other: 404."""
    db = plan_service.execution_db
    damage_id = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_DAMAGE_SESSION, api_headers),
        ) as damaged:
            pending = await damaged.post(
                "/api/tools/open_application/invoke",
                json={"arguments": {"app_id": "notepad"}},
            )
            assert pending.status_code == 202
            damage_id = pending.json()["approval_id"]
        _corrupt_payload(db, damage_id)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(ISOLATION_FOREIGN_SESSION, api_headers),
        ) as foreign:
            foreign_read = await foreign.get(
                f"/api/approvals/{damage_id}"
            )
    finally:
        if damage_id:
            _retire(db, damage_id)

    assert foreign_read.status_code == 404
    assert "Approval request was not found" in foreign_read.text
    assert DAMAGE_MARKER not in foreign_read.text
    assert "owner_id" not in foreign_read.text


# --- store level: row-local policy on isolated object graphs -----------------


def _controlled_clock(base: datetime):
    now = {"value": base}

    def clock() -> datetime:
        return now["value"]

    return now, clock


def _trigger_scan(graph):
    """Creation runs ``_cleanup`` pre-insert: the scan trigger."""
    return graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": ARG_MARKER},
        action_description="cleanup scan trigger probe",
        risk_level="LOW_RISK",
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )


def test_expired_corrupt_row_is_skipped_without_lifecycle_change():
    """Past-TTL corrupt payload: untouched row, no invented expiry.

    The healthy sibling expires exactly per TTL (status EXPIRED with
    the scan-time resolved_at and the expiry audit event), while the
    corrupt row keeps every persisted byte: status still PENDING,
    resolved_at still None, never purged, payload never rewritten.
    """
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    now, clock = _controlled_clock(base)
    graph = make_graph(clock=clock, with_hanging=False)

    stale = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "stale"},
        action_description="scan expiry probe",
        risk_level="LOW_RISK",
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    corrupt = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": ARG_MARKER},
        action_description="scan skip probe",
        risk_level="LOW_RISK",
        owner_id="foreign-owner",
        session_id="foreign-session",
    )
    _corrupt_payload(graph.db, corrupt.approval_id)
    damaged_before = _row(graph.db, corrupt.approval_id).payload

    now["value"] = base + timedelta(minutes=10)
    watcher = _trigger_scan(graph)  # scan runs under this clock

    # The healthy stale row expired exactly per TTL: the SCAN did it
    # (no owned read happened before this point). The absolute TTL
    # deadline is untouched by the expiry rewrite.
    expired_row = _row(graph.db, stale.approval_id)
    assert expired_row.status == "EXPIRED"
    assert expired_row.resolved_at == now["value"].isoformat()
    assert expired_row.expires_at == (
        base + timedelta(minutes=5)
    ).isoformat()
    assert expired_row.purged == 0

    # The corrupt row keeps its lifecycle untouched.
    corrupt_row = _row(graph.db, corrupt.approval_id)
    assert corrupt_row.status == "PENDING"
    assert corrupt_row.resolved_at is None
    assert corrupt_row.purged == 0
    assert corrupt_row.payload == damaged_before

    # The scan completed: the probe created and served normally.
    assert watcher.status is ApprovalStatus.PENDING

    # Audit: exactly one expiry event, for the healthy row only.
    expiries = [
        event for event in graph.db.recent_events(limit=100)
        if event["event"] == "approval_expired"
    ]
    assert [e["detail"]["approval_id"] for e in expiries] == [
        stale.approval_id
    ]

    # The damaging identity still fails closed on its own record.
    with pytest.raises(ApprovalCorruptError):
        graph.store.get_approval_request(
            corrupt.approval_id, "foreign-owner", "foreign-session"
        )
    # ...while unrelated identities keep working.
    viewed = graph.store.get_approval_request(
        watcher.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    assert viewed.status is ApprovalStatus.PENDING


def test_invalid_status_column_is_skipped_without_lifecycle_change():
    """An unparsable status column never flips a row's lifecycle."""
    graph = make_graph(with_hanging=False)
    odd = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "odd"},
        action_description="invalid status probe",
        risk_level="LOW_RISK",
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    _set_columns(
        graph.db, odd.approval_id, status="SIDEWAYS", resolved_at=None,
        purged=0,
    )
    before = _row(graph.db, odd.approval_id)
    sibling = _trigger_scan(graph)  # scan completed despite the odd row

    after = _row(graph.db, odd.approval_id)
    assert after.status == "SIDEWAYS"
    assert after.resolved_at is None
    assert after.purged == 0
    assert after.payload == before.payload
    assert after.expires_at == before.expires_at
    assert sibling.status is ApprovalStatus.PENDING
    # No lifecycle event was invented for the skipped row.
    events = graph.db.recent_events(limit=100)
    assert [
        e["detail"].get("approval_id") for e in events
        if e["event"] == "approval.expired"
    ] == []


def test_corrupt_terminal_row_still_purges_by_retention():
    """Retention purging is metadata-only: corrupt terminal rows leave
    capacity without their payload ever being parsed."""
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    now, clock = _controlled_clock(base)
    graph = make_graph(clock=clock, with_hanging=False)

    terminal = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": ARG_MARKER},
        action_description="corrupt terminal probe",
        risk_level="LOW_RISK",
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    graph.store.reject_request(
        terminal.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    _corrupt_payload(graph.db, terminal.approval_id)
    assert _row(graph.db, terminal.approval_id).purged == 0

    now["value"] = base + timedelta(seconds=61)  # 60s retention + epoch
    watcher = _trigger_scan(graph)

    assert _row(graph.db, terminal.approval_id).purged == 1
    # A purged row answers not-found thereafter and leaves capacity.
    with pytest.raises(ApprovalNotFoundError):
        graph.store.get_approval_request(
            terminal.approval_id, GRAPH_OWNER, GRAPH_SESSION
        )
    assert watcher.status is ApprovalStatus.PENDING


def test_terminal_row_with_invalid_resolved_at_is_not_purged():
    """An unparsable resolution stamp blocks retention purging: no
    invented timestamps, no invented purge."""
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    now, clock = _controlled_clock(base)
    graph = make_graph(clock=clock, with_hanging=False)

    terminal = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "terminal"},
        action_description="invalid resolved_at probe",
        risk_level="LOW_RISK",
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    graph.store.reject_request(
        terminal.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    _set_columns(graph.db, terminal.approval_id, resolved_at="not-a-timestamp")

    now["value"] = base + timedelta(hours=1)
    _trigger_scan(graph)

    row = _row(graph.db, terminal.approval_id)
    assert row.purged == 0
    assert row.status == "REJECTED"
    assert row.resolved_at == "not-a-timestamp"  # never rewritten
    # Phase 5.4: a record whose duplicated lifecycle metadata disagrees
    # with its columns also fails closed on reads — never served, never
    # repaired. Retention still leaves the inconsistent terminal row
    # unpurged (verified raw above).
    with pytest.raises(ApprovalCorruptError):
        graph.store.get_approval_request(
            terminal.approval_id, GRAPH_OWNER, GRAPH_SESSION
        )


def test_single_use_claim_semantics_preserved_with_corrupt_sibling():
    """Single-use claims and lifecycle transitions are unchanged."""
    graph = make_graph(with_hanging=False)
    corrupt = graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": ARG_MARKER},
        action_description="lifecycle sibling damage",
        risk_level="LOW_RISK",
        owner_id="foreign-owner",
        session_id="foreign-session",
    )
    _corrupt_payload(graph.db, corrupt.approval_id)

    request = _trigger_scan(graph)
    assert request.status is ApprovalStatus.PENDING
    approved = graph.store.approve_request(
        request.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    assert approved.status is ApprovalStatus.APPROVED
    with pytest.raises(ApprovalStateError):
        graph.store.approve_request(
            request.approval_id, GRAPH_OWNER, GRAPH_SESSION
        )  # single-use: approving twice is never possible
    claimed = graph.store.claim_approved_for_execution(
        request.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    assert claimed.status is ApprovalStatus.EXECUTED
    with pytest.raises(ApprovalStateError):
        graph.store.claim_approved_for_execution(
            request.approval_id, GRAPH_OWNER, GRAPH_SESSION
        )
    # The corrupt sibling never participated in the lifecycle above.
    assert _row(graph.db, corrupt.approval_id).purged == 0


def test_isolated_graph_state_db_is_not_shared():
    """Sanity: isolated graphs never leak into the shared database."""
    graph = make_graph(with_hanging=False)
    created = _trigger_scan(graph)
    assert plan_service.execution_db.get_approval_record(
        created.approval_id
    ) is None
    assert isinstance(graph.db, ExecutionStateDb)
