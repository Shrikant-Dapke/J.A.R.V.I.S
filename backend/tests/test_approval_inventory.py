"""Approval inventory (Phase 5.2-b): read-only listing with identity
scoping, deterministic ordering, one-transaction page+total pairing,
verbatim (advisory) lifecycle statuses, structural exclusion of raw
arguments/digests/retention flags, and strict fail-closed handling of
corrupted approval payloads.

Object graphs for store-level tests are isolated (make_graph over an
in-memory façade, matching the Phase 4.3 test pattern). API tests run
over the shared state DB the whole suite uses, with dedicated throwaway
session identities for deterministic totals and corruption damage —
a damaged approval row is session-local and never poisons other tests.
"""

import dataclasses
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.schemas.approval import ApprovalStatus
from app.schemas.policy import RiskLevel
from app.services.approval_store import (
    ApprovalCorruptError,
    ApprovalNotFoundError,
    ApprovalStore,
)
from app.services.execution_db import ExecutionStateDb
from app.services.plan_service import plan_service
from tests._exec_helpers import (
    OWNER_ID as GRAPH_OWNER,
    SESSION_ID as GRAPH_SESSION,
    make_graph,
)


EMPTY_APPROVALS_SESSION = "approvals-empty-session-01234567890"
OTHER_APPROVALS_SESSION = "approvals-other-session-012345678901"
CORRUPT_APPROVALS_SESSION = "approvals-corrupt-session-0123456789"
META_APPROVALS_SESSION = "approvals-meta-session-0123456789012"
PAGE_APPROVALS_SESSION = "approvals-page-session-0123456789012"
MAIN_SESSION = "test-session-012345678901234567"

# A value bound to created approvals that must NEVER appear in a listing.
ARG_MARKER = "argument-marker-do-not-list-9f31c2"
FORBIDDEN_LISTING_KEYS = ('"arguments"', '"digest"', '"purged"', '"owner_id"',
                          '"session_id"')


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


def _create(
    store: ApprovalStore,
    *,
    owner: str = GRAPH_OWNER,
    session: str = GRAPH_SESSION,
    tool_name: str = "stub_mut_ok",
    risk: RiskLevel = RiskLevel.LOW_RISK,
    args: dict | None = None,
    description: str | None = None,
):
    return store.create_approval_request(
        tool_name=tool_name,
        arguments=args if args is not None else {"value": ARG_MARKER},
        action_description=description or f"{tool_name}: guarded action",
        risk_level=risk,
        owner_id=owner,
        session_id=session,
    )


def _summary_strings(view) -> str:
    """Full text of one summary view, for marker leak assertions."""
    return json.dumps(
        dataclasses.asdict(view), default=str, ensure_ascii=False
    )


# --- store level (isolated object graphs) --------------------------------------


def test_approval_page_pairs_page_with_total():
    store = make_graph().store
    made = [_create(store) for _ in range(5)]
    views, total = store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=2, offset=1
    )
    # created_at DESC ordering: newest first, offset 1 skips the newest.
    assert [v.approval_id for v in views] == [
        made[3].approval_id,
        made[2].approval_id,
    ]
    assert total == 5
    # Deterministic: an identical read returns an identical page.
    again, again_total = store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=2, offset=1
    )
    assert [_summary_strings(v) for v in again] == [
        _summary_strings(v) for v in views
    ]
    assert again_total == total
    # Foreign identity observes an empty page, not foreign rows.
    assert store.list_approvals_page("foreign-owner", GRAPH_SESSION, limit=5) == (
        [],
        0,
    )


def test_approval_page_is_identity_scoped():
    store = make_graph().store
    own = [_create(store, description=f"own {i}") for i in range(2)]
    foreign_owner_other_session = _create(
        store,
        owner="other-owner",
        session="other-session",
        description="foreign elsewhere",
    )
    same_owner_other_session = _create(
        store,
        owner=GRAPH_OWNER,
        session="unshared-session",
        description="cross A: same owner, other session",
    )
    other_owner_same_session = _create(
        store,
        owner="third-owner",
        session=GRAPH_SESSION,
        description="cross B: other owner, same session",
    )

    views, total = store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert {v.approval_id for v in views} == {
        a.approval_id for a in own
    }
    assert total == 2

    # Owner matches but session differs: exactly the cross-A row.
    views_a, total_a = store.list_approvals_page(
        GRAPH_OWNER, "unshared-session", limit=10
    )
    assert [v.approval_id for v in views_a] == [
        same_owner_other_session.approval_id
    ]
    assert total_a == 1

    # Session matches but owner differs: exactly the cross-B row.
    views_b, total_b = store.list_approvals_page(
        "third-owner", GRAPH_SESSION, limit=10
    )
    assert [v.approval_id for v in views_b] == [
        other_owner_same_session.approval_id
    ]
    assert total_b == 1

    # Fully foreign identity: exactly the foreign row.
    other_views, other_total = store.list_approvals_page(
        "other-owner", "other-session", limit=10
    )
    assert [v.approval_id for v in other_views] == [
        foreign_owner_other_session.approval_id
    ]
    assert other_total == 1

    # Wrong session combination: empty, never foreign rows.
    assert store.list_approvals_page(GRAPH_OWNER, "other-session", limit=10) == (
        [],
        0,
    )


def test_listing_order_tiebreak_is_deterministic():
    """Equal created_at falls back to approval_id DESC determinism.

    The equal stamps come from a controlled creation clock, so the
    duplicated metadata agrees between columns and payloads (Phase 5.4
    integrity) instead of being forged on healthy rows.
    """
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    graph = make_graph(clock=lambda: base, with_hanging=False)
    made = [_create(graph.store, description=f"tie {i}") for i in range(3)]
    views, total = graph.store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert [v.approval_id for v in views] == sorted(
        [a.approval_id for a in made], reverse=True
    )
    assert total == 3
    again, _ = graph.store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert [v.approval_id for v in again] == [v.approval_id for v in views]


def test_out_of_bounds_arguments_fail_closed():
    store = make_graph().store
    with pytest.raises(ValueError):
        store.list_approvals_page(GRAPH_OWNER, GRAPH_SESSION, limit=0)
    with pytest.raises(ValueError):
        store.list_approvals_page(
            GRAPH_OWNER, GRAPH_SESSION, limit=5, offset=-1
        )


def test_summary_view_strips_arguments_digest_and_identity():
    store = make_graph().store
    created = _create(
        store,
        risk=RiskLevel.HIGH_RISK,
    )
    views, _ = store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert len(views) == 1
    view = views[0]
    assert set(dataclasses.asdict(view).keys()) == {
        "approval_id",
        "tool_name",
        "action_description",
        "risk_level",
        "status",
        "created_at",
        "expires_at",
        "resolved_at",
    }
    assert view.approval_id == created.approval_id
    assert view.tool_name == "stub_mut_ok"
    assert view.status is ApprovalStatus.PENDING
    assert view.risk_level is RiskLevel.HIGH_RISK
    assert view.resolved_at is None
    # Raw arguments are structurally absent, never merely renamed.
    text = _summary_strings(view)
    assert ARG_MARKER not in text
    assert "arguments" not in text
    # The digest column never reaches summary views either.
    plaintext_args = json.dumps({"value": ARG_MARKER})
    assert plaintext_args not in text


def test_corrupt_payload_fails_closed_strictly():
    """A damaged payload raises; nothing is skipped, repaired, or leaked."""
    graph = make_graph()
    healthy = _create(
        graph.store,
        owner="healthy-owner",
        session="healthy-session",
        description="stays healthy",
    )
    damaged = _create(graph.store, description="internal-damage-marker")
    graph.db._conn.execute(
        "UPDATE approval_requests SET payload = '{\"tool_name\": 42, [nope' "
        "WHERE approval_id = ?",
        (damaged.approval_id,),
    )

    with pytest.raises(ApprovalCorruptError) as excinfo:
        graph.store.list_approvals_page(GRAPH_OWNER, GRAPH_SESSION, limit=10)
    assert str(excinfo.value) == "Approval state could not be validated."
    assert "internal-damage-marker" not in str(excinfo.value)

    # The damaged row still exists in its own identity's space (COUNT is
    # raw); the unaffected identity's listing is fully served.
    views, total = graph.store.list_approvals_page(
        "healthy-owner", "healthy-session", limit=10
    )
    assert [v.approval_id for v in views] == [healthy.approval_id]
    assert total == 1


def test_listing_is_read_only_and_never_lazy_expires():
    """Listing reports the persisted status verbatim and mutates nothing;
    lazy expiration belongs to the authoritative detail path only."""
    now = {"value": datetime(2030, 1, 1, tzinfo=timezone.utc)}

    def clock() -> datetime:
        return now["value"]

    graph_store = ApprovalStore(clock=clock, state_db=ExecutionStateDb())
    created = _create(graph_store, description="clocked pending")

    views, total = graph_store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert total == 1 and views[0].status is ApprovalStatus.PENDING

    # Advance far past the 5-minute TTL: the listing stays verbatim.
    now["value"] = now["value"] + timedelta(hours=1)
    views, total = graph_store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert total == 1 and views[0].status is ApprovalStatus.PENDING

    record = graph_store.execution_db.get_approval_record(created.approval_id)
    assert record.request.status is ApprovalStatus.PENDING
    assert record.purged is False
    assert record.resolved_at is None  # column: never resolved by listing
    assert record.request.resolved_at is None  # payload untouched by listing

    # The authoritative read path, in contrast, applies lazy expiration.
    viewed = graph_store.get_approval_request(
        created.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    assert viewed.status is ApprovalStatus.EXPIRED

    # And a later listing reflects the persisted (now authoritative) state.
    views, total = graph_store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert total == 1 and views[0].status is ApprovalStatus.EXPIRED


def test_listing_does_not_advance_retention_or_purge():
    """Terminal records are purged only by cleanup reads, never listing;
    a purged row leaves the listing exactly as it leaves the detail 404."""
    now = {"value": datetime(2030, 1, 1, tzinfo=timezone.utc)}

    def clock() -> datetime:
        return now["value"]

    store = ApprovalStore(clock=clock, state_db=ExecutionStateDb())
    rejected = _create(store, description="terminal row")
    store.reject_request(rejected.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    probe = _create(store, description="cleanup trigger probe")

    # Way beyond the 60s terminal retention: the listing still shows both
    # rows verbatim because listing never runs cleanup / purging.
    now["value"] = now["value"] + timedelta(hours=2)
    views, total = store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert total == 2
    statuses = {v.approval_id: v.status for v in views}
    assert statuses[rejected.approval_id] is ApprovalStatus.REJECTED
    assert statuses[probe.approval_id] is ApprovalStatus.PENDING

    # A cleanup read (authoritative detail path) purges the aged terminal
    # row and lazily expires the stale probe — unchanged semantics.
    viewed = store.get_approval_request(
        probe.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    assert viewed.status is ApprovalStatus.EXPIRED

    # Purged rows leave the listing exactly as they leave the detail path.
    views, total = store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert total == 1
    assert [v.approval_id for v in views] == [probe.approval_id]
    assert views[0].status is ApprovalStatus.EXPIRED

    with pytest.raises(ApprovalNotFoundError):
        store.get_approval_request(
            rejected.approval_id, GRAPH_OWNER, GRAPH_SESSION
        )


def test_lifecycle_statuses_listed_verbatim():
    store = make_graph().store
    pending = _create(store, description="stays pending")
    approved = _create(store, description="approved, unclaimed")
    store.approve_request(approved.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    rejected = _create(store, description="rejected")
    store.reject_request(rejected.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    executed = _create(store, description="executed once")
    store.approve_request(executed.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    store.claim_approved_for_execution(
        executed.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )

    views, total = store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert total == 4
    statuses = {v.approval_id: v.status for v in views}
    assert statuses[pending.approval_id] is ApprovalStatus.PENDING
    assert statuses[approved.approval_id] is ApprovalStatus.APPROVED
    assert statuses[rejected.approval_id] is ApprovalStatus.REJECTED
    assert statuses[executed.approval_id] is ApprovalStatus.EXECUTED


def test_page_pairs_total_under_concurrent_writes():
    """A writer creates approvals and rewrites one status while snapshots
    are taken; every snapshot's (page, total) stays bound by one instant."""
    store = make_graph().store
    made = [_create(store, description=f"race {i}") for i in range(4)]
    flipped = made[0].model_copy(
        update={
            "status": ApprovalStatus.REJECTED,
            "resolved_at": datetime(2030, 1, 2, tzinfo=timezone.utc),
        }
    )

    done = threading.Event()
    writer_error: list[Exception] = []

    def writer():
        try:
            for i in range(3):
                _create(store, description=f"late {i}")
            while not done.is_set():
                # Façade-level churn: one row rewritten every pass via
                # its trusted row identity (Phase 5.4 write contract).
                store.execution_db.save_approval(
                    flipped,
                    made[0].approval_id,
                    GRAPH_OWNER,
                    GRAPH_SESSION,
                    digest="churn-digest-placeholder",
                )
        except Exception as exc:  # pragma: no cover - surfaced below
            writer_error.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        seen_totals = 0
        for _ in range(25):
            page, total = store.list_approvals_page(
                GRAPH_OWNER, GRAPH_SESSION, limit=3
            )
            assert len(page) <= 3
            assert total >= len(page)  # page and total from one instant
            stamps = [(v.created_at, v.approval_id) for v in page]
            assert stamps == sorted(stamps, reverse=True)
            seen_totals = max(seen_totals, total)
        assert seen_totals >= 4
    finally:
        done.set()
        writer_thread.join()
    assert writer_error == []

    # After the writer stopped, page and total are exactly consistent.
    page, total = store.list_approvals_page(GRAPH_OWNER, GRAPH_SESSION, limit=10)
    assert total == 7
    assert len(page) == 7


def test_approvals_survive_restart_and_list_after_reopen(tmp_path):
    path = str(tmp_path / "approvals.db")
    store = ApprovalStore(state_db=ExecutionStateDb(path=path))
    kept = _create(store, description="kept pending")
    rejected = _create(store, description="kept rejected")
    store.reject_request(rejected.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    store.execution_db.close()

    # Second service generation over the same file (fresh objects).
    reopened_store = ApprovalStore(state_db=ExecutionStateDb(path=path))
    views, total = reopened_store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=10
    )
    assert total == 2
    statuses = {v.approval_id: v.status for v in views}
    assert statuses[kept.approval_id] is ApprovalStatus.PENDING
    assert statuses[rejected.approval_id] is ApprovalStatus.REJECTED
    # Summaries remain metadata-only after a restart as well.
    for view in views:
        assert ARG_MARKER not in _summary_strings(view)


# --- API level (shared state DB, HTTP boundary) ---------------------------------


@pytest_asyncio.fixture
async def client(api_headers):
    """Async test client for the authenticated local owner."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers=api_headers,
    ) as ac:
        yield ac


def _api_graph():
    """Approvals graph anchored on the shared state DB (HTTP-visible rows)."""
    return make_graph(state_db=plan_service.execution_db)


@pytest.mark.asyncio
async def test_list_approvals_empty_for_throwaway_session(api_headers):
    """An identity with no approvals lists an empty page (default echo)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers=_headers_for(EMPTY_APPROVALS_SESSION, api_headers),
    ) as fresh:
        response = await fresh.get("/api/approvals")
    assert response.status_code == 200
    assert response.json() == {
        "approvals": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_list_approvals_returns_metadata_only_summaries(api_headers):
    """Mixed lifecycle states, exact key set, no secrets anywhere."""
    owner, session = _token_owner(), META_APPROVALS_SESSION
    store = _api_graph().store
    pending = _create(store, owner=owner, session=session,
                      description="meta pending")
    other_owner = "meta-foreign-owner"
    foreign = _create(store, owner=other_owner,
                      session="meta-foreign-session",
                      description="meta foreign")

    views, total = store.list_approvals_page(owner, session, limit=200)
    assert total == 1
    assert [v.approval_id for v in views] == [pending.approval_id]
    assert foreign.approval_id not in {v.approval_id for v in views}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers=_headers_for(META_APPROVALS_SESSION, api_headers),
    ) as scoped:
        response = await scoped.get("/api/approvals?limit=200")
    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"approvals", "total", "limit", "offset"}
    assert data["limit"] == 200 and data["offset"] == 0
    assert data["total"] == 1

    summaries = {a["approval_id"]: a for a in data["approvals"]}
    assert pending.approval_id in summaries
    summary = summaries[pending.approval_id]
    assert set(summary.keys()) == {
        "approval_id",
        "tool_name",
        "action_description",
        "risk_level",
        "status",
        "created_at",
        "expires_at",
        "resolved_at",
    }
    assert summary["tool_name"] == "stub_mut_ok"
    assert summary["status"] == "PENDING"
    assert summary["resolved_at"] is None
    import json as _json
    body = _json.dumps(data)
    # Raw execution arguments and storage internals never appear.
    assert ARG_MARKER not in body
    for forbidden in FORBIDDEN_LISTING_KEYS:
        assert forbidden not in body, forbidden


@pytest.mark.asyncio
async def test_list_approvals_pagination_partitions_deterministically(
    api_headers,
):
    owner, session = _token_owner(), PAGE_APPROVALS_SESSION
    # Controlled creation clock: strictly increasing stamps make the
    # newest-first ordering observable exactly, while columns and
    # payload metadata agree (Phase 5.4 integrity).
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    tick = {"index": 0}

    def stepped_clock() -> datetime:
        stamp = base + timedelta(seconds=tick["index"])
        tick["index"] += 1
        return stamp

    graph = make_graph(clock=stepped_clock, state_db=plan_service.execution_db)
    created = [
        _create(graph.store, owner=owner, session=session,
                description=f"page {i}")
        for i in range(5)
    ]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers=_headers_for(PAGE_APPROVALS_SESSION, api_headers),
    ) as scoped:
        page_one = (await scoped.get("/api/approvals?limit=3&offset=0")).json()
        page_two = (await scoped.get("/api/approvals?limit=3&offset=3")).json()
        repeat = (await scoped.get("/api/approvals?limit=3&offset=0")).json()

    seen = [a["approval_id"] for a in page_one["approvals"]] + [
        a["approval_id"] for a in page_two["approvals"]
    ]
    assert len(set(seen)) == len(seen)  # disjoint pages
    for approval in created:
        assert approval.approval_id in seen
    # Newest-first ordering across the partition boundary (dedicated
    # session: the forced stamps make the expected order exact).
    ids_by_created = [a.approval_id for a in reversed(created)]
    assert seen == ids_by_created
    assert page_one["total"] == 5 and page_two["total"] == 5

    assert [a["approval_id"] for a in repeat["approvals"]] == [
        a["approval_id"] for a in page_one["approvals"]
    ]


@pytest.mark.asyncio
async def test_list_approvals_invalid_pagination_is_422(client: AsyncClient):
    for bad in ("limit=0", "limit=201", "limit=-5", "limit=abc", "offset=-1"):
        response = await client.get(f"/api/approvals?{bad}")
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_list_approvals_requires_authentication(client: AsyncClient):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as unauthenticated:
        response = await unauthenticated.get("/api/approvals")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_401"

    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1",
        headers={
            "Authorization": "Bearer not-the-real-token-value-0123456789",
            "X-JARVIS-Session": "forged-session-012345678901234",
        },
    ) as invalid:
        invalid_response = await invalid.get("/api/approvals")
    assert invalid_response.status_code == 401
    assert invalid_response.json()["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_list_approvals_identity_isolation(api_headers, client: AsyncClient):
    """Each session sees exactly its own approvals; foreign rows never leak."""
    owner = _token_owner()
    store = _api_graph().store
    mine = _create(store, owner=owner, session=MAIN_SESSION,
                   description="isolation mine")
    other = _create(store, owner=owner, session=OTHER_APPROVALS_SESSION,
                    description="isolation theirs")

    data = (await client.get("/api/approvals?limit=200")).json()
    my_ids = {a["approval_id"] for a in data["approvals"]}
    assert mine.approval_id in my_ids
    assert other.approval_id not in my_ids

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=_headers_for(OTHER_APPROVALS_SESSION, api_headers),
    ) as scoped:
        their_data = (await scoped.get("/api/approvals?limit=200")).json()
    their_ids = {a["approval_id"] for a in their_data["approvals"]}
    assert other.approval_id in their_ids
    assert mine.approval_id not in their_ids


@pytest.mark.asyncio
async def test_list_approvals_detail_endpoint_compatibility(
    api_headers, client: AsyncClient
):
    """The listing's ids resolve on the detail endpoint with the same
    status; arguments stay exclusive to the detail response."""
    owner = _token_owner()
    store = _api_graph().store
    created = _create(store, owner=owner, session=MAIN_SESSION,
                      args={"value": ARG_MARKER},
                      description="detail compatibility probe")

    listing = (await client.get("/api/approvals?limit=200")).json()
    match = [
        a for a in listing["approvals"]
        if a["approval_id"] == created.approval_id
    ]
    assert len(match) == 1
    listed = match[0]
    assert listed["status"] == "PENDING"
    assert listed["resolved_at"] is None

    detail = await client.get(f"/api/approvals/{created.approval_id}")
    assert detail.status_code == 200
    detail_data = detail.json()
    assert detail_data["status"] == "PENDING"
    assert detail_data["arguments"] == {"value": ARG_MARKER}
    # Full payload only on detail; the listing keeps metadata only.
    assert '"arguments"' not in json.dumps(listing)


@pytest.mark.asyncio
async def test_list_approvals_corrupt_payload_maps_to_structured_500(
    api_headers, client: AsyncClient
):
    """Real corruption end to end: a damaged approval fails closed (500)."""
    owner = _token_owner()
    store = _api_graph().store
    created = _create(store, owner=owner, session=CORRUPT_APPROVALS_SESSION,
                      description="corrupt damage marker")
    plan_service.execution_db._conn.execute(
        "UPDATE approval_requests SET payload = '{\"approval_id\": 42, [nope' "
        "WHERE approval_id = ?",
        (created.approval_id,),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=_headers_for(CORRUPT_APPROVALS_SESSION, api_headers),
    ) as damaged:
        response = await damaged.get("/api/approvals")
    assert response.status_code == 500
    data = response.json()
    assert data["error"]["code"] == "HTTP_500"
    assert data["error"]["message"] == "Approval state could not be validated."
    # Corrupted internals never leak into the response.
    assert "corrupt damage marker" not in response.text
    assert "nope" not in response.text

    # Test-scoped teardown: retire this damaged audit row. Purged rows
    # are excluded from every gated scan (including the cleanup scans
    # other suites trigger process-wide), so the self-created damage
    # cannot poison later reads; production behavior is untouched.
    plan_service.execution_db._conn.execute(
        "UPDATE approval_requests SET purged = 1 WHERE approval_id = ?",
        (created.approval_id,),
    )

    # The damaged row is session-local: other identities stay served.
    healthy = await client.get("/api/approvals?limit=200")
    assert healthy.status_code == 200


@pytest.mark.asyncio
async def test_list_approvals_is_read_only_over_http(
    api_headers, client: AsyncClient
):
    """Listing twice returns identical bytes and mutates nothing."""
    owner = _token_owner()
    store = _api_graph().store
    created = _create(store, owner=owner, session=MAIN_SESSION,
                      description="read-only over http")

    before = await client.get(f"/api/approvals/{created.approval_id}")
    assert before.status_code == 200
    before_json = before.json()

    first = await client.get("/api/approvals?limit=200")
    assert first.status_code == 200
    second = await client.get("/api/approvals?limit=200")
    assert second.status_code == 200
    assert first.content == second.content

    after = await client.get(f"/api/approvals/{created.approval_id}")
    assert after.status_code == 200
    assert after.json() == before_json
    assert after.json()["status"] == "PENDING"

    record = store.execution_db.get_approval_record(created.approval_id)
    assert record.request.status is ApprovalStatus.PENDING
    assert record.purged is False
