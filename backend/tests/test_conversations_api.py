"""Conversation API integration (Phase 5.1-d): metadata listing with
offset pagination, bounded history retrieval, and explicit structured
error mapping (404 / 503 / 500), all under the existing authentication
and session-isolation model.

Read-only listing tests never create state for other tests' identities;
the end-to-end capacity test uses a throwaway session identity that is
fully cleaned up afterwards, so no test ever pushes a shared identity
toward the 256-conversation cap.
"""

import hashlib
import uuid
from uuid import uuid4
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.services.conversation_store import (
    MAX_CONVERSATIONS_PER_IDENTITY,
    ConversationCapacityError,
    ConversationCorruptError,
    PersistentConversationStore,
    conversation_store,
)


E2E_CAPACITY_SESSION = "capacity-e2e-session-012345678901"
EMPTY_LIST_SESSION = "empty-list-session-0123456789044321"

SENTINEL_CONTENT = "api-error-sentinel-content"


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


def _identity() -> tuple[str, str]:
    """(owner_id, session_id) the API client authenticates as."""
    owner = hashlib.sha256(
        settings.api_token.get_secret_value().encode("utf-8")
    ).hexdigest()
    return owner, "test-session-012345678901234567"


async def _create_conversation(client, text="Hello"):
    response = await client.post("/api/chat", json={"message": text})
    assert response.status_code == 200
    return response.json()["conversation_id"]


# --- listing -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_metadata_only(client: AsyncClient):
    conv_a = await _create_conversation(client, "first")
    conv_b = await _create_conversation(client, "second")

    response = await client.get("/api/conversations")
    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"conversations", "total", "limit", "offset"}
    assert data["limit"] == 50  # documented default
    assert data["offset"] == 0
    assert data["total"] >= 2
    listed_ids = [c["conversation_id"] for c in data["conversations"]]
    assert conv_a in listed_ids and conv_b in listed_ids

    summary = next(c for c in data["conversations"] if c["conversation_id"] == conv_b)
    assert set(summary.keys()) == {
        "conversation_id", "created_at", "updated_at", "turn_count",
    }
    # Metadata-only: no turns, no content, no identity fields.
    assert "turns" not in summary and "content" not in summary
    assert "owner_id" not in summary and "session_id" not in summary
    assert isinstance(summary["turn_count"], int) and summary["turn_count"] >= 2
    datetime.fromisoformat(summary["created_at"])
    datetime.fromisoformat(summary["updated_at"])


@pytest.mark.asyncio
async def test_list_empty_for_throwaway_session(api_headers):
    """A session that created no conversations lists an empty page.

    Uses a dedicated read-only session header so the result cannot be
    affected by state other suites created for their other sessions.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1",
        headers={
            "Authorization": api_headers["Authorization"],
            "X-JARVIS-Session": EMPTY_LIST_SESSION,
        },
    ) as fresh:
        response = await fresh.get("/api/conversations?limit=10&offset=0")
    assert response.status_code == 200
    data = response.json()
    assert data == {"conversations": [], "total": 0, "limit": 10, "offset": 0}


@pytest.mark.asyncio
async def test_list_orders_by_most_recent_activity(client: AsyncClient):
    first = await _create_conversation(client, "first conv")
    second = await _create_conversation(client, "second conv")

    response = await client.get("/api/conversations?limit=200")
    order = [
        c["conversation_id"]
        for c in response.json()["conversations"]
        if c["conversation_id"] in (first, second)
    ]
    assert order == [second, first]  # second, created (updated) later, leads

    # Activity bumps a conversation to the front of the next page.
    await client.post(
        "/api/chat", json={"message": "bump", "conversation_id": first}
    )
    response = await client.get("/api/conversations?limit=200")
    order = [
        c["conversation_id"]
        for c in response.json()["conversations"]
        if c["conversation_id"] in (first, second)
    ]
    assert order == [first, second]


@pytest.mark.asyncio
async def test_list_tiebreak_is_deterministic_by_conversation_id(
    client: AsyncClient,
):
    """Equal updated_at falls back to conversation_id DESC determinism."""
    conv_a = await _create_conversation(client, "alpha")
    conv_b = await _create_conversation(client, "beta")

    stamp = "2030-01-01T00:00:00+00:00"
    conversation_store.execution_db._conn.execute(
        "UPDATE conversations SET updated_at = ? "
        "WHERE conversation_id IN (?, ?)",
        (stamp, conv_a, conv_b),
    )

    response = await client.get("/api/conversations?limit=200")
    listed = [
        c["conversation_id"] for c in response.json()["conversations"]
        if c["conversation_id"] in (conv_a, conv_b)
    ]
    assert listed == sorted([conv_a, conv_b], reverse=True)
    # Repeated queries are identical (reproducible page).
    again = await client.get("/api/conversations?limit=200")
    assert listed == [
        c["conversation_id"] for c in again.json()["conversations"]
        if c["conversation_id"] in (conv_a, conv_b)
    ]


@pytest.mark.asyncio
async def test_list_pagination_partitions_deterministically(client: AsyncClient):
    created = [await _create_conversation(client, f"conv {i}") for i in range(7)]

    page_one = (await client.get("/api/conversations?limit=3&offset=0")).json()
    page_two = (await client.get("/api/conversations?limit=3&offset=3")).json()
    page_three = (await client.get("/api/conversations?limit=3&offset=6")).json()

    ids_on_pages = [
        [c["conversation_id"] for c in page["conversations"]]
        for page in (page_one, page_two, page_three)
    ]
    seen = ids_on_pages[0] + ids_on_pages[1] + ids_on_pages[2]
    # Disjoint pages covering every created conversation exactly once.
    assert all(set(page) for page in ids_on_pages)
    assert len(set(seen)) == len(seen)
    for conv_id in created:
        assert conv_id in seen
    assert page_one["total"] >= 7
    # Same query -> same page.
    repeat = (await client.get("/api/conversations?limit=3&offset=0")).json()
    assert [c["conversation_id"] for c in repeat["conversations"]] == ids_on_pages[0]


@pytest.mark.asyncio
async def test_list_limit_validation_and_echo(client: AsyncClient):
    conv_id = await _create_conversation(client)

    # Default limit is 50 and echoed.
    default = (await client.get("/api/conversations")).json()
    assert default["limit"] == 50
    assert conv_id in [c["conversation_id"] for c in default["conversations"]]

    # An explicit maximum limit is accepted and echoed.
    maximum = (await client.get("/api/conversations?limit=200")).json()
    assert maximum["limit"] == 200
    assert maximum["total"] == len(maximum["conversations"])

    # Out-of-range limits are unprocessable.
    for bad in ("limit=0", "limit=201", "limit=-5", "limit=abc"):
        response = await client.get(f"/api/conversations?{bad}")
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    bad_offset = await client.get("/api/conversations?offset=-1")
    assert bad_offset.status_code == 422
    assert bad_offset.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_list_is_identity_isolated(
    client: AsyncClient, other_session_headers
):
    """Listing never shows another session's (or another owner's) rows."""
    mine = await _create_conversation(client, "mine-private")
    theirs = None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=other_session_headers,
    ) as other:
        theirs = (await other.post("/api/chat", json={"message": "their conv"})
                  ).json()["conversation_id"]

        my_list = (await client.get("/api/conversations?limit=200")).json()
        their_list = (await other.get("/api/conversations?limit=200")).json()

    my_ids = {c["conversation_id"] for c in my_list["conversations"]}
    their_ids = {c["conversation_id"] for c in their_list["conversations"]}
    assert theirs in their_ids
    assert theirs not in my_ids
    assert mine in my_ids
    assert mine not in their_ids
    assert their_list["total"] >= 1
    assert mine in {
        c["conversation_id"] for c in my_list["conversations"]
    }


@pytest.mark.asyncio
async def test_list_requires_authentication(client: AsyncClient):
    conv_id = await _create_conversation(client)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as unauthenticated:
        response = await unauthenticated.get("/api/conversations")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_401"

    # An invalid token cannot list either.
    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1",
        headers={
            "Authorization": "Bearer wrong-wrong-wrong-wrong-wrong-wrong",
            "X-JARVIS-Session": "wrong-session-0123456789012",
        },
    ) as invalid:
        invalid_response = await invalid.get("/api/conversations")
    assert invalid_response.status_code == 401
    assert invalid_response.json()["error"]["code"] == "HTTP_401"


# --- bounded history -------------------------------------------------------------


async def _fill_turns(conversation_id: str, user_messages: list[str]) -> None:
    """Append turns directly through the store under the API identity."""
    owner, session = _identity()
    for index, message in enumerate(user_messages):
        conversation_store.add_turn(
            conversation_id,
            "user" if index % 2 == 0 else "assistant",
            message,
            owner_id=owner,
            session_id=session,
        )


@pytest.mark.asyncio
async def test_history_default_limit_is_100_and_latest_window(client: AsyncClient):
    conv_id = await _create_conversation(client, "seed")
    messages = [f"m{i}" for i in range(120)]  # 120 turns + seed pair
    # Seed turn produced 2 turns; add 120 more alternating user/assistant.
    owner, session = _identity()
    for index, message in enumerate(messages):
        conversation_store.add_turn(
            conv_id,
            "user" if index % 2 == 0 else "assistant",
            message,
            owner_id=owner,
            session_id=session,
        )

    response = await client.get(f"/api/conversations/{conv_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == conv_id
    assert data["total_turn_count"] == 122
    assert data["turn_count"] == 100
    assert len(data["turns"]) == 100
    # LATEST window with the most recent turn last (chronological).
    assert data["turns"][-1]["content"] == "m119"
    assert data["turns"][0]["content"] == "m20"
    roles = [t["role"] for t in data["turns"]]
    assert set(roles) <= {"user", "assistant"}
    stamps = [t["timestamp"] for t in data["turns"]]
    assert stamps == sorted(stamps)


@pytest.mark.asyncio
async def test_history_custom_limit_returns_latest_turns(client: AsyncClient):
    conv_id = await _create_conversation(client, "seed")
    owner, session = _identity()
    for index in range(40):
        conversation_store.add_turn(
            conv_id,
            "user" if index % 2 == 0 else "assistant",
            f"n{index}",
            owner_id=owner,
            session_id=session,
        )
    # 42 total turns; the latest 30 must be returned, oldest to newest.
    response = await client.get(f"/api/conversations/{conv_id}?limit=30")
    assert response.status_code == 200
    data = response.json()
    assert data["turn_count"] == 30
    assert data["total_turn_count"] == 42
    contents = [t["content"] for t in data["turns"]]
    # index 0 = "seed", 1 = seeded reply, 2+i = "n{i}" -> window starts
    # at index 12 ("n10") and runs to the newest turn ("n39").
    assert contents == [f"n{i}" for i in range(10, 40)]


@pytest.mark.asyncio
async def test_history_maximum_limit_allowed_full_window(client: AsyncClient):
    conv_id = await _create_conversation(client, "seed")
    owner, session = _identity()
    for index in range(40):
        conversation_store.add_turn(
            conv_id,
            "user" if index % 2 == 0 else "assistant",
            f"n{index}",
            owner_id=owner,
            session_id=session,
        )
    response = await client.get(f"/api/conversations/{conv_id}?limit=400")
    assert response.status_code == 200
    data = response.json()
    assert data["turn_count"] == 42
    assert data["total_turn_count"] == 42
    assert len(data["turns"]) == 42
    assert data["turns"][0]["content"] == "seed"
    assert data["turns"][-1]["content"] == "n39"
    assert data["turns"][1]["role"] == "assistant"
    # Beyond the cap is rejected.
    invalid = await client.get(f"/api/conversations/{conv_id}?limit=401")
    assert invalid.status_code == 422
    zero = await client.get(f"/api/conversations/{conv_id}?limit=0")
    assert zero.status_code == 422
    assert zero.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_history_is_backward_compatible_for_small_conversations(
    client: AsyncClient,
):
    conv_id = await _create_conversation(client, "Ping")
    response = await client.get(f"/api/conversations/{conv_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == conv_id
    assert data["turn_count"] == 2
    assert data["total_turn_count"] == 2
    assert len(data["turns"]) == 2
    assert [t["role"] for t in data["turns"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_history_unknown_and_foreign_are_indistinguishable_404(
    client: AsyncClient, other_session_headers
):
    conv_id = await _create_conversation(client, "private")
    unknown = str(uuid4())

    for target in (unknown,):
        response = await client.get(f"/api/conversations/{target}?limit=30")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "HTTP_404"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=other_session_headers,
    ) as other:
        foreign = await other.get(f"/api/conversations/{conv_id}?limit=30")
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "HTTP_404"
    assert "private" not in foreign.text
    # And the real one is untouched for its owner.
    retained = await client.get(f"/api/conversations/{conv_id}")
    assert retained.status_code == 200


# --- explicit error mapping ------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_error_maps_to_structured_503(
    client: AsyncClient, monkeypatch
):
    def raise_capacity(*args, **kwargs):
        raise ConversationCapacityError(
            "The conversation store has reached its configured capacity."
        )

    monkeypatch.setattr(
        PersistentConversationStore, "add_turn", raise_capacity
    )
    response = await client.post("/api/chat", json={"message": SENTINEL_CONTENT})
    monkeypatch.undo()
    assert response.status_code == 503
    data = response.json()
    assert data["error"]["code"] == "HTTP_503"
    assert (
        data["error"]["message"]
        == "The conversation store has reached its configured capacity."
    )
    assert SENTINEL_CONTENT not in response.text


@pytest.mark.asyncio
async def test_capacity_error_end_to_end_with_throwaway_identity(
    client: AsyncClient,
):
    """A full identity produces 503 through the real chat flow; the
    identity used by every other test stays far below its cap."""
    owner, _ = _identity()
    store = conversation_store
    for _ in range(MAX_CONVERSATIONS_PER_IDENTITY):
        store.create_conversation(owner_id=owner, session_id=E2E_CAPACITY_SESSION)
    assert store.count_conversations(owner, E2E_CAPACITY_SESSION) == MAX_CONVERSATIONS_PER_IDENTITY

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://127.0.0.1",
            headers={
                "Authorization": f"Bearer {settings.api_token.get_secret_value()}",
                "X-JARVIS-Session": E2E_CAPACITY_SESSION,
            },
        ) as exhaust_client:
            response = await exhaust_client.post(
                "/api/chat", json={"message": "no capacity left"}
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "HTTP_503"
        assert "no capacity left" not in response.text
    finally:
        # Restore a clean slate for the throwaway identity.
        records = store.list_conversations(
            owner_id=owner, session_id=E2E_CAPACITY_SESSION, limit=1000
        )
        for record in records:
            store.clear_conversation(
                record.conversation_id,
                owner_id=owner,
                session_id=E2E_CAPACITY_SESSION,
            )
        assert store.count_conversations(owner, E2E_CAPACITY_SESSION) == 0


@pytest.mark.asyncio
async def test_corrupt_history_maps_to_structured_500(client: AsyncClient):
    """Real corruption end to end: unreadable history fails closed."""
    conv_id = await _create_conversation(client, SENTINEL_CONTENT)
    owner, session = _identity()
    conversation_store.add_turn(
        conv_id, "assistant", "ok", owner_id=owner, session_id=session
    )

    try:
        # Direct storage damage (documented SQLite pragma bypasses CHECKs).
        conn = conversation_store.execution_db._conn
        conn.execute("PRAGMA ignore_check_constraints = ON")
        try:
            conn.execute(
                "UPDATE conversation_messages SET content = '' "
                "WHERE conversation_id = ?",
                (conv_id,),
            )
        finally:
            conn.execute("PRAGMA ignore_check_constraints = OFF")

        response = await client.get(f"/api/conversations/{conv_id}")
        assert response.status_code == 500
        data = response.json()
        assert data["error"]["code"] == "HTTP_500"
        assert (
            data["error"]["message"]
            == "Conversation history could not be validated."
        )
        # The damaged content never appears in the response.
        assert SENTINEL_CONTENT not in response.text
    finally:
        # Damaged rows stay deletable (deletion never validates), so the
        # shared identity returns to a clean slate for later tests.
        conversation_store.clear_conversation(
            conv_id, owner_id=owner, session_id=session
        )


@pytest.mark.asyncio
async def test_corrupt_error_not_confused_with_unknown(client: AsyncClient):
    """Damage is a 500, an unknown id stays a 404 - clearly distinct."""
    conv_id = await _create_conversation(client, "will damage")
    owner, session = _identity()
    conversation_store.add_turn(
        conv_id, "user", "turn", owner_id=owner, session_id=session
    )
    try:
        conversation_store.execution_db._conn.execute(
            "UPDATE conversation_messages SET created_at = 'garbage' "
            "WHERE conversation_id = ?",
            (conv_id,),
        )
        damaged = await client.get(f"/api/conversations/{conv_id}")
        assert damaged.status_code == 500
        unknown = await client.get(f"/api/conversations/{uuid4()}")
        assert unknown.status_code == 404
    finally:
        conversation_store.clear_conversation(
            conv_id, owner_id=owner, session_id=session
        )


@pytest.mark.asyncio
async def test_corrupt_conversation_metadata_maps_to_structured_500(
    client: AsyncClient,
):
    """Damaged conversation METADATA timestamps fail closed with the
    established structured corruption response (Phase 5.1-e)."""
    conv_id = await _create_conversation(client, "metadata-damage")
    owner, session = _identity()
    try:
        # Timestamp columns carry no CHECK constraint: a direct UPDATE
        # emulates real metadata damage (earlier assertion proves it).
        conversation_store.execution_db._conn.execute(
            "UPDATE conversations SET created_at = 'not-a-date' "
            "WHERE conversation_id = ?",
            (conv_id,),
        )
        response = await client.get(f"/api/conversations/{conv_id}")
        assert response.status_code == 500
        data = response.json()
        assert data["error"]["code"] == "HTTP_500"
        assert (
            data["error"]["message"]
            == "Conversation history could not be validated."
        )
        assert "metadata-damage" not in response.text
    finally:
        conversation_store.clear_conversation(
            conv_id, owner_id=owner, session_id=session
        )
    # After the damaged row is gone, listing is healthy again.
    listing = (await client.get("/api/conversations?limit=200")).json()
    assert conv_id not in [c["conversation_id"] for c in listing["conversations"]]


@pytest.mark.asyncio
async def test_corrupt_listing_row_maps_to_structured_500(client: AsyncClient):
    """One damaged row poisons the listing: it must fail closed instead
    of silently skipping or serving the damaged entry (Phase 5.1-e)."""
    conv_id = await _create_conversation(client, "list-row-damage")
    owner, session = _identity()
    try:
        conversation_store.execution_db._conn.execute(
            "UPDATE conversations SET updated_at = 'garbage' "
            "WHERE conversation_id = ?",
            (conv_id,),
        )
        response = await client.get("/api/conversations?limit=200")
        assert response.status_code == 500
        data = response.json()
        assert data["error"]["code"] == "HTTP_500"
        assert (
            data["error"]["message"]
            == "Conversation history could not be validated."
        )
        assert "list-row-damage" not in response.text
    finally:
        conversation_store.clear_conversation(
            conv_id, owner_id=owner, session_id=session
        )
    # Listing recovers for the same identity once the row is deleted.
    response = await client.get("/api/conversations?limit=200")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_delete_integrity_with_listing(client: AsyncClient):
    """DELETE behavior unchanged and the listing follows deletions."""
    conv_id = await _create_conversation(client, "temporary")

    listed = (await client.get("/api/conversations?limit=200")).json()
    assert conv_id in [c["conversation_id"] for c in listed["conversations"]]
    total_before = listed["total"]

    # Unconfigured browser origin still fails closed (unchanged DELETE).
    denied = await client.delete(
        f"/api/conversations/{conv_id}",
        headers={"Origin": "https://evil.example"},
    )
    assert denied.status_code == 403
    after_denied = (await client.get("/api/conversations?limit=200")).json()
    assert conv_id in [c["conversation_id"] for c in after_denied["conversations"]]

    deleted = await client.delete(f"/api/conversations/{conv_id}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"

    listed_after = (await client.get("/api/conversations?limit=200")).json()
    assert conv_id not in [
        c["conversation_id"] for c in listed_after["conversations"]
    ]
    assert listed_after["total"] == total_before - 1
    gone = await client.get(f"/api/conversations/{conv_id}")
    assert gone.status_code == 404
