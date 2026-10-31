"""Tests for POST /api/chat contract."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.schemas.chat import MAX_MESSAGE_LENGTH


@pytest_asyncio.fixture
async def client(api_headers):
    """Create an async test client for the authenticated local owner."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers=api_headers,
    ) as ac:
        yield ac


def _unauthenticated_client():
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    )


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


# Authentication and origin security tests


@pytest.mark.asyncio
async def test_chat_missing_authentication_rejected(client: AsyncClient):
    """API chat requests without credentials receive structured 401."""
    async with _unauthenticated_client() as unauthenticated:
        response = await unauthenticated.post(
            "/api/chat", json={"message": "Hello"}
        )
    assert response.status_code == 401
    data = response.json()
    assert data["error"]["code"] == "HTTP_401"
    assert "request_id" in data["error"]
    assert data["error"]["message"] == "Authentication is required."


@pytest.mark.asyncio
async def test_chat_invalid_token_rejected(client: AsyncClient):
    """A wrong bearer token is rejected with structured 401."""
    async with _unauthenticated_client() as unauthenticated:
        response = await unauthenticated.post(
            "/api/chat",
            json={"message": "Hello"},
            headers={
                "Authorization": "Bearer invalid-invalid-invalid-invalid-invalid",
                "X-JARVIS-Session": "invalid-token-session-012345678",
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_chat_non_bearer_authorization_scheme_rejected(client: AsyncClient):
    """Authorization schemes other than Bearer are rejected with 401."""
    async with _unauthenticated_client() as unauthenticated:
        response = await unauthenticated.post(
            "/api/chat",
            json={"message": "Hello"},
            headers={
                "Authorization": "Basic invalid-invalid-invalid-invalid-invalid",
                "X-JARVIS-Session": "invalid-token-session-012345678",
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_chat_missing_session_id_rejected(api_headers):
    """Valid token without the session header is rejected with 401."""
    async with _unauthenticated_client() as unauthenticated:
        response = await unauthenticated.post(
            "/api/chat",
            json={"message": "Hello"},
            headers={"Authorization": api_headers["Authorization"]},
        )
    assert response.status_code == 401
    data = response.json()
    assert data["error"]["code"] == "HTTP_401"
    assert data["error"]["message"] == "An authenticated client session is required."


@pytest.mark.asyncio
async def test_chat_invalid_session_id_rejected(api_headers):
    """Session identifiers outside the allowed format are rejected."""
    async with _unauthenticated_client() as unauthenticated:
        response = await unauthenticated.post(
            "/api/chat",
            json={"message": "Hello"},
            headers={
                "Authorization": api_headers["Authorization"],
                "X-JARVIS-Session": "bad session id!@#",
            },
        )
    assert response.status_code == 401
    data = response.json()
    assert data["error"]["code"] == "HTTP_401"
    assert data["error"]["message"] == "An authenticated client session is required."


@pytest.mark.asyncio
async def test_chat_valid_authenticated_request(client: AsyncClient):
    """Authenticated chat succeeds with the existing response contract."""
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200
    data = response.json()
    assert data["response"] == "JARVIS assistant core is online."
    assert data["status"] == "success"
    assert "conversation_id" in data


@pytest.mark.asyncio
async def test_chat_disallowed_origin_rejected(client: AsyncClient):
    """State-changing chat requests from unconfigured origins fail closed."""
    response = await client.post(
        "/api/chat",
        json={"message": "Hello"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "HTTP_403"
    assert data["error"]["message"] == "The request origin is not allowed."


@pytest.mark.asyncio
async def test_chat_native_client_without_origin_allowed(client: AsyncClient):
    """Native clients that send no Origin header keep working."""
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200


# Conversation ownership tests


@pytest.mark.asyncio
async def test_chat_rejects_foreign_conversation_id(
    client: AsyncClient, other_session_headers
):
    """A conversation created in one session is never reused by another."""
    created = await client.post("/api/chat", json={"message": "First"})
    conv_id = created.json()["conversation_id"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers=other_session_headers,
    ) as other:
        response = await other.post(
            "/api/chat",
            json={"message": "Hijack", "conversation_id": conv_id},
        )

    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "HTTP_404"

    retained = await client.get(f"/api/conversations/{conv_id}")
    assert retained.status_code == 200
    user_contents = [
        t["content"] for t in retained.json()["turns"] if t["role"] == "user"
    ]
    assert user_contents == ["First"]
