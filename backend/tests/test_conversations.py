"""Tests for conversation context (GET/DELETE + chat integration)."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services.conversation_store import conversation_store


@pytest_asyncio.fixture
async def client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _fresh_id() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_new_conversation_creation(client: AsyncClient):
    """POST without ID creates a retrievable conversation."""
    posted = await client.post("/api/chat", json={"message": "Hello"})
    assert posted.status_code == 200
    conv_id = posted.json()["conversation_id"]

    history = await client.get(f"/api/conversations/{conv_id}")
    assert history.status_code == 200
    assert history.json()["conversation_id"] == conv_id


@pytest.mark.asyncio
async def test_user_turn_stored(client: AsyncClient):
    """User turn is stored with role, content, and timestamp."""
    conv_id = _fresh_id()
    await client.post(
        "/api/chat", json={"message": "Remember this", "conversation_id": conv_id}
    )
    history = await client.get(f"/api/conversations/{conv_id}")
    user_turns = [t for t in history.json()["turns"] if t["role"] == "user"]
    assert len(user_turns) == 1
    assert user_turns[0]["content"] == "Remember this"
    assert user_turns[0]["timestamp"]


@pytest.mark.asyncio
async def test_assistant_turn_stored(client: AsyncClient):
    """Assistant turn is stored with placeholder content."""
    conv_id = _fresh_id()
    await client.post(
        "/api/chat", json={"message": "Hi", "conversation_id": conv_id}
    )
    history = await client.get(f"/api/conversations/{conv_id}")
    assistant_turns = [
        t for t in history.json()["turns"] if t["role"] == "assistant"
    ]
    assert len(assistant_turns) == 1
    assert assistant_turns[0]["content"] == "JARVIS assistant core is online."


@pytest.mark.asyncio
async def test_multiple_messages_preserve_order(client: AsyncClient):
    """Multiple messages preserve chronological user/assistant order."""
    conv_id = _fresh_id()
    for text in ("First", "Second", "Third"):
        await client.post(
            "/api/chat", json={"message": text, "conversation_id": conv_id}
        )
    history = await client.get(f"/api/conversations/{conv_id}")
    data = history.json()
    assert data["turn_count"] == 6
    roles = [t["role"] for t in data["turns"]]
    assert roles == ["user", "assistant"] * 3
    user_contents = [t["content"] for t in data["turns"] if t["role"] == "user"]
    assert user_contents == ["First", "Second", "Third"]


@pytest.mark.asyncio
async def test_supplied_conversation_id_preserved(client: AsyncClient):
    """Supplied conversation ID is preserved across chat and history."""
    conv_id = _fresh_id()
    posted = await client.post(
        "/api/chat", json={"message": "Hello", "conversation_id": conv_id}
    )
    assert posted.json()["conversation_id"] == conv_id
    history = await client.get(f"/api/conversations/{conv_id}")
    assert history.json()["conversation_id"] == conv_id


@pytest.mark.asyncio
async def test_get_history_returns_correct_turns(client: AsyncClient):
    """GET history returns ID, ordered turns, and turn count."""
    conv_id = _fresh_id()
    await client.post(
        "/api/chat", json={"message": "Ping", "conversation_id": conv_id}
    )
    response = await client.get(f"/api/conversations/{conv_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == conv_id
    assert data["turn_count"] == 2
    assert len(data["turns"]) == 2
    assert data["turns"][0]["role"] == "user"
    assert data["turns"][1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_get_unknown_conversation_404(client: AsyncClient):
    """GET unknown conversation returns structured 404."""
    unknown = _fresh_id()
    assert not conversation_store.exists(unknown)
    response = await client.get(f"/api/conversations/{unknown}")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "HTTP_404"
    assert "request_id" in data["error"]


@pytest.mark.asyncio
async def test_delete_conversation_succeeds(client: AsyncClient):
    """DELETE removes the conversation and confirms deletion."""
    conv_id = _fresh_id()
    await client.post(
        "/api/chat", json={"message": "Bye", "conversation_id": conv_id}
    )
    deleted = await client.delete(f"/api/conversations/{conv_id}")
    assert deleted.status_code == 200
    assert deleted.json()["conversation_id"] == conv_id
    assert deleted.json()["status"] == "deleted"

    gone = await client.get(f"/api/conversations/{conv_id}")
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_conversation_404(client: AsyncClient):
    """DELETE unknown conversation returns structured 404."""
    unknown = _fresh_id()
    response = await client.delete(f"/api/conversations/{unknown}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HTTP_404"


@pytest.mark.asyncio
async def test_chat_response_still_works(client: AsyncClient):
    """Chat response contract unchanged after store integration."""
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200
    data = response.json()
    assert data["response"] == "JARVIS assistant core is online."
    assert data["status"] == "success"


@pytest.mark.asyncio
async def test_correlation_ids_still_work(client: AsyncClient):
    """Correlation IDs work on chat and conversation endpoints."""
    conv_id = _fresh_id()
    posted = await client.post(
        "/api/chat",
        json={"message": "Hi", "conversation_id": conv_id},
        headers={"X-Request-ID": "conv-test-1"},
    )
    assert posted.headers["X-Request-ID"] == "conv-test-1"

    history = await client.get(
        f"/api/conversations/{conv_id}", headers={"X-Request-ID": "conv-test-2"}
    )
    assert history.headers["X-Request-ID"] == "conv-test-2"

    deleted = await client.delete(
        f"/api/conversations/{conv_id}", headers={"X-Request-ID": "conv-test-3"}
    )
    assert deleted.headers["X-Request-ID"] == "conv-test-3"


@pytest.mark.asyncio
async def test_health_still_works(client: AsyncClient):
    """Health endpoint unaffected."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_root_still_works(client: AsyncClient):
    """Root endpoint unaffected."""
    response = await client.get("/")
    assert response.status_code == 200
