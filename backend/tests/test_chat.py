"""Tests for POST /api/chat contract."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.schemas.chat import MAX_MESSAGE_LENGTH


@pytest_asyncio.fixture
async def client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_chat_valid_request(client: AsyncClient):
    """Valid chat request returns 200 with placeholder response."""
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200
    data = response.json()
    assert data["response"] == "JARVIS assistant core is online."
    assert data["status"] == "success"
    assert "conversation_id" in data
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_chat_empty_message_rejected(client: AsyncClient):
    """Empty message is rejected with 422."""
    response = await client.post("/api/chat", json={"message": ""})
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_chat_whitespace_only_message_rejected(client: AsyncClient):
    """Whitespace-only message is rejected with 422."""
    response = await client.post("/api/chat", json={"message": "   \n\t  "})
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_chat_oversized_message_rejected(client: AsyncClient):
    """Oversized message is rejected with 422."""
    response = await client.post(
        "/api/chat", json={"message": "a" * (MAX_MESSAGE_LENGTH + 1)}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_chat_missing_conversation_id_generates_one(client: AsyncClient):
    """Missing conversation_id results in a generated UUID."""
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200
    data = response.json()
    uuid.UUID(data["conversation_id"])


@pytest.mark.asyncio
async def test_chat_supplied_conversation_id_preserved(client: AsyncClient):
    """Supplied conversation_id is preserved in the response."""
    supplied_id = str(uuid.uuid4())
    response = await client.post(
        "/api/chat",
        json={"message": "Hello", "conversation_id": supplied_id},
    )
    assert response.status_code == 200
    assert response.json()["conversation_id"] == supplied_id


@pytest.mark.asyncio
async def test_chat_response_schema(client: AsyncClient):
    """Response contains conversation_id, response, status, timestamp."""
    response = await client.post("/api/chat", json={"message": "Hello"})
    data = response.json()
    assert set(data.keys()) >= {
        "conversation_id",
        "response",
        "status",
        "timestamp",
    }
    assert isinstance(data["conversation_id"], str)
    assert isinstance(data["response"], str)
    assert data["status"] == "success"
    assert isinstance(data["timestamp"], str)


@pytest.mark.asyncio
async def test_chat_deterministic_placeholder(client: AsyncClient):
    """Different inputs return the same deterministic placeholder."""
    first = await client.post("/api/chat", json={"message": "Hello"})
    second = await client.post("/api/chat", json={"message": "Something else"})
    assert first.json()["response"] == second.json()["response"]
    assert first.json()["response"] == "JARVIS assistant core is online."


@pytest.mark.asyncio
async def test_chat_health_still_works(client: AsyncClient):
    """Health endpoint still works after chat wiring."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_chat_root_still_works(client: AsyncClient):
    """Root endpoint still works after chat wiring."""
    response = await client.get("/")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_chat_correlation_id_present(client: AsyncClient):
    """POST /api/chat returns X-Request-ID and echoes valid IDs."""
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert "X-Request-ID" in response.headers

    echoed = await client.post(
        "/api/chat",
        json={"message": "Hello"},
        headers={"X-Request-ID": "chat-test-123"},
    )
    assert echoed.headers["X-Request-ID"] == "chat-test-123"


@pytest.mark.asyncio
async def test_chat_structured_validation_errors(client: AsyncClient):
    """Missing message field yields structured 422 with request_id."""
    response = await client.post("/api/chat", json={})
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "VALIDATION_ERROR"
    assert "request_id" in data["error"]
